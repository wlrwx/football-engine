#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盘口实时监控脚本
- 高频抓取赔率变化
- 智能阈值触发推送
- 自动Git提交并推送到GitHub
"""
import json
import hashlib
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOG_FILE = PROJECT_ROOT / "logs" / "realtime_monitor.log"

# 配置
ODDS_CHANGE_THRESHOLD = 0.05  # 赔率变化≥5%触发推送
MIN_PUSH_INTERVAL = 30  # 最小推送间隔(分钟)，避免频繁提交
MAX_PUSHES_PER_DAY = 12  # 每天最多推送次数


def log(msg: str):
    """写日志"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {msg}"
    print(log_line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(log_line + "\n")


def compute_odds_hash(odds_data: dict) -> str:
    """计算赔率数据的哈希值，用于检测变化"""
    # 只取核心赔率字段
    core_fields = {}
    for match_id, match in odds_data.items():
        core_fields[match_id] = {
            "home_odds": match.get("home_odds"),
            "draw_odds": match.get("draw_odds"),
            "away_odds": match.get("away_odds"),
            "handicap": match.get("handicap"),
            "handicap_home": match.get("handicap_home_odds"),
            "handicap_away": match.get("handicap_away_odds"),
            "total_goals_line": match.get("total_goals_line"),
        }
    return hashlib.md5(json.dumps(core_fields, sort_keys=True).encode()).hexdigest()[:16]


def get_last_push_state() -> dict:
    """获取上次推送的状态"""
    state_file = DATA_DIR / ".last_push_state.json"
    if state_file.exists():
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {
        "last_hash": "",
        "last_push_time": "2000-01-01 00:00:00",
        "push_count_today": 0,
        "last_push_date": ""
    }


def save_push_state(hash_val: str):
    """保存推送状态"""
    state_file = DATA_DIR / ".last_push_state.json"
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    
    state = get_last_push_state()
    
    # 如果是新的一天，重置计数器
    if state.get("last_push_date", "") != today:
        state["push_count_today"] = 0
    
    state["last_hash"] = hash_val
    state["last_push_time"] = now.strftime("%Y-%m-%d %H:%M:%S")
    state["last_push_date"] = today
    state["push_count_today"] = state.get("push_count_today", 0) + 1
    
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def should_push(current_hash: str) -> tuple[bool, str]:
    """判断是否应该推送
    
    Returns:
        (是否需要推送, 原因)
    """
    state = get_last_push_state()
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    
    # 检查每日推送上限
    if state.get("last_push_date", "") == today:
        if state.get("push_count_today", 0) >= MAX_PUSHES_PER_DAY:
            return False, f"已达每日推送上限({MAX_PUSHES_PER_DAY}次)"
    
    # 检查最小间隔
    last_push = datetime.strptime(state["last_push_time"], "%Y-%m-%d %H:%M:%S")
    if (now - last_push).total_seconds() < MIN_PUSH_INTERVAL * 60:
        remaining = MIN_PUSH_INTERVAL - int((now - last_push).total_seconds() / 60)
        return False, f"距上次推送不足{MIN_PUSH_INTERVAL}分钟(剩余{remaining}分钟)"
    
    # 检查赔率是否变化
    if current_hash == state["last_hash"]:
        # ✅ 优化：每4小时至少强制同步一次，确保项目保持活跃
        # 解决：赔率没变但一整天不更新的问题
        last_push = datetime.strptime(state["last_push_time"], "%Y-%m-%d %H:%M:%S")
        hours_since_last = (now - last_push).total_seconds() / 3600
        if hours_since_last >= 4:
            return True, f"每4小时强制同步（距上次推送{hours_since_last:.1f}小时）"
        return False, "赔率无变化"
    
    return True, "赔率变化超过阈值"


def run_prediction_pipeline() -> bool:
    """运行预测流水线"""
    log("=" * 60)
    log("开始执行实时预测流水线")
    
    try:
        # Step 1: 运行预测
        log("Step 1/3: 运行预测模型...")
        result = subprocess.run(
            [sys.executable, "-m", "engine.main", "--predict-only"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=180
        )
        if result.returncode != 0:
            log(f"❌ 预测失败: {result.stderr}")
            return False
        log("✓ 预测完成")
        
        # Step 2: 构建页面
        log("Step 2/3: 生成网站页面...")
        result = subprocess.run(
            [sys.executable, "-c", "from engine.build_site import build_site; build_site()"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=120
        )
        if result.returncode != 0:
            log(f"❌ 页面构建失败: {result.stderr}")
            return False
        log("✓ 页面构建完成")
        
        return True
    except Exception as e:
        log(f"❌ 流水线异常: {str(e)}")
        return False


def git_push_with_retry(max_retries: int = 5, retry_delay: int = 30) -> bool:
    """Git推送（带重试 + 自动同步 + 强制跟踪标志清除）"""
    try:
        # 清除所有 skip-worktree 和 assume-unchanged 标志
        # 这是Git隐藏的"忽略本地变更"机制，必须清除才能提交
        subprocess.run(
            ["git", "update-index", "--no-skip-worktree"] + 
            subprocess.run(["git", "ls-files", "data/", "web/"], 
                          capture_output=True, text=True).stdout.strip().split("\n"),
            cwd=PROJECT_ROOT,
            capture_output=True
        )
        subprocess.run(
            ["git", "update-index", "--no-assume-unchanged"] +
            subprocess.run(["git", "ls-files", "data/", "web/"],
                          capture_output=True, text=True).stdout.strip().split("\n"),
            cwd=PROJECT_ROOT,
            capture_output=True
        )
        
        # Add所有变更（强制添加，绕过Git缓存问题）
        subprocess.run(["git", "add", "-f", "data/", "web/", "engine/"], cwd=PROJECT_ROOT)
        
        # 检查是否有变更
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True
        )
        if not result.stdout.strip():
            log("没有文件变更，跳过提交")
            return True
        
        log(f"  变更文件: {result.stdout.count(chr(10)) + 1} 个")
        
        # Commit
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        result = subprocess.run(
            ["git", "commit", "-m", f"🔄 实时更新: {timestamp} 盘口异动"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True
        )
        
        # Push with retries
        for i in range(1, max_retries + 1):
            log(f"  Git推送尝试 {i}/{max_retries}...")
            
            # 配置Git网络优化
            subprocess.run(["git", "config", "http.version", "HTTP/1.1"], cwd=PROJECT_ROOT)
            subprocess.run(["git", "config", "http.postBuffer", "524288000"], cwd=PROJECT_ROOT)
            
            # 先拉取远程变更（自动解决数据冲突，用本地最新数据覆盖）
            log(f"    同步远程变更...")
            subprocess.run(
                ["git", "pull", "--rebase", "-X", "ours"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                timeout=60
            )
            
            result = subprocess.run(
                ["git", "push"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=120
            )
            
            if result.returncode == 0:
                return True
            
            log(f"  推送失败，{retry_delay}秒后重试...")
            import time
            time.sleep(retry_delay)
        
        return False
        
    except Exception as e:
        log(f"Git操作异常: {str(e)}")
        return False


def load_current_odds() -> dict:
    """加载当前赔率数据"""
    today = datetime.now().strftime("%Y-%m-%d")
    today_file = DATA_DIR / "daily" / today / "predictions.json"
    
    if not today_file.exists():
        return {}
    
    try:
        with open(today_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            # 转换为 match_id -> 赔率 的映射
            # 注意：data 可能直接是 list，也可能是 dict 带 predictions 键
            predictions = data if isinstance(data, list) else data.get("predictions", [])
            odds_dict = {}
            for match in predictions:
                if "match_id" in match:
                    odds_dict[match["match_id"]] = match
            return odds_dict
    except Exception as e:
        print(f"加载赔率数据失败: {e}")
        return {}


def main():
    """主函数"""
    LOG_FILE.parent.mkdir(exist_ok=True)
    
    log("")
    log("╔══════════════════════════════════════════════════════╗")
    log("║          football-engine 盘口实时监控                  ║")
    log("╚══════════════════════════════════════════════════════╝")
    
    # Step 1: 先抓取最新赔率
    log("正在抓取最新盘口数据...")
    odds_data = load_current_odds()
    
    if not odds_data:
        log("⚠ 未获取到赔率数据，执行完整预测流水线")
        success = run_prediction_pipeline()
        if not success:
            log("❌ 预测流水线执行失败，退出")
            sys.exit(1)
        # 重新加载数据
        odds_data = load_current_odds()
    
    if not odds_data:
        log("❌ 仍无数据，退出")
        sys.exit(1)
    
    match_count = len(odds_data)
    log(f"✓ 加载 {match_count} 场比赛赔率")
    
    # Step 2: 计算当前赔率哈希
    current_hash = compute_odds_hash(odds_data)
    log(f"当前赔率哈希: {current_hash}")
    
    # Step 3: 判断是否需要推送
    need_push, reason = should_push(current_hash)
    
    if not need_push:
        log(f"⏭ 跳过推送: {reason}")
        sys.exit(0)
    
    log(f"✅ 触发推送: {reason}")
    
    # Step 4: 执行完整预测流水线
    if not run_prediction_pipeline():
        log("❌ 预测流水线执行失败")
        sys.exit(1)
    
    # Step 5: Git 推送
    log("Step 3/3: Git 推送至 GitHub...")
    if git_push_with_retry():
        log("✅ 推送成功！GitHub 项目已实时更新")
        save_push_state(current_hash)
    else:
        log("❌ 所有推送重试均失败")
        sys.exit(1)
    
    log("=" * 60)


if __name__ == "__main__":
    main()
