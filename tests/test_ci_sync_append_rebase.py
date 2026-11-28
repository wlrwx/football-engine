"""并发追加 odds_series 不许把 ci-sync 回仓顶成冲突（2026-11-28，ci-sync run #201）。

失败原文（`.github/workflows/ci-sync.yml` 第 12 步「提交回仓库」）：

    CONFLICT (content): Merge conflict in data/state/odds_series/2026-11-29_3747068.jsonl
    ...（同类 69 个文件，全部落在 data/state/odds_series/）
    Rebasing (1/1)
    error: could not apply 5439b30... ci-sync: 11-28 13:07
    ##[error]ci-sync 与上游冲突——不覆盖上游，红一轮；生成物下一轮重算
    ##[error]Process completed with exit code 1.

根因不在代码质量，在**数据结构**：`scripts/fetch_sina_odds.py` 对
`data/state/odds_series/<销售日>_<match_id>.jsonl` 是「末尾追加一行」的写法，
而这个脚本同时挂在 ci-sync（每小时 :47）与 daily-forecast 两个 workflow 上。
两个写入方都在同一个 EOF 位置插行，git 三方合并的插入点重合 → 只要
daily-forecast 在 ci-sync 那 9 分钟的 run 期间推了 main，ci-sync 的
`git pull --rebase` 必冲突。工作流按设计「不覆盖上游」exit 1，于是随机红。

修法 = `.gitattributes` 把这类只追加的 jsonl 声明成 git 内置 `merge=union`。
本文件用真 git + 本地裸库跑**同一段抠出来的工作流脚本**，正反两面都钉住：

* 带该属性：两侧新增行都在，回仓成功，不覆盖上游；
* 去掉该属性：同样的场景必然红 —— 证明测试测的是这个修复，不是 git 心情。

同时钉住 union 的边界：非追加类的真实内容冲突（`data/x.json`）照旧必须红，
不许被 union 悄悄吞掉。文本断言一律不做（旧代码文本里也有 git push，看着一切正常）。
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows" / "ci-sync.yml"
GITATTR = ROOT / ".gitattributes"
STEP = "提交回仓库"

SERIES_DIR = "data/state/odds_series"
SERIES_FILES = [f"{SERIES_DIR}/2026-11-29_3747068.jsonl",
                f"{SERIES_DIR}/2026-11-29_3748491.jsonl",
                f"{SERIES_DIR}/2026-11-29_3751707.jsonl"]


def _bash():
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    candidates = []
    if git:
        candidates.append(str(Path(git).resolve().parent.parent / "bin" / "bash.exe"))
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            candidates.append(str(Path(base) / "Git" / "bin" / "bash.exe"))
    return next((c for c in candidates if Path(c).exists()), None)


BASH = _bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="需要 Git Bash / POSIX bash")


def sh(cmd, cwd):
    # Actions 的默认 shell 是 `bash -e --noprofile --norc -o pipefail`，
    # 这里同样开 -e：脚本在 errexit 下的走向才是 CI 里的走向。
    # LC_ALL=C 把 git 消息钉成英文——断言要看的是冲突本身，不是机器语言。
    env = {**os.environ, "LC_ALL": "C", "LANG": "C", "GIT_MERGE_AUTOEDIT": "no"}
    return subprocess.run([BASH, "-e", "-o", "pipefail", "-c", cmd], cwd=str(cwd),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=False, timeout=180, env=env)


def commitback_block():
    """把 ci-sync「提交回仓库」的 run 脚本原样抠出来交给真 bash。"""
    lines = WF.read_text(encoding="utf-8").splitlines()
    start = next(k for k, l in enumerate(lines) if l.strip() == f"- name: {STEP}")
    run_at = next(k for k in range(start, len(lines)) if lines[k].strip() == "run: |")
    indent = len(lines[run_at]) - len(lines[run_at].lstrip())
    out = []
    for line in lines[run_at + 1:]:
        if line.strip() and (len(line) - len(line.lstrip())) <= indent:
            break
        body = line[indent + 2:] if line.strip() else ""
        if "remote set-url" in body:      # 测试用本地裸库，不改 remote
            continue
        out.append(body)
    text = "\n".join(out)
    assert "git push" in text and "exit 1" in text, "抠错了段落"
    return text


def repo_gitattributes(union: bool) -> str:
    """仓库真实的 .gitattributes；union=False 时只摘掉那一行，用于对照组。"""
    text = GITATTR.read_text(encoding="utf-8")
    if not union:
        kept = [l for l in text.splitlines() if not l.startswith(f"{SERIES_DIR}/*.jsonl")]
        text = "\n".join(kept) + "\n"
    assert ("merge=union" in text) is union, "对照组的属性文件没对照上"
    return text


def snap(ts, home):
    return json.dumps({"ts": ts, "match_no": "周日016", "home_team": "图卢兹",
                       "away_team": "雷恩", "league": "法甲",
                       "match_time": "2026-11-30 00:15:00",
                       "euro": {"home": home, "draw": 3.67, "away": 2.4}},
                      ensure_ascii=False)


@pytest.fixture
def env(tmp_path):
    """bare 远端 + 两个克隆：a=ci-sync，b=daily-forecast，各自都在追加水位序列。"""
    remote = tmp_path / "remote.git"
    sh('git init -q --bare remote.git && git -C remote.git symbolic-ref HEAD refs/heads/main',
       tmp_path)
    a = tmp_path / "a"
    sh("git clone -q remote.git a", tmp_path)
    sh("git config user.email ci@sync && git config user.name ci-sync", a)

    seed = ["mkdir -p data/state/odds_series web"]
    for f in SERIES_FILES:
        seed.append(f"mkdir -p $(dirname '{f}')")
        seed.append(f"printf '%s\\n%s\\n' "
                    f"'{snap('2026-11-27 19:15:11', 2.37)}' "
                    f"'{snap('2026-11-28 03:59:12', 2.32)}' > '{f}'")
    seed.append('echo \'{"v":0}\' > data/x.json')
    seed.append("echo base > web/index.html")
    seed.append("git add -A && git commit -qm base && git push -q origin HEAD:main")
    r = sh("\n".join(seed), a)
    assert r.returncode == 0, r.stdout + r.stderr

    b = tmp_path / "b"
    sh("git clone -q remote.git b", tmp_path)
    sh("git config user.email daily@forecast && git config user.name daily-forecast", b)
    return remote, a, b, tmp_path


def install_attr(a, b, union: bool):
    """把 .gitattributes（含或不含 union）落到 main，两个克隆都同步到。

    属性必须来自远端 main 而不是各自本地新提交：否则 rebase 时两边 add/add
    同一份文件，测的就是别的分支了。
    """
    r = sh(f"cat > .gitattributes <<'ATTR_EOF'\n{repo_gitattributes(union)}\nATTR_EOF\n"
           "git add .gitattributes && git commit -qm attrs && git push -q origin HEAD:main", a)
    assert r.returncode == 0, r.stdout + r.stderr
    r = sh("git pull -q origin main", b)
    assert r.returncode == 0, r.stdout + r.stderr
    assert ("merge=union" in sh("cat .gitattributes", b).stdout) is union


def append_series(repo, ts, home):
    lines = [f"printf '%s\\n' '{snap(ts, home)}' >> '{f}'" for f in SERIES_FILES]
    r = sh("\n".join(lines), repo)
    assert r.returncode == 0, r.stdout + r.stderr


def remote_lines(remote, path, repo):
    r = sh(f"git --git-dir='{remote}' show main:'{path}'", repo)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.splitlines()


# ---- 修复生效 ----------------------------------------------------------------

@pytest.mark.parametrize("union", [True, False], ids=["union", "no-union"])
def test_concurrent_append_to_series_files(env, union):
    """#201 场景：daily-forecast 先推了一行，ci-sync 随后回仓。

    union 版必须绿并把两侧的行都留在仓里；对照版必须红（根因复现）。
    """
    remote, a, b, _ = env
    install_attr(a, b, union)

    # daily-forecast 先写先推（05:01 抓的快照）
    append_series(b, "2026-11-28 05:01:10", 2.30)
    r = sh("git commit -qam 'daily: forecast 2026-11-28' && git push -q origin HEAD:main", b)
    assert r.returncode == 0, r.stdout + r.stderr

    # ci-sync 抓完自己的快照（05:06）后走工作流那段回仓脚本
    append_series(a, "2026-11-28 05:06:33", 2.28)
    res = sh(commitback_block(), a)
    out = res.stdout + res.stderr

    if not union:
        # 根因：两个写入方在同一 EOF 位置各插一行，rebase 必冲突 → 红一轮
        assert res.returncode != 0, "无 union 属性时本应复现 #201 的冲突红"
        assert "CONFLICT" in out and "odds_series" in out, out[-600:]
        assert "::error::" in out, out[-600:]
        # 失败必须是干净的：既不能停在 rebase 中间态，也不能把标记写进数据
        assert "rebase" not in sh("git status --short", a).stdout.lower(), "退出时停在 rebase 中间态"
        for f in SERIES_FILES:
            assert "<<<<<<<" not in sh(f"cat '{f}'", a).stdout, "冲突标记写进了生成物"
        return

    assert res.returncode == 0, out[-900:]
    for f in SERIES_FILES:
        body = remote_lines(remote, f, a)
        assert any('"2026-11-28 05:01:10"' in l for l in body), \
            "上游（daily-forecast）那一行被覆盖丢了"
        assert any('"2026-11-28 05:06:33"' in l for l in body), "本次采集没有进仓"
        assert len(body) == 4, f"序列行数不对: {body}"
        assert not any(m in l for l in body for m in ("<<<<<<<", "=======", ">>>>>>>"))
    assert "已回仓" in out


def test_union_keeps_chronological_order_on_read(env):
    """union 的落盘顺序是「上游侧 + 本地侧」，上游写得晚时文件会时间倒挂。

    这是 union 的已知代价，由读侧 `sort_snapshots` 兜住（见 tests/test_odds_series.py）。
    这里只钉住「两行都在、git 层不丢数据」。
    """
    remote, a, b, _ = env
    install_attr(a, b, True)

    # 上游这一行其实是我们之后抓的（ts 更大），先推上去了
    append_series(b, "2026-11-28 09:00:00", 2.10)
    r = sh("git commit -qam 'daily: forecast' && git push -q origin HEAD:main", b)
    assert r.returncode == 0, r.stdout + r.stderr
    append_series(a, "2026-11-28 05:06:33", 2.28)
    res = sh(commitback_block(), a)
    assert res.returncode == 0, (res.stdout + res.stderr)[-900:]

    body = remote_lines(remote, SERIES_FILES[0], a)
    ts = [json.loads(l)["ts"] for l in body]
    assert set(ts) >= {"2026-11-28 09:00:00", "2026-11-28 05:06:33"}, "两侧的行必须都在"
    assert [json.loads(l)["euro"]["home"] for l in
            sorted(body, key=lambda l: json.loads(l)["ts"])] == [2.37, 2.32, 2.28, 2.10], \
        "按 ts 正序还原后应是一条完整的水位链（读侧排序的前提）"


def test_union_does_not_swallow_real_content_conflict(env):
    """union 只救只追加的序列文件。真实内容冲突必须继续红、继续不覆盖上游。"""
    remote, a, b, _ = env
    install_attr(a, b, True)
    append_series(b, "2026-11-28 05:01:10", 2.30)

    # 同一个非序列文件两边都改：这类冲突 union 不该插手
    r = sh("echo '{\"v\":99}' > data/x.json && git commit -qam '上游先改' "
           "&& git push -q origin HEAD:main", b)
    assert r.returncode == 0, r.stdout + r.stderr
    append_series(a, "2026-11-28 05:06:33", 2.28)
    sh("echo '{\"v\":3}' > data/x.json", a)

    res = sh(commitback_block(), a)
    out = res.stdout + res.stderr
    assert res.returncode != 0, "data/x.json 的真冲突被 union 属性掩盖了"
    assert "::error::" in out, out[-600:]
    assert '"v":99' in sh(f"git --git-dir='{remote}' show main:data/x.json", a).stdout, \
        "冲突处理覆盖了先推的一方"


def test_extraction_unchanged():
    """脚本仍然保留 3 轮重试与非零退出（修复不许把它改回静默）。"""
    b = commitback_block()
    assert re.search(r"for i in 1 2 3", b), "丢了重试"
    assert b.count("exit 1") >= 2


# ---- 修完之后，读侧必须还能给出正确方向 --------------------------------------

def test_union_merged_series_still_yields_directed_features(env, tmp_path, monkeypatch):
    """把两层缝起来真跑：并发追加 → git union 合并 → engine 读特征。

    union 的输出是「上游侧新增行 + 本地侧新增行」，上游那一行写得比我们晚时
    落盘就时间倒挂。`series_features` 取 seq[0]/[-1]/[-2]，倒挂会把资金流方向算反。
    读侧 `sort_snapshots` 是 union 的必要配套，这里钉住整链条。
    """
    from datetime import datetime

    import engine.odds_series as osd

    remote, a, b, _ = env
    install_attr(a, b, True)

    append_series(b, "2026-11-28 09:00:00", 2.10)     # 先推，但抓得更晚
    r = sh("git commit -qam 'daily: forecast' && git push -q origin HEAD:main", b)
    assert r.returncode == 0, r.stdout + r.stderr
    append_series(a, "2026-11-28 05:06:33", 2.28)     # 后推，抓得更早
    res = sh(commitback_block(), a)
    assert res.returncode == 0, (res.stdout + res.stderr)[-900:]

    body = remote_lines(remote, SERIES_FILES[0], a)
    ts = [json.loads(l)["ts"] for l in body]
    assert ts != sorted(ts), "这一轮的构造本该让 union 落盘时间倒挂，否则测不到读侧"

    d = tmp_path / "series"
    d.mkdir()
    (d / "2026-11-29_3747068.jsonl").write_text("\n".join(body) + "\n", encoding="utf-8")
    monkeypatch.setattr(osd, "SERIES_DIR", d)

    seq = osd.load_series("2026-11-29_3747068")
    assert [s["ts"] for s in seq] == sorted(ts), "读侧必须把 union 的顺序乱修复"

    feat = osd.series_features("2026-11-29_3747068")
    assert feat["points"] == 4
    # 真实方向：2.37（11-27 19:15）→ … → 2.10（11-28 09:00），一路走低
    assert feat["overall_home"] == round((2.10 - 2.37) / 2.37 * 100, 2)
    assert feat["recent_home"] == round((2.10 - 2.28) / 2.28 * 100, 2)
    t0, t1 = (datetime.fromisoformat(x) for x in (min(ts), max(ts)))
    assert feat["span_min"] == round((t1 - t0).total_seconds() / 60, 1)
    assert feat["span_min"] > 0, "span 为负 = 首末被顺序倒挂读反了"
