# D:/ContractReviewSys-合同审批智能审查项目/backend/app/schemas/task.py
"""
任务管理接口契约模型

【目录职责】
    定义"任务管理"接口的请求与响应模型，与 models/documents/review_task.py
    的存储模型严格区分：本模块只暴露前端需要的字段，不泄露
    thread_id / work_dir 等内部实现细节。

使用示例：
    from app.schemas.task import TaskCreateRequest, TaskDetailResponse
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field



# ======================================================================
# 一、任务创建
# ======================================================================
class TaskCreateRequest(BaseModel):
    """
    创建审查任务请求

    字段：
        contract_id: 合同主档 ID
        contract_title: 合同标题
        contract_no: 合同编号
        submit_dept: 送审部门
        applicant: 申请人
        counterparty: 合同相对方
        contract_amount: 合同金额（元）
        currency: 币种
        performance_period: 履约周期描述
        business_type: 业务类型
        contract_type: 合同类型（purchase/sales/service/labor 等）
        source_type: 来源类型（digital/scanned）
        file_object_key: 合同原文在 MinIO 中的对象键（可选，上传后回填）
    """

    contract_id: str = Field(description="合同主档 ID")
    contract_title: str = Field(default="", description="合同标题")
    contract_no: str = Field(default="", description="合同编号")
    submit_dept: str = Field(default="", description="送审部门")
    applicant: str = Field(default="", description="申请人")
    counterparty: str = Field(default="", description="合同相对方")
    contract_amount: float = Field(default=0.0, ge=0.0, description="合同金额（元）")
    currency: str = Field(default="CNY", description="币种")
    performance_period: str = Field(default="", description="履约周期")
    business_type: str = Field(default="", description="业务类型")
    contract_type: str = Field(default="other", description="合同类型")
    source_type: str = Field(default="digital", description="来源类型")
    file_object_key: str = Field(default="", description="MinIO 对象键")


class TaskCreateResponse(BaseModel):
    """
    创建任务响应

    字段：
        task_id: 任务 ID
        task_no: 任务编号
    """

    task_id: str = Field(description="任务 ID")
    task_no: str = Field(description="任务编号")


# ======================================================================
# 二、任务列表与详情
# ======================================================================
class TaskListItem(BaseModel):
    """
    任务列表项（精简）

    字段：
        task_id: 任务 ID
        task_no: 任务编号
        contract_title: 合同标题
        status: 任务状态
        route_type: 路由类型
        progress: 进度百分比
        blocked_reason: 阻塞原因（仅 blocked 有值）
        created_at: 创建时间
    """

    task_id: str = Field(description="任务 ID")
    task_no: str = Field(description="任务编号")
    contract_title: str = Field(default="", description="合同标题")
    status: str = Field(description="任务状态")
    route_type: str = Field(default="", description="路由类型")
    progress: int = Field(default=0, description="进度百分比")
    blocked_reason: Optional[str] = Field(default=None, description="阻塞原因")
    created_at: Optional[datetime] = Field(default=None, description="创建时间")
    # 失败分类（2026-09-20 交互优化：失败行标红悬浮 + 异常筛选）
    failure_type: Optional[str] = Field(default=None, description="失败类型（FailureType）")
    failure_reason: Optional[str] = Field(default=None, description="失败原因摘要（脱敏）")
    created_by: str = Field(default="", description="提交经办人用户名（重新上传权限判定）")


class TaskDetailResponse(BaseModel):
    """
    任务详情响应

    字段：
        task_id / task_no / contract_id：任务与合同标识
        contract_title / contract_no：合同标题与编号
        status / route_type / progress：状态机与进度
        current_node：当前执行节点
        blocked_reason / blocked_detail：阻塞信息
        retry_count / max_retry：重试计数
        write_back_status：回写状态
        risk_level：综合风险等级（规则引擎判定）
        is_blocked：是否阻塞
        created_at / updated_at：时间戳
    """

    task_id: str = Field(description="任务 ID")
    task_no: str = Field(description="任务编号")
    contract_id: str = Field(default="", description="合同 ID")
    contract_title: str = Field(default="", description="合同标题")
    contract_no: str = Field(default="", description="合同编号")
    status: str = Field(description="任务状态")
    route_type: str = Field(default="", description="路由类型")
    progress: int = Field(default=0, description="进度")
    current_node: Optional[str] = Field(default=None, description="当前节点")
    blocked_reason: Optional[str] = Field(default=None, description="阻塞原因")
    blocked_detail: Optional[str] = Field(default=None, description="阻塞详情")
    retry_count: int = Field(default=0, description="重试次数")
    max_retry: int = Field(default=3, description="重试上限")
    write_back_status: str = Field(default="not_written", description="回写状态")
    risk_level: Optional[str] = Field(default=None, description="综合风险等级")
    is_blocked: bool = Field(default=False, description="是否阻塞")
    created_at: Optional[datetime] = Field(default=None, description="创建时间")
    updated_at: Optional[datetime] = Field(default=None, description="更新时间")
    failure_type: Optional[str] = Field(default=None, description="失败类型（FailureType）")
    failure_reason: Optional[str] = Field(default=None, description="失败原因摘要（脱敏）")
    created_by: str = Field(default="", description="提交经办人用户名")


# ======================================================================
# 三、任务重试与阻塞处理
# ======================================================================
class TaskRetryRequest(BaseModel):
    """
    任务重试请求

    字段：
        task_no: 任务编号
    """

    task_no: str = Field(description="任务编号")


class TaskRetryResponse(BaseModel):
    """
    任务重试响应
    """

    task_no: str = Field(description="任务编号")
    resumed: bool = Field(default=False, description="是否已恢复执行")
    message: str = Field(default="", description="结果说明")


class BlockedTaskResolveRequest(BaseModel):
    """
    处理阻塞任务请求

    字段：
        task_no: 任务编号
        action: 处理动作（retry=重跑 / dismiss=驳回/忽略）
        reason: 处理说明（可选）
    """

    task_no: str = Field(description="任务编号")
    action: str = Field(default="retry", description="处理动作：retry / dismiss")
    reason: str = Field(default="", description="处理说明")


class BlockedTaskResolveResponse(BaseModel):
    """
    处理阻塞任务响应
    """

    task_no: str = Field(description="任务编号")
    action: str = Field(description="执行的动作")
    message: str = Field(default="", description="结果说明")


# ======================================================================
# 四、审批待办
# ======================================================================
class ApprovalPendingItem(BaseModel):
    """
    审批待办项

    字段：
        task_no: 任务编号
        contract_title: 合同标题
        risk_level: 综合风险等级
        conclusion_text: 审查结论
        write_back_status: 回写状态
    """

    task_no: str = Field(description="任务编号")
    contract_title: str = Field(default="", description="合同标题")
    risk_level: Optional[str] = Field(default=None, description="综合风险等级")
    conclusion_text: str = Field(default="", description="审查结论")
    write_back_status: str = Field(default="", description="回写状态")
