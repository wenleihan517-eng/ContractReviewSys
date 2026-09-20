# D:/ContractReviewSys-合同审批智能审查项目/backend/app/models/documents/contract.py
"""
合同主档文档模型 — 对应 MongoDB 集合 cr_contract

【模型定位】
    承接入流转模块，是合同全生命周期的元数据载体。
    路由决策所需的三个关键输入均在此模型内：
        template_flag（是否模板合同）→ 决定是否走快速通道
        amount（合同金额）           → 决定是否走深度复核
        contract_type（合同类型）    → 影响规则集选择

返回值说明：
    本模块只定义数据模型，无函数返回值

使用示例：
    from app.models.documents.contract import Contract
    contract = Contract(contract_no="HT20260918001", contract_name="设备采购合同")
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.constants import ContractStatus, FileSourceType

# 需要强制深度复核的合同类型（高风险类型）
HIGH_RISK_CONTRACT_TYPES = frozenset(
    {
        "foreign_cooperation",  # 涉外合作
        "equity_transfer",      # 股权转让
        "guarantee",            # 担保合同
        "loan",                 # 借款合同
    }
)


class PartyInfo(BaseModel):
    """
    合同主体信息

    字段：
        party_name: 主体名称
        credit_code: 统一社会信用代码（企业）
        legal_person: 法定代表人
        contact: 联系方式
        address: 地址
    """

    party_name: str = Field(description="主体名称")
    credit_code: Optional[str] = Field(default=None, description="统一社会信用代码")
    legal_person: Optional[str] = Field(default=None, description="法定代表人")
    contact: Optional[str] = Field(default=None, description="联系方式")
    address: Optional[str] = Field(default=None, description="地址")


class Contract(BaseModel):
    """
    合同主档模型

    字段：
        id: 合同主键
        contract_no: 合同编号（唯一）
        contract_name: 合同名称
        contract_type: 合同类型
        template_flag: 是否模板合同（路由快速通道依据）
        amount: 合同金额（元），路由深度复核依据
        currency: 币种，默认 CNY
        party_a: 甲方信息
        party_b: 乙方信息
        sign_date: 签订日期
        effective_date: 生效日期
        expire_date: 到期日期
        source_type: 来源类型 digital / scanned
        file_object_id: 关联文件对象 ID
        status: 合同状态
        route_type: 路由结果
        created_by: 提交人
        created_at / updated_at: 时间戳
    """

    id: Optional[str] = Field(default=None, description="合同主键")
    contract_no: str = Field(description="合同编号（唯一）")
    contract_name: str = Field(description="合同名称")
    contract_type: str = Field(default="other", description="合同类型")

    # ---------------------- 路由决策依据 ----------------------
    template_flag: bool = Field(
        default=False,
        description="是否模板合同。为 True 且无自定义条款时走快速通道",
    )
    amount: float = Field(
        default=0.0,
        ge=0.0,
        description="合同金额（元）。达到大额阈值时走深度复核通道",
    )
    currency: str = Field(default="CNY", description="币种，默认人民币")

    # ---------------------- 主体信息 ----------------------
    party_a: Optional[PartyInfo] = Field(default=None, description="甲方信息")
    party_b: Optional[PartyInfo] = Field(default=None, description="乙方信息")

    # ---------------------- 关键日期 ----------------------
    sign_date: Optional[datetime] = Field(default=None, description="签订日期")
    effective_date: Optional[datetime] = Field(default=None, description="生效日期")
    expire_date: Optional[datetime] = Field(default=None, description="到期日期")

    # ---------------------- 文件与状态 ----------------------
    source_type: str = Field(
        default=FileSourceType.DIGITAL.value,
        description="合同来源类型：digital（电子）/ scanned（扫描件）",
    )
    file_object_id: Optional[str] = Field(
        default=None, description="关联 cr_file_object 的文件对象 ID"
    )
    file_hash: Optional[str] = Field(
        default=None, description="文件内容哈希，用于重复上传检测"
    )
    status: str = Field(
        default=ContractStatus.UPLOADED.value,
        description="合同状态，取值见 ContractStatus 枚举",
    )
    route_type: Optional[str] = Field(
        default=None, description="路由结果：fast/standard/deep"
    )

    # ---------------------- 人员与时间 ----------------------
    created_by: str = Field(default="", description="提交人")
    created_at: datetime = Field(default_factory=datetime.now, description="创建时间")
    updated_at: datetime = Field(default_factory=datetime.now, description="更新时间")

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        """
        校验合同状态合法性

        参数：
            value: 状态字符串
        返回：
            校验通过的状态值
        异常：
            ValueError：取值不在 ContractStatus 枚举内时抛出
        """
        allowed = {item.value for item in ContractStatus}
        if value not in allowed:
            raise ValueError(f"合同状态非法：{value}，允许值：{sorted(allowed)}")
        return value

    @field_validator("source_type")
    @classmethod
    def validate_source_type(cls, value: str) -> str:
        """
        校验来源类型合法性

        参数：
            value: 来源类型字符串
        返回：
            校验通过的来源类型
        异常：
            ValueError：取值不在 FileSourceType 枚举内时抛出
        """
        allowed = {item.value for item in FileSourceType}
        if value not in allowed:
            raise ValueError(f"来源类型非法：{value}，允许值：{sorted(allowed)}")
        return value

    @field_validator("contract_no")
    @classmethod
    def validate_contract_no(cls, value: str) -> str:
        """
        校验合同编号非空且去除首尾空格

        参数：
            value: 合同编号
        返回：
            清洗后的合同编号
        异常：
            ValueError：编号为空时抛出
        """
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("合同编号不能为空")
        return cleaned

    @property
    def is_template(self) -> bool:
        """
        是否模板合同

        返回：
            True 表示模板合同，路由决策时优先考虑快速通道
        """
        return self.template_flag

    @property
    def is_high_risk_type(self) -> bool:
        """
        是否为高风险合同类型

        返回：
            True 表示该类型合同强制走深度复核通道
        """
        return self.contract_type in HIGH_RISK_CONTRACT_TYPES

    def requires_ocr(self) -> bool:
        """
        判断是否需要走 OCR 链路

        返回：
            True 表示扫描件，必须 OCR；False 表示电子合同，可直接抽文本
        """
        return self.source_type == FileSourceType.SCANNED.value
