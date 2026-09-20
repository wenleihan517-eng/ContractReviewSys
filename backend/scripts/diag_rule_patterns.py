# D:/ContractReviewSys-合同审批智能审查项目/backend/scripts/diag_rule_patterns.py
"""
规则正则模式诊断脚本（临时工具）

【用途】
    规则配置文件中的 regex 类规则若模式书写有误，不会报错，只会在真实
    合同上静默不命中。本脚本逐条检出配置中的正则模式，用"应命中样本"与
    "不应命中样本"做对照验证，把静默失效暴露出来。

【运行方式】
    cd backend && python scripts/diag_rule_patterns.py
"""

import os
import re
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.engines.rule_engine import rule_engine

# (规则编码, 条款类型, 应命中文本, 不应命中文本)
CASES = [
    ("PAY-002", "payment",
     "甲方应在收到货物后及时支付款项。",
     "甲方应在收到发票之日起三十日内支付款项。"),
    ("PAY-002", "payment",
     "甲方尽快支付剩余款项。",
     "甲方应在三十日内支付剩余款项。"),

    ("NUM-003", "payment",
     "甲方应在收货后九十个工作日内支付货款。",
     "甲方应在收货后三十个工作日内支付货款。"),
    ("NUM-003", "payment",
     "甲方应在收货后90个工作日内支付货款。",
     "甲方应在三十日内支付货款。"),
    ("NUM-003", "payment",
     "甲方应在收货后七十五个自然日内支付货款。",
     "甲方应在六十日内支付货款。"),

    ("JUR-001", "dispute",
     "协商不成的，提交乙方所在地有管辖权的人民法院诉讼解决。",
     "协商不成的，提交甲方住所地有管辖权的人民法院诉讼解决。"),
    ("JUR-001", "dispute",
     "因本合同产生的争议由供方所在地法院管辖。",
     "因本合同产生的争议由甲方住所地人民法院管辖。"),
    ("JUR-001", "dispute",
     "争议提交境外仲裁机构裁决。",
     "争议提交中国国际经济贸易仲裁委员会仲裁。"),

    ("SUBJ-001", "party_info",
     "甲方为公司，乙方为供货商，双方达成如下协议。",
     "甲方为北京智远科技有限公司，统一社会信用代码 91110108MA01XXXXX。"),
    ("SUBJ-001", "party_info",
     "乙方为厂家，负责供货。",
     "乙方为供货商提供全部技术支持服务。"),

    ("FORCE-001", "force_majeure",
     "遭遇不可抗力的一方应在事件发生后四十五日内通知对方。",
     "因不可抗力导致无法履行的，双方互不承担违约责任。"),
    ("FORCE-001", "force_majeure",
     "遭受不可抗力一方应在7日内通知对方。",
     "因不可抗力导致无法履行的，双方互不承担违约责任。"),

    ("CONF-001", "confidentiality",
     "双方保密义务的保密期限为六个月。",
     "双方保密义务的保密期限为三年。"),
    ("CONF-001", "confidentiality",
     "保密期限为6个月。",
     "保密期限为五年。"),

    ("LAB-006", "termination",
     "乙方试用期为六个月。",
     "乙方试用期为二个月。"),
    ("LAB-006", "termination",
     "乙方试用期为6个月。",
     "乙方试用期为一个月。"),

    ("NUM-002", "liability",
     "逾期付款的，按未付款项的每日千分之五支付违约金。",
     "逾期付款的，按未付款项的万分之五支付违约金。"),

    ("JUR-002", "dispute",
     "协商不成的，提交北京仲裁委员会仲裁或向人民法院提起诉讼。",
     "协商不成的，提交北京仲裁委员会按其仲裁规则仲裁。"),

    ("BAL-005", "liability",
     "乙方不承担超出本合同总价款限额以外的任何赔偿责任。",
     "双方赔偿总额均以本合同总价款为上限。"),

    ("IP-001", "ip",
     "因知识产权侵权产生的全部责任和赔偿均由乙方承担。",
     "双方应各自承担相应的知识产权责任。"),
]


def matches(rule, text: str) -> tuple:
    """
    按规则自身的匹配方式判断文本是否命中

    说明：
        规则配置可能是 regex（pattern）或 keyword（keywords）两种形式，
        诊断脚本必须按实际 match_type 选择判断方式，否则会出现"用不存在的
        pattern 去匹配"导致的假失败。

    参数：
        rule: RuleTemplate 实例
        text: 待判断文本

    返回：
        (是否命中, 命中片段)
    """
    config = rule.match_config or {}

    if rule.match_type == "regex":
        pattern = config.get("pattern", "")
        if not pattern:
            return False, "<缺少 pattern>"
        found = re.search(pattern, text)
        return bool(found), (found.group(0) if found else "")

    if rule.match_type == "keyword":
        for keyword in config.get("keywords", []) or []:
            if keyword and keyword in text:
                return True, keyword
        return False, ""

    # 其它匹配方式（missing_clause / numeric_threshold 等）不走文本匹配
    return False, f"<match_type={rule.match_type} 不走文本匹配>"


def main() -> int:
    """逐条验证配置中的正则模式"""
    rules = {r.rule_code: r for r in rule_engine.load_rules_from_yaml(
        "config/risk_rules.yaml"
    )}

    passed, failed = 0, []
    for code, _ctype, hit_text, miss_text in CASES:
        rule = rules.get(code)
        if rule is None:
            failed.append((code, "规则不存在", "", ""))
            print(f"[MISS] {code:9s} 规则不存在")
            continue

        hit, hit_frag = matches(rule, hit_text)
        miss, miss_frag = matches(rule, miss_text)

        ok = True
        detail = ""
        if hit_text and not hit:
            ok, detail = False, f"应命中但未命中：{hit_text}"
        elif miss_text and miss:
            ok, detail = False, f"不应命中但命中：{miss_text!r} → {miss_frag!r}"

        if ok:
            passed += 1
            print(f"[PASS] {code:9s} 命中={hit!s:5s} | {hit_frag[:44] or '-'}")
        else:
            failed.append((code, detail, hit_text, miss_text))
            print(f"[FAIL] {code:9s} {detail}")

    print()
    print("=" * 68)
    print(f"正则模式诊断：通过 {passed} 项，失败 {len(failed)} 项")
    if failed:
        for code, detail, h, m in failed:
            print(f"  [FAIL] {code} | {detail}")
    print("=" * 68)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
