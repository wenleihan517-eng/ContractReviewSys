# D:/ContractReviewSys-合同审批智能审查项目/backend/scripts/smoke_local_run.py
"""
本地试用冒烟脚本 — 验证服务已就绪并跑通核心链路

用途：
    在本地启动后端后，一键验证以下环节：
        1. 根路径探活 / 健康检查
        2. 三个内置账号登录 + 权限矩阵
        3. 上传合同（MinIO）→ 创建审查任务（差异化路由）
        4. 触发解析 → 触发审查（规则引擎 + 向量 + 大模型）
        5. 读取审查结论 / 风险命中 / 生成并下载报告

用法：
    cd backend
    "<venv>/Scripts/python.exe" scripts/smoke_local_run.py [--base http://127.0.0.1:8100]

参数：
    --base       后端根地址，默认 http://127.0.0.1:8100
    --sample     样例文件绝对路径，默认取 docs/demo_samples/sample2_大额销售合同.pdf
    --no-llm     跳过审查触发（当 Embedding/LLM 未启动、只想验证上传+解析时使用）

返回：
    标准输出打印每步 HTTP 状态与关键字段；全部通过时退出码 0
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

# 保证可以 import 到 app 包（脚本位于 backend/scripts/ 下）
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

DEFAULT_SAMPLE = os.path.abspath(
    os.path.join(_BACKEND_DIR, "..", "docs", "demo_samples", "sample2_大额销售合同.pdf")
)


def http(url: str, data=None, token: str = None, method: str = None,
         raw: bytes = None, content_type: str = "application/json", timeout: int = 120):
    """
    极简 HTTP 封装

    返回：
        (状态码, 响应体文本)；网络异常时状态码为 0
    """
    headers = {"Content-Type": content_type}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = raw if raw is not None else (
        json.dumps(data).encode("utf-8") if data is not None else None
    )
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method=method or ("POST" if body else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def show(title: str, status: int, body: str, limit: int = 700) -> None:
    """统一打印小节标题与响应摘要"""
    print(f"\n=== {title} ===")
    print(f"HTTP {status}")
    print(body[:limit] + ("..." if len(body) > limit else ""))


def jload(body: str) -> dict:
    """安全解析 JSON，失败返回空 dict"""
    try:
        return json.loads(body)
    except Exception:  # noqa: BLE001
        return {}


def jdata(body: str) -> dict:
    """取出统一响应体中的 data 段"""
    return jload(body).get("data") or {}


def upload(base: str, token: str, sample: str):
    """构造 multipart 请求上传文件，返回 (状态码, 响应体)"""
    with open(sample, "rb") as fh:
        content = fh.read()
    boundary = "----smokeBoundary1a2b3c4d"
    fname = os.path.basename(sample)
    ext = os.path.splitext(fname)[1].lower()
    ctype = {".pdf": "application/pdf",
             ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             ".doc": "application/msword",
             ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(ext, "application/octet-stream")
    parts = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{fname}"\r\n'.encode(),
        f"Content-Type: {ctype}\r\n\r\n".encode(),
        content,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    return http(base + "/api/v1/parse/upload", raw=b"".join(parts), token=token,
                content_type=f"multipart/form-data; boundary={boundary}")


def main() -> int:
    parser = argparse.ArgumentParser(description="本地试用冒烟验证")
    parser.add_argument("--base", default="http://127.0.0.1:8100", help="后端根地址")
    parser.add_argument("--sample", default=DEFAULT_SAMPLE, help="待上传的合同样例路径")
    parser.add_argument("--no-llm", action="store_true", help="跳过审查触发（Embedding/LLM 未启动时使用）")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    failures = []

    # ---------- 1. 探活 ----------
    status, body = http(base + "/")
    show("1. 根路径探活", status, body, limit=300)
    if status != 200:
        failures.append("根路径探活失败")

    status, body = http(base + "/api/v1/health")
    show("2. 健康检查 /health", status, body, limit=500)

    # ---------- 2. 登录 ----------
    tokens = {}
    for username in ("user_business", "user_law", "user_admin"):
        status, body = http(base + "/api/v1/auth/login",
                            {"username": username, "password": "123456"})
        data = jdata(body)
        role = data.get("user", {}).get("role") or data.get("role") or ""
        print(f"\n=== 3. 登录 {username} === HTTP {status} | 角色={role} | {jload(body).get('message')}")
        tok = data.get("access_token") or data.get("token")
        if tok:
            tokens[username] = tok

    if len(tokens) != 3:
        failures.append(f"登录成功数={len(tokens)}，期望 3")

    admin_token = tokens.get("user_admin") or next(iter(tokens.values()), None)
    biz_token = tokens.get("user_business") or admin_token
    law_token = tokens.get("user_law") or admin_token

    # ---------- 3. 权限矩阵 ----------
    if admin_token:
        status, body = http(base + "/api/v1/auth/permission-matrix", token=admin_token)
        data = jdata(body)
        print(f"\n=== 4. 权限矩阵 === HTTP {status} | 角色数={len(data.get('roles') or [])} | 权限总数={data.get('total_permissions')}")
        for r in data.get("roles") or []:
            print(f"  - {r.get('role_label')}({r.get('role')}): {len(r.get('permissions') or [])} 项")
        if status != 200:
            failures.append("权限矩阵获取失败")

    # ---------- 4. 上传合同 ----------
    object_key = None
    source_type = "digital"
    if not os.path.exists(args.sample):
        print(f"\n[跳过] 样例文件不存在：{args.sample}")
        failures.append("样例文件缺失")
    elif biz_token:
        status, body = upload(base, biz_token, args.sample)
        data = jdata(body)
        show("5. 上传合同 /parse/upload", status, body, limit=600)
        if status != 200:
            failures.append("合同上传失败")
        else:
            object_key = data.get("object_key")
            source_type = data.get("source_type") or "digital"

    # ---------- 5. 创建审查任务 ----------
    task_no = None
    task_id = None
    if object_key and biz_token:
        payload = {
            "contract_id": os.path.splitext(os.path.basename(args.sample))[0],
            "contract_title": "大额销售合同（本地试用样例）",
            "contract_no": f"SMOKE-{int(time.time())}",
            "submit_dept": "销售部",
            "applicant": "张经办",
            "counterparty": "示例科技有限公司",
            "contract_amount": 8000000.0,
            "currency": "CNY",
            "business_type": "销售",
            "contract_type": "sales",
            "source_type": source_type,
            "file_object_key": object_key,
            "file_name": os.path.basename(args.sample),
        }
        status, body = http(base + "/api/v1/tasks", payload, token=biz_token)
        data = jdata(body)
        show("6. 创建审查任务 /tasks", status, body, limit=900)
        task_id = data.get("task_id")
        task_no = data.get("task_no")
        if status not in (200, 201):
            failures.append("任务创建失败")

    # 说明：路径参数 {task_id} 接受的是任务 ID（Mongo ObjectId），
    #      而 task_no（T2026...）是业务号，两者不可混用，此处统一用 task_id 拼路径。
    tid = task_id or task_no

    # ---------- 6. 触发解析 ----------
    if tid and biz_token:
        status, body = http(base + f"/api/v1/parse/{tid}/trigger",
                            {"task_id": tid, "object_key": object_key,
                             "source_type": source_type,
                             "file_name": os.path.basename(args.sample)},
                            token=biz_token)
        show("7. 触发解析 /parse/{task_id}/trigger", status, body, limit=600)

        status, body = http(base + f"/api/v1/parse/{tid}/result", token=biz_token)
        data = jdata(body)
        text = data.get("full_text") or ""
        blocks = data.get("text_blocks") or []
        print(f"\n=== 8. 解析结果 /parse/{{task_id}}/result === HTTP {status}")
        print(f"  | 解析状态 = {data.get('parse_status')} | 引擎 = {data.get('parse_engine')}")
        print(f"  | 文本长度 = {len(text)} | 文本块 = {len(blocks)} | 页数 = {data.get('page_count')}")
        if text:
            print(f"  | 全文预览：{text[:100].replace(chr(10), ' ')}...")
        if status != 200:
            failures.append("解析结果获取失败")

    # ---------- 7. 触发审查 ----------
    if tid and biz_token and not args.no_llm:
        status, body = http(base + f"/api/v1/review/{tid}/trigger",
                            {"task_id": tid, "object_key": object_key,
                             "source_type": source_type,
                             "file_name": os.path.basename(args.sample)},
                            token=biz_token)
        show("9. 触发审查 /review/{task_id}/trigger", status, body, limit=700)

        status, body = http(base + f"/api/v1/review/{tid}/result", token=biz_token)
        data = jdata(body)
        # 注意：审查结果接口返回的是平铺结构（risk_level / is_blocked / risk_distribution
        #      / conclusion_text 直接位于 data 下），不是嵌套的 conclusion 对象。
        items = data.get("risk_items") or []
        print(f"\n=== 10. 审查结论 /review/{{task_id}}/result === HTTP {status}")
        print(f"  | 是否已判定 = {data.get('rule_evaluated')}")
        print(f"  | 综合风险等级 = {data.get('risk_level')}")
        print(f"  | 是否阻塞 = {data.get('is_blocked')}")
        print(f"  | 等级分布 = {data.get('risk_distribution')}")
        print(f"  | 风险条目 = {len(items)}")
        for it in items[:8]:
            print(f"     · [{it.get('risk_level')}/{it.get('severity')}] {it.get('rule_code') or '-'} | "
                  f"{str(it.get('rule_name') or '')[:30]} | 决策={it.get('decision')}")
        text = (data.get("conclusion_text") or "").strip()
        if text:
            print(f"  | 结论：{text[:200]}")
        if status != 200:
            failures.append("审查结果获取失败")

        # ---------- 8. 报告生成与下载 ----------
        status, body = http(base + f"/api/v1/report/{tid}/generate",
                            {"task_id": tid, "format": "md"}, token=biz_token)
        show("11. 生成报告 /report/{task_id}/generate", status, body, limit=800)

        status, body = http(base + f"/api/v1/report/{tid}/download", token=biz_token)
        print(f"\n=== 12. 下载报告 /report/{{task_id}}/download === HTTP {status} | 报告长度={len(body)}")
        print(body[:1000] + ("..." if len(body) > 1000 else ""))
        if status != 200:
            failures.append("报告下载失败")

    # ---------- 汇总 ----------
    print("\n" + "=" * 66)
    if task_no:
        print(f"本次任务号：{task_no}（task_id={task_id}）")
    if failures:
        print("冒烟结果：存在失败项")
        for f in failures:
            print("  [FAIL]", f)
        return 1
    print("冒烟结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
