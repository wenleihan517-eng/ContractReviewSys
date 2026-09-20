# D:/ContractReviewSys-合同审批智能审查项目/backend/app/api/v1/endpoints/__init__.py
"""
接口端点包入口

说明：
    本包存放各业务模块的接口实现。每个业务域一个文件，路由对象命名统一为 router。
    当前已实现：health（健康检查）。
    后续按模块追加：contract / review / rule_template / report / approval。

返回值说明：
    本模块无函数返回值
"""

from app.api.v1.endpoints import health

__all__ = ["health"]
