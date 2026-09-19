# D:/ContractReviewSys-合同审批智能审查项目/deploy/scripts/init_all.py
"""
一键初始化 — 按正确顺序完成全部数据层初始化

【脚本职责】
    系统首次部署或重建数据环境时，需要按**严格顺序**完成 4 步初始化。
    顺序错了会失败或产生残缺状态，因此这里固化成一条命令。

        步骤 1  MongoDB 库/集合/索引/角色/系统配置/规则模板
                └─ 必须先做：后续所有服务都依赖业务主数据存储

        步骤 2  Milvus 独立 database + 三类集合
                └─ 必须在向量化之前：没有集合无法写入向量

        步骤 3  规则向量化（把 risk_rules.yaml 的 34 条规则写入 Milvus）
                └─ 依赖步骤 1 的规则文件 + 步骤 2 的集合 + Embedding 服务

        步骤 4  连通性自检
                └─ 全链路验证，确认初始化结果可用

【为什么顺序不能乱】
    - 步骤 3 需要调用 Embedding 服务把规则文本转成 1024 维向量；
      如果 Milvus 集合还没建，向量算出来了也无处可写。
    - 步骤 4 会读取 MongoDB 集合与 Milvus 集合做交叉验证，
      必须放在前两步之后才有意义。

【幂等性保证】
    本脚本调用的三个子脚本自身都是幂等的：
        - init_mongodb.py        —— 集合/索引按需创建，规则按 rule_code 覆盖
        - init_milvus_vectors.py —— 集合按需创建，向量按 id 覆盖
        - system_check.py        —— 纯只读
    因此本脚本可以安全重复执行，不会产生重复数据。

【运行方式】
    cd deploy/scripts

    # 完整初始化（首次部署走这个）
    python init_all.py

    # 只做 MongoDB
    python init_all.py --only mongodb

    # 只做 Milvus 建库建集合
    python init_all.py --only milvus

    # 只做规则向量化
    python init_all.py --only vectors

    # 跳过某一步（如 MongoDB 已经好了）
    python init_all.py --skip mongodb

    # 预览模式：只打印将执行的命令，不真正执行
    python init_all.py --dry-run

    # 重建：先删除再创建（会丢数据，慎用）
    python init_all.py --recreate

【退出码】
    0 = 全部步骤成功
    1 = 存在步骤失败（后续依赖步骤会被跳过）
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ======================================================================
# 一、路径与常量
# ======================================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEPLOY_DIR = os.path.dirname(SCRIPT_DIR)
PROJECT_ROOT = os.path.dirname(DEPLOY_DIR)

BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
BACKEND_SCRIPTS = os.path.join(BACKEND_DIR, "scripts")

IS_WINDOWS = os.name == "nt"


def _python_exe() -> str:
    """
    返回应使用的 Python 解释器

    与 devctl.py 保持一致的解析顺序，保证 init 与 start 用的是同一套环境。
    混合环境是常见事故来源（初始化用一个 Python，启动用另一个，
    结果一个装了依赖一个没装）。
    """
    candidates = [
        os.path.join(BACKEND_DIR, ".venv", "Scripts", "python.exe"),
        os.path.join(BACKEND_DIR, ".venv", "bin", "python"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    if sys.executable:
        return sys.executable
    return "python"


# ======================================================================
# 二、初始化步骤定义
# ======================================================================
# 每个步骤：
#   key        —— 步骤标识（供 --only / --skip 使用）
#   label      —— 展示名
#   script     —— 相对 backend/scripts 的脚本文件名
#   args       —— 基础参数
#   extra_args —— 可被 --recreate 追加的参数
#   requires   —— 前置依赖的步骤 key 列表
#   purpose    —— 目的说明（打印给用户看）
#   check      —— 输出中用于判定成功的标记文本（可空）

STEPS: List[Dict[str, Any]] = [
    {
        "key": "mongodb",
        "label": "MongoDB 初始化",
        "script": "init_mongodb.py",
        "args": ["--init-roles"],
        "extra_args": [],
        "requires": [],
        "purpose": "创建库、17 个业务集合、全部索引、最小权限角色、系统配置与规则模板",
        "check": "",
    },
    {
        "key": "milvus",
        "label": "Milvus 建库建集合",
        "script": "init_milvus_vectors.py",
        "args": ["--skip-rules"],
        "extra_args": [],
        "requires": [],
        "purpose": "创建独立 database contract_review 及 cr_clauses / cr_standard_clause_lib / cr_rule_templates 三个集合",
        "check": "",
    },
    {
        "key": "vectors",
        "label": "规则向量化写入",
        "script": "init_milvus_vectors.py",
        "args": [],
        "extra_args": [],
        "requires": ["milvus"],
        "purpose": "把 risk_rules.yaml 的 34 条风险规则批量向量化（BGE-M3 / 1024 维）写入 Milvus",
        "check": "",
    },
    {
        "key": "check",
        "label": "全链路自检",
        "script": "system_check.py",
        "args": [],
        "extra_args": [],
        "requires": [],
        "purpose": "逐组件验证连通性、集合完整性、规则可加载，输出分级诊断报告",
        "check": "",
    },
]

STEP_INDEX: Dict[str, Dict[str, Any]] = {s["key"]: s for s in STEPS}


# ======================================================================
# 三、输出工具
# ======================================================================

def _banner() -> None:
    """打印脚本头"""
    print()
    print("=" * 74)
    print("  合同审批智能审查系统 — 一键初始化")
    print("=" * 74)
    print(f"  执行时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"  项目根目录：{PROJECT_ROOT}")
    print(f"  Python 解释器：{_python_exe()}")
    print("=" * 74)


def _section(step: Dict[str, Any], index: int, total: int) -> None:
    """打印步骤头"""
    print()
    print("=" * 74)
    print(f"  步骤 {index}/{total}：{step['label']}")
    print("=" * 74)
    print(f"  目的：{step['purpose']}")


def _run_script(
    step: Dict[str, Any],
    extra_args: List[str],
    dry_run: bool,
) -> Tuple[bool, str]:
    """
    执行一个初始化步骤

    参数：
        step       —— 步骤定义
        extra_args —— 额外参数（如 --recreate 展开后的参数）
        dry_run    —— True 表示只打印命令不执行

    返回：
        (是否成功, 失败原因摘要)
    """
    script_path = os.path.join(BACKEND_SCRIPTS, step["script"])

    if not os.path.isfile(script_path):
        return False, f"脚本不存在：{script_path}"

    cmd = [_python_exe(), step["script"]] + step["args"] + extra_args

    print()
    print(f"  执行命令：{' '.join(cmd)}")
    print(f"  工作目录：{BACKEND_SCRIPTS}")
    print()

    if dry_run:
        print("  [DRY-RUN] 已跳过实际执行。")
        return True, ""

    started = time.perf_counter()

    try:
        # 不捕获输出：让子脚本的彩色/分级报告直接透传到终端，
        # 用户能看到每一步的详细结果（这正是这些脚本的价值所在）。
        completed = subprocess.run(
            cmd,
            cwd=BACKEND_SCRIPTS,
            check=False,
        )
    except FileNotFoundError as exc:
        return False, f"解释器不可执行：{exc}"
    except KeyboardInterrupt:
        print("\n  用户中断。")
        raise
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"

    elapsed = time.perf_counter() - started
    print()
    print(f"  耗时 {elapsed:.1f}s，退出码 {completed.returncode}")

    if completed.returncode != 0:
        return False, f"退出码 {completed.returncode}（详见上方输出）"

    return True, ""


def _resolve_recreate_args(step: Dict[str, Any], recreate: bool) -> List[str]:
    """
    计算 --recreate 时应追加的参数

    说明：
        不同子脚本的"重建"开关名不同：
            init_mongodb.py        → --drop-all
            init_milvus_vectors.py → --drop-collections
        这里统一映射，避免用户记忆差异。

    参数：
        step     —— 步骤定义
        recreate —— 是否处于重建模式

    返回：
        追加参数列表（可能为空）
    """
    if not recreate:
        return []

    mapping = {
        "mongodb": ["--drop-all"],
        "milvus": ["--drop-collections"],
        # vectors 步骤在重建模式下由 milvus 步骤已删集合，
        # 这里只需重新写入即可，无需额外参数
        "vectors": [],
        "check": [],
    }
    return mapping.get(step["key"], [])


# ======================================================================
# 四、主流程
# ======================================================================

def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    all_keys = [s["key"] for s in STEPS]

    parser = argparse.ArgumentParser(
        prog="init_all",
        description="合同审批智能审查系统 — 一键初始化（MongoDB → Milvus → 向量 → 自检）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python init_all.py                     # 完整初始化\n"
            "  python init_all.py --only mongodb      # 只初始化 MongoDB\n"
            "  python init_all.py --skip vectors      # 跳过向量化\n"
            "  python init_all.py --dry-run           # 预览将执行的命令\n"
            "  python init_all.py --recreate          # 删除并重建（会丢数据）\n"
            f"\n可选步骤：{', '.join(all_keys)}\n"
        ),
    )

    parser.add_argument(
        "--only",
        nargs="+",
        choices=all_keys,
        metavar="STEP",
        help=f"只执行指定步骤（{', '.join(all_keys)}）",
    )
    parser.add_argument(
        "--skip",
        nargs="+",
        choices=all_keys,
        metavar="STEP",
        help="跳过指定步骤",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将执行的命令，不真正执行",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="删除已有集合与数据后重建（危险：会丢失全部业务数据）",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="某步失败后继续执行后续步骤（默认失败即停止）",
    )

    return parser.parse_args()


def main() -> int:
    """命令行入口"""
    args = parse_args()

    _banner()

    # --- 重建模式需二次确认 ---
    if args.recreate and not args.dry_run:
        print()
        print("  ⚠️  重建模式将删除以下数据：")
        print("      · MongoDB contract_review 库的全部业务集合与文档")
        print("      · Milvus contract_review 库的三个向量集合及全部向量")
        print("      此操作不可恢复！")
        print()
        try:
            answer = input("  确认继续？输入 yes 以继续：").strip().lower()
        except EOFError:
            # 非交互环境（CI / 重定向输入）下无法确认，
            # 出于安全考虑按"取消"处理，而不是默默执行破坏性操作。
            print("  （非交互环境无法确认，出于安全考虑已取消）")
            print("  如需在脚本中执行重建，请改用 --dry-run 预览后人工执行。")
            return 1
        if answer != "yes":
            print("  已取消。")
            return 1

    # --- 计算待执行步骤 ---
    if args.only:
        selected = [s for s in STEPS if s["key"] in args.only]
    else:
        selected = list(STEPS)

    if args.skip:
        selected = [s for s in selected if s["key"] not in args.skip]

    if not selected:
        print("\n  没有需要执行的步骤（已被 --only/--skip 全部过滤）。")
        return 0

    # --- 打印执行计划 ---
    print()
    print("-- 执行计划 " + "-" * 62)
    for idx, step in enumerate(selected, start=1):
        extra = _resolve_recreate_args(step, args.recreate)
        suffix = f"  {' '.join(extra)}" if extra else ""
        print(f"  {idx}. [{step['key']:<8}] {step['label']}{suffix}")
        print(f"      {step['purpose']}")
    print("-" * 74)

    # --- 逐步执行 ---
    total = len(selected)
    completed_keys: List[str] = []
    failures: List[Tuple[str, str]] = []
    all_keys = [s["key"] for s in STEPS]

    for idx, step in enumerate(selected, start=1):
        key = step["key"]

        # 检查前置依赖（只在本轮执行范围内校验）
        missing = [
            dep
            for dep in step["requires"]
            if dep in all_keys
            and dep not in completed_keys
            and any(s["key"] == dep for s in selected)
        ]
        if missing:
            print()
            print(f"  步骤 {idx}/{total}：{step['label']}")
            print(f"  [SKIP] 前置步骤未成功完成：{', '.join(missing)}")
            failures.append((key, f"前置步骤未完成：{', '.join(missing)}"))
            if not args.continue_on_error:
                break
            continue

        _section(step, idx, total)

        extra = _resolve_recreate_args(step, args.recreate)
        ok, reason = _run_script(step, extra, args.dry_run)

        if ok:
            completed_keys.append(key)
            print(f"  [ OK ] {step['label']} 完成")
            continue

        failures.append((key, reason))
        print(f"  [FAIL] {step['label']} 失败：{reason}")

        if not args.continue_on_error:
            print()
            print("  已停止后续步骤（依赖链已断）。")
            print("  修复后可单独重跑失败步骤，例如：")
            print(f"      python init_all.py --only {key}")
            break

    # --- 汇总 ---
    print()
    print("=" * 74)
    print("  初始化结果汇总")
    print("=" * 74)

    for step in selected:
        key = step["key"]
        if key in completed_keys:
            mark = "成功"
        elif any(k == key for k, _ in failures):
            mark = "失败"
        else:
            mark = "未执行"
        print(f"  {step['label']:<20} {mark}")

    print("-" * 74)

    if failures:
        print(f"  {len(failures)} 个步骤失败：")
        for key, reason in failures:
            print(f"    · [{key}] {reason}")
        print()
        print("  排查建议：")
        print("    1. 先确认基础设施可达：python devctl.py probe")
        print("    2. 查看详细诊断：cd backend && python scripts/system_check.py")
        print("    3. 修复后重跑本脚本（幂等，可安全重复执行）")
        return 1

    print("  全部步骤执行成功，系统已就绪。")
    print()
    print("  下一步：")
    print("    启动服务：python devctl.py start")
    print("    访问界面：http://127.0.0.1:5173")
    return 0


if __name__ == "__main__":
    sys.exit(main())
