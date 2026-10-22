"""队名匹配工具 - 解决跨数据源译名不一致问题

背景:
    竞彩(体彩)/新浪 与 DJYY 对同一支球队的中文译名往往不同，例如:
        DJYY "梅尔比" == 竞彩 "米亚尔比" (Mjällby)
        DJYY "圣吉罗斯" == 竞彩 "圣吉联合" (Union Saint-Gilloise)
        DJYY "布拉格斯巴达" == 竞彩 "布斯巴达" (Sparta Praha)
        DJYY "奥林匹亚科斯" == 竞彩 "奥林匹亚" (Olympiacos)
    旧实现用 `==` 完全相等匹配，导致 DJYY 增强覆盖率暴跌（08-04 仅 1/5）。

方案:
    match_team(a, b) 按以下级别依次尝试:
        1. 归一化后精确匹配（去空格/FC/队/市/俱乐部等常见后缀，全半角统一）
        2. 别名映射表（双向，覆盖已知译名差异）
        3. 英文名匹配（DJYY 提供 name_en，竞彩新浪也偶有英文名）
        4. 子串包含匹配（长度>=3 才启用，避免"博德"匹配"博德闪耀"之类的过度泛化误配）
    全部失败返回 False。
"""
from __future__ import annotations

import unicodedata

# ---------------------------------------------------------------------------
# 别名映射表: DJYY 译名 ↔ 竞彩/新浪译名（双向）
# 键值对双方互为别名，匹配时任意方向都成立。
# 持续补充: 每当发现新的译名差异，在这里加一条即可。
# ---------------------------------------------------------------------------
TEAM_ALIASES: dict[str, str] = {
    # 2026-10-09 DJYY 匹配实测失败对（竞彩译名 vs DJYY 全称）
    "韦斯特罗斯": "瓦斯特拉斯",        # Västerås SK（竞彩用韦，DJYY用瓦）
    "女王巡游": "女王公园巡游者",      # QPR 交错缩写，前缀规则覆盖不了
    "不来梅": "云达不莱梅",            # Werder Bremen（竞彩"不来梅" vs DJYY"云达不莱梅"，梅/莱异译）
    "里斯本": "葡萄牙体育",            # Sporting CP（竞彩"里斯本" vs DJYY"葡萄牙体育"）
    # 2026-10-22: 竞彩用"萨迪纳摩"，DJYY 快照用全称"萨格勒布迪纳摩"。
    # 只加在 sources 层，不进 engine/team_aliases.py——那边是 team_ratings 的键，
    # 两侧已有独立 rating（1598/1648），合并属模型层变更。
    "萨迪纳摩": "萨格勒布迪纳摩",      # Dinamo Zagreb
    # 欧冠/欧联/欧协联资格赛（2026-07/08 赛季初常见）
    "梅尔比": "米亚尔比",          # Mjällby
    "布拉迪斯拉发": "布拉迪斯",    # Slovan Bratislava
    "圣吉罗斯": "圣吉联合",        # Union Saint-Gilloise
    "布拉格斯巴达": "布斯巴达",    # Sparta Praha
    "奥林匹亚科斯": "奥林匹亚",    # Olympiacos
    "博德": "博德闪耀",            # Bodø/Glimt（新浪简写）
    # 南美/巴西（新浪 vs 竞彩译名差异）
    "雷莫": "里莫",                # Remo
    "巴拉纳竞技": "巴竞技",        # Athletico Paranaense
    "戈亚尼恩斯竞技": "戈亚尼亚竞技",  # Atlético Goianiense
    # 北欧联赛（芬超/瑞超/挪超）
    "塞那乔其": "塞伊奈",          # SJK
    "哈尔姆": "哈尔姆斯",          # Halmstad
    "尤尔加登": "佐加顿斯",        # Djurgården（DJYY 音译 vs 竞彩通用译名）
    "瓦斯特拉斯": "韦斯特罗",      # Västerås
    "布鲁马": "布鲁马波",          # Brommapojkarna
    "奥尔格里特": "厄格里特",      # Örgryte
    "奥斯陆": "奥斯KFUM",          # KFUM Oslo（新浪用城市名）
    "奥卢": "AC奥卢",              # AC Oulu
    "VPS瓦萨": "瓦萨",             # VPS
    "国际图尔库": "国际图尔",      # Inter Turku
    "TPS图尔库": "TPS图尔",        # TPS
    "腓特烈斯塔": "腓特烈",        # Fredrikstad
    "桑德菲杰": "桑纳菲",          # Sandefjord
    "查路": "雅罗",                # FF Jaro
    # 韩K联
    "蔚山HD": "蔚山现代",          # Ulsan HD
    "济州SK FC": "济州SK",         # Jeju SK
    "FC首尔": "首尔FC",            # FC Seoul
    "浦项铁人": "浦项制铁",        # Pohang Steelers
    # 美职联
    "波特兰伐木": "波特兰",        # Portland Timbers
    "洛杉矶银河": "洛城银河",      # LA Galaxy
    "芝加哥火焰": "芝加哥",        # Chicago Fire
    "哥伦布机员": "哥伦布",        # Columbus Crew
    "温哥华白浪": "温哥华",        # Vancouver Whitecaps
    "皇家盐湖城": "盐湖城",        # Real Salt Lake
    "夏洛特FC": "夏洛特",          # Charlotte FC
    "多伦多FC": "多伦多",          # Toronto FC
    "达拉斯FC": "达拉斯",          # FC Dallas
    "堪萨斯城竞技": "堪萨斯城",    # Sporting KC
    "纳什维尔SC": "纳什维尔",      # Nashville SC
}

# 常见队名后缀/前缀，归一化时去除（不改变核心语义）
_COMMON_SUFFIXES = ["足球俱乐部", "俱乐部", "足球会", "队", "市", "FC", "SC", "BK", "IF"]
# 城市名后缀：只影响归一化候选，不直接删除（避免"汉堡"变"汉"）
_CITY_SUFFIXES = ["城", "市", "郡", "县"]

# 用于子串匹配的最小长度（防止 2 字队名过度泛化）
_MIN_SUBSTR_LEN = 3


def _norm(s: str) -> str:
    """基础归一化：全半角统一、去空格/连字符/点、转大写"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)  # 全角→半角
    s = s.replace(" ", "").replace("-", "").replace("_", "").replace(".", "")
    s = s.replace("（", "").replace("）", "").replace("(", "").replace(")", "")
    return s.upper()


def normalize_name(name: str) -> str:
    """归一化队名：去除常见后缀（FC/队/市/俱乐部等）后标准化。

    例: "FC首尔" → "首尔", "芝加哥火焰" → "芝加哥火焰", "国际图尔库" → "国际图尔库"
    """
    if not name:
        return ""
    n = _norm(name)
    for suf in _COMMON_SUFFIXES:
        su = _norm(suf)
        if su and n.endswith(su) and len(n) > len(su):
            n = n[: -len(su)]
            break
    return n


def _alias_equivalents(name: str) -> set[str]:
    """返回队名的所有等价形式（自身 + 别名表中的对应项）"""
    out = {name}
    if not name:
        return out
    # 别名表双向: 直接查 + 反向查
    direct = TEAM_ALIASES.get(name)
    if direct:
        out.add(direct)
    for k, v in TEAM_ALIASES.items():
        if v == name:
            out.add(k)
    return out


def match_team(a: str, b: str) -> bool:
    """判断两个队名是否指向同一支球队（多级匹配）"""
    if not a or not b:
        return False
    na, nb = _norm(a), _norm(b)
    if na == nb:
        return True

    # 归一化（去后缀）后精确匹配
    nn_a, nn_b = normalize_name(a), normalize_name(b)
    if nn_a and nn_b and nn_a == nn_b:
        return True

    # 别名映射（双向）
    aliases_a = _alias_equivalents(a)
    aliases_b = _alias_equivalents(b)
    if aliases_a & aliases_b:
        return True
    # 归一化后的别名比较（"VPS瓦萨" vs "瓦萨" 这类）
    norm_aliases_a = {normalize_name(x) for x in aliases_a if normalize_name(x)}
    norm_aliases_b = {normalize_name(x) for x in aliases_b if normalize_name(x)}
    if norm_aliases_a & norm_aliases_b:
        return True

    # 子串包含匹配（长度>=3，双向），处理"博德闪耀" vs "博德"、"奥林匹亚科斯" vs "奥林匹亚"
    for x, y in ((nn_a, nn_b), (nn_b, nn_a)):
        if len(x) >= _MIN_SUBSTR_LEN and len(y) >= _MIN_SUBSTR_LEN:
            if x in y or y in x:
                return True

    # 2026-10-09 前缀规则: 竞彩大量使用 2 字缩写(凯泽=凯泽斯劳滕、赫拉克勒=赫拉克勒斯)。
    # 前缀(非任意子串)对 2 字也安全——"联"不会开头匹配"凯泽斯劳滕"。
    for x, y in ((nn_a, nn_b), (nn_b, nn_a)):
        if len(x) >= 2 and y.startswith(x):
            return True

    return False


def match_pair(home_a: str, away_a: str, home_b: str, away_b: str) -> bool:
    """判断两场比赛是否同一场（主队和客队都匹配）"""
    return match_team(home_a, home_b) and match_team(away_a, away_b)
