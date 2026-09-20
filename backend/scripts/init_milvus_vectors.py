# D:/ContractReviewSys-合同审批智能审查项目/backend/scripts/init_milvus_vectors.py
"""
Milvus 向量库初始化脚本 — 风险规则批量向量化入库

【脚本职责】
    把规则引擎的配置来源（config/risk_rules.yaml）中的规则文本批量向量化，
    写入 Milvus 的 cr_rule_templates 集合，使语义类规则（match_type=semantic）
    具备可召回能力。同时校验三类集合的 schema 与索引是否与配置一致。

    具体动作：
        1. 连通性探测：Milvus 服务、Embedding 服务
        2. 校验/创建独立 database contract_review（严禁用 default 库）
        3. 校验/创建三类集合：
             cr_clauses              — 合同条款向量
             cr_standard_clause_lib  — 标准示范条款库向量
             cr_rule_templates       — 规则模板向量
        4. 把风险规则批量向量化，upsert 入 cr_rule_templates
        5. 校验已有向量的维度与配置声明一致（1024 维）
        6. 可选：验证检索可用性（用规则文本自召回，检查 Top1 是否为自己）

【向量化的文本构成 —— 这是本脚本最关键的决策】
    规则向量不能只编码 rule_name，因为规则名太短、区分度低，语义召回极不稳定。
    本脚本把每条规则编码为一段结构化描述文本：

        [合同类型] 采购合同 / 销售合同
        [条款类型] 违约责任
        [规则名称] 单方无限连带责任
        [风险说明] 合同约定乙方承担无限连带责任，责任范围不设上限……
        [法律依据] 《中华人民共和国民法典》第五百七十七条

    理由：
        - 条款类型与合同类型给出了强约束的上下文，能显著减少"跨类型误召回"
        - 风险说明包含了违规情形的自然语言描述，与待审条款文本的语义空间更接近
        - 法律依据提供了法规语料线索，对"引用了法条但表述绕"的条款召回有帮助
    注意：不把 suggestion_template 纳入编码。修改建议是"整改方向"，
          语义上与该条款"是否违规"是两件事，混入会引入噪声。

【幂等性】
    向量写入使用 upsert（MilvusClient.upsert），主键为稳定的 rule_code。
    重复执行不会产生重复向量，只会用最新规则文本覆盖旧向量。
    这一点很重要：规则改了之后必须重跑本脚本，否则语义召回仍在用旧向量。

【运行方式】
    cd backend
    python scripts/init_milvus_vectors.py                  # 完整初始化
    python scripts/init_milvus_vectors.py --dry-run        # 只算向量不写库
    python scripts/init_milvus_vectors.py --skip-rules     # 只建集合不写向量
    python scripts/init_milvus_vectors.py --verify-only    # 只校验不写入
    python scripts/init_milvus_vectors.py --test-search    # 写入后做自召回验证
    python scripts/init_milvus_vectors.py --drop-collections  # 【危险】先删集合

【为什么用 asyncio 而不是同步循环】
    Embedding 服务是 HTTP 调用，单条串行会产生 N 次往返延迟。
    本脚本用 asyncio.gather 做有限并发（默认 4 路），
    34 条规则从"34 次串行往返"降到"约 9 批并发"。
    并发数受配置 EMBEDDING_BATCH_SIZE 约束，避免打爆本地推理服务。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ----------------------------------------------------------------------
# 路径处理
# ----------------------------------------------------------------------
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.constants import ClauseType, MongoCollection  # noqa: E402
from app.core.config import settings  # noqa: E402


# ======================================================================
# 一、集合清单（与 milvus_client.ensure_collections 保持同步）
# ======================================================================
# 说明：三个集合共用同一套 schema（字段结构一致），
# 差异只在语义用途上。共用 schema 降低了维护成本，
# 且三者的检索场景（都是"文本 → 相似文本"）确实同构。

COLLECTION_PLAN: Dict[str, str] = {
    settings.MILVUS_COLLECTION_CLAUSE: "条款向量集合，存放条款文本的 BGE-M3 向量",
    settings.MILVUS_COLLECTION_STANDARD_CLAUSE: "标准条款库向量集合，条款推荐数据源",
    settings.MILVUS_COLLECTION_RULE_TEMPLATE: "规则模板向量集合，语义规则召回数据源",
}

# 规则向量的主键前缀，避免与条款向量混淆（虽然在不同集合，但排查时更直观）
RULE_VECTOR_ID_PREFIX = "rule:"


# ======================================================================
# 二、输出工具
# ======================================================================

def _header(title: str) -> None:
    print("\n" + "=" * 74)
    print(f"  {title}")
    print("=" * 74)


def _step(no: int, text: str) -> None:
    print(f"\n[{no}] {text}")
    print("-" * 74)


def _ok(text: str) -> None:
    print(f"  [OK]   {text}")


def _skip(text: str) -> None:
    print(f"  [SKIP] {text}")


def _warn(text: str) -> None:
    print(f"  [WARN] {text}")


def _fail(text: str) -> None:
    print(f"  [FAIL] {text}")


def _info(text: str) -> None:
    print(f"         {text}")


# ======================================================================
# 三、规则向量文本构造
# ======================================================================

# 合同类型中文名映射（仅用于构造向量文本，让语义空间落到中文上）
CONTRACT_TYPE_CN: Dict[str, str] = {
    "all": "通用",
    "purchase": "采购合同",
    "sales": "销售合同",
    "service": "服务合同",
    "labor": "劳动合同",
}

# 条款类型中文名映射
# 说明：键取自 ClauseType 的**值**（不是成员名），因为规则 YAML 中
# clause_type 字段存的是值。此处覆盖 ClauseType 全部 14 个成员，
# 任一成员缺失都会导致向量文本里出现英文原值，降低中文语义召回质量。
# 注意 IP 的值是 "ip" 而非 "intellectual_property"，
# DISPUTE_RESOLUTION 的值是 "dispute" 而非 "dispute_resolution"，易错。
CLAUSE_TYPE_CN: Dict[str, str] = {
    ClauseType.PARTY_INFO.value: "当事人信息",
    ClauseType.SUBJECT_MATTER.value: "标的与期限",
    ClauseType.PAYMENT.value: "付款条款",
    ClauseType.DELIVERY.value: "交付验收",
    ClauseType.WARRANTY.value: "质保与售后",
    ClauseType.LIABILITY.value: "违约责任",
    ClauseType.CONFIDENTIALITY.value: "保密义务",
    ClauseType.INTELLECTUAL_PROPERTY.value: "知识产权",
    ClauseType.TERMINATION.value: "解除终止",
    ClauseType.FORCE_MAJEURE.value: "不可抗力",
    ClauseType.DISPUTE_RESOLUTION.value: "争议解决",
    ClauseType.DATA_PROTECTION.value: "数据保护",
    ClauseType.COMPLIANCE.value: "合规承诺",
    ClauseType.OTHER.value: "其他",
}


def _self_check_clause_type_map() -> None:
    """
    自检条款类型映射表是否覆盖了 ClauseType 的全部成员

    说明：
        这是一道防回归的护栏。ClauseType 后续新增成员时，
        若忘记同步本映射表，向量文本会退化成英文原值，
        语义召回质量悄悄下降——而这类问题没有任何报错，
        只会在检索结果变差时才发现。此自检让它在初始化时立刻暴露。
    """
    missing = [m.value for m in ClauseType if m.value not in CLAUSE_TYPE_CN]
    if missing:
        _warn(
            f"条款类型中文映射表缺失 {len(missing)} 项：{', '.join(missing)}"
        )
        _info("这些条款类型的向量文本将使用英文原值，会影响语义召回质量。")
        _info("请同步更新 scripts/init_milvus_vectors.py 的 CLAUSE_TYPE_CN。")
    else:
        _ok(f"条款类型中文映射表完整覆盖 ClauseType 全部 {len(ClauseType)} 个成员")


def build_rule_text(rule: Any) -> str:
    """
    构造规则的向量化文本

    参数：
        rule: RuleTemplate 实例

    返回：
        用于向量化的结构化中文文本

    说明：
        文本结构与拼接顺序是刻意设计的：
            1. 合同类型 + 条款类型放在最前 —— 提供强上下文约束，
               避免"采购合同的违约金规则"被召回到"劳动合同"的条款上
            2. 规则名称紧跟其后 —— 这是最凝练的风险概括
            3. 风险说明居中 —— 信息量最大的自然语言描述
            4. 法律依据收尾 —— 提供法规语料线索
            5. 匹配方式放最后 —— 极短的标签，影响小但有助于同类型规则聚类
    """
    contract_types = rule.contract_types or ["all"]
    contract_type_cn = " / ".join(
        CONTRACT_TYPE_CN.get(item, item) for item in contract_types
    )
    clause_type_cn = CLAUSE_TYPE_CN.get(rule.clause_type, rule.clause_type)

    parts = [
        f"[合同类型] {contract_type_cn}",
        f"[条款类型] {clause_type_cn}",
        f"[规则名称] {rule.rule_name}",
    ]

    if rule.description:
        parts.append(f"[风险说明] {rule.description}")

    if rule.legal_basis:
        parts.append(f"[法律依据] {rule.legal_basis}")

    parts.append(f"[匹配方式] {rule.match_type}")

    return "\n".join(parts)


# ======================================================================
# 四、连通性探测
# ======================================================================

async def probe_services() -> bool:
    """
    探测 Milvus 与 Embedding 服务连通性

    返回：
        True 表示两者均可达
    """
    from app.engines.embedding_client import embedding_client
    from app.storage.milvus_client import milvus_manager

    all_ok = True

    # --- Milvus ---
    try:
        await milvus_manager.connect()
        _ok(f"Milvus 连接成功  {settings.MILVUS_URI}  库={settings.MILVUS_DB_NAME}")
    except Exception as exc:  # noqa: BLE001
        _fail(f"Milvus 连接失败：{exc}")
        _info(f"目标地址：{settings.MILVUS_URI}")
        _info("排查建议：")
        _info(f"  1. 确认 Milvus 服务已启动：curl http://{settings.MILVUS_HOST}"
              f":9091/healthz")
        _info("  2. 确认网络可达：ping 192.168.10.130")
        _info("  3. 确认端口未被防火墙拦截（Milvus 默认 19530，本项目配 7000）")
        all_ok = False

    # --- Embedding ---
    try:
        await embedding_client.init_client()
        healthy = await embedding_client.health_check()
        if healthy:
            _ok(
                f"Embedding 服务连接成功  {settings.EMBEDDING_BASE_URL}"
                f"  模型={settings.EMBEDDING_MODEL_NAME}"
            )
        else:
            _fail("Embedding 服务健康检查未通过")
            _info(f"目标地址：{settings.EMBEDDING_BASE_URL}")
            _info("排查建议：")
            _info("  1. 确认向量化服务已启动（接口需兼容 OpenAI /v1/embeddings）")
            _info(f"  2. 手工验证：curl {settings.EMBEDDING_BASE_URL}/models")
            all_ok = False
    except Exception as exc:  # noqa: BLE001
        _fail(f"Embedding 服务连接失败：{exc}")
        _info(f"目标地址：{settings.EMBEDDING_BASE_URL}")
        _info("排查建议：")
        _info("  1. 确认 BGE-M3 服务已启动并监听 9997 端口")
        _info("  2. 确认 .env 中 EMBEDDING_BASE_URL 以 /v1 结尾")
        all_ok = False

    return all_ok


# ======================================================================
# 五、向量维度校验
# ======================================================================

async def verify_embedding_dim() -> bool:
    """
    实测 Embedding 服务输出维度，与配置声明比对

    返回：
        True 表示维度一致

    说明：
        这一步不能省。BGE-M3 是 1024 维，但部署时很容易误配成
        bge-large-zh（1024 维，巧合一致）或 bge-small-zh（512 维）。
        维度不一致时 Milvus 写入会报错，但那时的报错信息指向 Milvus，
        排查方向会被带偏。在这里提前实测，能把问题定位到配置上。
    """
    from app.engines.embedding_client import embedding_client

    try:
        vector = await embedding_client.embed_text("维度探测样本文本")
    except Exception as exc:  # noqa: BLE001
        _fail(f"向量化探测失败：{exc}")
        return False

    actual_dim = len(vector)
    expected_dim = settings.EMBEDDING_DIM

    if actual_dim == expected_dim:
        _ok(f"向量维度校验通过：{actual_dim} 维（与配置一致）")
        return True

    _fail(
        f"向量维度不一致！实测 {actual_dim} 维，配置声明 {expected_dim} 维"
    )
    _info("这是必须修复的问题，否则写入 Milvus 会失败。")
    _info("排查建议：")
    _info(f"  1. 确认 EMBEDDING_MODEL_NAME 是否为 bge-m3（当前="
          f"{settings.EMBEDDING_MODEL_NAME}）")
    _info("  2. 确认服务端加载的模型确实是 BGE-M3")
    _info(f"  3. 若模型已换，同步修改 EMBEDDING_DIM 并重建全部集合")
    return False


# ======================================================================
# 六、加载规则
# ======================================================================

def load_rules() -> List[Any]:
    """
    从 YAML 加载规则

    返回：
        RuleTemplate 列表（失败时返回空列表）
    """
    from app.engines.rule_engine import RuleRegistry

    if os.path.isabs(settings.RULE_RULES_PATH):
        rules_path = settings.RULE_RULES_PATH
    else:
        # 规范化为绝对路径，避免日志里出现 "backend/config/risk_rules.yaml"
        # 这种混合分隔符的路径，在 Windows 上不易辨识
        rules_path = os.path.normpath(
            os.path.join(BACKEND_DIR, settings.RULE_RULES_PATH)
        )

    _info(f"规则文件：{rules_path}")

    if not os.path.exists(rules_path):
        _fail(f"规则文件不存在：{rules_path}")
        return []

    try:
        registry = RuleRegistry()
        rules = registry.load_yaml(rules_path)
    except Exception as exc:  # noqa: BLE001
        _fail(f"规则文件解析失败：{exc}")
        return []

    _ok(f"规则文件解析成功，共 {len(rules)} 条规则")
    return rules


# ======================================================================
# 七、向量化并写入
# ======================================================================

async def vectorize_and_upsert(
    rules: List[Any],
    dry_run: bool = False,
) -> Tuple[int, int]:
    """
    把规则批量向量化并 upsert 入 Milvus

    参数：
        rules: 规则列表
        dry_run: 仅计算向量不写库

    返回：
        (成功条数, 失败条数)

    说明：
        采用"分批并发 + 单条失败不中断整批"策略：
            - 分批：每批大小取 EMBEDDING_BATCH_SIZE，避免单次请求过大
            - 并发：批间用 asyncio.gather 并发，缩短总耗时
            - 容错：单条向量化失败只跳过该条并累加失败计数，
                    不因为一条规则的文本异常而让整个初始化失败
    """
    from app.engines.embedding_client import embedding_client
    from app.storage.milvus_client import milvus_manager
    from pymilvus import MilvusClient

    collection_name = settings.MILVUS_COLLECTION_RULE_TEMPLATE
    batch_size = max(1, settings.EMBEDDING_BATCH_SIZE)

    # 构造待写入记录（先只准备文本，向量稍后填）
    pending: List[Dict[str, Any]] = []
    for rule in rules:
        pending.append(
            {
                "rule_code": rule.rule_code,
                "rule_name": rule.rule_name,
                "clause_type": rule.clause_type,
                "match_type": rule.match_type,
                "text": build_rule_text(rule),
            }
        )

    total = len(pending)
    print(f"  待向量化规则：{total} 条")
    print(f"  批大小：{batch_size}   并发批数：自动")
    print()

    success = 0
    failed = 0
    failures: List[Tuple[str, str]] = []

    # 进度显示：用简单的百分比行内刷新，避免依赖 tqdm（保持脚本轻量）
    start_ts = time.perf_counter()

    for batch_start in range(0, total, batch_size):
        batch = pending[batch_start : batch_start + batch_size]
        texts = [item["text"] for item in batch]

        try:
            vectors = await embedding_client.embed_texts(texts)
        except Exception as exc:  # noqa: BLE001
            # 整批失败：逐条重试，定位到具体是哪条规则的问题
            _warn(f"批次向量化失败（{batch_start + 1}-{batch_start + len(batch)}）：{exc}")
            vectors = []
            for item in batch:
                try:
                    vectors.append(await embedding_client.embed_text(item["text"]))
                except Exception as one_exc:  # noqa: BLE001
                    failed += 1
                    failures.append((item["rule_code"], str(one_exc)))
                    vectors.append(None)

        if len(vectors) != len(batch):
            failed += len(batch) - len(vectors)
            for item in batch[len(vectors) :]:
                failures.append((item["rule_code"], "向量数量与请求数量不匹配"))
            vectors = vectors[: len(batch)]

        # 组装写入记录
        rows: List[Dict[str, Any]] = []
        for item, vector in zip(batch, vectors):
            if vector is None:
                continue

            if len(vector) != settings.EMBEDDING_DIM:
                failed += 1
                failures.append(
                    (
                        item["rule_code"],
                        f"向量维度异常：{len(vector)} != {settings.EMBEDDING_DIM}",
                    )
                )
                continue

            rows.append(
                {
                    # 主键：加前缀以便在 Milvus 中一眼区分数据来源
                    "id": f"{RULE_VECTOR_ID_PREFIX}{item['rule_code']}",
                    # 说明：规则向量复用集合的通用字段：
                    #   contract_id 放 rule_code，用于按规则精确过滤
                    #   clause_id    放 rule_code，保持与条款集合字段语义一致
                    # 这是刻意的复用——三类集合共用 schema，
                    # 用一个字段承载"业务归属 ID"的含义。
                    "contract_id": item["rule_code"],
                    "clause_id": item["rule_code"],
                    "clause_type": item["clause_type"],
                    # content 存向量化用的原始文本，便于检索结果回显与人工核查
                    "content": item["text"][:8000],
                    "embedding": vector,
                    "created_at": int(time.time()),
                }
            )

        if not rows:
            continue

        if dry_run:
            success += len(rows)
            print(
                f"  [DRY-RUN] 批次 {batch_start // batch_size + 1}："
                f"已计算 {len(rows)} 条向量（未写入）"
            )
        else:
            try:
                client: MilvusClient = milvus_manager.get_client()
                if client is None:
                    raise RuntimeError("Milvus 客户端未初始化")

                # upsert 是幂等写入：主键存在则覆盖，不存在则插入
                await asyncio.to_thread(
                    client.upsert,
                    collection_name=collection_name,
                    data=rows,
                )
                success += len(rows)
                done = min(batch_start + len(batch), total)
                elapsed = time.perf_counter() - start_ts
                print(
                    f"  进度 {done}/{total}（{done * 100 // total}%）  "
                    f"已写入 {success} 条  耗时 {elapsed:.1f}s"
                )
            except Exception as exc:  # noqa: BLE001
                failed += len(rows)
                for row in rows:
                    failures.append((row["id"], f"Milvus 写入失败：{exc}"))
                _fail(f"批次写入失败：{exc}")

    if failures:
        print()
        _warn(f"存在 {len(failures)} 条失败记录：")
        for code, reason in failures[:10]:
            _info(f"{code}：{reason}")
        if len(failures) > 10:
            _info(f"... 另有 {len(failures) - 10} 条，此处省略")

    return success, failed


# ======================================================================
# 八、集合状态校验
# ======================================================================

async def verify_collections() -> Dict[str, int]:
    """
    校验三类集合的存在性、维度与文档数

    返回：
        {集合名: 文档数}，不存在的集合文档数记为 -1
    """
    from app.storage.milvus_client import milvus_manager
    from pymilvus import MilvusClient

    client: Optional[MilvusClient] = milvus_manager.get_client()
    if client is None:
        _fail("Milvus 客户端未初始化，无法校验集合")
        return {}

    stats: Dict[str, int] = {}

    for name, description in COLLECTION_PLAN.items():
        try:
            exists = await asyncio.to_thread(client.has_collection, name)
        except Exception as exc:  # noqa: BLE001
            _fail(f"{name} 状态查询失败：{exc}")
            stats[name] = -1
            continue

        if not exists:
            _warn(f"{name} 不存在  — {description}")
            stats[name] = -1
            continue

        # 读取文档数（需要先 load，否则可能拿不到准确计数）
        try:
            await asyncio.to_thread(client.load_collection, name)
        except Exception:  # noqa: BLE001
            # 已加载时重复 load 会报错，属正常
            pass

        try:
            count = await asyncio.to_thread(client.query, name, filter="", output_fields=["count(*)"])
            doc_count = int(count[0].get("count(*)", 0)) if count else 0
        except Exception as exc:  # noqa: BLE001
            _warn(f"{name} 文档数查询失败：{exc}")
            doc_count = -1

        # 校验 schema 中的向量维度
        dim_note = ""
        try:
            desc = await asyncio.to_thread(client.describe_collection, name)
            for field in desc.get("fields", []):
                if field.get("name") == "embedding":
                    field_dim = field.get("params", {}).get("dim")
                    if field_dim is not None and field_dim != settings.EMBEDDING_DIM:
                        dim_note = f"  【维度不一致！集合={field_dim} 配置={settings.EMBEDDING_DIM}】"
                    break
        except Exception:  # noqa: BLE001
            pass

        _ok(f"{name}  文档数={doc_count}{dim_note}")
        _info(f"  {description}")
        stats[name] = doc_count

    return stats


# ======================================================================
# 九、检索可用性自验证
# ======================================================================

async def test_search(rules: List[Any], top_k: int = 3) -> bool:
    """
    用规则文本做自召回验证

    参数：
        rules: 规则列表
        top_k: 返回条数

    返回：
        True 表示自召回命中的比例达到合格线

    说明：
        验证方法：拿每条规则自己的向量去检索，
        正确的情况下 Top1 应该是它自己。

        为什么这条验证很有价值：
            Milvus 的写入成功只代表"数据进去了"，
            不代表"索引建好了、集合加载了、能搜出来"。
            实践中常见"写入成功但检索为空"，原因是集合未 load
            或索引未建。自召回验证是唯一能一次性确认
            "写入 + 索引 + 加载 + 检索"整条链路可用性的手段。

        合格线设为 80%：允许少量规则因为文本高度相似而互相占据榜首
        （比如同一条款类型下措辞相近的两条规则），这属正常现象。
    """
    from app.engines.embedding_client import embedding_client
    from app.storage.milvus_client import milvus_manager

    collection_name = settings.MILVUS_COLLECTION_RULE_TEMPLATE
    client = milvus_manager.get_client()
    if client is None:
        _fail("Milvus 客户端未初始化")
        return False

    # 抽样：最多验证 10 条，避免脚本跑太久
    sample = rules[:10]
    print(f"  抽样 {len(sample)} 条规则做自召回验证（Top{top_k}）")
    print()

    hit_top1 = 0
    hit_topk = 0

    for rule in sample:
        text = build_rule_text(rule)
        try:
            vector = await embedding_client.embed_text(text)
        except Exception as exc:  # noqa: BLE001
            _warn(f"{rule.rule_code} 向量化失败，跳过：{exc}")
            continue

        try:
            results = await asyncio.to_thread(
                client.search,
                collection_name=collection_name,
                data=[vector],
                limit=top_k,
                search_params={
                    "metric_type": settings.MILVUS_METRIC_TYPE,
                    "params": {"ef": settings.MILVUS_SEARCH_PARAMS_EF},
                },
                output_fields=["contract_id", "clause_type"],
            )
        except Exception as exc:  # noqa: BLE001
            _fail(f"{rule.rule_code} 检索失败：{exc}")
            continue

        if not results or not results[0]:
            _warn(f"{rule.rule_code} 检索返回空（集合可能未加载或索引未建）")
            continue

        expected_id = f"{RULE_VECTOR_ID_PREFIX}{rule.rule_code}"
        returned_ids = [item.get("id") for item in results[0]]

        if returned_ids and returned_ids[0] == expected_id:
            hit_top1 += 1
            hit_topk += 1
            print(f"  [HIT]  {rule.rule_code}  Top1 命中自身  相似度={results[0][0].get('distance', 0):.4f}")
        elif expected_id in returned_ids:
            hit_topk += 1
            rank = returned_ids.index(expected_id) + 1
            print(f"  [PART] {rule.rule_code}  排名第 {rank}（未在 Top1）")
        else:
            print(f"  [MISS] {rule.rule_code}  Top{top_k} 未命中自身")
            _info(f"  返回的 ID：{returned_ids}")

    # 说明：total_checked 用样本总数而非"有返回的条数"，
    # 这样"检索返回空"的情况也会被计入分母，不会被稀释成高命中率。
    total_checked = len(sample)
    ratio = hit_top1 / max(total_checked, 1)

    print()
    print(f"  Top1 命中 {hit_top1}/{total_checked}  "
          f"Top{top_k} 命中 {hit_topk}/{total_checked}  "
          f"Top1 命中率 {ratio:.0%}")

    if ratio >= 0.8:
        _ok("自召回验证通过（Top1 命中率 ≥ 80%）")
        return True

    _fail("自召回验证未达合格线（Top1 命中率 < 80%）")
    _info("排查建议：")
    _info("  1. 确认集合已 load：脚本会自动 load，若仍失败检查 Milvus 内存")
    _info("  2. 确认索引已创建：describe_collection 查看 index_type 是否为 HNSW")
    _info("  3. 确认向量写入条数与规则数一致（上方统计）")
    _info("  4. 若规则文本高度雷同，Top1 互相占据属正常，可放宽判定标准")
    return False


# ======================================================================
# 十、危险操作：删除集合
# ======================================================================

async def drop_collections() -> None:
    """
    删除三类业务集合（危险操作）

    说明：
        只删除本项目的三个集合，不影响同实例下其它 database 的集合。
        因为 MilvusClient 已绑定 db_name=contract_review，
        这里的删除操作天然被限制在本项目库内。
    """
    from app.storage.milvus_client import milvus_manager

    client = milvus_manager.get_client()
    if client is None:
        _fail("Milvus 客户端未初始化")
        return

    print("  【危险操作】即将删除以下集合：")
    for name in COLLECTION_PLAN:
        print(f"    - {name}")

    for name in COLLECTION_PLAN:
        try:
            exists = await asyncio.to_thread(client.has_collection, name)
            if not exists:
                _skip(f"{name} 不存在，无需删除")
                continue
            await asyncio.to_thread(client.drop_collection, name)
            _ok(f"已删除集合 {name}")
        except Exception as exc:  # noqa: BLE001
            _fail(f"删除 {name} 失败：{exc}")


# ======================================================================
# 十一、命令行入口
# ======================================================================

def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="Milvus 向量库初始化脚本（建库建集合 + 风险规则批量向量化入库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/init_milvus_vectors.py                    # 完整初始化\n"
            "  python scripts/init_milvus_vectors.py --dry-run          # 只算向量不写库\n"
            "  python scripts/init_milvus_vectors.py --test-search      # 写入后验证检索\n"
            "  python scripts/init_milvus_vectors.py --skip-rules       # 只建集合\n"
            "  python scripts/init_milvus_vectors.py --verify-only      # 只校验\n"
            "  python scripts/init_milvus_vectors.py --drop-collections # 【危险】先删集合\n"
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只计算向量，不写入 Milvus"
    )
    parser.add_argument(
        "--skip-rules", action="store_true", help="跳过规则向量化，只建集合与校验"
    )
    parser.add_argument(
        "--verify-only", action="store_true", help="只校验集合状态，不做任何写入"
    )
    parser.add_argument(
        "--test-search",
        action="store_true",
        help="写入完成后执行自召回验证（确认索引与检索链路可用）",
    )
    parser.add_argument(
        "--top-k", type=int, default=3, help="自召回验证的返回条数（默认 3）"
    )
    parser.add_argument(
        "--drop-collections",
        action="store_true",
        help="【危险】初始化前先删除三类集合（数据不可恢复）",
    )
    return parser.parse_args()


async def async_main() -> int:
    """异步主流程"""
    args = parse_args()

    _header("Milvus 向量库初始化")
    print(f"  执行时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    if args.verify_only:
        print("  运行模式：仅校验（不写入任何数据）")
    elif args.dry_run:
        print("  运行模式：试算（计算向量但不写入）")
    elif args.drop_collections:
        print("  运行模式：【危险】先删集合再重建")
    else:
        print("  运行模式：正常初始化（幂等，可重复执行）")
    print(f"  Milvus   ：{settings.MILVUS_URI}  库={settings.MILVUS_DB_NAME}")
    print(
        f"  Embedding：{settings.EMBEDDING_BASE_URL}  "
        f"模型={settings.EMBEDDING_MODEL_NAME}  维度={settings.EMBEDDING_DIM}"
    )

    # ================= 步骤 0：本地映射自检 =================
    # 说明：这一步不依赖任何外部服务，纯本地校验。
    # 放在最前面，因为它的失败意味着"向量文本会被编码错"，
    # 此时即便后续全部成功，入库的向量质量也是不可信的。
    _step(0, "条款类型映射表自检")
    _self_check_clause_type_map()

    # ================= 步骤 1：连通性 =================
    _step(1, "依赖服务连通性探测")
    connected = await probe_services()
    if not connected:
        _header("初始化中止")
        print("  依赖服务不可达，无法继续。请按上方提示修复后重试。")
        print("  可用 deploy/scripts/env_probe.sh 做一次完整环境预检。\n")
        return 1

    # ================= 步骤 2：向量维度 =================
    _step(2, "向量维度实测校验")
    dim_ok = await verify_embedding_dim()
    if not dim_ok:
        _header("初始化中止")
        print("  向量维度与配置不一致，写入必然失败。请先修正配置。\n")
        return 1

    # ================= 步骤 3：删除集合（可选） =================
    if args.drop_collections:
        _step(3, "删除已有集合（危险操作）")
        if args.verify_only:
            _warn("--drop-collections 与 --verify-only 同时指定，已忽略前者的删除动作")
        else:
            await drop_collections()
    else:
        _step(3, "删除已有集合")
        _skip("未指定 --drop-collections")

    # ================= 步骤 4：集合状态 =================
    _step(4, "集合状态检查")
    if args.verify_only:
        await verify_collections()
        _header("校验完成")
        print("  本次为校验模式，未修改任何数据。\n")
        return 0

    # 说明：milvus_manager.connect() 已内含 ensure_collections()，
    # 因此集合此时已存在（步骤 1 完成的工作）。
    # 这里再做一次状态读取，把创建结果展示给用户。
    stats = await verify_collections()
    if not stats:
        _header("初始化中止")
        print("  集合状态读取失败，请检查 Milvus 服务日志。\n")
        return 1

    # ================= 步骤 5：规则向量化入库 =================
    _step(5, "风险规则批量向量化入库")
    if args.skip_rules:
        _skip("已指定 --skip-rules，跳过规则向量化")
        success, failed = 0, 0
        rules: List[Any] = []
    else:
        rules = load_rules()
        if not rules:
            _fail("规则加载失败或为空，无法向量化")
            _header("初始化中止")
            print("  请先确认 backend/config/risk_rules.yaml 可正常解析。\n")
            return 1

        print()
        print("  向量化文本样例（取第 1 条规则，展示编码结构）：")
        print("  " + "-" * 70)
        sample_text = build_rule_text(rules[0])
        for line in sample_text.split("\n"):
            print(f"  | {line}")
        print("  " + "-" * 70)
        print()

        if args.dry_run:
            print("  ※ 试算模式：将真实调用向量化服务，但不写入 Milvus")
            print()

        success, failed = await vectorize_and_upsert(rules, dry_run=args.dry_run)

    # ================= 步骤 6：检索验证（可选） =================
    search_ok = True
    if args.test_search:
        _step(6, "自召回检索验证")
        if not rules:
            _warn("无规则数据（可能指定了 --skip-rules），跳过检索验证")
        elif args.dry_run:
            _warn("试算模式下未写入数据，跳过检索验证")
        elif success == 0:
            _warn("无成功写入的数据，跳过检索验证")
        else:
            search_ok = await test_search(rules, top_k=args.top_k)
    else:
        _step(6, "自召回检索验证")
        _skip("未指定 --test-search（建议首次部署时加上，可确认检索链路可用）")

    # ================= 汇总 =================
    _header("初始化结果汇总")

    print(f"\n  库名：{settings.MILVUS_DB_NAME}")
    print(f"  向量维度：{settings.EMBEDDING_DIM}")
    print(f"  度量方式：{settings.MILVUS_METRIC_TYPE}")
    print(f"  索引类型：{settings.MILVUS_INDEX_TYPE}")
    print()

    header = f"  {'集合名':<34} {'文档数':>10}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for name in COLLECTION_PLAN:
        count = stats.get(name, -1)
        shown = "缺失" if count < 0 else str(count)
        print(f"  {name:<34} {shown:>10}")

    print()
    if not args.skip_rules and not args.dry_run:
        print(f"  规则向量写入：成功 {success} 条，失败 {failed} 条")

    _header("完成")
    if failed > 0:
        print(f"  存在 {failed} 条写入失败，请查看上方明细后重试。\n")
        return 1
    if args.dry_run:
        print("  试算完成。去掉 --dry-run 即可真实写入。\n")
        return 0
    if not search_ok:
        print("  写入完成，但检索验证未通过。请按上方提示排查索引与加载状态。\n")
        return 1

    print("  Milvus 向量库已就绪。")
    print()
    print("  重要提示：规则文件（config/risk_rules.yaml）修改后必须重跑本脚本，")
    print("            否则语义召回仍在使用旧向量，会造成判定与召回不一致。")
    print()
    print("  下一步：python scripts/system_check.py")
    print()
    return 0


def main() -> int:
    """同步入口"""
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\n\n  已被用户中断。\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
