# D:/ContractReviewSys-合同审批智能审查项目/backend/app/__init__.py
"""
应用包入口

说明：
    声明 app 为 Python 包，并暴露应用版本信息。
    不在此处 import main，避免循环导入与副作用。

返回值说明：
    本模块仅定义包级元信息，无函数返回值
"""

__all__ = ["__version__", "__app_name__"]

# 与 app/core/config.py 中的默认版本保持一致，供包外部快速读取
__version__ = "1.0.0"
__app_name__ = "合同审批智能审查系统"
