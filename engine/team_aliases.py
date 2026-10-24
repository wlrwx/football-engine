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

    # ---- 欧冠主流球队简称（2026-10-13 挑战池匹配实测补齐）----
    # 背景：NJStats 开放池用全称，我们赛程用竞彩简称，8 场欧冠里 6 场匹配失败
    # （匹配失败 = 有注不能提）。逐条来自实测对齐，非猜测：
    # `node scripts/njstats_challenge/submit.js --date 2026-10-13` 可复验命中数。
    # 只收「简称→全称」的同队异写；看起来像但不是同一队的一律不收
    # （如 里斯本 vs 里斯本竞技 的歧义已在下方注释标出）。
    "比利亚雷": "比利亚雷亚尔",            # Villarreal（竞彩简称）
    "莱红牛": "莱比锡红牛",                # RB Leipzig（竞彩简称）
    "巴萨": "巴塞罗那",                    # Barcelona
    "马竞": "马德里竞技",                  # Atlético Madrid
    "加拉塔萨": "加拉塔萨雷",              # Galatasaray
    "里斯本": "葡萄牙体育",                # Sporting CP（NJStats 用"葡萄牙体育"）
    "拜仁": "拜仁慕尼黑",                  # Bayern München
    "国米": "国际米兰",                    # Inter
    "曼城": "曼彻斯特城",                  # Man City
    "曼联": "曼彻斯特联",                  # Man Utd
    "热刺": "托特纳姆热刺",                # Tottenham
    "多特": "多特蒙德",                    # Dortmund
    "尤文": "尤文图斯",                    # Juventus
    "国竞": "国际米兰",                    # Inter（竞彩另一写法，实测出现过）
    # 2026-10-14 实测补齐：挑战池匹配仍 3 场 MISS，全在下面这三条。
    # 复验：`node scripts/njstats_challenge/submit.js --date 2026-10-14`。
    # 与上面不同的是——这三条的**分歧横跨三个数据源**，不只是"赛程 vs 池子"：
    #   皇马        竞彩赛程/新浪赛果      → 池子用"皇家马德里"
    #   LASK林茨    竞彩赛程              → 新浪赛果/DJYY 用"林茨"/"林茨竞技"
    #   巴黎圣曼    竞彩赛程              → 新浪赛果/DJYY 用"巴黎圣日耳曼"
    # 队名不合并的代价不只是挑战提不了注：team_ratings.json 的键就是
    # normalize_team 的输出，同一支队会分裂成多个条目，各自的 elo/攻防
    # 只拿到一半的真实赛果。实测（2026-10-14）三支球队都已分裂：
    #   皇马 1792 / 皇家马德里 1932；LASK林茨 1542 / 林茨 1529 / 林茨竞技 1575；
    #   巴黎圣曼 1729 / 巴黎圣日耳曼 1649 / 巴黎圣日尔曼 2005。
    # 规范名的选择依据：以**赛果/回放侧**（新浪 + DJYY + matches.csv 的
    # 主流写法）为准，因为那一侧的名字承载了历史样本与 elo 积累；
    # 赛程侧的简称映射过去。matches.csv 计数：皇家马德里 715 vs 皇马 19、
    # 巴黎圣日尔曼 660 vs 巴黎圣曼 7、LASK林茨 25 vs 林茨 0。
    "皇马": "皇家马德里",                  # Real Madrid（竞彩/新浪赛果用"皇马"）
    "巴黎圣曼": "巴黎圣日耳曼",            # Paris Saint-Germain（竞彩简称）
    # LASK林茨 的情况更绕：三个 key 分别来自三个源（竞彩赛程"LASK林茨"、
    # 新浪赛果"林茨"、DJYY"林茨竞技"），且 loose_normalize("林茨竞技")
    # 已经等于 "林茨"，所以规范名取"林茨"能同时收编 DJYY 那一支，
    # 不会新增第四种写法。
    "LASK林茨": "林茨",                    # LASK Linz（竞彩赛程带俱乐部前缀 LASK）
    # 2026-10-14 补测：上面那三条是"联赛在池内、队名不匹配"，这两条属于
    # **竞彩赛程自己的简称**（池子与新浪赛果都用全称）。性质不同：前三条
    # 不修会让 ratings 分裂成多个 key（elo 各拿一半赛果），这两条不修
    # 只是挑战提不了注——赛果侧本来就写全称， ratings 没分裂。
    #   fixtures"纽喷气机" → 新浪赛果/池子"纽卡斯尔喷气机"
    #   fixtures"布里兰"   → 新浪赛果/池子"武里南联"
    "纽喷气机": "纽卡斯尔喷气机",          # Newcastle Jets（竞彩把 Newcastle 缩成"纽"）
    "布里兰": "武里南联",                  # Buriram United（旧译，现通译武里南联）

    # ---- DJYY 池译名对齐（2026-10-13 实测补齐）----
    # 背景：DJYY 富化匹配用的是「主队_vs_客队」精确键，任一侧译名不同整场就丢失。
    # 实测跨 4 天（10-11~10-14）跑出 39 处未命中，其中 18 处是译名差异
    # （另 21 处是 DJYY 真不覆盖的联赛，如 K1/日职/亚冠精英）。
    # 逐条来自实测配对（`fetch_fixtures` 对 `predictions.json`），非猜测：
    #   {简称} → {DJYY 用名}
    "维拉": "阿斯顿维拉",                  # Aston Villa
    "布伦特": "布伦特福德",                # Brentford
    "纽卡斯尔": "纽卡斯尔联",              # Newcastle United
    "伊普斯": "伊普斯维奇",                # Ipswich Town
    "诺丁汉": "诺丁汉森林",                # Nottingham Forest
    "门兴": "门兴格拉德巴赫",              # Borussia M'gladbach
    "斯特拉斯": "斯特拉斯堡",              # Strasbourg
    "弗洛西诺": "弗洛西诺尼",              # Frosinone
    "凯泽": "凯泽斯劳滕",                  # Kaiserslautern
    "阿尔克马": "阿尔克马尔",              # AZ Alkmaar
    "福图纳": "福图纳锡塔德",              # Fortuna Sittard
    "利雅新月": "利雅得新月",              # Al Hilal
    "京都": "京都不死鸟",                  # Kyoto Sanga
    "町田泽维": "町田泽维亚",              # Machida Zelvia
    "毕尔巴鄂": "毕尔巴鄂竞技",            # Athletic Bilbao
    "拉科": "拉科鲁尼亚",                  # Deportivo La Coruña
    "贝蒂斯": "皇家贝蒂斯",                # Real Betis
    "桑坦德": "桑坦德竞技",                # Racing Santander
    "蔚山现代": "蔚山HD",                  # Ulsan HD（DJYY 用 HD）
    "名古屋鲸": "名古屋鲸八",              # Nagoya Grampus
    "女王巡游": "女王公园巡游者",          # QPR
    # 这几个的特殊性：我们和 DJYY 各用一个"都不标准"的写法，
    # 所以两边的写法都要指向同一个规范名（否则归一化后仍不相等）。
    "米堡": "米德尔斯堡",                  # Middlesbrough（竞彩简称）
    "RB莱比锡": "莱比锡红牛",              # RB Leipzig（DJYY 写法）

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


# 历史遗留的粗暴删字规则（**2026-10-23 收窄**：评审 P2#16）
#
# 原实现对子串做无条件删除：("国际","") 会把"国际米兰"削成"米兰"、
# ("竞技","") 会把"米内罗竞技"削成"米内罗" —— 不同球队可能被归一成同键。
# 现在只保留**整名精确匹配**的白名单映射，不再做子串删除。
_LOOSE_EXACT = {
    "迈阿密国际": "迈阿密",
    "布加勒斯特": "布加勒斯特",
    "里斯本竞技": "葡萄牙体育",
}

# 保留为文档：已停用的危险规则（勿恢复）
_DEPRECATED_LOOSE_RULES = [
    ("国际", ""),
    ("罗姆", ""),
    ("体育会", ""),
    ("体育", ""),
    ("竞技", ""),
]


def loose_normalize(name: str) -> str:
    """宽松归一化：normalize_team 后，再做后缀剥离与白名单映射。

    仅用于主匹配失败后的兜底。2026-10-23 起**不做子串删字**（原做法会把
    不同球队并成同键，例如"国际米兰"→"米兰"）。
    """
    n = normalize_team(name)
    for suf in ("FC", "体育会", "队"):
        if n.endswith(suf) and len(n) > len(suf) + 1:
            n = n[: -len(suf)]
            break
    return _LOOSE_EXACT.get(n, n).strip()


def team_key(home: str, away: str, loose: bool = False) -> str:
    """生成 (主队, 客队) 匹配键。"""
    fn = loose_normalize if loose else normalize_team
    return f"{fn(home)}_vs_{fn(away)}"


# 赛程名 → data/historical/matches.csv（lottery-football 冷启动库）译名映射。
# 仅收录人工确认的同队异译；模糊匹配候选含高危误配（如 新英格兰 Revolution
# ≠ 英格兰国家队），未经确认一律不入表。用法：canon(name) = get(name) or normalize_team(name)
#
# 注意与 TEAM_ALIASES 的关系：TEAM_ALIASES 决定**规范名**，本表只负责把规范名
# 翻到 matches.csv 的队名空间（CSV 的译名自成一套，且无法改动——它是外部
# 冷启动库，改动会让历史行失配）。所以规范名若在本表里（如"巴黎圣日耳曼"），
# 走 CSV 路径时会被再翻一次，这是预期行为，不是重复映射。
CSV_NAME_ALIAS: dict[str, str] = {
    "利雅得新月": "利雅新月",
    "吉尔维森特": "吉维森特",
    "巴黎圣日耳曼": "巴黎圣日尔曼",
    "特拉布宗体育": "特拉布宗",
    "萨尔茨堡红牛": "萨尔茨堡",
    "阿马多拉之星": "阿马多拉",
    "帕福斯FC": "帕福斯",
    # LASK林茨 是唯一一个规范名（林茨）与 CSV 译名（LASK林茨）不一致的：
    # matches.csv 的 25 行欧战/欧冠全部写作 "LASK林茨"，而规范名 "林茨"
    # 在 CSV 里查不到。不补这一条，Elo 回放臂（scripts/elo_backfill_replay.py
    # 的 canon()）会把 LASK 的 25 场历史全部丢到默认 rating 上。
    "林茨": "LASK林茨",
}


def canon_csv_team(name: str) -> str:
    """赛程名 → matches.csv 队名空间（先查专表，再走通用归一化）。

    两步的顺序很重要：先查专表是因为 CSV 的译名自成一套，与规范名
    可能不同（如规范名"林茨"在 CSV 里叫"LASK林茨"）；查不到才退回
    通用归一化。若顺序反过来，"林茨"会先被 normalize_team 变成规范名
    而永远进不了专表的反向映射。

    注意：这里只做**规范名 → CSV 译名**，不做 CSV 译名 → 规范名
    （EloUpdater 内部对 ratings 键自己会调 normalize_team）。
    """
    return CSV_NAME_ALIAS.get(name) or normalize_team(name)
