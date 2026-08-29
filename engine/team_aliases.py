"""队名归一化 - 解决多数据源（预测/新浪/DJYY）中文译名不一致导致的赛果匹配失败。

背景:
  同一支球队在预测、新浪赛果、DJYY 中的中文译名可能不同（简称/全称/异译），
  如 "奥林匹亚" vs "奥林匹亚科斯"、"圣吉联合" vs "圣吉罗斯"、"塞伊奈" vs "塞那乔其"。
  若不归一化，结算时赛果合并不到预测 → 复盘缺场次、命中率被低估、重复条目。

设计:
  - normalize_team(): 主归一化（别名表 + 去空白/连字符），用于匹配键。
  - loose_normalize(): 宽松归一化（再去 FC/队/体育会 后缀 + 历史遗留删字规则），
    仅在主归一化匹配失败时兜底，避免过度合并不同球队。
  - 注意: 以下球队看起来相似但**不是同一队**，严禁加入别名表:
    维京(Viking NO) vs 维京古(Víkingur IS)；哥德堡(IFK) vs 哥德堡盖斯(GAIS)；
    赫尔辛基(HJK) vs 赫尔辛基火花；布拉加(Braga) vs 布卡拉曼加(Bucaramanga)；
    利勒斯特伦(Lillestrøm) vs 利斯特雷(Riestra)；索非亚中央陆军 vs 索非亚中央陆军1948；
    阿根廷(国家) vs 阿根廷独立(俱乐部)。
"""

# 译名变体 → 规范名（同一俱乐部的不同中文写法）
TEAM_ALIASES: dict[str, str] = {
    # ---- 欧战/欧冠资格赛（08-04 翻车重灾区）----
    "奥林匹亚": "奥林匹亚科斯",            # Olympiacos 简称
    "圣吉联合": "圣吉罗斯",                # Union SG
    "圣吉罗斯联合": "圣吉罗斯",
    "博德": "博德闪耀",                    # Bodø/Glimt 简称
    "布拉迪斯": "布拉迪斯拉发",            # Slovan Bratislava 简称
    "斯洛伐克布拉迪斯拉发": "布拉迪斯拉发",
    "布斯巴达": "布拉格斯巴达",            # Sparta Praha 简称
    "格风暴": "格拉茨风暴",                # Sturm Graz 简称
    "斯海杜克": "斯普利特海杜克",          # Hajduk Split 简称
    "布拉格斯巴达": "布拉格斯巴达",

    # ---- 北欧联赛 ----
    "哈尔姆": "哈尔姆斯塔德",              # Halmstad
    "哈尔姆斯": "哈尔姆斯塔德",
    "塞伊奈": "塞那乔其",                  # SJK Seinäjoki
    "韦斯特罗": "韦斯特罗斯",              # Västerås SK
    "瓦斯特拉斯": "韦斯特罗斯",            # Västerås SK (新浪译名 2026-08-03/08-10 实证)
    "佐加顿斯": "佐加顿斯",                # Djurgården (新浪: 尤尔加登 → 佐加顿斯)
    "尤尔加登": "佐加顿斯",
    "克里斯蒂": "克里斯蒂安松",            # Kristiansund
    "利勒斯特": "利勒斯特伦",              # Lillestrøm
    "利勒斯": "利勒斯特伦",
    "布鲁马": "布鲁马波",                  # Brommapojkarna
    "布罗马波伊卡纳": "布鲁马波",          # Brommapojkarna (DJYY 全称 2026-08-10 实证)
    "萨普斯堡": "萨普斯堡08",              # Sarpsborg 08
    "布兰恩": "布兰",                      # Brann 异译
    "维京古": "维京古尔",                  # Víkingur (冰岛)
    "腓特烈": "腓特烈斯塔",                # Fredrikstad
    "厄尔格里特": "奥尔格里特",            # Örgryte IS
    "厄格里特": "奥尔格里特",
    "埃夫斯堡": "埃尔夫斯堡",              # Elfsborg
    "坦佩雷山猫": "坦佩雷山猫",
    "坦山猫": "坦佩雷山猫",
    "TPS图尔": "TPS图尔库",                # TPS Turku
    "国际图尔": "国际图尔库",              # Inter Turku
    "赫尔火花": "赫尔辛基火花",
    "古比斯": "库奥皮奥",                  # KuPS Kuopio 异译
    "查路": "雅罗",                        # FF Jaro 异译 (2026-08-01 实证)
    "纳西奥纳尔": "马德拉国民",            # Nacional Madeira (新浪 2026-08-10 实证)
    "葡国民": "马德拉国民",                # Nacional Madeira 简称 (2026-08-10 实证)

    # ---- 南美 ----
    "米竞技": "米内罗竞技",                # Atlético Mineiro 简称
    "巴竞技": "巴拉纳竞技",                # Athletico Paranaense 简称
    "帕梅拉斯": "帕尔梅拉斯",              # Palmeiras
    "弗鲁米嫩": "弗鲁米嫩塞",              # Fluminense
    "沙佩科": "沙佩科恩斯",                # Chapecoense
    "里莫": "雷莫",                        # Remo (巴西) 异译：预测侧"里莫" vs 新浪侧"雷莫"
    "圣塔菲": "圣塔菲联合",                # Unión Santa Fe
    "维尔斯萨斯菲尔德": "萨斯菲尔德",      # Vélez Sarsfield
    "阿根廷独立": "阿根廷独立",
    "博卡青年": "博卡青年",

    # ---- 美职联 / MLS ----
    "迈国际": "迈阿密国际",                # Inter Miami 简称
    "波特兰伐木": "波特兰伐木者",          # Portland Timbers
    "波特兰": "波特兰伐木者",
    "盐湖城": "皇家盐湖城",                # Real Salt Lake
    "温哥华": "温哥华白浪",                # Vancouver Whitecaps
    "芝加哥": "芝加哥火焰",                # Chicago Fire
    "多伦多": "多伦多FC",                  # Toronto FC
    "夏洛特": "夏洛特FC",                  # Charlotte FC
    "圣迭戈": "圣迭戈FC",                  # San Diego FC
    "圣何塞": "圣何塞地震",                # San Jose Earthquakes
    "哥伦布": "哥伦布机员",                # Columbus Crew
    "达拉斯": "达拉斯FC",                  # FC Dallas
    "圣路易斯": "圣路易斯城",              # St. Louis City SC
    "圣路易城": "圣路易斯城",
    "波兹南": "波兹南莱赫",                # Lech Poznań 简称

    # ---- 欧洲各国联赛 ----
    "安德莱": "安德莱赫特",                # Anderlecht
    "贝西克塔": "贝西克塔斯",              # Beşiktaş
    "特拉维马卡比": "特拉维夫马卡比",      # Maccabi Tel Aviv
    "帕纳辛纳科斯": "帕纳辛奈科斯",        # Panathinaikos
    "索菲亚列夫斯基": "索非亚列夫斯基",    # Levski Sofia
    "索菲亚中央陆军": "索非亚中央陆军",    # CSKA Sofia
    "费伦茨瓦罗斯TC": "费伦茨瓦罗斯",      # Ferencváros
    "叶里温凤凰": "埃里温凤凰",            # Pyunik Yerevan
    "马瑟威尔": "马瑟韦尔",                # Motherwell
    "兹林斯基": "兹林斯基",                # Zrinjski
    "日林斯基": "兹林斯基",
    "斯塔尔南": "斯捷尔南",                # Stjarnan
    "伊尔维斯": "伊尔维斯",                # Ilves
    "埃尔维斯": "伊尔维斯",
    "浦项铁人": "浦项制铁",                # Pohang Steelers
    "帕福斯FC": "帕福斯FC",                # Pafos FC
    "艾斯卡迪斯体育会": "艾斯卡迪斯",      # Escaldes
    "伏伊伏丁那自治省": "伏伊伏丁那",      # Vojvodina
    "阿卢米尼j": "阿卢米尼",               # Aluminij
    "济州SK FC": "济州SK",                 # Jeju SK
    "赫拉德茨-克拉洛韦": "赫拉德茨克拉洛韦",
    "赫拉德茨克拉洛韦": "赫拉德茨克拉洛韦",
    # ---- 2026-08-12 结算去重审计新增（新浪 vs 预测/DB 译名）----
    "FC首尔": "首尔FC",                    # FC Seoul (2026-08-01/08-08 实证)
    "克拉瓦约": "克拉约瓦大学",            # CS U Craiova (2026-08-06 实证)
    "朴次茅斯": "朴茨茅斯",                # Portsmouth (2026-08-08 实证)
    "卡萨皮亚": "卡萨比亚",                # Casa Pia (2026-08-08 实证)
    "圣旺红星": "红星",                    # Red Star FC (2026-08-08 实证，南特 vs 红星)
    "枥木城": "枥木市FC",                  # Tochigi City (2026-08-09 实证)
    "艾华卡": "阿尔维卡",                  # Alverca (2026-08-09 实证)
}

# 需保留原名（防误映射）：这些名字作为映射目标出现在表内，额外加白名单保护不必要，
# 因为 TEAM_ALIASES 只做单向替换，源名不会被二次处理。


def normalize_team(name: str) -> str:
    """规范队名（用于匹配键，不改变落盘原文）。

    规则: 去空白(含全角) → 去连字符 → 别名表替换。
    未在表中的名字原样返回。
    """
    if not name:
        return ""
    n = str(name).strip().replace(" ", "").replace("\u3000", "")
    n = n.replace("-", "").replace("·", "")
    return TEAM_ALIASES.get(n, n)


# 历史遗留的粗暴删字规则（保留在 loose 层，仅兜底）
_LOOSE_RULES = [
    ("迈阿密国际", "迈阿密"),
    ("迈阿密", "迈"),
    ("国际", ""),      # 国际米兰 → 米兰? 慎用：只在 loose 层
    ("罗姆", ""),      # 罗马 → (无)
    ("体育会", ""),
    ("体育", ""),
    ("竞技", ""),
    ("FC", ""),
    ("队", ""),
]


def loose_normalize(name: str) -> str:
    """宽松归一化：normalize_team 后，再去常见后缀/历史删字规则。

    仅用于主匹配失败后的兜底。删除后可能产生歧义（如"米内罗竞技"→"米内罗"），
    因此必须放在别名表之后，且只在精确/主归一化都失败时才调用。
    """
    n = normalize_team(name)
    for suf in ("FC", "体育会", "队"):
        if n.endswith(suf) and len(n) > len(suf) + 1:
            n = n[: -len(suf)]
            break
    for src, dst in _LOOSE_RULES:
        if src in n:
            n = n.replace(src, dst)
    return n.strip()


def team_key(home: str, away: str, loose: bool = False) -> str:
    """生成 (主队, 客队) 匹配键。"""
    fn = loose_normalize if loose else normalize_team
    return f"{fn(home)}_vs_{fn(away)}"


# 赛程名 → data/historical/matches.csv（lottery-football 冷启动库）译名映射。
# 仅收录人工确认的同队异译；模糊匹配候选含高危误配（如 新英格兰 Revolution
# ≠ 英格兰国家队），未经确认一律不入表。用法：canon(name) = get(name) or normalize_team(name)
CSV_NAME_ALIAS: dict[str, str] = {
    "利雅得新月": "利雅新月",
    "吉尔维森特": "吉维森特",
    "巴黎圣日耳曼": "巴黎圣日尔曼",
    "特拉布宗体育": "特拉布宗",
    "萨尔茨堡红牛": "萨尔茨堡",
    "阿马多拉之星": "阿马多拉",
    "帕福斯FC": "帕福斯",
}


def canon_csv_team(name: str) -> str:
    """赛程名 → matches.csv 队名空间（先查专表，再走通用归一化）。"""
    return CSV_NAME_ALIAS.get(name) or normalize_team(name)
