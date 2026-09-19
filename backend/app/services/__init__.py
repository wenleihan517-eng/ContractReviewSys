# D:/ContractReviewSys-合同审批智能审查项目/backend/app/services/__init__.py
"""
业务服务层入口

【目录职责】
    本层承担业务编排职责，位于接口层（api）与能力层（engines/storage）之间：
        api         → 参数解析、权限校验、响应封装
        services    → 业务流程编排、事务控制、多能力组合
        engines     → 单一能力实现（规则判定、模型调用、向量化）
        storage     → 数据持久化（MongoDB / MinIO / Milvus）

【分层调用规则（强制）】
    1. api 层禁止直接调用 engines 与 storage，必须经过 services 层
    2. services 层禁止直接操作 HTTP 请求/响应对象
    3. engines 层禁止访问数据库，只做纯能力计算
    4. storage 层禁止包含业务逻辑，只做数据存取

【规划中的服务模块】
    - contract_service    合同提交、元数据维护、文件资产管理
    - parse_service       文档解析（文本抽取 / OCR）、条款结构化
    - review_service      审查流程编排、多路召回、质量闭环
    - suggestion_service  条款推荐与修改建议管理
    - report_service      审查报告生成与导出
    - writeback_service   审批评论区回写（受 approval_guard 约束）

返回值说明：
    本模块仅做包声明，无函数返回值
"""

__all__ = []
