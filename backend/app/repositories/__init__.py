# D:/ContractReviewSys-合同审批智能审查项目/backend/app/repositories/__init__.py
"""
数据访问层（Repository）入口

【目录职责】
    本层封装 MongoDB 集合的读写操作，向上层屏蔽数据访问细节：
        - 统一集合名引用（禁止在业务代码中出现集合名字符串）
        - 统一 ObjectId 与字符串 ID 的双向转换
        - 统一分页、排序、投影的实现
        - 统一数据库异常转换为业务异常

【分层价值】
    services 层只关心"取什么数据"，不关心"怎么取"。
    集合名变更、索引调整、查询优化都收敛在本层，不扩散到业务代码。

【规划中的 Repository 模块】
    - base_repository       通用 CRUD / 分页 / 批量操作基类
    - contract_repo         合同主档读写
    - clause_repo           条款读写（含按字符位置反查）
    - task_repo             任务读写（含状态流转、节点历史更新）
    - risk_hit_repo         风险命中读写（按来源过滤）
    - conclusion_repo       结论读写（规则引擎独占写入入口）
    - rule_template_repo    规则模板读写（含版本递增）
    - report_repo           报告记录读写
    - writeback_repo        回写日志读写
    - event_repo            任务事件流读写（供 SSE 推送）

返回值说明：
    本模块仅做包声明，无函数返回值
"""

__all__ = []
