# D:/ContractReviewSys-合同审批智能审查项目/backend/app/api/v1/router.py
"""
API v1 路由聚合器

本模块职责：
    1. 将 v1 版本下所有业务路由模块聚合为单一 router
    2. 统一暴露给 main.py 挂载到 API_V1_PREFIX 前缀下
    3. 集中管理路由注册顺序与标签分组

【已注册模块】
    - auth            登录鉴权（登录 / 刷新 / 登出 / 当前用户 / 权限校验）
    - health          系统健康检查（存活 / 就绪 / 配置自检）
    - tasks           任务管理（创建 / 列表 / 详情 / 重试 / 阻塞处理 / 待办）
    - parse           文档解析（上传 / 触发 / 结果查询）
    - review          风险审查（触发 / 结果 / 编辑风险项 / 修改条款）
    - report_comment  报告与评论（生成 / 预览 / 回写 / 下载）
    - rules           规则管理（规则模板 / 示范条款 / 启用禁用）
    - audit           审计日志（任务操作记录查询）

【注册顺序说明】
    auth 必须优先注册：OpenAPI 文档中的接口分组顺序按注册顺序排列，
    登录鉴权作为系统入口放在最前，便于联调时快速定位。
    该顺序不影响路由匹配（各模块 path 前缀互不冲突）。

返回值说明：
    导出 api_router，供 main.py 使用 app.include_router(api_router, prefix=...)
"""

from fastapi import APIRouter

from app.api.v1 import auth
from app.api.v1.endpoints import (
    audit,
    health,
    notifications,
    parse,
    report_comment,
    review,
    rules,
    tasks,
)

# v1 版本路由聚合器
api_router = APIRouter()

# ----------------------------------------------------------------------
# 路由注册区
# 说明：按业务优先级排列，标签在各自模块内已声明
# ----------------------------------------------------------------------
api_router.include_router(auth.router)                   # 认证-登录鉴权
api_router.include_router(health.router)                 # 系统-健康检查
api_router.include_router(tasks.router)                  # 审查任务
api_router.include_router(parse.router)                  # 文档解析
api_router.include_router(review.router)                 # 风险审查
api_router.include_router(report_comment.router)         # 报告与评论
api_router.include_router(rules.router)                  # 规则管理
api_router.include_router(audit.router)                  # 审计日志
api_router.include_router(notifications.router)          # 站内通知（消息中心）
