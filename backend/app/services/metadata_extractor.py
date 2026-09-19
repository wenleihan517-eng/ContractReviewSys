# D:/ContractReviewSys-合同审批智能审查项目/backend/app/services/metadata_extractor.py
"""
合同关键元数据抽取器

【模块定位】
    从解析后的合同正文中，确定性抽取业务元数据：
        签约主体（甲乙双方名称）、统一社会信用代码、合同金额、履约期限、
        合同编号、签订日期、联系人等。

【核心设计原则 —— 为什么不用大模型】
    本模块抽取的全部字段都会参与下游的**路由决策与规则判定**：
        · 合同金额 → 决定是否走深度复核通道（≥500万）
        · 合同类型 → 决定是否触发高风险类型复核
        · 履约期限 → 参与规则模板的期限类阈值比对
    依据项目权责红线，这些字段**必须来自确定性规则，不得由大模型产出**。
    大模型的产出只能作为"候选线索"进入辅助链路，不能直接喂给判定逻辑。

    因此本模块全部采用正则与规则实现，特点：
        1. 可复现：同一份正文多次抽取结果完全一致
        2. 可审计：每个字段都附带抽取依据（命中的正则与原文片段）
        3. 可解释：抽取失败时能明确说明"因为没找到 XX 模式"

【抽取失败的处理原则】
    抽取失败一律返回 None，**绝不用默认值或猜测值填充**。
    —— 猜测出来的金额会导致路由与规则判定基于错误数据，后果比"缺失"严重得多。
    缺失的字段由上层决定是否交人工补录。

【金额的中文大写解析】
    合同金额在正文中常同时以阿拉伯数字与中文大写出现（如"人民币壹佰贰拾万元整"），
    大写金额的解析在实务中具有法律意义（大写为准），因此本模块完整实现
    中文数字到大写金额的转换，并优先采用大写金额。
"""

import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

# ======================================================================
# 一、中文数字与金额大写解析
# ======================================================================

# 中文数字字符 → 数值
_CN_DIGITS: Dict[str, int] = {
    "零": 0, "〇": 0,
    "一": 1, "壹": 1,
    "二": 2, "贰": 2, "两": 2, "貳": 2,
    "三": 3, "叁": 3, "參": 3,
    "四": 4, "肆": 4,
    "五": 5, "伍": 5,
    "六": 6, "陆": 6, "陸": 6,
    "七": 7, "柒": 7,
    "八": 8, "捌": 8,
    "九": 9, "玖": 9,
}

# 中文数量级（节内）
_CN_UNITS: Dict[str, int] = {
    "十": 10, "拾": 10,
    "百": 100, "佰": 100,
    "千": 1000, "仟": 1000,
}

# 中文数量级（节）
_CN_SECTIONS: Dict[str, int] = {
    "万": 10_000,
    "亿": 100_000_000,
}

# 中文大写金额中的货币单位与收尾词
_CN_CURRENCY_WORDS: Tuple[str, ...] = (
    "人民币", "RMB", "rmb", "元", "圆", "整", "正", "角", "分",
)


def chinese_to_number(text: str) -> Optional[int]:
    """
    将中文数字串解析为整数

    支持形式：
        "壹佰贰拾万" → 1200000
        "三百五十"   → 350
        "一万二千"   → 12000
        "十"         → 10
        "二零二六"   → 2026（逐位读法，用于年份）

    算法说明：
        采用"节累积"算法。中文数字按 万 / 亿 分节，每节内部按
        十/百/千 累积。遇到 亿 或 万 时，把当前节的累积值乘以对应
        数量级并归入总结果。

        特殊处理"十"开头的情况（如"十二"应解析为 12 而非 10+2=12 恰好
        相同，但"十五"若无处理会算成 10+5=15 也正确；真正的问题在
        "十"单独出现时为 10）。

    参数：
        text: 中文数字串，可含"零"等占位字符

    返回：
        解析出的整数；无法解析时返回 None
    """
    if not text:
        return None

    # 剔除货币单位与收尾词，只保留数字与数量级字符
    cleaned = text
    for word in _CN_CURRENCY_WORDS:
        cleaned = cleaned.replace(word, "")

    # 剔除空白与标点
    cleaned = re.sub(r"[\s,，、.]", "", cleaned)
    if not cleaned:
        return None

    # -------------------- 情形一：纯逐位读法（如年份"二零二六"）--------------------
    # 判定条件：全是数字字符且不含任何数量级字符
    if all(ch in _CN_DIGITS for ch in cleaned):
        # 当长度≥2 且首位为"一/壹"以外的数字时，更可能是逐位读法
        digits = "".join(str(_CN_DIGITS[ch]) for ch in cleaned)
        return int(digits)

    # -------------------- 情形二：混合逐位与节读法（如"二〇二六年"）--------------------
    # 当不含节数量级（万/亿）但含多个连续数字时为逐位读法
    if not any(ch in _CN_SECTIONS for ch in cleaned) and not any(
        ch in _CN_UNITS for ch in cleaned
    ):
        if all(ch in _CN_DIGITS for ch in cleaned):
            return int("".join(str(_CN_DIGITS[ch]) for ch in cleaned))

    # -------------------- 情形三：标准节读法 --------------------
    total = 0        # 已结算的总额
    section = 0      # 当前节的累积值
    current = 0      # 当前位待用数字
    has_digit = False  # 是否解析到有效数字，用于区分"整串无法解析"

    for ch in cleaned:
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
            has_digit = True

        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            # "十"开头时（如"十五"），隐含的系数为 1
            if current == 0:
                current = 1
            section += current * unit
            current = 0
            has_digit = True

        elif ch in _CN_SECTIONS:
            scale = _CN_SECTIONS[ch]
            # 节数归入当前节后乘以节数量级
            section = (section + current) * scale
            total += section
            section = 0
            current = 0
            has_digit = True

        else:
            # 出现无法识别的字符，说明不是规范的中文数字串
            return None

    if not has_digit:
        return None

    return total + section + current


def parse_chinese_amount(text: str) -> Optional[float]:
    """
    解析中文大写金额为浮点数（单位：元）

    支持形式：
        "人民币壹佰贰拾万元整"     → 1200000.0
        "叁佰万元"                 → 3000000.0
        "伍拾万元整"               → 500000.0

    实现要点：
        1. 分离"元"与"角分"部分：元部分走 chinese_to_number，
           角分部分按 10%/1% 折算成小数
        2. 若含"角"或"分"，说明有小数部分，需精确到分

    参数：
        text: 中文大写金额文本

    返回：
        金额（元）；无法解析时返回 None
    """
    if not text:
        return None

    raw = text.strip()

    # 剔除货币前缀与常见修饰
    for prefix in ("人民币", "RMB", "rmb", "币种：", "金额："):
        raw = raw.replace(prefix, "")
    raw = raw.strip()

    # -------------------- 分离角、分 --------------------
    jiao = 0  # 角
    fen = 0   # 分

    jiao_match = re.search(r"([零〇一壹二贰两貳三叁參四肆五伍六陆陸七柒八捌九玖])角", raw)
    if jiao_match:
        jiao = _CN_DIGITS.get(jiao_match.group(1), 0)

    fen_match = re.search(r"([零〇一壹二贰两貳三叁參四肆五伍六陆陸七柒八捌九玖])分", raw)
    if fen_match:
        fen = _CN_DIGITS.get(fen_match.group(1), 0)

    # -------------------- 解析元部分 --------------------
    yuan_part = raw
    # 截断到"元/圆"为止；不含"元"时按整串处理
    for stop_word in ("元", "圆"):
        if stop_word in yuan_part:
            yuan_part = yuan_part.split(stop_word)[0]
            break

    # 移除角分描述，避免干扰元部分解析
    yuan_part = re.sub(r"[零〇一壹二贰两貳三叁參四肆五伍六陆陸七柒八捌九玖][角分]", "", yuan_part)

    yuan = chinese_to_number(yuan_part)
    if yuan is None:
        # 元部分解析失败但角分有效时，仍可返回小数金额
        if jiao or fen:
            return round(jiao * 0.1 + fen * 0.01, 2)
        return None

    return round(yuan + jiao * 0.1 + fen * 0.01, 2)


def parse_amount_value(text: str) -> Optional[float]:
    """
    解析含货币单位的阿拉伯数字金额

    支持形式：
        "人民币 1,200,000.00 元"  → 1200000.0
        "￥500000元"              → 500000.0
        "金额为 1234.56万元"      → 12345600.0（自动折算"万"）
        "300万元"                 → 3000000.0

    参数：
        text: 含金额与单位的文本片段

    返回：
        金额（元）；无法解析时返回 None
    """
    if not text:
        return None

    # 提取数值（含千分位与小数）
    num_match = re.search(r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)", text)
    if not num_match:
        return None

    try:
        value = float(num_match.group(1).replace(",", ""))
    except ValueError:
        return None

    # 处理"万/亿"量词折算
    tail = text[num_match.end():]
    if re.search(r"亿", tail):
        value *= 100_000_000
    elif re.search(r"万", tail):
        value *= 10_000

    return round(value, 2)


# ======================================================================
# 二、统一社会信用代码校验
# ======================================================================

# 信用代码字符集（不含 I、O、S、V、Z，避免与数字混淆）
_CREDIT_CODE_CHARS = "0123456789ABCDEFGHJKLMNPQRTUWXY"
# GB 32100-2015 规定的加权因子
_CREDIT_CODE_WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)
# 校验码取值表
_CREDIT_CODE_CHECK = "0123456789ABCDEFGHJKLMNPQRTUWXY"


def validate_credit_code(code: str) -> bool:
    """
    校验统一社会信用代码的合法性

    说明：
        依据 GB 32100-2015 实现校验位算法：
            1. 前 17 位字符各自映射为数值，乘以对应加权因子后求和
            2. 总和对 31 取模，得到校验位索引
            3. 与第 18 位比对

        为什么要做校验：正文中的 18 位串不一定都是信用代码（可能是
        合同编号、身份证号）。只有校验位通过才认定为信用代码，
        避免把无关数字串误认为主体标识喂给下游。

    参数：
        code: 待校验的 18 位代码

    返回：
        合法返回 True，否则 False
    """
    if not code or len(code) != 18:
        return False

    code = code.upper()

    # 字符集合法性
    for ch in code:
        if ch not in _CREDIT_CODE_CHARS:
            return False

    # 加权求和
    total = 0
    for index in range(17):
        char_value = _CREDIT_CODE_CHARS.index(code[index])
        total += char_value * _CREDIT_CODE_WEIGHTS[index]

    # 取模得到校验位
    check_index = 31 - (total % 31)
    if check_index == 31:
        check_index = 0

    return _CREDIT_CODE_CHECK[check_index] == code[17]


# ======================================================================
# 三、签约主体抽取
# ======================================================================

# 甲方标识词（按优先级排列，越靠前越可信）
_PARTY_A_MARKERS: Tuple[str, ...] = (
    "甲方", "出卖方", "卖方", "发包方", "发包人", "委托方", "委托人",
    "采购方", "采购人", "借款方", "借款人", "出租方", "出租人",
)
# 乙方标识词
_PARTY_B_MARKERS: Tuple[str, ...] = (
    "乙方", "买受方", "买方", "承包方", "承包人", "受托方", "受托人",
    "供应方", "供应商", "贷款方", "贷款人", "承租方", "承租人",
)

# 主体名称的常见结尾词（用于截断名称）
_ENTITY_SUFFIXES: Tuple[str, ...] = (
    "有限公司", "股份有限公司", "有限责任公司", "集团有限公司",
    "公司", "企业", "工厂", "中心", "研究院", "研究所", "学院", "大学",
    "医院", "银行", "事务所", "合作社", "委员会", "管理局", "事务所",
    "分公司", "办事处", "商店", "商行", "门市部",
)


def _extract_name_after_marker(
    text: str,
    markers: Tuple[str, ...],
) -> Tuple[Optional[str], Optional[str]]:
    """
    从正文中抽取标识词之后的主体名称

    匹配形式（按优先级）：
        1. "甲方：某某有限公司" / "甲方: 某某有限公司"
        2. "甲方（全称）：某某有限公司" / "甲方(全称)：某某有限公司"
        3. "甲方：某某有限公司（以下简称"甲方"）"
        4. "甲方 某某有限公司"（无冒号，需紧跟主体结尾词）

    参数：
        text: 合同正文
        markers: 主体标识词元组

    返回：
        (主体名称, 命中的原文片段)；未找到时返回 (None, None)
    """
    for marker in markers:
        # -------------------- 模式一/二：带冒号（可含括号说明）--------------------
        # 中文冒号与英文冒号均支持；括号内允许最多 8 个字符的说明
        pattern = (
            rf"{marker}\s*[（(][^）)]{{0,8}}[）)]?\s*[:：]\s*"
            rf"([^\s，,。；;：:]{{2,60}})"
        )
        match = re.search(pattern, text)
        if match:
            name = _clean_entity_name(match.group(1))
            if name:
                return name, match.group(0)

        # 简化形式：标识词直接接冒号
        pattern = rf"{marker}\s*[:：]\s*([^\s，,。；;：:]{{2,60}})"
        match = re.search(pattern, text)
        if match:
            name = _clean_entity_name(match.group(1))
            if name:
                return name, match.group(0)

        # -------------------- 模式三：无冒号，但后接主体结尾词 --------------------
        suffix_alt = "|".join(_ENTITY_SUFFIXES)
        pattern = rf"{marker}\s*[（(]?[^）)]{{0,8}}[）)]?\s*([\u4e00-\u9fff]{{2,40}}(?:{suffix_alt}))"
        match = re.search(pattern, text)
        if match:
            name = _clean_entity_name(match.group(1))
            if name:
                return name, match.group(0)

    return None, None


def _clean_entity_name(raw: str) -> Optional[str]:
    """
    清洗抽取到的主体名称

    处理：
        1. 截断到主体结尾词（去掉名称后粘连的正文）
        2. 剔除括号注释与特殊字符
        3. 过滤明显的噪声（长度过短、含"以下简称"等）

    参数：
        raw: 原始抽取文本

    返回：
        清洗后的名称；无有效名称时返回 None
    """
    if not raw:
        return None

    name = raw.strip()

    # 截断到第一个主体结尾词（含该词）
    for suffix in _ENTITY_SUFFIXES:
        index = name.find(suffix)
        if index != -1:
            name = name[: index + len(suffix)]
            break

    # 剔除括号及其内容
    name = re.sub(r"[（(][^）)]*[）)]", "", name)
    # 剔除不能出现在企业名称中的字符
    name = re.sub(r"[，,。；;：:、\"'“”‘’\s]", "", name)

    # 过滤噪声：长度不足 4 个汉字，或含明显非名称词汇
    if len(name) < 4:
        return None
    if any(word in name for word in ("以下简称", "以下称", "全称", "名称", "盖章", "签字")):
        return None

    return name


def extract_parties(text: str) -> Dict[str, Any]:
    """
    抽取合同签约双方信息

    说明：
        返回的 name 与 evidence 均为 None 表示抽取失败 —— 不使用任何默认值，
        由上层决定是否交人工补录。

    参数：
        text: 合同正文

    返回：
        字典：
            {
                "party_a_name": Optional[str],
                "party_b_name": Optional[str],
                "party_a_evidence": Optional[str],
                "party_b_evidence": Optional[str],
            }
    """
    result: Dict[str, Any] = {
        "party_a_name": None,
        "party_b_name": None,
        "party_a_evidence": None,
        "party_b_evidence": None,
    }

    if not text:
        return result

    name_a, evidence_a = _extract_name_after_marker(text, _PARTY_A_MARKERS)
    name_b, evidence_b = _extract_name_after_marker(text, _PARTY_B_MARKERS)

    result["party_a_name"] = name_a
    result["party_a_evidence"] = evidence_a
    result["party_b_name"] = name_b
    result["party_b_evidence"] = evidence_b

    return result


def extract_credit_codes(text: str) -> List[str]:
    """
    抽取正文中全部合法的统一社会信用代码

    说明：
        正则先宽匹配 18 位候选串，再用校验位算法过滤，
        确保返回的都是通过 GB 32100-2015 校验的合法代码。

    参数：
        text: 合同正文

    返回：
        去重后的合法信用代码列表（保持出现顺序）
    """
    if not text:
        return []

    # 宽匹配：18 位，首字符为数字或大写字母
    candidates = re.findall(r"[0-9A-HJ-NPQRTUWXY]{18}", text.upper())

    valid: List[str] = []
    for code in candidates:
        if validate_credit_code(code) and code not in valid:
            valid.append(code)

    return valid


# ======================================================================
# 四、金额抽取
# ======================================================================

# 金额关键上下文词（用于定位金额所在语句）
_AMOUNT_CONTEXT_WORDS: Tuple[str, ...] = (
    "合同总金额", "合同金额", "合同总额", "总价款", "价款", "总金额",
    "金额合计", "合计金额", "合同价", "价格", "总价", "租金总额",
    "借款金额", "担保金额", "人民币",
)


def extract_contract_amount(text: str) -> Dict[str, Any]:
    """
    抽取合同金额

    抽取优先级（重要）：
        1. **中文大写金额** —— 法律实务中以大写为准，优先采信
        2. 关键上下文词后的阿拉伯数字金额
        3. 全文范围最大的金额数值（兜底）

    为什么优先取大写：合同正文常同时写"人民币壹佰万元整（￥1,000,000.00）"，
    二者不一致时法律上以大写为准。若优先取阿拉伯数字，在
    "金额（大写）：壹佰万元整  小写：￥100,000.00"这类矛盾表述中
    会得到错误结果。

    参数：
        text: 合同正文

    返回：
        字典：
            {
                "amount": Optional[float],          # 金额（元）
                "amount_source": Optional[str],     # 取值来源：chinese_uppercase / arabic_context / arabic_fallback
                "amount_evidence": Optional[str],   # 命中的原文片段
            }
    """
    result: Dict[str, Any] = {
        "amount": None,
        "amount_source": None,
        "amount_evidence": None,
    }

    if not text:
        return result

    # -------------------- 优先级一：中文大写金额 --------------------
    # 匹配"壹佰万元整"这类连续大写数字串（至少含一个数量级词，避免误匹配"一般"等词）
    uppercase_pattern = (
        r"([零〇一壹二贰两貳三叁參四肆五伍六陆陸七柒八捌九玖十拾百佰千仟万亿]{3,}"
        r"(?:元|圆)(?:[零〇一壹二贰三叁四肆五伍六陆七柒八捌九玖][角分])?(?:整|正)?)"
    )
    for match in re.finditer(uppercase_pattern, text):
        raw = match.group(1)
        # 必须至少含一个数量级词，否则形如"一二三元"属噪声
        if not any(ch in raw for ch in "十拾百佰千仟万亿"):
            continue
        value = parse_chinese_amount(raw)
        if value is not None and value > 0:
            result["amount"] = value
            result["amount_source"] = "chinese_uppercase"
            result["amount_evidence"] = _extract_context_window(text, match.start(), match.end())
            return result

    # -------------------- 优先级二：关键上下文词后的阿拉伯数字 --------------------
    for word in _AMOUNT_CONTEXT_WORDS:
        # 在关键词后 40 字符窗口内找金额
        for match in re.finditer(re.escape(word), text):
            window = text[match.start(): match.start() + 60]
            value = parse_amount_value(window)
            if value is not None and value > 0:
                result["amount"] = value
                result["amount_source"] = "arabic_context"
                result["amount_evidence"] = _extract_context_window(
                    text, match.start(), min(match.start() + 60, len(text))
                )
                return result

    # -------------------- 优先级三：全文最大金额兜底 --------------------
    # 该分支仅在完全没有上下文线索时启用，置信度最低
    all_values: List[Tuple[float, int, int]] = []
    for match in re.finditer(r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?:万|亿)?\s*(?:元|圆)", text):
        value = parse_amount_value(match.group(0))
        if value is not None and value > 0:
            all_values.append((value, match.start(), match.end()))

    if all_values:
        # 取最大值：合同金额通常是文中出现的最大金额
        best_value, best_start, best_end = max(all_values, key=lambda item: item[0])
        result["amount"] = best_value
        result["amount_source"] = "arabic_fallback"
        result["amount_evidence"] = _extract_context_window(text, best_start, best_end)

    return result


def _extract_context_window(text: str, start: int, end: int, window: int = 40) -> str:
    """
    截取命中位置周围的上下文片段，作为抽取依据留痕

    参数：
        text: 完整文本
        start: 命中起始位置
        end: 命中结束位置
        window: 前后各扩展的字符数

    返回：
        上下文片段（首尾空白已清理）
    """
    left = max(0, start - window)
    right = min(len(text), end + window)
    return text[left:right].strip().replace("\n", " ")


# ======================================================================
# 五、日期与履约期限抽取
# ======================================================================

# 日期正则的核心片段（不含命名组，便于安全拼接）
# 说明：命名组在同一正则中只能出现一次，因此拼接复用必须用无名组。
#       单独匹配日期时用下方 _DATE_PATTERN（含命名组，便于按名取值）。
_DATE_CORE = r"(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*日?"

# 日期正则：支持 2026年1月1日 / 2026-01-01 / 2026/1/1 / 2026.1.1
_DATE_PATTERN = re.compile(
    r"(?P<year>\d{4})\s*[年\-/.]\s*(?P<month>\d{1,2})\s*[月\-/.]\s*(?P<day>\d{1,2})\s*日?"
)

# 起止日期区间正则（用无名组拼接，共 6 个捕获组：起 3 个 + 止 3 个）
_DATE_RANGE_PATTERN = re.compile(
    r"自?\s*" + _DATE_CORE + r"\s*[起开]?\s*(?:至|到|—|－|-|~|～)\s*"
    + _DATE_CORE + r"\s*[止末]?"
)

# 履约期限关键词
_DURATION_CONTEXT_WORDS: Tuple[str, ...] = (
    "合同工期", "履约期限", "合同期限", "服务期限", "合作期限",
    "有效期", "租赁期限", "工期", "履行期限",
)


def _parse_date(year: str, month: str, day: str) -> Optional[date]:
    """
    将年月日字符串构造为 date 对象

    参数：
        year: 四位年份
        month: 月份（1-2 位）
        day: 日（1-2 位）

    返回：
        date 对象；非法日期返回 None
    """
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        # 如 2 月 30 日等非法日期
        return None


def extract_dates(text: str) -> List[Tuple[date, int, int]]:
    """
    抽取正文中全部合法日期及其位置

    参数：
        text: 合同正文

    返回：
        [(日期, 起始位置, 结束位置), ...] 按出现顺序
    """
    if not text:
        return []

    found: List[Tuple[date, int, int]] = []
    for match in _DATE_PATTERN.finditer(text):
        parsed = _parse_date(match.group("year"), match.group("month"), match.group("day"))
        if parsed is not None:
            found.append((parsed, match.start(), match.end()))

    return found


def extract_performance_period(text: str) -> Dict[str, Any]:
    """
    抽取履约期限（起止日期与期限天数）

    抽取策略（按优先级）：
        1. 在"履约期限/合同期限"等关键词附近查找两个日期 → 起止日期
        2. 查找"自 X 年 X 月 X 日起至 X 年 X 月 X 日止"结构
        3. 查找"N 个月 / N 年 / N 天"形式的期限描述
        4. 若全文有多个日期，取最早与最晚作为兜底区间

    参数：
        text: 合同正文

    返回：
        字典：
            {
                "period_start": Optional[str],   # ISO 格式起始日期
                "period_end": Optional[str],     # ISO 格式结束日期
                "period_days": Optional[int],    # 期限天数（含头不含尾的差值+1）
                "period_evidence": Optional[str],
                "period_source": Optional[str],  # 取值来源标识
            }
    """
    result: Dict[str, Any] = {
        "period_start": None,
        "period_end": None,
        "period_days": None,
        "period_evidence": None,
        "period_source": None,
    }

    if not text:
        return result

    # -------------------- 策略一：起止结构 --------------------
    # 形如"自2026年1月1日起至2026年12月31日止"
    range_match = _DATE_RANGE_PATTERN.search(text)
    if range_match:
        groups = range_match.groups()
        # 无名组共 6 个：前 3 个为起始日期，后 3 个为结束日期
        start_date = _parse_date(groups[0], groups[1], groups[2])
        end_date = _parse_date(groups[3], groups[4], groups[5])
        if start_date and end_date and end_date >= start_date:
            result["period_start"] = start_date.isoformat()
            result["period_end"] = end_date.isoformat()
            result["period_days"] = (end_date - start_date).days + 1
            result["period_source"] = "explicit_range"
            result["period_evidence"] = _extract_context_window(
                text, range_match.start(), range_match.end(), window=10
            )
            return result

    # -------------------- 策略二：关键词附近的两个日期 --------------------
    for word in _DURATION_CONTEXT_WORDS:
        for match in re.finditer(re.escape(word), text):
            window_text = text[match.start(): match.start() + 200]
            dates_in_window = extract_dates(window_text)
            if len(dates_in_window) >= 2:
                start_date = dates_in_window[0][0]
                end_date = dates_in_window[1][0]
                if end_date >= start_date:
                    result["period_start"] = start_date.isoformat()
                    result["period_end"] = end_date.isoformat()
                    result["period_days"] = (end_date - start_date).days + 1
                    result["period_source"] = "keyword_window"
                    result["period_evidence"] = _extract_context_window(
                        text, match.start(), min(match.start() + 200, len(text)), window=0
                    )
                    return result

    # -------------------- 策略三：数量 + 单位形式的期限 --------------------
    # 形如"合同期限为 12 个月"、"工期 90 天"、"履约期限 2 年"
    quantity_pattern = re.compile(
        r"(\d+)\s*(个月|月|年|天|日|周)"
    )
    for word in _DURATION_CONTEXT_WORDS:
        for match in re.finditer(re.escape(word), text):
            window = text[match.start(): match.start() + 80]
            quantity_match = quantity_pattern.search(window)
            if quantity_match:
                amount = int(quantity_match.group(1))
                unit = quantity_match.group(2)

                if unit in ("年",):
                    days = amount * 365
                elif unit in ("个月", "月"):
                    days = amount * 30
                elif unit in ("周",):
                    days = amount * 7
                else:
                    days = amount

                result["period_days"] = days
                result["period_source"] = "quantity_unit"
                result["period_evidence"] = _extract_context_window(
                    text, match.start(), match.start() + 80, window=0
                )
                return result

    # -------------------- 策略四：全文日期兜底 --------------------
    all_dates = extract_dates(text)
    if len(all_dates) >= 2:
        sorted_dates = sorted(all_dates, key=lambda item: item[0])
        start_date = sorted_dates[0][0]
        end_date = sorted_dates[-1][0]
        if end_date > start_date:
            result["period_start"] = start_date.isoformat()
            result["period_end"] = end_date.isoformat()
            result["period_days"] = (end_date - start_date).days + 1
            result["period_source"] = "fulltext_fallback"

    return result


# ======================================================================
# 六、合同编号与标题抽取
# ======================================================================

# 合同编号关键词
_CONTRACT_NO_WORDS: Tuple[str, ...] = (
    "合同编号", "合同编码", "编号", "协议编号", "协议号",
)


def extract_contract_no(text: str) -> Dict[str, Any]:
    """
    抽取合同编号

    匹配形式：
        "合同编号：HT-2026-001"
        "合同编号 HT2026001"
        "编号：采购2026第001号"

    参数：
        text: 合同正文（建议只传入前若干行，编号通常出现在抬头）

    返回：
        字典 {"contract_no": Optional[str], "contract_no_evidence": Optional[str]}
    """
    result: Dict[str, Any] = {
        "contract_no": None,
        "contract_no_evidence": None,
    }

    if not text:
        return result

    for word in _CONTRACT_NO_WORDS:
        # 编号由字母、数字、连字符、汉字组成
        pattern = rf"{word}\s*[:：]?\s*([A-Za-z0-9\u4e00-\u9fff\-/]{{3,40}})"
        match = re.search(pattern, text)
        if match:
            raw = match.group(1).strip()
            # 剔除尾部可能粘连的正文标点
            raw = raw.rstrip("，,。；;：:、")
            # 过滤纯汉字噪声（如"编号：详见附件"）
            if raw and not raw.startswith(("详", "见", "另", "无")):
                result["contract_no"] = raw
                result["contract_no_evidence"] = match.group(0)
                return result

    return result


def extract_contract_title(text: str) -> Optional[str]:
    """
    抽取合同标题

    策略：
        合同标题通常位于正文最前几行的独立行，且以"合同/协议/书"结尾。

    参数：
        text: 合同正文

    返回：
        标题字符串；未找到时返回 None
    """
    if not text:
        return None

    # 只在前 20 行内查找，标题不会出现在正文深处
    lines = [line.strip() for line in text.split("\n")[:20]]

    for line in lines:
        if not line:
            continue
        # 长度合理且以合同类词结尾
        if 4 <= len(line) <= 60 and re.search(r"(合同|协议|协议书|契约|订单|意向书)\s*$", line):
            return line

    return None


# ======================================================================
# 七、签订日期抽取
# ======================================================================

_SIGN_DATE_WORDS: Tuple[str, ...] = (
    "签订日期", "签署日期", "签订时间", "签署时间", "签约日期",
)


def extract_sign_date(text: str) -> Dict[str, Any]:
    """
    抽取签订日期

    说明：
        优先在"签订日期"等关键词附近查找；找不到时，
        若正文末尾存在日期，取其作为签订日期的兜底（落款习惯）。

    参数：
        text: 合同正文

    返回：
        字典 {"sign_date": Optional[str], "sign_date_evidence": Optional[str]}
    """
    result: Dict[str, Any] = {
        "sign_date": None,
        "sign_date_evidence": None,
    }

    if not text:
        return result

    # -------------------- 关键词附近查找 --------------------
    for word in _SIGN_DATE_WORDS:
        for match in re.finditer(re.escape(word), text):
            window = text[match.start(): match.start() + 60]
            dates = extract_dates(window)
            if dates:
                result["sign_date"] = dates[0][0].isoformat()
                result["sign_date_evidence"] = _extract_context_window(
                    text, match.start(), match.start() + 60, window=0
                )
                return result

    # -------------------- 兜底：正文末尾的日期 --------------------
    # 落款日期位于合同末尾，取最后两个日期中的后者
    tail = text[-500:] if len(text) > 500 else text
    tail_dates = extract_dates(tail)
    if tail_dates:
        result["sign_date"] = tail_dates[-1][0].isoformat()
        result["sign_date_evidence"] = _extract_context_window(
            text, len(text) - len(tail) + tail_dates[-1][1],
            len(text) - len(tail) + tail_dates[-1][2],
        )

    return result


# ======================================================================
# 八、统一抽取入口
# ======================================================================

def extract_all_metadata(
    text: str,
    contract_type: str = "other",
) -> Dict[str, Any]:
    """
    抽取合同全部关键元数据（统一入口）

    说明：
        本函数是解析服务调用的唯一入口。所有字段均为"抽取失败即 None"，
        绝不填充默认值或猜测值。调用方应通过 `extraction_success_rate`
        判断抽取质量，低质量时应触发人工补录流程。

    参数：
        text: 合同正文（应为解析后的完整文本）
        contract_type: 已知的合同类型，仅用于结果标注（不参与抽取逻辑）

    返回：
        字典，含：
            contract_title / contract_no / amount / currency /
            party_a_name / party_b_name / party_a_credit_code /
            party_b_credit_code / credit_codes / sign_date /
            period_start / period_end / period_days
        以及各字段的 evidence 与 source 说明，与 extraction_success_rate
    """
    if not text:
        text = ""

    # -------------------- 基础字段 --------------------
    parties = extract_parties(text)
    amount = extract_contract_amount(text)
    period = extract_performance_period(text)
    contract_no = extract_contract_no(text)
    title = extract_contract_title(text)
    sign_date = extract_sign_date(text)

    # -------------------- 信用代码与主体绑定 --------------------
    credit_codes = extract_credit_codes(text)
    party_a_code: Optional[str] = None
    party_b_code: Optional[str] = None

    # 策略：在主体名称附近查找信用代码，实现主体与代码的绑定。
    # 甲方先绑定，乙方绑定前排除甲方已占用的代码，
    # 避免"乙方未写代码时误绑甲方代码"。
    if parties["party_a_name"]:
        party_a_code = _find_code_near_name(text, parties["party_a_name"])

    if parties["party_b_name"]:
        party_b_code = _find_code_near_name(
            text,
            parties["party_b_name"],
            exclude=[party_a_code] if party_a_code else None,
        )

    # -------------------- 币种判定 --------------------
    currency = "CNY"
    if re.search(r"(美元|USD|\$)\s*[\d壹贰叁肆伍陆柒捌玖]", text):
        currency = "USD"
    elif re.search(r"(欧元|EUR|€)", text):
        currency = "EUR"
    elif re.search(r"(港币|港元|HKD)", text):
        currency = "HKD"

    # -------------------- 组装结果 --------------------
    metadata: Dict[str, Any] = {
        # 合同标识
        "contract_title": title,
        "contract_no": contract_no["contract_no"],
        "contract_no_evidence": contract_no["contract_no_evidence"],
        "contract_type": contract_type,

        # 金额与币种
        "amount": amount["amount"],
        "amount_source": amount["amount_source"],
        "amount_evidence": amount["amount_evidence"],
        "currency": currency,

        # 签约主体
        "party_a_name": parties["party_a_name"],
        "party_a_evidence": parties["party_a_evidence"],
        "party_b_name": parties["party_b_name"],
        "party_b_evidence": parties["party_b_evidence"],

        # 信用代码
        "party_a_credit_code": party_a_code,
        "party_b_credit_code": party_b_code,
        "credit_codes": credit_codes,

        # 日期与期限
        "sign_date": sign_date["sign_date"],
        "sign_date_evidence": sign_date["sign_date_evidence"],
        "period_start": period["period_start"],
        "period_end": period["period_end"],
        "period_days": period["period_days"],
        "period_source": period["period_source"],
        "period_evidence": period["period_evidence"],
    }

    # -------------------- 抽取质量统计 --------------------
    # 参与统计的核心字段：这些字段缺失会直接影响路由决策与规则判定
    core_fields = (
        "contract_no", "amount", "party_a_name", "party_b_name",
        "sign_date", "period_start",
    )
    hit_count = sum(1 for field in core_fields if metadata.get(field) is not None)

    metadata["extraction_success_rate"] = round(hit_count / len(core_fields), 4)
    metadata["extraction_missing_fields"] = [
        field for field in core_fields if metadata.get(field) is None
    ]

    return metadata


def _find_code_near_name(
    text: str,
    name: str,
    window: int = 200,
    exclude: Optional[List[str]] = None,
) -> Optional[str]:
    """
    在指定主体名称附近查找统一社会信用代码

    【为什么按距离取最近，而不是取窗口内第一个】
        合同抬头常形如：
            甲方（采购方）：甲公司
            统一社会信用代码：<A 的代码>
            乙方（供应方）：乙公司
            统一社会信用代码：<B 的代码>
        以名称位置为中心的前后对称窗口会同时覆盖到"上一个主体的代码"。
        若简单取窗口内第一个出现的代码，乙方会被错误绑定到甲方的代码。
        因此必须按"与名称的字符距离"排序，取最近的一个。

    【为什么需要 exclude 参数】
        同一段正文中，甲方代码可能同时是乙方的最近代码（当乙方未写代码时）。
        调用方传入已占用的代码集合，避免一个代码被绑定给多个主体。

    参数：
        text: 合同正文
        name: 主体名称
        window: 前后搜索窗口字符数
        exclude: 需要排除的代码列表（通常为已分配给其他主体的代码）

    返回：
        与名称距离最近的合法信用代码；未找到返回 None
    """
    if not text or not name:
        return None

    position = text.find(name)
    if position == -1:
        return None

    left = max(0, position - window)
    right = min(len(text), position + len(name) + window)
    segment = text[left:right]

    # 定位段内全部合法代码及其绝对位置，按与名称的距离升序排列
    candidates: List[Tuple[int, str]] = []
    for match in re.finditer(r"[0-9A-HJ-NPQRTUWXY]{18}", segment.upper()):
        code = match.group(0)
        if not validate_credit_code(code):
            continue
        if exclude and code in exclude:
            continue
        # 段内绝对位置 → 全文绝对位置
        absolute_start = left + match.start()
        # 计算代码与名称区间的字符距离：名称结束位置之后为正，之前为负
        if absolute_start >= position + len(name):
            distance = absolute_start - (position + len(name))
        elif absolute_start + 18 <= position:
            distance = position - (absolute_start + 18)
        else:
            distance = 0   # 与名称区间重叠
        candidates.append((distance, code))

    if not candidates:
        return None

    # 距离相同时保持原文出现顺序（sorted 稳定）
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def metadata_to_party_info(metadata: Dict[str, Any], party: str) -> Dict[str, Any]:
    """
    将抽取到的元数据转换为 PartyInfo 结构的字典

    说明：
        供上层构造 models/documents/contract.py 的 PartyInfo 使用，
        避免在每个调用点重复拼装字段映射。

    参数：
        metadata: extract_all_metadata 的返回值
        party: "a" 或 "b"，指定甲方或乙方

    返回：
        可直接用于构造 PartyInfo 的字典；主体名称为空时返回空字典
    """
    if party not in ("a", "b"):
        raise ValueError(f"party 只接受 'a' 或 'b'，实际：{party}")

    name = metadata.get(f"party_{party}_name")
    if not name:
        # 主体名称为空时不返回残缺结构，避免下游出现半成品数据
        return {}

    return {
        "party_name": name,
        "credit_code": metadata.get(f"party_{party}_credit_code"),
        "legal_person": None,
        "contact": None,
        "address": None,
    }
