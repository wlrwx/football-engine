#!/usr/bin/env python3
"""确定性挑战值守（零模型版）——launchd 每小时 :05/:35 触发。

流程：cookie→mint JWT→（变化时）推 GitHub secret→跑 challenge_hourly→state 冲突自愈→commit/push。
退出码：0 正常（含网络异常跳过，日志会写明）；20 需人工登录（浏览器 session 死，
跑 scripts/njstats_seed_login.py 重新播种）。网络异常 ≠ 登录失效：2026-10-30~11-01 断网
42 小时里 81 轮全被报成「需人工登录」，把人和自动化都带偏。
依赖：~/.football-engine/njstats_session.json（由 seed_login 播种）、pynacl、requests。
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
import njstats_api_submit as api  # noqa: E402

HOME = Path.home()
SESS = HOME / ".football-engine" / "njstats_session.json"
JWT_FILE = HOME / ".football-engine" / "njstats_jwt"
LOG = HOME / ".football-engine" / "duty.log"
BASE = "https://www.njstats.cn"
REPO = "wlrwx/football-engine"
# GitHub Actions 里 Challenge (1x2) workflow 的数字 ID（gh api 可查）。
# 平台对公开仓库的定时任务限流严重（实测 24h 只跑 3/24 次），这个看门狗
# 在本机这一轮零提交、且 CI 已超 CI_STALE_H 小时没跑时补触发一轮。
CI_WORKFLOW_ID = 385270775
CI_STALE_H = 3.0
# daily-forecast 的看门狗（2026-10-23）：它负责生成当天预测，GitHub 限流会让
# 「今天还没跑过」变成挑战池匹配 0（10-23 01:30 实测匹配0/候选0）。
DAILY_WORKFLOW_ID = 316265897
DAILY_STALE_H = 4.0
DAILY_COOLDOWN_H = 2.0
BJT = timezone(timedelta(hours=8))
# duty 自己拥有的落盘路径。分成两组是因为提交时机不同：state 在投完注就要推
# （CI 那边靠它去重），三份快照在所有采集步跑完后才齐。
# 只列这三份，不整目录 add——njstats/ 下还有别的脚本的产物，
# 让 duty 顺手提交它们等于替别人决定版本，会和 ci-sync 抢写。
STATE_PATHS = ["data/state/challenge_state.json"]
SNAPSHOT_PATHS = ["data/state/njstats/site_odds/",
                  "data/state/njstats/my_predictions/",
                  "data/state/njstats/community/"]
# DJYY 快照必须由本机住宅网络抓（见 _maybe_djyy_snapshot 的归因）。
DJYY_PATH = "data/djyy_matches.json"
DJYY_MAX_AGE_H = 10.0
DJYY_ATTEMPT_GAP_H = 2.0
DJYY_MARKER = HOME / ".football-engine" / "djyy_last_attempt"
NET_STATE = HOME / ".football-engine" / "net_fail_state.json"
CHALLENGE_STATE = "data/state/challenge_state.json"
# main 写入者监督（见 watch_main_writes）：小号拿到 Write 之后，main 的作者不再是常量。
MAIN_WATCH = HOME / ".football-engine" / "main_watch.json"
MAIN_WATCH_SEED_DAYS = "90.days"
MINT_NET = "__net__"  # mint 因网络/SSL 拦截失败的哨兵（不是登录问题）
PX = {"http": None, "https": None}  # 直连；显式 None 防环境代理回落
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"


def log(msg: str) -> None:
    line = f"{time.strftime('%m-%d %H:%M:%S')} {msg}"
    print(line)
    try:
        if LOG.exists() and LOG.stat().st_size > 512_000:
            LOG.rename(LOG.with_suffix(".log.old"))
        with LOG.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def sh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=False, encoding="utf-8", errors="replace")


def commit_push(paths: list[str], msg: str) -> bool:
    """把指定路径的工作区变更提交并推送（3 次重试）。无变更视为已完成，返回 True。

    2026-10-22 修：原先这里是一段只认 `challenge_state.json` 的内联 commit，
    而 duty 每天另外写的三份快照（site_odds / my_predictions / community）
    **从来没人提交**——它们只存在于这台笔记本的工作区里。站点赔率快照存在的
    唯一理由就是攒样本做配对回放（docs/CHALLENGE_ODDS_GAP_20261021.md），
    不进 origin 等于没攒；已知坑②「git reset --hard 清归档」已经发生过两次。
    与 rho 写回「进工作区无人提交」是同一个病。
    """
    if not sh("git", "status", "--short", "--", *paths).stdout.strip():
        return True
    sh("git", "add", "-A", "--", *paths)
    if sh("git", "commit", "-m", msg).returncode != 0:
        log(f"commit 失败: {msg}")
        return False
    for _ in range(3):
        pull = sh("git", "pull", "--rebase", "--autostash", "origin", "main")
        if pull.returncode == 0:
            if sh("git", "push", "origin", "main").returncode == 0:
                log(f"已推: {msg}")
                return True
            time.sleep(4)
            continue
        # 失败的 rebase 必须 abort，否则仓库停在「rebase 进行中 + 未合并文件」，
        # 下一轮开头那句 pull --rebase 直接报
        #   error: Pulling is not possible because you have unmerged files
        # ——这台机器就再也提交不了任何东西（daily-forecast 已栽过同一坑，
        # 2026-10-22 CI 状态回仓开始真提交之后，这个窗口才真正会出现）。
        # 冲突不猜方向：本地提交留在原地，下一轮在新 base 上重放。
        # abort 的退出码同时用来区分病因：成功=真有 rebase 被中止（内容冲突），
        # 失败=根本没进 rebase（网络/凭据/远端不可达），别把两者混成「冲突」。
        aborted = sh("git", "rebase", "--abort").returncode == 0
        lines = [l.strip() for l in (pull.stderr or pull.stdout).splitlines() if l.strip()]
        msg_line = next((l for l in lines if l.startswith(("fatal:", "error:"))),
                        lines[-1] if lines else "无输出")
        log(("pull 冲突，已 abort" if aborted else "pull 失败（未进入 rebase）")
            + f": {msg_line}（本地提交待下一轮重放）")
        time.sleep(4)
    log(f"push 失败（3 次重试）: {msg}")
    return False


def autoheal_session() -> bool:
    """SESSION cookie 过期时用持久 profile 的 remember-me 无头续期，无需人工过验证码。

    只在 mint 拿到非 200 时调用。失败返回 False，由调用方按「需人工播种」处理。
    """
    if not (HOME / ".football-engine" / "pwprofile").exists():
        return False
    p = subprocess.run([str(ROOT / ".venv" / "bin" / "python3"),
                        "scripts/njstats_refresh_session.py"],
                       cwd=ROOT, capture_output=True, text=True, check=False, timeout=180, encoding="utf-8", errors="replace")
    ok = p.returncode == 0
    log(f"续期脚本 rc={p.returncode} {(p.stdout or p.stderr).strip().splitlines()[-1] if (p.stdout or p.stderr).strip() else ''}")
    return ok


def mint() -> str | None:
    if not SESS.exists():
        log("无 session 文件，需播种登录")
        return None
    cookie = json.loads(SESS.read_text(encoding="utf-8")).get("SESSION", "")
    if not cookie:
        log("session 文件缺 SESSION")
        return None
    try:
        r = requests.get(f"{BASE}/api/user/current",
                         headers={"Cookie": f"SESSION={cookie}", "User-Agent": UA,
                                  "Accept": "application/json", "Referer": BASE + "/prediction/mine"},
                         timeout=20, proxies=PX)
    except requests.RequestException as e:
        log(f"mint 网络异常 {e}")
        return MINT_NET
    if r.status_code != 200:
        log(f"mint HTTP {r.status_code}（session 死，尝试无头续期）")
        if not autoheal_session():
            log("无头续期失败，需人工登录（跑 njstats_seed_login.py）")
            return None
        cookie = json.loads(SESS.read_text(encoding="utf-8")).get("SESSION", "")
        try:
            r = requests.get(f"{BASE}/api/user/current",
                             headers={"Cookie": f"SESSION={cookie}", "User-Agent": UA,
                                      "Accept": "application/json", "Referer": BASE + "/prediction/mine"},
                             timeout=20, proxies=PX)
        except requests.RequestException as e:
            log(f"续期后 mint 网络异常 {e}")
            return MINT_NET
        if r.status_code != 200:
            log(f"续期后仍 HTTP {r.status_code}，需人工登录")
            return None
        log("无头续期成功")
    tok = ((r.json() or {}).get("data") or {}).get("token")
    return tok or None


def _mint_action(tok):
    """把 mint() 结果翻成 (退出码, 日志)；退出码 None 表示继续本轮。

    网络异常返回 0 而不是 20：让 launchd/巡检能分清「等下轮自愈」和「要人登录」。
    """
    if tok == MINT_NET:
        return 0, "本轮：网络异常导致 mint 失败，跳过（不需人工登录，下轮自动重试）"
    if not tok:
        return 20, "本轮：mint 失败，需人工登录（跑 njstats_seed_login.py）"
    return None, ""


def _net_fail_note(now_ts: float, path: Path | None = None) -> str:
    """断网计时：让一条日志就能看出「断了多久」，而不是每轮重复同一句话。"""
    p = path or NET_STATE
    st: dict = {}
    try:
        if p.exists():
            st = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    since = st.get("since")
    if not since:
        st = {"since": now_ts, "last": now_ts}
    else:
        st["last"] = now_ts
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(st), encoding="utf-8")
    except OSError:
        pass
    dur = (now_ts - float(st["since"])) / 3600
    start = datetime.fromtimestamp(float(st["since"])).strftime("%m-%d %H:%M")
    return f"（自 {start} 起已 {dur:.1f}h，下轮自动重试）"


def _net_fail_clear(now_ts: float, path: Path | None = None) -> str:
    """恢复时给出总停机时长并清计时；没在计时则返回空串。"""
    p = path or NET_STATE
    if not p.exists():
        return ""
    try:
        since = json.loads(p.read_text(encoding="utf-8")).get("since")
    except (OSError, ValueError):
        since = None
    try:
        p.unlink()
    except OSError:
        pass
    if not since:
        return ""
    return f"（中断 {(now_ts - float(since)) / 3600:.1f}h 后恢复）"


def merge_challenge_state(a: str, b: str) -> str | None:
    """challenge_state 冲突按并集合并，而不是取某一边（2026-11-01）。

    27/28 配额 + E8/E10 是实验样本；原来「取新生成侧」会整份覆盖对侧记录，
    CI 侧写入的 ev_site/site_odds/exp 字段可能整批消失，而服务端去重只防重复
    下单、不防样本丢失。records 按 (match, pick) 去重并保留字段更全的一条。
    任一侧解析失败返回 None，由调用方回退到原有「取新侧」逻辑。
    """
    try:
        da = json.loads(a) if (a or "").strip() else {}
        db = json.loads(b) if (b or "").strip() else {}
    except json.JSONDecodeError:
        return None
    if not isinstance(da, dict) or not isinstance(db, dict):
        return None
    newer = da if str(da.get("week") or "") >= str(db.get("week") or "") else db
    out = dict(newer)
    keyed: dict[tuple, dict] = {}
    for src in (da, db):
        recs = src.get("records")
        for r in (recs if isinstance(recs, list) else []):
            if not isinstance(r, dict):
                continue
            k = (str(r.get("match")), str(r.get("pick")))
            old = keyed.get(k)
            if old is None or len(json.dumps(r, sort_keys=True)) > len(json.dumps(old, sort_keys=True)):
                keyed[k] = r
    out["records"] = list(keyed.values())
    def _int(v, dflt):
        try:
            return int(v)
        except (TypeError, ValueError):
            return dflt
    out["used"] = max(_int(da.get("used"), 0), _int(db.get("used"), 0))
    out["quota"] = max(_int(da.get("quota"), 28), _int(db.get("quota"), 28))
    return json.dumps(out, ensure_ascii=False, indent=1)


def push_secret(jwt: str) -> None:
    try:
        from nacl.encoding import Base64Encoder
        from nacl.public import PublicKey, SealedBox
    except ImportError:
        log("无 pynacl，跳过推 secret"); return
    p = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                       capture_output=True, text=True, cwd=ROOT, check=False, encoding="utf-8", errors="replace")
    tok = next((l.split("=", 1)[1] for l in p.stdout.splitlines() if l.startswith("password=")), "")
    # launchd 环境可能碰不到 keychain：文件 token 优先
    ftok = HOME / ".football-engine" / "github_token"
    if ftok.exists():
        tok = ftok.read_text(encoding="utf-8").strip() or tok
    if not tok:
        log("无 GitHub token，跳过推 secret"); return
    h = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
    pk = requests.get(f"https://api.github.com/repos/{REPO}/actions/secrets/public-key", headers=h, timeout=30, proxies=PX).json()
    if "key" not in pk:
        log(f"取 public-key 失败 {pk.get('message')}"); return
    sealed = SealedBox(PublicKey(pk["key"], Base64Encoder)).encrypt(jwt.encode())
    rr = requests.put(f"https://api.github.com/repos/{REPO}/actions/secrets/NJSTATS_JWT", headers=h,
                      json={"key_id": pk["key_id"], "encrypted_value": base64.b64encode(sealed).decode()},
                      timeout=30, proxies=PX)
    log(f"推 secret → {rr.status_code}")


def fix_conflicts() -> None:
    """state 并发写冲突自愈：取本地新生成侧（stash 侧），逐个 JSON 校验后提交。"""
    u = sh("git", "diff", "--name-only", "--diff-filter=U").stdout.split()
    if not u:
        return
    files = " ".join(u)
    merged = None
    if CHALLENGE_STATE in u:
        head = sh("git", "show", f"HEAD:{CHALLENGE_STATE}").stdout
        side = sh("git", "show", f"stash@{{0}}:{CHALLENGE_STATE}").stdout
        merged = merge_challenge_state(head, side)
    sh("sh", "-c", f"git checkout stash@{{0}} -- {files} 2>/dev/null || git checkout --theirs -- {files}")
    if merged:
        (ROOT / CHALLENGE_STATE).write_text(merged + "\n", encoding="utf-8")
        log("challenge_state 冲突已按并集合并（不丢对侧注单）")
    sh("git", "stash", "drop")
    bad = []
    for f in u:
        if f.endswith(".json"):
            try:
                json.loads((ROOT / f).read_text(encoding="utf-8"))
            except Exception:
                bad.append(f)
    if bad:
        # 坏 JSON 回退到远端版本
        sh("git", "checkout", "origin/main", "--", *bad)
        log(f"坏 JSON 回退远端: {bad}")
    sh("git", "add", *u)
    sh("git", "commit", "-m", "duty: state 并发写冲突自愈（取新生成侧）")
    log(f"冲突已清: {len(u)} 文件")


def _main_rows(rng: str = "", limit: int = 25) -> list[dict]:
    """读 origin/main 的提交（sha/作者邮箱/标题）。rng 给定时按范围取。"""
    args = ["git", "log", "--format=%H%x09%ae%x09%s"]
    args += [rng] if rng else [f"-n{limit}"]
    args.append("origin/main")
    rows = []
    for line in sh(*args).stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            rows.append({"sha": parts[0], "email": parts[1], "subject": parts[2]})
    return rows


def unexpected_authors(rows: list[dict], known: set[str]) -> list[str]:
    """不在基线里的作者邮箱，按出现顺序去重。"""
    seen: set[str] = set()
    out: list[str] = []
    for r in rows:
        if r["email"] not in known and r["email"] not in seen:
            seen.add(r["email"])
            out.append(r["email"])
    return out


def watch_main_writes(path: Path = MAIN_WATCH) -> list[str]:
    """核对上次以来 origin/main 的新增提交作者，返回需要上报的行。

    为什么现在加它：一年多来 main 的直推只有两类来源——duty（wlrwx 的 SSH key）
    和 github-actions[bot]。接 dsh-superagent 必须给小号 Write（006 的 setup 校验
    push 权限），而 Write 也能直推 main，所以「谁改了 main」从此不再是常量。

    只报警、不自动 revert：第一次给第二个身份开写权限，既没有坏样本可以校准
    allowlist，也没有好样本的完备清单（同事邮箱、未来换的 commit identity 都算
    变量）。把 revert 建在猜出来的名单上，误判就是撤掉真提交——比漏报严重。
    所以 v1 只做可见性，出事由人决定；state 里的 last_sha 就是取证起点。
    首次运行不比对，只用近 90 天的真实历史建基线。
    """
    tip = sh("git", "log", "-n1", "--format=%H", "origin/main").stdout.strip()
    if not tip:
        return []
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = None
    if not isinstance(state, dict) or "known_authors" not in state:
        known = {r["email"] for r in _main_rows(rng=f"--since={MAIN_WATCH_SEED_DAYS}")}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"last_sha": tip, "known_authors": sorted(known)},
                                       ensure_ascii=False), encoding="utf-8")
            os.chmod(path, 0o600)
        except OSError:
            pass
        log(f"main 作者基线建立：{len(known)} 个邮箱（近 90 天历史）")
        return []

    known = set(state.get("known_authors") or [])
    last = str(state.get("last_sha") or "")
    alerts: list[str] = []
    if last and last != tip and sh("git", "merge-base", "--is-ancestor", last,
                                   "origin/main").returncode != 0:
        # 非快进：历史被重写或 force-push，基线作废，重建后继续盯。
        alerts.append(f"⚠ main 非快进（{last[:8]} 不在 origin/main 上）——"
                      f"有人 force-push 或重写了历史，作者基线重建")
        known = {r["email"] for r in _main_rows(rng=f"--since={MAIN_WATCH_SEED_DAYS}")}
    elif last:
        rows = _main_rows(rng=f"{last}..origin/main")
        for email in unexpected_authors(rows, known):
            who = next((r for r in rows if r["email"] == email), {})
            alerts.append(f"⚠ main 陌生作者 {email}（{who.get('sha','')[:8]} {who.get('subject','')[:40]}）"
                          f"——不自动回退，请人工确认这轮提交是谁推的")
        # 陌生作者不进基线：进了就等于「看过一次就永久合法」，持续偷推只在第一轮出声。
        # 基线只在首次运行或历史被重写时从近 90 天真实历史重建。
    try:
        path.write_text(json.dumps({"last_sha": tip, "known_authors": sorted(known)},
                                   ensure_ascii=False), encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        pass
    return alerts


def _maybe_ops_digest() -> None:
    """每轮跑确定性运维摘要（零模型成本），红线写进 duty.log。

    2026-11-03：原来巡检 4 次/天 + 对账 2 次/天都是模型会话，实测新版客户端下
    约 500+ credits/天，而它们做的事全是确定性的。判定搬进 scripts/ops_digest.py
    后由这里每轮调用（48 次/天，覆盖更密），模型只在有红线时才需要介入。
    摘要自身出错只记一行，绝不影响本轮采集与提交链。
    """
    if os.environ.get("DUTY_SKIP_OPS_DIGEST") == "1":
        return
    try:
        p = subprocess.run([str(ROOT / ".venv" / "bin" / "python3"), "scripts/ops_digest.py",
                            "--write", "--quiet"], cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", check=False, timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"⚠ ops_digest 跑不起来: {type(exc).__name__}")
        return
    if p.returncode == 2:
        log(f"⚠ ops_digest 自身出错: {(p.stdout or p.stderr).strip()[:160]}")
        return
    try:
        ctx = json.loads((HOME / ".football-engine" / "ops_digest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log("⚠ ops_digest 没产出可读摘要")
        return
    flags = ctx.get("red_flags") or []
    if not flags:
        log("ops: 无红线")
        return
    for f in flags:
        log(f"⚠ 运维红线 {f.get('code')}: {f.get('detail')} → {f.get('action')}")


def main() -> int:
    os.chdir(ROOT)
    sh("git", "pull", "--rebase", "--autostash", "origin", "main")
    for _line in watch_main_writes():
        log(_line)
    if sh("git", "diff", "--name-only", "--diff-filter=U").stdout.strip():
        fix_conflicts()
    tok = mint()
    _code, _msg = _mint_action(tok)
    if _code is not None:
        log(_msg + (_net_fail_note(time.time()) if _code == 0 else ""))
        return _code
    _back = _net_fail_clear(time.time())
    if _back:
        log("网络恢复" + _back)
    old = JWT_FILE.read_text(encoding="utf-8").strip() if JWT_FILE.exists() else ""
    JWT_FILE.write_text(tok, encoding="utf-8")
    os.chmod(JWT_FILE, 0o600)
    if tok != old:
        push_secret(tok)
        log("mint(新)")
    else:
        log("mint(无变化)")
    env = dict(os.environ, NJSTATS_JWT=tok)
    p = subprocess.run([str(ROOT / ".venv" / "bin" / "python3"), "scripts/challenge_hourly.py"],
                       cwd=ROOT, capture_output=True, text=True, env=env, check=False, encoding="utf-8", errors="replace")
    tail = (p.stdout or p.stderr).strip().splitlines()
    log("1x2: " + (tail[-1] if tail else "无输出"))
    # 本轮到底投了几注——看门狗要不要补跑 CI 取决于这个
    _submitted = 0
    for line in tail:
        m = re.search(r"提交\s+(\d+)\s+注", line)
        if m:
            _submitted = int(m.group(1))
            break
    commit_push(STATE_PATHS, "duty: challenge state 更新")
    # 社区倾向快照（E6 观测，见 docs/EXPERIMENTS.md）。放在最后：前面 1x2 该投
    # 的都投完了，这一步再慢/再失败都不会影响提交链。每天只跑一次（date 门控），
    # 用 marker 而不是看快照文件在不在——避免手工删了文件后一轮轮重跑。
    if os.environ.get("DUTY_SKIP_SERVER_SNAPSHOT") != "1":
        _maybe_server_records_snapshot()
    if os.environ.get("DUTY_SKIP_COMMUNITY") != "1":
        _maybe_community_snapshot()
    # 三份 njstats 快照一写完就先提交，不等 DJYY：后者是 600s 超时的外网抓取，
    # 2026-10-23 00:10 实测把快照落地拖了 10 分钟（文件已写盘，只是没进仓）。
    # 提交失败不影响本轮采集（文件在本地，下一轮再试）——但要可见，否则又回到
    # 「静默不提交、样本只活在笔记本上」。
    commit_push(SNAPSHOT_PATHS, "duty: njstats 每日快照（站点赔率/服务端真值/社区倾向）")
    # 摘要放在快照提交之后、DJYY 之前：快照龄/入库检查要反映本轮，而 DJYY 是
    # 600s 级的外网抓取，不该让摘要等它。
    _maybe_ops_digest()
    if os.environ.get("DUTY_SKIP_DJYY") != "1":
        _maybe_djyy_snapshot()
    # CI 看门狗：平台定时任务限流严重，CI 长时间没跑时补一轮。
    # 放在最后、且只在本地零提交时动手（见函数 docstring 的三条约束）。
    if os.environ.get("DUTY_SKIP_CI_WATCHDOG") != "1":
        # 先保证当天预测存在；daily 刚补过就不要再叠一轮 challenge（下一轮再看）。
        if not _maybe_dispatch_daily_ci():
            _maybe_dispatch_ci(_submitted)
    return 0


def _gh_token() -> str:
    ftok = HOME / ".football-engine" / "github_token"
    try:
        return ftok.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _last_run_info(workflow_id: int) -> tuple[float, str] | None:
    """返回 (距上次触发小时数, 上次触发的北京日期)；取不到返回 None。"""
    tok = _gh_token()
    if not tok:
        return None
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{workflow_id}/runs"
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {tok}",
                                       "Accept": "application/vnd.github+json"},
                         params={"per_page": 1}, timeout=25, proxies=PX)
        if r.status_code != 200:
            return None
        runs = (r.json() or {}).get("workflow_runs") or []
        if not runs:
            return None
        # created_at 是 UTC ISO8601（形如 2026-10-20T18:26:16Z）
        ts = datetime.fromisoformat(runs[0]["created_at"].replace("Z", "+00:00"))
        age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
        return age_h, ts.astimezone(BJT).strftime("%Y-%m-%d")
    except (requests.RequestException, KeyError, ValueError):
        return None


def _ci_last_run_age_h() -> float | None:
    """Challenge workflow 距上次触发的小时数（测试与旧调用点保留这个名字）。"""
    info = _last_run_info(CI_WORKFLOW_ID)
    return info[0] if info else None


def _daily_last_run_info() -> tuple[float, str] | None:
    return _last_run_info(DAILY_WORKFLOW_ID)


def _dispatch_workflow(workflow_id: int) -> bool:
    """手动触发一轮 workflow（补跑）。返回是否成功受理。"""
    tok = _gh_token()
    if not tok:
        return False
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{workflow_id}/dispatches"
    try:
        r = requests.post(url, headers={"Authorization": f"Bearer {tok}",
                                        "Accept": "application/vnd.github+json"},
                          json={"ref": "main"}, timeout=25, proxies=PX)
        return r.status_code in (204, 201)
    except requests.RequestException:
        return False


def _dispatch_ci_challenge() -> bool:
    return _dispatch_workflow(CI_WORKFLOW_ID)


def _dispatch_daily_ci() -> bool:
    return _dispatch_workflow(DAILY_WORKFLOW_ID)


def _maybe_dispatch_daily_ci() -> bool:
    """daily-forecast 今天还没跑过（或已 4h 没跑）时补一轮。

    GitHub 对公开仓的 cron 限流实测 24h 只有 4-6 次；而 daily-forecast 负责生成
    当天预测。2026-10-23 01:30 实测：上次 daily 是 10-22 23:15，10-23 的预测
    一直没生成 → 挑战池连续两轮「匹配0/候选0」，页面/结算/rho 也全停。
    只在「北京时间今天还没跑过」或「已超 DAILY_STALE_H（4h）」时补；2h 冷却防叠。
    取不到状态时保守不动。
    """
    info = _daily_last_run_info()
    if info is None:
        return False
    age_h, last_date = info
    today = datetime.now(BJT).strftime("%Y-%m-%d")
    if not (last_date < today or age_h >= DAILY_STALE_H):
        return False
    marker = HOME / ".football-engine" / ".daily_watchdog_last"
    now = time.time()
    try:
        if marker.exists() and now - float(marker.read_text(encoding="utf-8").strip()) < DAILY_COOLDOWN_H * 3600:
            return False
    except (OSError, ValueError):
        pass
    if _dispatch_daily_ci():
        log(f"CI 看门狗: Daily Forecast 上次 {last_date}（{age_h:.1f}h 前），已补触发一轮")
        try:
            marker.write_text(str(int(now)), encoding="utf-8")
        except OSError:
            pass
        return True
    return False


def _maybe_dispatch_ci(local_submitted: int) -> None:
    """CI 长时间没跑时补触发一轮。

    2026-10-21 更新：去重已改走服务端真值（challenge_hourly 用
    `api.my_predictions` 取 msgId=11 的「我已预测」列表并入去重名单），
    所以**不再需要「本地零提交」这道闸**——CI 与本机即使完全并发，
    后到的那个也会看到服务端多出来的记录而跳过，不会重复下单。

    保留的两条约束是防刷爆和防叠加，与重复投注无关：
      1) 只在 CI 距上次运行超过 CI_STALE_H 小时时才补，正常调度时不叠加；
      2) 每小时最多补一次（marker），避免 3 小时空档里 6 轮 duty 各触发
         一次、堆出一串并发 run（那会挤占 Actions 配额，反过来加剧限流）。

    `local_submitted` 参数保留只为兼容旧调用点与日志，不再参与判断。
    """
    age = _ci_last_run_age_h()
    if age is None:
        return
    if age < CI_STALE_H:
        return
    marker = HOME / ".football-engine" / ".ci_watchdog_last"
    now = time.time()
    try:
        if marker.exists() and now - float(marker.read_text(encoding="utf-8").strip()) < 3600:
            return
    except (OSError, ValueError):
        pass
    if _dispatch_ci_challenge():
        log(f"CI 看门狗: Challenge 已 {age:.1f}h 未跑，已补触发一轮")
        try:
            marker.write_text(str(int(now)), encoding="utf-8")
        except OSError:
            pass


def _maybe_server_records_snapshot() -> None:
    """每次把服务端「我已预测」真值落盘，作为账本缺口的对照物。

    为什么需要：challenge_state.json 历史上出现过回写失败（5 条缺口），
    而那 5 条的服务端记录现在只能靠猜口径。以后每天存一份真值，
    一旦 state 再漏，就能直接 diff 出来、按当时的 market 码+赔率还原。
    成本很低（一次请求），但能让「账目对不上」从无解变成可追。

    落盘到 data/state/njstats/my_predictions/<YYYY-MM-DD>.json，
    每次覆盖当天（一天内多次轮询以最后一次为准，只会变多不会变少）。
    """
    try:
        recs = api.my_predictions()
    except Exception as e:  # noqa: BLE001 - 快照失败绝不能影响值守
        log(f"server-snapshot: 取数失败 {type(e).__name__}")
        return
    if recs is None:
        return
    out_dir = ROOT / "data" / "state" / "njstats" / "my_predictions"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        day = time.strftime("%Y-%m-%d")
        payload = {
            "snapshot_at": time.strftime("%F %T"),
            "count": len(recs),
            "note": "msgId=11 账号私有「我已预测」服务端真值；与 challenge_state 对照用",
            "records": recs,
        }
        (out_dir / f"{day}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as e:
        log(f"server-snapshot: 写盘失败 {e}")
        return

    # 站点赔率快照（含让球/大小球场次的 1x2 三档）。
    #
    # 为什么单独存：本地算 edge 用体彩官方赔率，而挑战赛按站点赔率结算，
    # 两者差约 10%（见 docs/CHALLENGE_ODDS_GAP_20261021.md）。站点赔率
    # 只在「当前板」可见，赛后就变了，不存下来永远没法回测「换成站点
    # 赔率算 EV 能不能提高命中」。每天存一份，攒够再做配对回放。
    try:
        board = api.board_with_odds()
    except Exception as e:  # noqa: BLE001
        log(f"odds-snapshot: 取数失败 {type(e).__name__}")
        return
    if not board:
        return
    with_odds = [b for b in board if b.get("odds")]
    odds_dir = ROOT / "data" / "state" / "njstats" / "site_odds"
    try:
        odds_dir.mkdir(parents=True, exist_ok=True)
        (odds_dir / f"{day}.json").write_text(json.dumps({
            "snapshot_at": time.strftime("%F %T"),
            "total": len(board),
            "with_odds": len(with_odds),
            "note": "msgId=1 全板 + 站点赔率（挑战赛结算口径）。与 predictions 的体彩赔率对照用",
            "matches": [{"sid": b["sid"], "home": b["home"], "away": b["away"],
                         "kick_ts": b["kick_ts"], "odds": b["odds"]} for b in with_odds],
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"odds-snapshot: 站点赔率 {len(with_odds)}/{len(board)} 场")
    except OSError as e:
        log(f"odds-snapshot: 写盘失败 {e}")


def _maybe_community_snapshot() -> None:
    """每天一轮把社区倾向落盘（站点无历史接口，只能自己攒）。"""
    marker = ROOT / "data" / "state" / ".community_snapshot_done"
    today = time.strftime("%Y-%m-%d")
    try:
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == today:
            return
    except OSError:
        pass
    p = subprocess.run([str(ROOT / ".venv" / "bin" / "python3"),
                        "scripts/njstats_community_snapshot.py"],
                       cwd=ROOT, capture_output=True, text=True, check=False, timeout=300, encoding="utf-8", errors="replace")
    out = (p.stdout or p.stderr).strip().splitlines()
    log("community: " + (out[-1] if out else f"无输出 rc={p.returncode}"))
    if p.returncode == 0:
        try:
            marker.write_text(today, encoding="utf-8")
        except OSError:
            pass


def _maybe_djyy_snapshot() -> None:
    """快照超过 10h 才由本机直连抓一次，成功即提交（失败 2h 内不重试）。

    2026-10-22 归因：djyylive 的 Cloudflare 对 Worker 出口整体返回
    `cf-mitigated: challenge`（ORD/IAD/SJC/DFW 实测，连首页都 403），
    GitHub Actions 直连同样 403；重试、cookie、浏览器头都不改变结果。
    本机住宅网络直连 200，所以把「抓取」放在 duty，「消费」留给 CI：
    CI 每轮仍会尝试刷新，失败时保留仓库里这份快照。
    """
    age_h = float("inf")
    try:
        prev = json.loads((ROOT / DJYY_PATH).read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(prev.get("extracted_at", ""))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    except (OSError, ValueError, TypeError):
        pass
    if age_h < DJYY_MAX_AGE_H:
        return
    try:
        if time.time() - float(DJYY_MARKER.read_text(encoding="utf-8")) < DJYY_ATTEMPT_GAP_H * 3600:
            return
    except (OSError, ValueError):
        pass
    try:
        DJYY_MARKER.parent.mkdir(parents=True, exist_ok=True)
        DJYY_MARKER.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass
    env = dict(os.environ)
    env.pop("DJYY_KEY", None)
    # 清空 DJYY_PROXY 只是别用 Worker 地址（Worker 出口被上游整体挑战）。
    # 注意：本机 urllib 会走系统代理，2026-10-23 实测比 Python 直连快 5 倍
    # （单请求 4.6s vs 26s）——不要为了「直连」去禁系统代理。
    env["DJYY_PROXY"] = ""
    try:
        p = subprocess.run([str(ROOT / ".venv" / "bin" / "python3"),
                            "scripts/fetch_djyy_ssr.py"],
                           cwd=ROOT, capture_output=True, text=True, env=env,
                           check=False, timeout=600, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        log("djyy: 抓取超时（>600s），保留旧快照")
        return
    out = (p.stdout or p.stderr).strip().splitlines()
    log(f"djyy({age_h:.1f}h): " + (out[-1] if out else f"无输出 rc={p.returncode}"))
    if p.returncode == 0:
        commit_push([DJYY_PATH], "duty: DJYY 快照（本机住宅网络抓取）")


# ------------------------------------------------------------------ 并发锁
#
# 原先这把锁只写在 harness/run_duty.sh 里（2026-10-21 加，mkdir 而不是 flock：
# macOS 自带的 flock 只在部分版本有）。那正是 PROJECT.md「下一步②」记的缺陷
# ——脚本本身无锁，手动跑一轮撞上 :05/:35 就是两个进程并发
# `git pull --rebase --autostash` + 写 njstats_jwt + 写 challenge_state.json，
# 实测撞过一次（08:35:03 与 08:35:10 并存约 7 秒，侥幸没事）。
#
# 现在入口要从壳脚本换成 Qoder 自动化，壳要退役，所以锁必须搬进脚本本身：
# 否则删掉壳脚本 = 连同唯一那份并发保护一起删掉。
# 锁路径必须是**跨入口一致**的固定值。原先壳脚本用 ${TMPDIR:-/tmp}，
# 但 launchd 的 TMPDIR 是每用户私有目录（/var/folders/...），
# 而 launchd / Qoder 自动化 / 手工 shell 三者不一定拿到同一个值——
# 路径不一致 = 三个入口各锁各的，并发保护形同不存在。
# 凭据目录 ~/.football-engine 是所有入口都有的（本文件已经依赖它）。
LOCK = HOME / ".football-engine" / "duty.lock"
LOCK_STALE_SEC = 1800


def acquire_lock() -> bool:
    """拿到锁返回 True；已被占且锁龄未过陈限返回 False（调用方应直接退 0）。"""
    if LOCK.is_dir():
        try:
            age = time.time() - LOCK.stat().st_mtime
        except OSError:
            age = 0.0
        if age < LOCK_STALE_SEC:
            log(f"已有 duty 在跑（锁龄 {age:.0f}s），本轮跳过")
            return False
        log(f"清理陈锁（{age:.0f}s，进程被 kill -9 时不会自己清）")
        try:
            LOCK.rmdir()
        except OSError:
            return False
    try:
        LOCK.mkdir()
    except FileExistsError:
        log("抢锁失败（并发），本轮跳过")
        return False
    return True


def release_lock() -> None:
    try:
        LOCK.rmdir()
    except OSError:
        pass


def _locked_main() -> int:
    """进程级入口：壳脚本退役后，唯一能保证「同时只有一个 duty」的地方。

    `DUTY_SKIP_LOCK=1` 供手工调试用（明确知道自己不会撞车时）。
    测试直接调 main()，不走这里。
    """
    if os.environ.get("DUTY_SKIP_LOCK") == "1":
        return main()
    if not acquire_lock():
        return 0
    try:
        return main()
    finally:
        release_lock()


if __name__ == "__main__":
    # 本机值守是唯一跑在 Windows 上的生产入口：控制台默认 cp936，
    # 日志里的中文/符号（✅ ⚠ 🧹）会 UnicodeEncodeError 打断整轮（本机实测）。
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(_locked_main())
