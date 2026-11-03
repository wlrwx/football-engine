"""大小号身份不变量（2026-11-03）。

用真 git 仓库跑：串台的根子在 remote 用什么凭据，不在代码文本里。
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "agent_identity_preflight", ROOT / "scripts" / "agent_identity_preflight.py")
pf = importlib.util.module_from_spec(SPEC)
sys.modules["agent_identity_preflight"] = pf
SPEC.loader.exec_module(pf)

FORK = "https://github.com/dykily/football-engine.git"
OWNER_SSH = "git@github-football-engine:wlrwx/football-engine.git"
AGENT_EMAIL = "351429880+dykily@users.noreply.github.com"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False, timeout=60)


def make_repo(path, remote, email=None):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "remote", "add", "origin", remote)
    if email:
        git(path, "config", "--local", "user.email", email)
    return path


def test_wrong_login_is_violation():
    assert pf.check_agent_login("wlrwx")
    assert pf.check_agent_login("")
    assert pf.check_agent_login("DYKILY") == []


def test_upstream_write_permission_is_violation():
    for key in ("push", "admin", "maintain", "triage"):
        bad = pf.check_upstream_permissions({"pull": True, key: True})
        assert bad and key in bad[0], f"{key}=true 竟然被放行"
    assert pf.check_upstream_permissions({"pull": True, "push": False, "admin": False}) == []


def test_agent_remote_inheriting_owner_ssh_is_violation():
    # 这就是最坏的串台：署名小号、认证大号（admin）
    bad = pf.check_agent_remotes([OWNER_SSH])
    assert bad and "认证成大号" in bad[0]


def test_agent_remote_pointing_at_upstream_is_violation():
    assert pf.check_agent_remotes(["https://github.com/wlrwx/football-engine.git"])
    assert pf.check_agent_remotes([])


def test_agent_remote_own_fork_passes():
    assert pf.check_agent_remotes([FORK]) == []
    assert pf.check_agent_remotes([FORK, FORK]) == []


def test_agent_commit_email_must_not_be_owner_label():
    assert pf.check_agent_commit_email("bot@football.engine")
    assert pf.check_agent_commit_email("")
    assert pf.check_agent_commit_email("someone@example.com")
    assert pf.check_agent_commit_email(AGENT_EMAIL) == []


def test_token_env_overrides_identity():
    assert pf.check_token_env({"GH_TOKEN": "x"})
    assert pf.check_token_env({"GITHUB_TOKEN": "x"})
    assert pf.check_token_env({"PATH": "/usr/bin"}) == []


def test_owner_remote_must_stay_ssh_alias():
    assert pf.check_owner_remote(OWNER_SSH) == []
    assert pf.check_owner_remote("https://github.com/wlrwx/football-engine.git")
    assert pf.check_owner_remote("")


def test_collect_passes_on_properly_separated_repos(tmp_path):
    owner = make_repo(tmp_path / "owner", OWNER_SSH, "bot@football.engine")
    agent = make_repo(tmp_path / "agent", FORK, AGENT_EMAIL)
    assert pf.collect(agent, owner, tmp_path / "ghcfg", offline=True) == []


def test_collect_catches_inherited_ssh_remote(tmp_path):
    owner = make_repo(tmp_path / "owner", OWNER_SSH)
    agent = make_repo(tmp_path / "agent", OWNER_SSH, AGENT_EMAIL)
    bad = pf.collect(agent, owner, tmp_path / "ghcfg", offline=True)
    assert any("认证成大号" in b for b in bad), bad


def test_collect_catches_owner_label_on_agent(tmp_path):
    owner = make_repo(tmp_path / "owner", OWNER_SSH)
    agent = make_repo(tmp_path / "agent", FORK, "bot@football.engine")
    bad = pf.collect(agent, owner, tmp_path / "ghcfg", offline=True)
    assert any("main-watch 基线" in b for b in bad), bad


def test_collect_refuses_missing_agent_dir(tmp_path):
    owner = make_repo(tmp_path / "owner", OWNER_SSH)
    bad = pf.collect(tmp_path / "nope", owner, tmp_path / "ghcfg", offline=True)
    assert any("不要复用" in b for b in bad), bad


def test_cli_exit_codes(tmp_path):
    owner = make_repo(tmp_path / "owner", OWNER_SSH)
    agent = make_repo(tmp_path / "agent", FORK, AGENT_EMAIL)
    common = ["--offline", "--owner-dir", str(owner), "--agent-dir", str(agent),
              "--gh-config-dir", str(tmp_path / "ghcfg")]
    assert pf.main(common) == 0
    git(agent, "remote", "set-url", "origin", OWNER_SSH)
    assert pf.main(common) == 1
