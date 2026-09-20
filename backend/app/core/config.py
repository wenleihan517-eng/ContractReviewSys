# D:/ContractReviewSys-合同审批智能审查项目/backend/app/core/config.py
"""
配置中心 — 全局唯一配置入口

本模块职责：
    1. 集中管理项目所有可配置项（应用 / 服务 / MongoDB / MinIO / Milvus / Embedding / LLM / 规则引擎）
    2. 支持多环境切换（dev / test / prod），通过环境变量 APP_ENV 指定，默认 dev
    3. 提供全局单例 settings 与 @lru_cache 缓存，避免重复加载
    4. 严禁在业务代码中硬编码任何连接串、密钥、端口，一律从本模块读取

设计要点：
    - 基于 pydantic-settings，自动读取 .env 文件 + 系统环境变量
    - 环境变量优先级：系统环境变量 > .env 文件 > 代码默认值
    - 使用 Field 的 description 标注每一项含义，便于后续维护

返回值说明：
    - settings：Settings 实例（全局唯一，导入即用）
    - get_settings()：带缓存的获取函数，供 FastAPI 依赖注入使用

使用示例：
    from app.core.config import settings
    print(settings.MILVUS_HOST)
"""

from functools import lru_cache
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    全局配置类

    所有配置项按业务分组，组内使用注释分隔，便于快速检索。
    字段命名统一使用大写下划线风格，与 .env 文件中的键名一一对应。
    """

    # ------------------------------------------------------------------
    # 一、基础应用配置
    # ------------------------------------------------------------------
    APP_NAME: str = Field(
        default="合同审批智能审查系统",
        description="应用名称，用于日志、接口文档标题、健康检查返回",
    )
    APP_VERSION: str = Field(
        default="1.0.0",
        description="应用版本号，遵循语义化版本规范",
    )
    APP_ENV: str = Field(
        default="dev",
        description="运行环境，可选值：dev（开发）/ test（测试）/ prod（生产）",
    )
    APP_DEBUG: bool = Field(
        default=True,
        description="是否开启调试模式。生产环境必须置为 False，以关闭详细报错堆栈",
    )
    API_V1_PREFIX: str = Field(
        default="/api/v1",
        description="后端接口统一前缀，所有 v1 路由均挂载在此前缀下",
    )

    # ------------------------------------------------------------------
    # 二、服务监听配置
    # ------------------------------------------------------------------
    SERVER_HOST: str = Field(
        default="0.0.0.0",
        description="服务监听地址。0.0.0.0 表示监听全部网卡，便于内网访问",
    )
    SERVER_PORT: int = Field(
        default=8000,
        description="服务监听端口，默认 8000",
    )
    SERVER_WORKERS: int = Field(
        default=4,
        description="生产环境 Uvicorn 工作进程数，开发环境不使用该值",
    )
    SERVER_RELOAD: bool = Field(
        default=True,
        description="代码热重载开关，仅开发环境开启，生产环境必须关闭",
    )

    # ------------------------------------------------------------------
    # 三、跨域配置（前后端分离必需）
    # ------------------------------------------------------------------
    CORS_ORIGINS: List[str] = Field(
        default=["http://localhost:5173", "http://127.0.0.1:5173"],
        description="允许跨域访问的前端源列表。Vite 默认端口为 5173",
    )
    CORS_ALLOW_CREDENTIALS: bool = Field(
        default=True,
        description="是否允许跨域请求携带凭证（Cookie / Authorization 头）",
    )
    CORS_ALLOW_METHODS: List[str] = Field(
        default=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        description="允许的跨域 HTTP 方法列表",
    )
    CORS_ALLOW_HEADERS: List[str] = Field(
        default=["Authorization", "Content-Type", "X-Request-Id", "Accept"],
        description="允许的跨域请求头列表",
    )

    # ------------------------------------------------------------------
    # 四、MongoDB 配置（业务主数据 / 审查记录 / 规则模板）
    # ------------------------------------------------------------------
    MONGO_URI: str = Field(
        default="mongodb://127.0.0.1:27017",
        description="MongoDB 连接串。生产环境如启用鉴权，需携带账号密码",
    )
    MONGO_DB_NAME: str = Field(
        default="contract_review",
        description="本项目专用数据库名，与其它项目物理隔离",
    )
    MONGO_MAX_POOL_SIZE: int = Field(
        default=50,
        description="MongoDB 连接池最大连接数",
    )
    MONGO_MIN_POOL_SIZE: int = Field(
        default=5,
        description="MongoDB 连接池最小空闲连接数",
    )
    MONGO_SERVER_SELECTION_TIMEOUT_MS: int = Field(
        default=5000,
        description="MongoDB 选主超时时间（毫秒），超时抛出配置异常",
    )

    # ------------------------------------------------------------------
    # 五、MinIO 配置（合同原文 / OCR 图片 / 导出报告对象存储）
    # ------------------------------------------------------------------
    MINIO_ENDPOINT: str = Field(
        default="127.0.0.1:9000",
        description="MinIO 服务地址，格式为 host:port，不带协议头",
    )
    MINIO_ACCESS_KEY: str = Field(
        default="minioadmin",
        description="MinIO 访问密钥（Access Key），生产环境务必修改",
    )
    MINIO_SECRET_KEY: str = Field(
        default="minioadmin",
        description="MinIO 私密密钥（Secret Key），生产环境务必修改",
    )
    MINIO_SECURE: bool = Field(
        default=False,
        description="是否启用 HTTPS 访问 MinIO。内网默认 False",
    )
    MINIO_BUCKET_CONTRACT: str = Field(
        default="contract-origin",
        description="合同原文存储桶，存放上传的 PDF / Word 原文件",
    )
    MINIO_BUCKET_OCR_IMAGE: str = Field(
        default="contract-ocr-image",
        description="OCR 图片存储桶，存放扫描件切分后的页面图片",
    )
    MINIO_BUCKET_REPORT: str = Field(
        default="contract-report",
        description="审查报告存储桶，存放导出的 Word / PDF 报告",
    )
    MINIO_BUCKET_CLAUSE_LIB: str = Field(
        default="contract-clause-lib",
        description="标准条款库存储桶，存放条款范本附件",
    )
    MINIO_BUCKET_TMP: str = Field(
        default="contract-tmp",
        description="任务临时文件存储桶，按任务 ID 划分目录实现隔离",
    )

    # ------------------------------------------------------------------
    # 六、Milvus 配置（条款向量检索）
    # 说明：复用现有 Milvus 实例，但使用独立 database + collection 做隔离
    # ------------------------------------------------------------------
    MILVUS_HOST: str = Field(
        default="192.168.10.130",
        description="Milvus 服务主机地址（复用现有实例，不新建服务）",
    )
    MILVUS_PORT: int = Field(
        default=7000,
        description="Milvus 服务端口，默认 7000",
    )
    MILVUS_DB_NAME: str = Field(
        default="contract_review",
        description=(
            "Milvus 数据库名。本项目独立库，严禁使用 default 库，"
            "避免与其它项目（如 RAG 知识库）的集合互相污染"
        ),
    )
    MILVUS_COLLECTION_CLAUSE: str = Field(
        default="cr_clauses",
        description="条款向量集合名，cr_ 前缀标识本合同审查项目",
    )
    MILVUS_COLLECTION_RULE_TEMPLATE: str = Field(
        default="cr_rule_templates",
        description="规则模板向量集合名，用于模板语义召回",
    )
    MILVUS_COLLECTION_STANDARD_CLAUSE: str = Field(
        default="cr_standard_clause_lib",
        description="标准条款库向量集合名，条款推荐模块的数据源",
    )
    MILVUS_INDEX_TYPE: str = Field(
        default="HNSW",
        description="向量索引类型。HNSW 检索性能好，适合中小规模条款库",
    )
    MILVUS_METRIC_TYPE: str = Field(
        default="COSINE",
        description="向量相似度度量方式。BGE-M3 归一化后推荐 COSINE",
    )
    MILVUS_INDEX_PARAMS_M: int = Field(
        default=16,
        description="HNSW 索引参数 M，控制图中每个节点的最大出边数",
    )
    MILVUS_INDEX_PARAMS_EF_CONSTRUCTION: int = Field(
        default=200,
        description="HNSW 索引参数 efConstruction，建索引时的搜索范围",
    )
    MILVUS_SEARCH_PARAMS_EF: int = Field(
        default=128,
        description="HNSW 检索参数 ef，值越大召回越高但越慢",
    )
    MILVUS_SEARCH_TOP_K: int = Field(
        default=5,
        ge=1,
        le=50,
        description=(
            "向量检索返回的最大候选条数。条款相似度对照场景下 5 条足以覆盖，"
            "过大会引入大量噪声候选，影响人工复核效率"
        ),
    )
    MILVUS_TIMEOUT_SECONDS: float = Field(
        default=10.0,
        description="Milvus 连接与调用超时时间（秒）",
    )

    # ------------------------------------------------------------------
    # 七、Embedding（BGE-M3）配置
    # 说明：本地部署的向量化服务，内网调用，禁止走公网
    # ------------------------------------------------------------------
    EMBEDDING_BASE_URL: str = Field(
        default="http://127.0.0.1:9997/v1",
        description="BGE-M3 向量化服务基础地址（OpenAI 兼容风格 /v1）",
    )
    EMBEDDING_MODEL_NAME: str = Field(
        default="bge-m3",
        description="向量化模型名称，固定为 BGE-M3",
    )
    EMBEDDING_DIM: int = Field(
        default=1024,
        description="BGE-M3 向量维度，固定 1024，与 Milvus 集合 schema 必须一致",
    )
    EMBEDDING_BATCH_SIZE: int = Field(
        default=16,
        description="单次批量向量化的文本条数，过大易触发服务端超时",
    )
    EMBEDDING_TIMEOUT_SECONDS: float = Field(
        default=30.0,
        description="向量化接口调用超时时间（秒）",
    )
    EMBEDDING_MAX_RETRIES: int = Field(
        default=3,
        description="向量化接口失败重试次数",
    )

    # ------------------------------------------------------------------
    # 八、LLM 配置（本地部署，OpenAI 兼容协议）
    # 说明：仅承担辅助能力，不下发最终判定结论
    # ------------------------------------------------------------------
    LLM_BASE_URL: str = Field(
        default="http://127.0.0.1:8001/v1",
        description=(
            "本地大模型服务基础地址（OpenAI 兼容协议）。"
            "后续更换底层模型仅需修改此配置，无需改动业务代码"
        ),
    )
    LLM_API_KEY: str = Field(
        default="EMPTY",
        description="本地模型服务密钥。vLLM 等本地部署常为占位值 EMPTY",
    )
    LLM_MODEL_NAME: str = Field(
        default="qwen2.5-14b-instruct",
        description="调用的本地模型名称，需与本地服务实际暴露的模型名一致",
    )
    LLM_TEMPERATURE: float = Field(
        default=0.1,
        description="采样温度。合同场景要求输出稳定可复现，固定使用低温度值，禁止调高",
    )
    LLM_MAX_TOKENS: int = Field(
        default=4096,
        description="单次生成最大 token 数，需与本地模型上下文窗口匹配",
    )
    LLM_TIMEOUT_SECONDS: float = Field(
        default=120.0,
        description="LLM 调用超时时间（秒）。长条款解析耗时较长，需留足余量",
    )
    LLM_MAX_RETRIES: int = Field(
        default=2,
        description="LLM 调用失败重试次数",
    )

    # ------------------------------------------------------------------
    # 九、文件上传配置
    # ------------------------------------------------------------------
    UPLOAD_MAX_SIZE_MB: int = Field(
        default=100,
        description="单文件上传大小上限（MB），超出直接拒绝",
    )
    UPLOAD_ALLOWED_EXTENSIONS: List[str] = Field(
        default=[".pdf", ".doc", ".docx", ".png", ".jpg", ".jpeg", ".tif", ".tiff"],
        description="允许上传的文件扩展名，覆盖电子合同与扫描件两类",
    )

    # ------------------------------------------------------------------
    # 十、OCR 配置
    # ------------------------------------------------------------------
    OCR_ENGINE: str = Field(
        default="paddleocr",
        description="OCR 引擎名称。默认 PaddleOCR，本地离线运行，不出内网",
    )
    OCR_LANG: str = Field(
        default="ch",
        description="OCR 识别语言，ch 表示中英文混合识别",
    )
    OCR_DET_IMAGE_MAX_SIDE: int = Field(
        default=2000,
        description="OCR 检测阶段图像最长边像素限制，过大影响速度",
    )
    OCR_MIN_CONFIDENCE_THRESHOLD: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description=(
            "OCR 平均置信度阈值。低于该值判定扫描件模糊，"
            "任务进入 blocked(SCAN_TOO_BLURRY) 等待人工重传"
        ),
    )
    MIN_CONTENT_LENGTH: int = Field(
        default=50,
        ge=0,
        description=(
            "抽取正文字符数下限。低于该值判定正文为空，"
            "任务进入 blocked(EMPTY_CONTENT)"
        ),
    )
    OCR_PAGE_TIMEOUT_SECONDS: float = Field(
        default=60.0,
        description="单页 OCR 识别超时时间（秒）",
    )
    OCR_ONLINE_BASE_URL: str = Field(
        default="",
        description=(
            "在线 OCR 服务的 OpenAI 兼容接口地址（如"
            " https://dashscope.aliyuncs.com/compatible-mode/v1）。"
            "OCR_ENGINE=online 时必填"
        ),
    )
    OCR_ONLINE_API_KEY: str = Field(
        default="",
        description="在线 OCR 服务的 API Key。OCR_ENGINE=online 时必填",
    )
    OCR_ONLINE_MODEL: str = Field(
        default="qwen-vl-plus",
        description=(
            "在线 OCR 使用的视觉多模态模型名（OpenAI 兼容 chat/completions 协议）"
        ),
    )
    OCR_ONLINE_LINE_CONFIDENCE: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        description=(
            "在线 OCR 的行级置信度占位值。在线视觉模型不返回行级置信度，"
            "模型可读出即视为清晰；该值需高于 OCR_MIN_CONFIDENCE_THRESHOLD，"
            "否则所有扫描件都会被判模糊阻断"
        ),
    )

    # ------------------------------------------------------------------
    # 十一、业务规则引擎配置
    # 说明：所有正式业务判定的最终裁决方，大模型不得参与
    # ------------------------------------------------------------------
    RULE_ENGINE_ENABLED: bool = Field(
        default=True,
        description="规则引擎总开关。生产环境必须为 True，禁止关闭",
    )
    RULE_ENGINE_VERSION: str = Field(
        default="v1.0.0",
        description="规则引擎版本号，写入审查结论快照以保证可追溯",
    )
    RULE_RULES_PATH: str = Field(
        default="config/risk_rules.yaml",
        description=(
            "规则配置文件路径（YAML）。规则引擎从该文件加载风险规则，"
            "支持按合同类型分组与优先级排序"
        ),
    )
    RULE_ENGINE_BLOCK_LEVELS: List[str] = Field(
        default=["high", "critical"],
        description="触发任务阻塞的风险等级列表。命中即阻塞审批流，由规则引擎判定",
    )
    RULE_RISK_LEVELS: List[str] = Field(
        default=["low", "medium", "high", "critical"],
        description="风险等级枚举全集，由低到高，供规则定级与前端展示使用",
    )
    RULE_SEMANTIC_DEFAULT_THRESHOLD: float = Field(
        default=0.82,
        ge=0.0,
        le=1.0,
        description="语义匹配默认阈值，规则模板未单独配置时使用",
    )
    RULE_SEMANTIC_RECALL_MIN_SIMILARITY: float = Field(
        default=0.70,
        ge=0.0,
        le=1.0,
        description=(
            "向量召回最低相似度门槛。低于该值的候选在 merge 前被过滤，"
            "避免噪声候选堆积到参考项列表中干扰人工复核"
        ),
    )

    # ------------------------------------------------------------------
    # 十二、差异化流程路由配置
    # 说明：由路由决策器消费，决定走快速通道 / 标准审查 / 深度复核
    # ------------------------------------------------------------------
    ROUTE_FAST_ENABLED: bool = Field(
        default=True,
        description="是否启用快速通道（模板合同走该通道，仅跑结构化规则）",
    )
    ROUTE_DEEP_AMOUNT_THRESHOLD: float = Field(
        default=5_000_000.0,
        ge=0.0,
        description="大额阈值（元）。合同金额达到该值即走深度复核通道",
    )
    ROUTE_DEEP_REQUIRE_LEGAL: bool = Field(
        default=True,
        description="深度复核通道是否强制法务介入（人工复核网关）",
    )
    ROUTE_DEEP_FORCE_TYPES: str = Field(
        default="foreign_cooperation,equity_transfer,guarantee,loan",
        description="强制走深度复核的合同类型，逗号分隔",
    )

    # ------------------------------------------------------------------
    # 十三、任务与重试机制配置
    # ------------------------------------------------------------------
    TASK_MAX_RETRY: int = Field(
        default=3,
        ge=1,
        description="节点级最大重试次数。达上限后任务进入 blocked(RETRY_EXHAUSTED)",
    )
    TASK_RETRY_BASE_DELAY: float = Field(
        default=1.0,
        gt=0.0,
        description="指数退避基数（秒），退避序列为 base → base*2 → base*4",
    )
    TASK_RETRY_MAX_DELAY: float = Field(
        default=8.0,
        gt=0.0,
        description="单次退避等待上限（秒），防止等待时间无限增长",
    )
    TASK_TMP_RETENTION_DAYS: int = Field(
        default=7,
        ge=1,
        description="任务临时文件保留天数，超期由清理任务自动删除",
    )
    TASK_MAX_CONCURRENCY: int = Field(
        default=5,
        ge=1,
        description="并发执行的最大任务数，防止资源耗尽",
    )

    # ------------------------------------------------------------------
    # 十四、生成质量闭环配置
    # 说明：生成前约束 → 生成后多维校验 → 校验失败自动修正
    # ------------------------------------------------------------------
    QUALITY_MAX_FIX_ROUNDS: int = Field(
        default=2,
        ge=0,
        description="校验失败后自动修正的最大轮次，超出则标记待人工处理",
    )
    QUALITY_PRECONSTRAINT_ENABLED: bool = Field(
        default=True,
        description="是否启用生成前规则约束注入",
    )
    QUALITY_POSTCHECK_ENABLED: bool = Field(
        default=True,
        description="是否启用生成后多维校验",
    )
    SUGGESTION_MAX_LENGTH: int = Field(
        default=2000,
        ge=1,
        description="修改建议初稿的最大字符长度，超出将被截断并告警",
    )

    # ------------------------------------------------------------------
    # 十五、报告导出配置
    # ------------------------------------------------------------------
    REPORT_FORMATS: List[str] = Field(
        default=["md", "pdf"],
        description="支持的报告导出格式列表",
    )
    REPORT_URL_EXPIRE_SECONDS: int = Field(
        default=86400,
        ge=60,
        description="报告文件预签名访问链接有效期（秒），默认 1 天",
    )
    REPORT_INCLUDE_LEGAL_OPINION: bool = Field(
        default=True,
        description="报告中是否包含法务补充意见",
    )

    # ------------------------------------------------------------------
    # 十五之二、PDF 报告版式配置（PyMuPDF 原生绘制）
    # ------------------------------------------------------------------
    # 设计说明：
    #   本系统 PDF 报告由 app/services/pdf_renderer.py 用 PyMuPDF 直接绘制，
    #   全部版式参数集中在此处，便于按企业 VI（视觉规范）统一调整，
    #   无需改动渲染代码。所有尺寸单位统一为「毫米(mm)」，由渲染器换算为点(pt)。
    #   换算关系：1 mm = 72 / 25.4 ≈ 2.8346 pt
    # ------------------------------------------------------------------
    PDF_ENABLED: bool = Field(
        default=True,
        description="是否启用 PDF 报告导出能力。关闭后下载接口仅提供 md 格式",
    )
    PDF_AUTO_WITH_MD: bool = Field(
        default=True,
        description=(
            "生成 Markdown 报告时是否自动联动生成 PDF。"
            "默认 True，使两种格式一次产出、内容天然对齐；"
            "关闭后 PDF 仅在显式请求 format=pdf 时按需生成"
        ),
    )
    PDF_HEADER_TEXT: str = Field(
        default="合同审批智能审查系统 · 审查报告",
        description="PDF 页眉左侧固定文字（系统名称 · 文档名称），空字符串表示不绘制页眉",
    )
    PDF_FOOTER_SHOW_PAGE: bool = Field(
        default=True,
        description="页脚是否显示页码（格式：第 X 页 / 共 Y 页）",
    )
    PDF_FOOTER_SHOW_TIME: bool = Field(
        default=True,
        description="页脚是否显示报告生成时间",
    )
    PDF_FONT_PATH: str = Field(
        default="",
        description=(
            "正文字体文件绝对路径（宋体系族，用于可嵌入 PDF 的常规正文）。"
            "留空则由渲染器自动探测系统字体（Windows: simsun.ttc / msyh.ttc，"
            "Linux: Noto Sans CJK）。当自动探测失败时回退 PyMuPDF 内置 CJK 字体"
            "（china-s），该内置字体无需任何外部文件，保证中文永不缺字"
        ),
    )
    PDF_FONT_PATH_BOLD: str = Field(
        default="",
        description=(
            "标题（加粗）字体文件绝对路径（黑体系族）。留空则由渲染器自动探测"
            "（Windows: simhei.ttf / msyhbd.ttc，Linux: Noto Sans CJK Bold）。"
            "探测失败时回退内置 CJK 字体，由渲染器用描边模拟加粗"
        ),
    )
    PDF_PAGE_MARGIN_TOP_MM: float = Field(
        default=25.0,
        gt=0.0,
        description="A4 页面上边距（毫米）。默认 25mm 符合正式公文规范",
    )
    PDF_PAGE_MARGIN_BOTTOM_MM: float = Field(
        default=22.0,
        gt=0.0,
        description="A4 页面下边距（毫米），需为页脚留出空间",
    )
    PDF_PAGE_MARGIN_LEFT_MM: float = Field(
        default=22.0,
        gt=0.0,
        description="A4 页面左边距（毫米）",
    )
    PDF_PAGE_MARGIN_RIGHT_MM: float = Field(
        default=22.0,
        gt=0.0,
        description="A4 页面右边距（毫米）",
    )
    PDF_BODY_FONT_SIZE_PT: float = Field(
        default=10.5,
        gt=0.0,
        description="正文字号（磅）。10.5pt 为中文正式文档常用正文五号字",
    )
    PDF_TABLE_FONT_SIZE_PT: float = Field(
        default=9.0,
        gt=0.0,
        description="表格内文字号（磅），小于正文以便容纳多列风险清单",
    )
    PDF_LINE_HEIGHT_RATIO: float = Field(
        default=1.6,
        gt=1.0,
        description="正文行高倍数（相对字号）。1.6 为公文常用行距",
    )
    PDF_MAX_PAGES: int = Field(
        default=200,
        ge=1,
        description="PDF 最大页数上限，超出则渲染中断并报错，防止异常数据导致内存耗尽",
    )

    # ------------------------------------------------------------------
    # 十六、审批系统对接配置
    # 【绝对禁止】仅允许写入评论区，严禁调用改变审批结论的接口
    # ------------------------------------------------------------------
    APPROVAL_BASE_URL: str = Field(
        default="http://127.0.0.1:9100",
        description="企业审批系统基础地址",
    )
    APPROVAL_COMMENT_API_PATH: str = Field(
        default="/comments",
        description="评论接口路径。系统仅允许调用评论区接口（白名单见 approval_guard）",
    )
    APPROVAL_API_KEY: str = Field(
        default="your-approval-api-key",
        description="审批系统对接鉴权密钥",
    )
    # ------------------ 消息通知（分级推送，QQ 邮箱） ------------------
    SMTP_HOST: str = Field(
        default="smtp.qq.com",
        description="SMTP 服务器地址（QQ 邮箱 smtp.qq.com）",
    )
    SMTP_PORT: int = Field(
        default=465,
        description="SMTP 端口（465=SSL，587=STARTTLS）",
    )
    SMTP_USE_SSL: bool = Field(
        default=True,
        description="是否使用 SSL（465 端口用 True；587 用 False）",
    )
    SMTP_USER: str = Field(
        default="",
        description="SMTP 发件账号（QQ 邮箱号，如 2584076700@qq.com）",
    )
    SMTP_PASSWORD: str = Field(
        default="",
        description="SMTP 授权码（QQ 邮箱-设置-账户-开启 SMTP 后获取的授权码，非登录密码）",
    )
    SMTP_FROM: str = Field(
        default="明契智审 <contract-review@qq.com>",
        description="发件人显示名与地址",
    )
    NOTIFY_OPERATOR_EMAIL: str = Field(
        default="2241071024@qq.com",
        description="经办员 QQ 邮箱：接收本人提交合同的审查成功/失败全部通知",
    )
    NOTIFY_ADMIN_EMAIL: str = Field(
        default="2584076700@qq.com",
        description="管理员 QQ 邮箱：仅接收执行失败、系统级异常通知",
    )
    NOTIFY_REVIEWER_EMAIL: str = Field(
        default="1623084893@qq.com",
        description="审查员 QQ 邮箱：仅接收异常任务通知",
    )
    APPROVAL_TIMEOUT_SECONDS: float = Field(
        default=30.0,
        gt=0.0,
        description="审批系统请求超时时间（秒）",
    )
    APPROVAL_CONNECT_TIMEOUT_SECONDS: float = Field(
        default=10.0,
        gt=0.0,
        description="审批系统连接建立超时时间（秒）",
    )
    APPROVAL_MAX_RETRIES: int = Field(
        default=3,
        ge=0,
        description="审批回写失败重试次数（指数退避）",
    )
    APPROVAL_WRITE_BACK_MODE: str = Field(
        default="formal",
        description=(
            "审批评论回写模式：formal=正式写入审批系统；preview=预览模式，"
            "仅记录评论内容不实际写入。开发联调阶段可切换 preview 避免污染审批数据"
        ),
    )

    # ------------------------------------------------------------------
    # 十七、角色与鉴权配置
    # ------------------------------------------------------------------
    AUTH_ENABLED: bool = Field(
        default=True,
        description="是否启用鉴权。开发阶段可关闭，生产环境必须开启",
    )
    JWT_SECRET_KEY: str = Field(
        default="change-this-to-a-strong-random-secret",
        description="JWT 签名密钥，生产环境务必替换为强随机值",
    )
    JWT_ALGORITHM: str = Field(
        default="HS256",
        description="JWT 签名算法",
    )
    JWT_EXPIRE_MINUTES: int = Field(
        default=120,
        ge=1,
        description=(
            "访问令牌（access_token）有效期（分钟）。"
            "默认 2 小时：短有效期可缩小 token 泄漏后的可利用窗口，"
            "续期由 refresh_token 通道承担，用户无感知"
        ),
    )
    JWT_REFRESH_EXPIRE_MINUTES: int = Field(
        default=10080,
        ge=1,
        description=(
            "刷新令牌（refresh_token）有效期（分钟），默认 7 天。"
            "仅用于换取新的 access_token，不可直接访问业务接口"
        ),
    )
    JWT_ISSUER: str = Field(
        default="contract-review-system",
        description="JWT 签发方标识（iss 声明），用于校验令牌来源",
    )
    BCRYPT_ROUNDS: int = Field(
        default=12,
        ge=4,
        le=16,
        description=(
            "bcrypt 哈希计算强度（轮数，2 的幂）。"
            "12 为安全与耗时的常用平衡点（单次约 200-300ms）；"
            "演示环境追求响应速度可降至 10，生产环境不建议低于 12"
        ),
    )
    AUTH_TOKEN_HEADER_NAME: str = Field(
        default="Authorization",
        description="承载访问令牌的请求头名称",
    )
    AUTH_TOKEN_PREFIX: str = Field(
        default="Bearer",
        description="访问令牌在请求头中的前缀，格式为 '<prefix> <token>'",
    )
    AUTH_WHITELIST_PATHS: str = Field(
        default=(
            "/,"
            "/api/v1/auth/login,"
            "/api/v1/auth/refresh,"
            "/api/v1/health,"
            "/api/v1/health/ready,"
            "/api/v1/health/live,"
            "/docs,"
            "/redoc,"
            "/openapi.json,"
            "/favicon.ico"
        ),
        description=(
            "鉴权白名单路径，逗号分隔。命中白名单的请求不校验令牌。"
            "说明：仅放行根路径服务信息、登录、刷新、健康探针与接口文档；"
            "其余接口一律默认需要登录（默认拒绝原则）。"
            "根路径 \"/\" 只返回应用名与版本等非敏感信息，"
            "供运维探活与联调时快速确认服务身份，故放行。"
        ),
    )
    AUTH_MAX_LOGIN_FAILURES: int = Field(
        default=5,
        ge=0,
        description="连续登录失败达到该次数后锁定账号；置 0 表示不自动锁定",
    )
    AUTH_LOGIN_LOCK_MINUTES: int = Field(
        default=30,
        ge=1,
        description="账号因连续登录失败被锁定后的自动解锁时长（分钟）",
    )

    # 角色标识（权威值定义在 app/constants 的 UserRole 枚举中，
    # 此处配置项仅作为部署期可覆盖的入口，默认值与枚举严格保持一致，
    # 避免出现"配置写 business_handler、代码判 business_user"的静默失效）
    ROLE_BUSINESS_USER: str = Field(
        default="business_user",
        description="角色标识：业务经办人（上传合同、查看自己的任务、下载报告）",
    )
    ROLE_LAW_USER: str = Field(
        default="law_user",
        description="角色标识：法务审查人（审查全部任务、编辑风险项、批注、触发回写）",
    )
    ROLE_SYSTEM_ADMIN: str = Field(
        default="admin",
        description="角色标识：系统管理员（用户管理、规则维护、处理阻塞、系统监控）",
    )

    # 内置演示账号（仅用于演示系统，生产环境务必删除或改密）
    SEED_BUILTIN_USERS: bool = Field(
        default=True,
        description=(
            "启动时是否自动初始化内置演示账号。"
            "采用幂等 upsert，已存在则不覆盖已改过的密码"
        ),
    )
    DEFAULT_PASSWORD: str = Field(
        default="123456",
        description="内置演示账号的初始密码（仅演示用，生产环境必须修改）",
    )

    # ------------------------------------------------------------------
    # 十二、日志配置
    # ------------------------------------------------------------------
    LOG_LEVEL: str = Field(
        default="INFO",
        description="日志级别，可选 DEBUG / INFO / WARNING / ERROR / CRITICAL",
    )
    LOG_DIR: str = Field(
        default="storage/logs",
        description="日志文件输出目录，相对项目根目录",
    )
    LOG_FILE_NAME: str = Field(
        default="contract_review.log",
        description="主日志文件名",
    )
    LOG_RETENTION_DAYS: int = Field(
        default=30,
        description="日志文件保留天数，超期自动清理",
    )
    LOG_CONSOLE_OUTPUT: bool = Field(
        default=True,
        description="是否同时输出到控制台",
    )

    # ------------------------------------------------------------------
    # Pydantic Settings 配置
    # ------------------------------------------------------------------
    model_config = SettingsConfigDict(
        # 指定 .env 文件路径，相对于后端工作目录
        env_file=".env",
        # 使用 UTF-8 编码读取，避免中文乱码
        env_file_encoding="utf-8",
        # 环境变量大小写敏感
        case_sensitive=True,
        # 允许 .env 中存在本类未声明的额外变量，不报错
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # 校验器
    # ------------------------------------------------------------------
    @field_validator("EMBEDDING_DIM")
    @classmethod
    def validate_embedding_dim(cls, value: int) -> int:
        """
        校验向量维度合法性

        参数：
            value: 传入的向量维度值
        返回：
            校验通过的维度值
        异常：
            ValueError：BGE-M3 固定输出 1024 维，传入其它值说明配置有误
        """
        if value != 1024:
            raise ValueError(
                f"EMBEDDING_DIM 必须为 1024（BGE-M3 固定维度），当前配置为 {value}"
            )
        return value

    @field_validator("APP_ENV")
    @classmethod
    def validate_app_env(cls, value: str) -> str:
        """
        校验运行环境取值

        参数：
            value: 传入的环境标识
        返回：
            校验通过的环境标识
        异常：
            ValueError：取值不在白名单内时抛出
        """
        allowed = {"dev", "test", "prod"}
        if value not in allowed:
            raise ValueError(f"APP_ENV 必须为 {allowed} 之一，当前为 {value}")
        return value

    @property
    def MILVUS_URI(self) -> str:
        """
        拼接 Milvus 标准连接 URI

        返回：
            形如 http://192.168.10.130:7000 的连接地址
        """
        return f"http://{self.MILVUS_HOST}:{self.MILVUS_PORT}"

    @property
    def is_production(self) -> bool:
        """
        判断当前是否为生产环境

        返回：
            True 表示生产环境，False 表示开发或测试环境
        """
        return self.APP_ENV == "prod"

    @property
    def deep_force_type_list(self) -> List[str]:
        """
        获取强制深度复核的合同类型列表

        说明：
            将逗号分隔的字符串配置解析为列表，便于业务层直接使用。

        返回：
            合同类型字符串列表，已去除空白项
        """
        return [
            item.strip()
            for item in self.ROUTE_DEEP_FORCE_TYPES.split(",")
            if item.strip()
        ]

    @property
    def approval_comment_url(self) -> str:
        """
        拼接审批系统评论接口完整 URL

        返回：
            形如 http://host:port/comments 的完整地址
        """
        return (
            f"{self.APPROVAL_BASE_URL.rstrip('/')}"
            f"/{self.APPROVAL_COMMENT_API_PATH.lstrip('/')}"
        )

    @property
    def all_minio_buckets(self) -> List[str]:
        """
        获取全部需初始化的 MinIO 存储桶列表

        返回：
            存储桶名列表，供启动阶段批量创建
        """
        return [
            self.MINIO_BUCKET_CONTRACT,
            self.MINIO_BUCKET_OCR_IMAGE,
            self.MINIO_BUCKET_REPORT,
            self.MINIO_BUCKET_CLAUSE_LIB,
            self.MINIO_BUCKET_TMP,
        ]

    # ------------------------------------------------------------------
    # 鉴权相关派生属性
    # ------------------------------------------------------------------
    @property
    def auth_whitelist_paths(self) -> frozenset:
        """
        获取鉴权白名单路径集合

        说明：
            将逗号分隔的配置解析为集合，集合查找为 O(1)，
            避免鉴权中间件在每次请求时做线性遍历。
            同时统一去除尾部斜杠，保证 "/api/v1/health/" 与
            "/api/v1/health" 都能命中白名单。

        返回：
            frozenset[str]，元素为规范化后的路径
        """
        paths = set()
        for raw in self.AUTH_WHITELIST_PATHS.split(","):
            item = raw.strip()
            if not item:
                continue
            # 保留根路径 "/" 原样，其余去掉尾部斜杠
            paths.add(item if item == "/" else item.rstrip("/"))
        return frozenset(paths)

    @property
    def role_values(self) -> tuple:
        """
        获取系统支持的全部角色标识

        说明：
            以配置项为准（而非硬编码枚举），使部署期可通过环境变量
            调整角色名称；但校验时仍会与 UserRole 枚举求交集，
            保证不出现系统无法识别的角色。

        返回：
            tuple[str, ...]，三个内置角色标识
        """
        return (
            self.ROLE_BUSINESS_USER,
            self.ROLE_LAW_USER,
            self.ROLE_SYSTEM_ADMIN,
        )

    @property
    def is_auth_ready(self) -> bool:
        """
        判断鉴权配置是否已具备上线条件

        说明：
            用于启动自检：生产环境下若仍使用默认弱密钥，
            应视为配置错误并给出明确告警（由自检脚本消费）。

        返回：
            True 表示密钥已替换为自定义值，False 表示仍为默认值
        """
        return self.JWT_SECRET_KEY != "change-this-to-a-strong-random-secret"

    @property
    def jwt_access_expire_seconds(self) -> int:
        """
        访问令牌有效期（秒）

        返回：
            JWT_EXPIRE_MINUTES × 60
        """
        return self.JWT_EXPIRE_MINUTES * 60

    @property
    def jwt_refresh_expire_seconds(self) -> int:
        """
        刷新令牌有效期（秒）

        返回：
            JWT_REFRESH_EXPIRE_MINUTES × 60
        """
        return self.JWT_REFRESH_EXPIRE_MINUTES * 60


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    获取全局配置单例（带缓存）

    说明：
        使用 lru_cache 保证进程内只实例化一次，
        避免每次调用都重新读取 .env 文件带来的 IO 开销。

    返回：
        Settings 实例
    """
    return Settings()


# 全局配置单例，业务代码直接 from app.core.config import settings 使用
settings = get_settings()
