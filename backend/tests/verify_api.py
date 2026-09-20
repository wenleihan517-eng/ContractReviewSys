# D:/ContractReviewSys-合同审批智能审查项目/backend/tests/verify_api.py
"""
API 接口层端到端验证

【验证目标】
    1. 应用可创建，Swagger/OpenAPI 文档可生成
    2. 7 类接口的路由完整注册（RESTful 路径）
    3. 统一响应格式（ApiResponse：code/message/data/request_id）
    4. 统一错误码（ErrorCode 常量表）
    5. Pydantic 请求/响应模型可正常构造
    6. 健康检查接口可用

【存储隔离】
    MongoDB/MinIO/Milvus 未就绪，本验证聚焦接口层的"可路由性"与
    "契约正确性"，不触发依赖存储的生命周期。健康检查接口（/health）
    不依赖存储，可直接请求。

【运行方式】
    cd backend && python tests/verify_api.py
"""

import sys

BACKEND_DIR = sys.path[0] if "backend" in sys.path[0] else None
import os

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.core.exceptions import ErrorCode
from app.core.response import ApiResponse, page_response, success_response

PASSED = []
FAILED = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name} | {detail}")


# ======================================================================
# 验证一：应用创建与 OpenAPI 文档
# ======================================================================

def test_app_and_openapi():
    print("\n[验证一] 应用创建与 OpenAPI 文档")
    from app.main import create_app

    app = create_app()
    check("应用创建成功", app is not None)
    check("应用标题正确", app.title == "合同审批智能审查系统" or "合同" in (app.title or ""))

    # 生成 OpenAPI schema（Swagger 文档，不触发 lifespan）
    openapi = app.openapi()
    check("OpenAPI 文档生成成功", openapi is not None and "paths" in openapi)

    paths = openapi.get("paths", {})
    check("OpenAPI 含路径定义", len(paths) > 0, f"路径数={len(paths)}")

    return openapi, paths


# ======================================================================
# 验证二：路由清单完整性（7 类接口）
# ======================================================================

def test_route_coverage(paths):
    print("\n[验证二] 路由清单完整性（7 类接口）")

    # 定义每类接口应包含的关键路径（RESTful）
    expected = {
        "通用-健康检查": ["/api/v1/health", "/api/v1/ready", "/api/v1/config-check"],
        "任务管理": ["/api/v1/tasks", "/api/v1/tasks/approval-pending",
                     "/api/v1/tasks/{task_no}", "/api/v1/tasks/{task_no}/retry",
                     "/api/v1/tasks/{task_no}/resolve"],
        "文档解析": ["/api/v1/parse/upload", "/api/v1/parse/{task_id}/trigger",
                     "/api/v1/parse/{task_id}/result"],
        "风险审查": ["/api/v1/review/{task_id}/trigger", "/api/v1/review/{task_id}/result",
                     "/api/v1/review/risk-items/{risk_id}",
                     "/api/v1/review/risk-items/{risk_id}/recommend"],
        "报告与评论": ["/api/v1/report/{task_id}/generate",
                       "/api/v1/report/{task_id}/comment/preview",
                       "/api/v1/report/{task_id}/comment/write-back",
                       "/api/v1/report/{task_id}/download"],
        "规则管理": ["/api/v1/rules", "/api/v1/rules/{rule_code}",
                     "/api/v1/rules/{rule_code}/enabled",
                     "/api/v1/rules/standard-clauses"],
        "审计日志": ["/api/v1/audit/tasks/{task_id}/logs"],
    }

    total_expected = 0
    total_found = 0
    for category, paths_list in expected.items():
        found = [p for p in paths_list if p in paths]
        total_expected += len(paths_list)
        total_found += len(found)
        check(f"{category} 路由齐全（{len(found)}/{len(paths_list)}）",
              len(found) == len(paths_list),
              f"缺失={[p for p in paths_list if p not in paths]}")

    check(f"7 类接口路由全覆盖（{total_found}/{total_expected}）",
          total_found == total_expected)

    return paths


# ======================================================================
# 验证三：统一响应格式
# ======================================================================

def test_unified_response():
    print("\n[验证三] 统一响应格式")

    # 成功响应
    resp = success_response(data={"id": "x"}, message="成功")
    check("成功响应 code=0", resp.code == ErrorCode.SUCCESS)
    check("成功响应含 message", bool(resp.message))
    check("成功响应含 data", resp.data is not None)

    # 分页响应
    paged = page_response(items=[{"a": 1}], total=10, page=1, page_size=5)
    check("分页响应 data 含 total", paged.data.get("total") == 10)
    check("分页响应 data 含 total_pages", paged.data.get("total_pages") == 2)
    check("分页响应 data 含 items", len(paged.data.get("items", [])) == 1)

    # ApiResponse 结构字段完整
    field_names = set(ApiResponse.model_fields.keys())
    check("ApiResponse 含 code/message/data/request_id",
          {"code", "message", "data", "request_id"} <= field_names)


# ======================================================================
# 验证四：统一错误码
# ======================================================================

def test_error_codes():
    print("\n[验证四] 统一错误码")

    check("错误码 SUCCESS=0", ErrorCode.SUCCESS == 0)
    check("错误码 NOT_FOUND=10003", ErrorCode.NOT_FOUND == 10003)
    check("错误码 VALIDATION_ERROR=20000", ErrorCode.VALIDATION_ERROR == 20000)
    check("错误码 MONGO_ERROR=30000", ErrorCode.MONGO_ERROR == 30000)
    check("错误码 RULE_ENGINE_ERROR=50005", ErrorCode.RULE_ENGINE_ERROR == 50005)
    check("错误码 APPROVAL_PERMISSION_DENIED=50008",
          ErrorCode.APPROVAL_PERMISSION_DENIED == 50008)
    check("错误码 TASK_BLOCKED=50007", ErrorCode.TASK_BLOCKED == 50007)


# ======================================================================
# 验证五：Pydantic 请求/响应模型
# ======================================================================

def test_schemas():
    print("\n[验证五] Pydantic 请求/响应模型")

    # 任务创建请求
    from app.schemas.task import TaskCreateRequest, TaskCreateResponse
    req = TaskCreateRequest(contract_id="c1", contract_title="设备采购合同")
    check("TaskCreateRequest 可构造", req.contract_id == "c1")
    check("TaskCreateResponse 可构造",
          TaskCreateResponse(task_id="t1", task_no="T001").task_no == "T001")

    # 风险项编辑请求（红线：不含判定字段）
    from app.schemas.review import RiskItemEditRequest
    edit = RiskItemEditRequest(risk_id="R1", suggestion_text="建议修改")
    check("RiskItemEditRequest 仅建议类字段",
          "risk_level" not in RiskItemEditRequest.model_fields)

    # 评论预览响应
    from app.schemas.report import CommentPreviewResponse
    check("CommentPreviewResponse 可构造",
          CommentPreviewResponse(task_id="t1").task_id == "t1")

    # 规则创建请求
    from app.schemas.rule import RuleUpsertRequest
    rule = RuleUpsertRequest(
        rule_code="R1", rule_name="缺违约金",
        match_type="missing_clause",
        match_config={"require_clause_types": ["liability"]},
    )
    check("RuleUpsertRequest 支持 missing_clause 配置",
          rule.match_config.get("require_clause_types") == ["liability"])

    # 分页参数
    from app.schemas.common import PaginationQuery
    check("PaginationQuery 默认值正确",
          PaginationQuery().page == 1 and PaginationQuery().page_size == 20)


# ======================================================================
# 验证六：健康检查接口（TestClient，不依赖存储）
# ======================================================================

def test_health_endpoint():
    print("\n[验证六] 健康检查接口（TestClient，mock 存储初始化）")
    from unittest import mock
    from fastapi.testclient import TestClient
    from app.main import create_app

    app = create_app()

    # mock 存储初始化，避免 lifespan 连接真实 MongoDB/MinIO/Milvus
    async def _noop(*args, **kwargs):
        return None

    with mock.patch("app.storage.init_all_storages", new=_noop), \
         mock.patch("app.storage.close_all_storages", new=_noop):

        with TestClient(app) as client:
            resp = client.get("/api/v1/health")
            check("GET /health 返回 200", resp.status_code == 200,
                  f"实际={resp.status_code}")
            body = resp.json()
            check("health 响应含 code=0", body.get("code") == 0)
            check("health 响应含 data.status", body.get("data", {}).get("status") == "alive")

            # 根路径
            resp_root = client.get("/")
            check("根路径返回服务信息", resp_root.status_code == 200)

            # OpenAPI 文档可在线访问（Swagger）
            resp_docs = client.get("/openapi.json")
            check("OpenAPI JSON 可访问", resp_docs.status_code == 200,
                  f"实际={resp_docs.status_code}")


# ======================================================================
# 主流程
# ======================================================================

def main() -> int:
    print("=" * 72)
    print("API 接口层端到端验证")
    print("=" * 72)

    openapi, paths = test_app_and_openapi()
    test_route_coverage(paths)
    test_unified_response()
    test_error_codes()
    test_schemas()
    test_health_endpoint()

    print("\n" + "=" * 72)
    print(f"结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("-" * 72)
        for name, detail in FAILED:
            print(f"  [FAIL] {name} | {detail}")
    print("=" * 72)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
