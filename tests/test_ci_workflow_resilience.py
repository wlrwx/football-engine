"""CI 工作流的**行为**回归测试 —— 用真实的临时 git 仓库跑，不只做文本断言。

## 为什么必须是行为测试

Daily Forecast 在 2026-10-12 02:24 的 schedule run 挂了。表面看只是"偶发冲突"，
真正的问题是重试循环：

    for i in 1 2 3; do
      git pull --rebase --autostash origin main && git push && break
      sleep 5
    done

它**从不 abort 失败的 rebase**。第一次冲突后仓库停在「rebase 进行中 + 有未合并
文件」状态，第二轮 `git pull --rebase` 直接报
`error: Pulling is not possible because you have unmerged files`，
于是**一次瞬时冲突必然升级成永久失败**。

这种 bug 用 grep 断言工作流文本完全测不出来 —— 文本里确实有 `for` 和 `&& break`，
看起来一切正常。只有真的建仓库、真的制造冲突、真的跑那段脚本，才会暴露。

CI 里没有 PyYAML，所以工作流定义用文本方式读取，脚本片段再喂给真 git 执行。
"""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"


def sh(cmd: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", cmd], cwd=str(cwd),
                          capture_output=True, text=True, timeout=180,
                          errors="replace")


def make_env() -> tuple[Path, Path, Path]:
    """建 remote + 写入方 A（落后，模拟 Actions checkout）+ 写入方 B（抢先推）。"""
    tmp = Path(tempfile.mkdtemp(prefix="wf-"))
    sh("git init -q --bare remote.git", tmp)
    sh("git clone -q remote.git a", tmp)

    a = tmp / "a"
    sh("git config user.email t@t && git config user.name t", a)
    (a / "data" / "state").mkdir(parents=True)
    (a / "web").mkdir()
    (a / "data" / "state" / "pred.json").write_text('{"v":"base"}')
    (a / "web" / "index.html").write_text("<html>base</html>")
    sh("git add -A && git commit -qm base && git push -q origin HEAD:main", a)

    # 写入方 B 先推，污染生成物
    sh("git clone -q remote.git b", tmp)
    b = tmp / "b"
    sh("git config user.email t@t && git config user.name t", b)
    (b / "data" / "state" / "pred.json").write_text('{"v":"B-pushed-first"}')
    (b / "web" / "index.html").write_text("<html>B</html>")
    sh("git add -A && git commit -qm 'auto update B' && git push -q origin HEAD:main", b)
    return tmp, a, b


def extract_commit_block() -> str:
    """从 daily-forecast.yml 里抠出 Commit data 的 run 脚本。"""
    text = (WF / "daily-forecast.yml").read_text(encoding="utf-8")
    m = re.search(r"- name: Commit data\n\s+run: \|\n((?:\s{10,}.*\n|\s*\n)+)", text)
    assert m, "找不到 Commit data 步骤"
    lines = []
    for ln in m.group(1).split("\n"):
        if not ln.strip():
            lines.append("")
            continue
        lines.append(ln[10:])          # 去掉 YAML 块缩进
    return "\n".join(lines)


class TestDailyForecastCommit(unittest.TestCase):

    def setUp(self):
        self.tmp, self.a, self.b = make_env()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_survives_concurrent_writer(self):
        """复现线上故障：A 基于旧 main 生成，B 已抢先推了同名生成物。

        期望：A 仍然能把本次生成结果推上去，且历史线性、无冲突标记。

        注意**不能**在这里预先 commit —— 真实工作流的 commit 就在被测脚本里
        （`git add data/ web/` + `git diff --cached --quiet`）。预先提交会让
        脚本判定「无变更」直接 exit 0，测到的就不是同一条路径了。
        """
        a = self.a
        # A 现在生成当日数据（相对 B 的内容不同 → rebase 必然冲突）
        (a / "data" / "state" / "pred.json").write_text('{"v":"A-generated-now"}')
        (a / "web" / "index.html").write_text("<html>A</html>")

        script = extract_commit_block().replace(
            "git config user.name \"github-actions[bot]\"",
            "git config user.name bot").replace(
            "git config user.email \"github-actions[bot]@users.noreply.github.com\"",
            "git config user.email bot@bot")
        r = sh(script, a)
        self.assertNotIn("Pulling is not possible because you have unmerged files",
                         r.stdout + r.stderr,
                         "重试循环没有 abort 失败的 rebase —— 一次冲突就升级成永久失败")

        # 远端最终应是本次生成的版本
        sh("git clone -q remote.git check", self.tmp)
        chk = self.tmp / "check"
        self.assertEqual((chk / "data" / "state" / "pred.json").read_text(),
                         '{"v":"A-generated-now"}',
                         "冲突解决方向错了：rebase 期间 --theirs 才是本次生成的版本")
        self.assertEqual((chk / "web" / "index.html").read_text(), "<html>A</html>")

        # 历史线性，无冲突标记残留
        log = sh("git log --oneline", chk).stdout
        self.assertIn("daily: forecast", log)
        self.assertIn("auto update B", log)
        markers = sh("grep -rl '<<<<<<<' . 2>/dev/null || true", chk).stdout.strip()
        self.assertEqual(markers, "", "推送后残留冲突标记，会污染 Pages")

    def test_no_change_is_a_clean_noop(self):
        """没有新数据时应当干净退出，而不是制造空提交或报错。"""
        a = self.a
        sh("git pull -q --rebase origin main", a)
        script = extract_commit_block()
        r = sh(script, a)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = sh("git log --oneline", a).stdout
        self.assertNotIn("daily: forecast", log, "无变更时不应产生空提交")

    def test_unresolvable_state_fails_loudly(self):
        """三轮都推不上去时必须 exit 1，不能「循环跑完就算过」。

        构造法：把 remote 指到一个不可写的裸仓库，push 必然失败。
        """
        a = self.a
        (a / "data" / "state" / "pred.json").write_text('{"v":"A"}')
        (a / "web" / "index.html").write_text("<html>A</html>")
        sh("git remote set-url origin /nonexistent/repo.git", a)
        script = extract_commit_block()
        r = sh(script, a)
        self.assertNotEqual(r.returncode, 0,
                            "推不上去却返回 0 —— 失败被静默吞掉，线上会静默停止更新")
        self.assertIn("重试 3 轮仍未能 push", r.stdout + r.stderr)


class TestWorkflowHygiene(unittest.TestCase):
    """文本层面的静态约束（CI 无 PyYAML，用正则即可）。"""

    def test_retry_loop_never_loops_without_abort(self):
        """只要存在 `for` 重试 + `git pull --rebase`，就必须有 abort/continue 收尾。"""
        text = (WF / "daily-forecast.yml").read_text(encoding="utf-8")
        self.assertIn("git pull --rebase", text)
        self.assertRegex(text, r"git rebase --abort",
                         "重试循环前必须存在 abort，否则一次冲突即永久失败")
        self.assertRegex(text, r"git rebase --continue",
                         "冲突需要显式 continue，否则 rebase 卡在进行中")

    def test_conflict_resolution_takes_generated_version(self):
        text = (WF / "daily-forecast.yml").read_text(encoding="utf-8")
        self.assertIn("git checkout --theirs", text,
                      "生成物冲突必须取本次生成的版本（rebase 语义下 theirs）")

    def test_sync_failure_is_not_swallowed(self):
        """`|| echo "继续"` 会把 rebase 失败留下的未合并状态传给下游。"""
        text = (WF / "daily-forecast.yml").read_text(encoding="utf-8")
        self.assertNotRegex(
            text, r"git pull --rebase origin main\s*\|\|\s*echo",
            "同步远端失败不能静默吞掉，否则下游必然报 unmerged files")

    def test_push_success_is_verified(self):
        """重试循环结束后必须显式校验是否真的 push 成功，
        不能「循环跑完就算过」—— 那是失败静默的经典形态。"""
        text = (WF / "daily-forecast.yml").read_text(encoding="utf-8")
        self.assertRegex(text, r"pushed=1",
                         "缺少 pushed 标志，无法判定重试是否真的成功")
        self.assertRegex(
            text,
            r'\[\s*"\$pushed"\s*-ne\s*1\s*\][\s\S]{0,200}?exit 1',
            "push 未成功时必须 exit 1（循环跑完不等于成功）")


class TestTestWorkflowConcurrency(unittest.TestCase):
    """test.yml 的并发组不能是全局的 —— 否则并行 PR 互相取消 CI。"""

    def test_concurrency_group_is_per_pr(self):
        text = (WF / "test.yml").read_text(encoding="utf-8")
        m = re.search(r"^concurrency:\n(?:\s*#.*\n)*\s*group:\s*(.+?)\s*$",
                      text, re.M | re.S)
        assert m, "找不到 concurrency.group"
        group = m.group(1)
        self.assertNotEqual(group, "sporttery-tests",
                            "全局并发组会让并行 PR 互相取消 CI（2026-10-12 实测）")
        self.assertIn("github.event.pull_request.number", group,
                      "并发组必须按 PR 编号分组，否则多个 PR 抢同一个槽位")

    def test_push_events_still_grouped_by_ref(self):
        text = (WF / "test.yml").read_text(encoding="utf-8")
        self.assertIn("github.ref", text,
                      "push 事件没有 PR 号，需回退到 ref 分组，否则所有 push 串行")


if __name__ == "__main__":
    unittest.main()
