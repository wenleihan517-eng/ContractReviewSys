# D:/ContractReviewSys-合同审批智能审查项目/backend/app/schemas/__init__.py
"""
接口契约层（Schema）入口

【目录职责】
    本层存放"接口出入参"契约模型，与 models/documents 的"数据存储模型"严格区分：
        - models/  数据存储模型，字段与 MongoDB 集合一一对应，含内部字段
        - schemas/ 接口契约模型，面向前端，只暴露必要字段，不泄露实现细节

【已实现的 Schema 模块】
    - common   分页参数、通用 ID 响应
    - task     任务管理（创建/列表/详情/重试/阻塞处理/待办）
    - parse    文档解析（触发/结果查询）
    - review   风险审查（触发/结果/编辑风险项/修改条款）
    - report   报告与评论（生成/预览/回写/下载）
    - rule     规则管理（规则模板/示范条款/启用禁用）
    - audit    审计日志查询

返回值说明：
    本模块仅做聚合导出，无函数返回值
"""

from app.schemas import audit, common, parse, report, review, rule, task

__all__ = [
    "common",
    "task",
    "parse",
    "review",
    "report",
    "rule",
    "audit",
]
