// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/constants/index.ts
/**
 * 全局常量与枚举映射
 *
 * 【文件职责】
 *   集中维护所有与后端契约对齐的枚举值、中文标签、颜色映射与展示口径。
 *
 * 【为什么集中维护】
 *   后端 app/constants/__init__.py 是枚举的权威定义处，前端必须与之逐字对齐。
 *   若散落在各组件里硬编码字符串，后端改一个枚举值时前端会静默显示错误标签，
 *   因此这里做唯一来源，组件一律从此导入。
 *
 * 【权责红线在前端的体现】
 *   风险等级（riskLevel）只读展示，前端不提供任何修改入口。
 *   等级由后端规则引擎产出，前端仅负责渲染与排序。
 */

// ======================================================================
// 一、风险等级（RiskLevel）
// ======================================================================

/** 风险等级枚举值，与后端 app.constants.RiskLevel 严格对齐 */
export const RISK_LEVEL = {
  LOW: 'low',
  MEDIUM: 'medium',
  HIGH: 'high',
  CRITICAL: 'critical',
} as const;

export type RiskLevel = (typeof RISK_LEVEL)[keyof typeof RISK_LEVEL];

/**
 * 风险等级排序权重
 *
 * 说明：数值越大风险越高，用于前端排序与取最高等级。
 * 与后端 RISK_LEVEL_ORDER 保持一致，保证前后端排序结果一致。
 */
export const RISK_LEVEL_ORDER: Record<RiskLevel, number> = {
  low: 1,
  medium: 2,
  high: 3,
  critical: 4,
};

/** 风险等级中文标签 */
export const RISK_LEVEL_LABEL: Record<RiskLevel, string> = {
  low: '低风险',
  medium: '中风险',
  high: '高风险',
  critical: '严重风险',
};

/**
 * 风险等级主题色
 *
 * 说明：遵循境内金融/风控场景"红高绿低"的视觉惯例，
 * 严重与高风险用红色系，中风险用橙色系，低风险用绿色系。
 */
export const RISK_LEVEL_COLOR: Record<RiskLevel, string> = {
  low: '#52c41a',
  medium: '#faad14',
  high: '#fa541c',
  critical: '#cf1322',
};

/** 风险等级对应的 Ant Design Tag 颜色 */
export const RISK_LEVEL_TAG_COLOR: Record<RiskLevel, string> = {
  low: 'success',
  medium: 'warning',
  high: 'volcano',
  critical: 'error',
};

/** 风险等级下拉选项（供筛选器使用） */
export const RISK_LEVEL_OPTIONS = (
  Object.keys(RISK_LEVEL_ORDER) as RiskLevel[]
).map((value) => ({
  value,
  label: RISK_LEVEL_LABEL[value],
}));

// ======================================================================
// 二、任务状态（ReviewTaskStatus）
// ======================================================================

/** 审查任务状态，与后端 app.constants.ReviewTaskStatus 严格对齐 */
export const TASK_STATUS = {
  PENDING: 'pending',
  RUNNING: 'running',
  LLM_DONE: 'llm_done',
  RULE_DONE: 'rule_done',
  BLOCKED: 'blocked',
  COMPLETED: 'completed',
  FAILED: 'failed',
  CANCELLED: 'cancelled',
} as const;

export type TaskStatus = (typeof TASK_STATUS)[keyof typeof TASK_STATUS];

/** 任务状态中文标签 */
export const TASK_STATUS_LABEL: Record<TaskStatus, string> = {
  pending: '待执行',
  running: '审查中',
  llm_done: 'AI 分析完成',
  rule_done: '规则判定完成',
  blocked: '已阻塞',
  completed: '已完成',
  failed: '执行失败',
  cancelled: '已取消',
};

/** 任务状态对应的 Ant Design Badge 状态 */
export const TASK_STATUS_BADGE: Record<
  TaskStatus,
  'success' | 'processing' | 'default' | 'error' | 'warning'
> = {
  pending: 'default',
  running: 'processing',
  llm_done: 'processing',
  rule_done: 'processing',
  blocked: 'warning',
  completed: 'success',
  failed: 'error',
  cancelled: 'default',
};

/** 处于进行中的状态集合，用于决定是否继续轮询进度 */
export const TASK_STATUS_IN_PROGRESS: TaskStatus[] = [
  'pending',
  'running',
  'llm_done',
  'rule_done',
];

/** 任务状态下拉选项 */
export const TASK_STATUS_OPTIONS = (
  Object.keys(TASK_STATUS_LABEL) as TaskStatus[]
).map((value) => ({
  value,
  label: TASK_STATUS_LABEL[value],
}));

// ======================================================================
// 三、合同类型（contract_types）
// ======================================================================

/** 合同类型枚举值，与后端规则配置 contract_types 取值对齐 */
export const CONTRACT_TYPE = {
  PURCHASE: 'purchase',
  SALES: 'sales',
  SERVICE: 'service',
  LABOR: 'labor',
  ALL: 'all',
} as const;

export type ContractType = (typeof CONTRACT_TYPE)[keyof typeof CONTRACT_TYPE];

/** 合同类型中文标签 */
export const CONTRACT_TYPE_LABEL: Record<string, string> = {
  purchase: '采购合同',
  sales: '销售合同',
  service: '服务合同',
  labor: '劳动合同',
  all: '通用（全部类型）',
  other: '其他',
};

/** 合同类型选项（不含 all，用于任务筛选与规则归属） */
export const CONTRACT_TYPE_OPTIONS = [
  CONTRACT_TYPE.PURCHASE,
  CONTRACT_TYPE.SALES,
  CONTRACT_TYPE.SERVICE,
  CONTRACT_TYPE.LABOR,
].map((value) => ({
  value,
  label: CONTRACT_TYPE_LABEL[value],
}));

/** 规则归属的合同类型选项（含通用） */
export const RULE_CONTRACT_TYPE_OPTIONS = [
  CONTRACT_TYPE.PURCHASE,
  CONTRACT_TYPE.SALES,
  CONTRACT_TYPE.SERVICE,
  CONTRACT_TYPE.LABOR,
  CONTRACT_TYPE.ALL,
].map((value) => ({
  value,
  label: CONTRACT_TYPE_LABEL[value],
}));

/** 合同类型主题色，用于规则页类型标签区分 */
export const CONTRACT_TYPE_COLOR: Record<string, string> = {
  purchase: 'blue',
  sales: 'green',
  service: 'purple',
  labor: 'orange',
  all: 'default',
  other: 'default',
};

// ======================================================================
// 四、业务类型
// ======================================================================

/** 业务类型选项（合同归口的业务场景） */
export const BUSINESS_TYPE_OPTIONS = [
  { value: 'material_purchase', label: '物资采购' },
  { value: 'equipment_purchase', label: '设备采购' },
  { value: 'service_outsourcing', label: '服务外包' },
  { value: 'product_sales', label: '产品销售' },
  { value: 'channel_agent', label: '渠道代理' },
  { value: 'technology_service', label: '技术服务' },
  { value: 'labor_employment', label: '劳动用工' },
  { value: 'lease', label: '租赁业务' },
  { value: 'other', label: '其他业务' },
];

/** 业务类型标签映射 */
export const BUSINESS_TYPE_LABEL: Record<string, string> = Object.fromEntries(
  BUSINESS_TYPE_OPTIONS.map((item) => [item.value, item.label]),
);

// ======================================================================
// 五、规则引擎相关枚举
// ======================================================================

/** 规则匹配方式，与后端 app.constants.RuleMatchType 严格对齐 */
export const MATCH_TYPE = {
  KEYWORD: 'keyword',
  REGEX: 'regex',
  CLAUSE_TYPE: 'clause_type',
  SEMANTIC: 'semantic',
  FIELD_COMPARE: 'field_compare',
  MISSING_CLAUSE: 'missing_clause',
  NUMERIC_THRESHOLD: 'numeric_threshold',
} as const;

export type MatchType = (typeof MATCH_TYPE)[keyof typeof MATCH_TYPE];

/** 匹配方式中文标签 */
export const MATCH_TYPE_LABEL: Record<MatchType, string> = {
  keyword: '关键词匹配',
  regex: '正则表达式',
  clause_type: '条款类型匹配',
  semantic: '语义相似度',
  field_compare: '字段数值比较',
  missing_clause: '必备条款缺失',
  numeric_threshold: '数值阈值校验',
};

/** 匹配方式选项 */
export const MATCH_TYPE_OPTIONS = Object.entries(MATCH_TYPE_LABEL).map(
  ([value, label]) => ({ value, label }),
);

/** 规则严重度，与后端 app.constants.RuleSeverity 严格对齐 */
export const RULE_SEVERITY = {
  INFO: 'info',
  MINOR: 'minor',
  MAJOR: 'major',
  FATAL: 'fatal',
} as const;

export type RuleSeverity = (typeof RULE_SEVERITY)[keyof typeof RULE_SEVERITY];

/** 规则严重度中文标签（含映射后的风险等级提示） */
export const RULE_SEVERITY_LABEL: Record<RuleSeverity, string> = {
  info: '提示级（→ 低风险）',
  minor: '轻微（→ 低风险）',
  major: '重大（→ 高风险）',
  fatal: '致命（→ 严重风险）',
};

/** 规则严重度选项 */
export const RULE_SEVERITY_OPTIONS = (
  Object.keys(RULE_SEVERITY_LABEL) as RuleSeverity[]
).map((value) => ({ value, label: RULE_SEVERITY_LABEL[value] }));

/** 条款类型，与后端 app.constants.ClauseType 严格对齐 */
export const CLAUSE_TYPE_LABEL: Record<string, string> = {
  party_info: '当事人信息',
  subject_matter: '标的与金额',
  payment: '付款条款',
  delivery: '交付与验收',
  warranty: '质量保证',
  liability: '违约责任',
  confidentiality: '保密条款',
  ip: '知识产权',
  termination: '合同解除与终止',
  force_majeure: '不可抗力',
  dispute: '争议解决',
  data_protection: '数据与个人信息保护',
  compliance: '合规与反腐败',
  other: '其他/未分类',
};

/** 条款类型选项 */
export const CLAUSE_TYPE_OPTIONS = Object.entries(CLAUSE_TYPE_LABEL).map(
  ([value, label]) => ({ value, label }),
);

// ======================================================================
// 六、阻塞原因码与回写状态
// ======================================================================

/** 任务阻塞原因码中文说明，与后端常量定义对齐 */
export const BLOCKED_REASON_LABEL: Record<string, string> = {
  DOC_ENCRYPTED: '文档已加密，无法解析',
  SCAN_TOO_BLURRY: '扫描件过于模糊，OCR 置信度不足',
  EMPTY_CONTENT: '文档内容为空或有效字符不足',
  PARSE_FAILED: '文档解析失败',
  RETRY_EXHAUSTED: '重试次数已耗尽',
  MANUAL: '人工手动阻塞',
  RULE_HIGH_RISK: '规则引擎判定为高风险，需人工处理',
};

/** 回写状态，与后端 WriteBackState 对齐 */
export const WRITE_BACK_STATUS = {
  NOT_WRITTEN: 'not_written',
  WRITING: 'writing',
  SUCCESS: 'success',
  FAILED: 'failed',
} as const;

export type WriteBackStatus =
  (typeof WRITE_BACK_STATUS)[keyof typeof WRITE_BACK_STATUS];

/** 回写状态中文标签 */
export const WRITE_BACK_STATUS_LABEL: Record<WriteBackStatus, string> = {
  not_written: '未回写',
  writing: '回写中',
  success: '回写成功',
  failed: '回写失败',
};

/** 回写状态对应的 Ant Design Tag 颜色 */
export const WRITE_BACK_STATUS_COLOR: Record<WriteBackStatus, string> = {
  not_written: 'default',
  writing: 'processing',
  success: 'success',
  failed: 'error',
};

// ======================================================================
// 七、通用展示配置
// ======================================================================

/** 统一分页配置，与后端 MAX_PAGE_SIZE 上限匹配 */
export const PAGINATION = {
  DEFAULT_PAGE_SIZE: 20,
  PAGE_SIZE_OPTIONS: ['10', '20', '50', '100'],
} as const;

/** 长任务进度轮询间隔（毫秒） */
export const POLL_INTERVAL_MS = 3000;

/** 文件上传大小上限（字节），与后端配置保持一致 */
export const MAX_UPLOAD_SIZE = 100 * 1024 * 1024;

/** 允许上传的文件扩展名 */
export const ALLOWED_UPLOAD_EXTS = [
  '.pdf',
  '.docx',
  '.doc',
  '.png',
  '.jpg',
  '.jpeg',
  '.tif',
  '.tiff',
  '.bmp',
];

/**
 * 无文字层、必须经 OCR 才能抽取文本的图片扩展名
 *
 * 说明：与后端 `is_scanned_image_ext` 保持同口径。这类文件上传后
 * `source_type` 会被判定为 `scanned`，解析阶段强制走 OCR 链路；
 * 若后端未部署 OCR 引擎则必然解析失败。前端据此提前给出提示。
 */
export const SCANNED_IMAGE_EXTS = [
  '.png',
  '.jpg',
  '.jpeg',
  '.tif',
  '.tiff',
  '.bmp',
];

/** 风险等级说明文案，用于工作台与报告的口径说明 */
export const RISK_LEVEL_DESCRIPTION: Record<RiskLevel, string> = {
  low: '可提示，不影响审批推进',
  medium: '建议整改，法务复核后可放行',
  high: '必须整改后方可提交审批',
  critical: '命中即阻塞审批流，须法务介入整改',
};

// ======================================================================
// 八、用户角色与权限点（与后端 app/constants/__init__.py 严格对齐）
// ======================================================================

/**
 * 用户角色枚举值
 *
 * 【为什么用 as const 而非 enum】
 *   as const 产出的是字面量联合类型，与后端下发的字符串可直接互赋，
 *   不需要任何运行时转换；TS enum 会引入双向映射对象，
 *   在 JSON 序列化与比较时容易出意外。
 */
export const USER_ROLE = {
  /** 业务经办人 */
  BUSINESS_USER: 'business_user',
  /** 法务审查人 */
  LAW_USER: 'law_user',
  /** 系统管理员 */
  ADMIN: 'admin',
} as const;

export type UserRole = (typeof USER_ROLE)[keyof typeof USER_ROLE];

/** 全部角色值（顺序即权限由低到高） */
export const ALL_USER_ROLES: UserRole[] = [
  USER_ROLE.BUSINESS_USER,
  USER_ROLE.LAW_USER,
  USER_ROLE.ADMIN,
];

/** 角色中文标签 */
export const USER_ROLE_LABEL: Record<UserRole, string> = {
  business_user: '业务经办人',
  law_user: '法务审查人',
  admin: '系统管理员',
};

/** 角色主题色（用于 Header 角色标签） */
export const USER_ROLE_TAG_COLOR: Record<UserRole, string> = {
  business_user: 'blue',
  law_user: 'green',
  admin: 'gold',
};

/**
 * 角色主色值（十六进制）
 *
 * 【与 USER_ROLE_TAG_COLOR 的分工（务必分清）】
 *   USER_ROLE_TAG_COLOR 存的是 antd Tag 的**预设色名**（'blue' / 'green' / 'gold'），
 *   只能交给 `<Tag color={...}>` 使用；而按钮边框与文字、头像底色等
 *   需要**真实色值**的场景无法复用它，必须用本表。
 *   两表语义完全一致（同一角色同一颜色），新增/调整角色配色时须同步维护。
 *
 * 【色值来源（已用浏览器实测 antd 渲染结果校准，勿凭记忆改）】
 *   直接取 antd 5 的 Tag 预设色中该色系的**正文色**（即 -7 档）：
 *     blue  #0958d9 / green #389e0d / gold #d48806
 *   注意 blue 的正文色是 #0958d9 而**不是**主色 #1677ff——
 *   antd 的预设 Tag 用 -7 档作正文色以保证对比度。
 *   这样"角色按钮"与"角色标签"在同一页面上是完全同一个颜色。
 */
export const USER_ROLE_COLOR_HEX: Record<UserRole, string> = {
  /** 业务经办人：蓝（antd blue-7，与角色标签正文色一致） */
  business_user: '#0958d9',
  /** 法务审查人：绿 */
  law_user: '#389e0d',
  /** 系统管理员：金 */
  admin: '#d48806',
};

/**
 * 角色头像渐变背景
 *
 * 【为什么仍用渐变而非纯色】
 *   沿用顶栏头像原有的"135° 双色渐变"观感，只把色相换成角色色，
 *   避免纯色圆块与紧邻的角色标签争夺视觉重心。
 *
 * 【明暗取值规则（三色统一）】
 *   亮端取该色系第 6 档、暗端取第 7 档，即
 *     blue  #1677ff → #0958d9
 *     green #52c41a → #389e0d
 *     gold  #faad14 → #d48806
 *   暗端与 USER_ROLE_COLOR_HEX 完全一致，使"头像底色"与"按钮/标签"
 *   收在同一个色相上；同时白色用户图标在渐变两端都有足够对比度。
 */
export const USER_ROLE_AVATAR_GRADIENT: Record<UserRole, string> = {
  business_user: 'linear-gradient(135deg, #1677ff, #0958d9)',
  law_user: 'linear-gradient(135deg, #52c41a, #389e0d)',
  admin: 'linear-gradient(135deg, #faad14, #d48806)',
};

/**
 * 权限点常量（与后端 PERM_* 逐字对齐）
 *
 * 【权威源声明】
 *   后端 app/constants/ROLE_PERMISSIONS 是权限矩阵的唯一权威源，
 *   并通过 GET /auth/permission-matrix 下发。
 *   此处定义权限点字符串常量只为"编译期类型安全 + 避免手写错别字"，
 *   前端**不得**自行维护"角色→权限"的映射关系。
 */
export const PERMISSION = {
  /** 上传合同 */
  CONTRACT_UPLOAD: 'contract:upload',
  /** 查看自己提交的任务 */
  TASK_VIEW_OWN: 'task:view_own',
  /** 查看全部任务 */
  TASK_VIEW_ALL: 'task:view_all',
  /** 下载审查报告 */
  REPORT_DOWNLOAD: 'report:download',
  /** 编辑风险项（批注/建议，B 类字段） */
  RISK_ITEM_EDIT: 'risk_item:edit',
  /** 人工批注 */
  COMMENT_ANNOTATE: 'comment:annotate',
  /** 触发审批回写（仅评论通道） */
  APPROVAL_WRITE_BACK: 'approval:write_back',
  /** 维护合规规则模板 */
  RULE_TEMPLATE_MANAGE: 'rule_template:manage',
  /** 管理用户 */
  USER_MANAGE: 'user:manage',
  /** 处理阻塞任务（重跑/解阻塞） */
  BLOCKED_TASK_HANDLE: 'blocked_task:handle',
  /** 系统监控 */
  SYSTEM_MONITOR: 'system:monitor',
} as const;

export type Permission = (typeof PERMISSION)[keyof typeof PERMISSION];

/** 权限点中文说明（用于无权限提示与权限清单展示） */
export const PERMISSION_LABEL: Record<Permission, string> = {
  'contract:upload': '上传合同',
  'task:view_own': '查看本人提交的任务',
  'task:view_all': '查看全部审查任务',
  'report:download': '下载审查报告',
  'risk_item:edit': '编辑风险项批注与建议',
  'comment:annotate': '人工批注',
  'approval:write_back': '触发审批意见回写',
  'rule_template:manage': '维护合规规则模板',
  'user:manage': '管理系统用户',
  'blocked_task:handle': '处理阻塞任务',
  'system:monitor': '系统监控',
};

// ======================================================================
// 九、路由权限映射（前端路由守卫的判定依据）
// ======================================================================

/**
 * 路由路径常量
 *
 * 集中定义避免在各处硬编码字符串导致跳转失效。
 */
export const ROUTE_PATH = {
  LOGIN: '/login',
  DASHBOARD: '/dashboard',
  WORKBENCH: '/workbench',
  RULES: '/rules',
  /**
   * 审查报告（只读）
   *
   * 【为什么单独开一条路由而不复用工作台】
   *   工作台是法务的作业面，含风险定级明细编辑、人工批注与审批回写，
   *   这些能力对业务经办人属于越权（见"经办人职责边界止于提交与查看报告"）。
   *   若让经办人进工作台再靠按钮隐藏来收权，一旦某个按钮漏藏就是越权事故；
   *   因此物理隔离出只读报告页，从路由层面就不给经办人任何写操作入口。
   */
  REPORT: '/report',
} as const;

export type RoutePath = (typeof ROUTE_PATH)[keyof typeof ROUTE_PATH];

/**
 * 角色默认首页
 *
 * 【产品约定】登录成功后按角色跳转到各自的核心作业面：
 *   business_user → 合同审查大盘（经办人只关心自己提交的合同）
 *   law_user      → 智能审查工作台（法务的主战场是条款级复核）
 *   admin         → 合规规则与模板（管理员职责是维护规则资产）
 *
 * 该映射同时用于"越权访问时的回跳目标"，保证被拦截的用户
 * 总是落到自己有权访问且有意义的页面，而不是一个空白页。
 */
export const ROLE_HOME_PATH: Record<UserRole, RoutePath> = {
  business_user: ROUTE_PATH.DASHBOARD,
  law_user: ROUTE_PATH.WORKBENCH,
  admin: ROUTE_PATH.RULES,
};

/**
 * 路由访问权限：路径 → 允许访问的角色清单
 *
 * 【设计说明】
 *   以"路径"为主键而非"角色"为主键，是因为守卫的判定时机是
 *   "路由匹配后"，此时已知的是目标路径，直接查表 O(1) 得出是否放行，
 *   比遍历角色清单更直观，也不容易在多角色扩展时漏配。
 *
 *   未在此表中登记的路径视为"任意登录用户可访问"。
 */
export const ROUTE_ALLOWED_ROLES: Record<string, UserRole[]> = {
  // 大盘：经办人 + 法务 + 管理员
  //   经办人需要看自己提交的合同进度；法务与管理员需要看全量
  [ROUTE_PATH.DASHBOARD]: [
    USER_ROLE.BUSINESS_USER,
    USER_ROLE.LAW_USER,
    USER_ROLE.ADMIN,
  ],

  // 工作台：法务（核心作业面）+ 管理员
  //   经办人**禁止进入**——工作台含风险定级明细与法务批注，
  //   经办人的职责边界止于"提交合同"与"查看报告"
  [ROUTE_PATH.WORKBENCH]: [USER_ROLE.LAW_USER, USER_ROLE.ADMIN],

  // 规则与模板：仅管理员
  //   法务可查看规则用于复核，但**规则编辑与模板维护**属管理员职责；
  //   为避免"能看不能改"的误导性界面，此处按整体页面拦截，
  //   法务侧只在大盘/工作台通过风险详情间接看到规则命中信息
  [ROUTE_PATH.RULES]: [USER_ROLE.ADMIN],

  // 审查报告（只读）：三种角色均可访问
  //   经办人需要查看自己提交合同的审查结论与报告（report:download 权限已下发）；
  //   法务与管理员需要复核/归档报告。
  //   数据可见范围由后端按"任务提交人 / 法务 / 管理员"二次裁定，
  //   因此前端放开路由不会造成跨用户越权：经办人即使手改 URL
  //   也只能打开自己的任务（他人任务后端返回 403）。
  [ROUTE_PATH.REPORT]: [
    USER_ROLE.BUSINESS_USER,
    USER_ROLE.LAW_USER,
    USER_ROLE.ADMIN,
  ],
};

/**
 * 路由显示名称（用于无权限提示语与页面标题）
 */
export const ROUTE_TITLE: Record<string, string> = {
  [ROUTE_PATH.DASHBOARD]: '合同审查大盘',
  [ROUTE_PATH.WORKBENCH]: '智能审查工作台',
  [ROUTE_PATH.RULES]: '合规规则与模板',
  [ROUTE_PATH.REPORT]: '审查报告',
};

// ======================================================================
// 十、登录页与会话相关配置
// ======================================================================

/** localStorage 存储键名（集中定义，避免各文件手写字符串不一致） */
export const STORAGE_KEY = {
  /** 访问令牌 */
  ACCESS_TOKEN: 'cr_access_token',
  /** 刷新令牌 */
  REFRESH_TOKEN: 'cr_refresh_token',
  /** 用户信息（不含任何凭证） */
  USER_INFO: 'cr_user_info',
  /** 访问令牌过期时间戳（毫秒） */
  TOKEN_EXPIRE_AT: 'cr_token_expire_at',
} as const;

/**
 * 演示版验证码配置
 *
 * 【为什么是"演示版"】
 *   本项目为演示系统，验证码仅用于演示登录表单的完整交互，
 *   不接入真实短信/图形验证码服务。其校验在前端本地完成，
 *   **不具备任何安全价值**，生产环境必须替换为服务端校验方案。
 */
export const CAPTCHA_CONFIG = {
  /** 验证码字符集（剔除 0/O/1/I/l 等易混淆字符） */
  CHARSET: '23456789ABCDEFGHJKLMNPQRSTUVWXYZ',
  /** 验证码位数 */
  LENGTH: 4,
  /** Canvas 宽度 */
  WIDTH: 108,
  /** Canvas 高度 */
  HEIGHT: 38,
  /** 绘制时的字符旋转幅度（弧度） */
  ROTATE_RANGE: 0.4,
} as const;

/** 登录成功后留在登录页的停留时长（毫秒），让用户看清成功提示 */
export const LOGIN_SUCCESS_DELAY_MS = 320;

/** 用户信息本地缓存的"保鲜期"（毫秒）：超过后进入应用会先向后端校验一次 */
export const USER_INFO_STALE_MS = 5 * 60 * 1000;

/** 无权限提示的展示时长（秒） */
export const NO_PERMISSION_NOTICE_SECONDS = 4;

/**
 * 无权限时跳转理由，随路由 state 传递
 *
 * 说明：用常量而非各处手写字符串，保证守卫写入端与页面读取端一致，
 * 否则一处改字面量就会导致提示静默失效。
 */
export const NAV_STATE_KEY = {
  /** 从哪来的（用于登录后回跳与提示文案） */
  FROM: 'from',
  /** 跳转原因 */
  REASON: 'reason',
} as const;

/** 跳转原因枚举值 */
export const NAV_REASON = {
  /** 未登录或登录已失效 */
  UNAUTHENTICATED: 'unauthenticated',
  /** 已登录但角色无权访问 */
  FORBIDDEN: 'forbidden',
} as const;

export type NavReason = (typeof NAV_REASON)[keyof typeof NAV_REASON];

/** 失败类型标签（异常任务筛选维度与悬浮提示） */
export const FAILURE_TYPE_LABEL: Record<string, string> = {
  ocr_timeout: 'OCR识别超时',
  file_format: '文件格式错误',
  file_corrupt: '文件损坏',
  rule_engine_error: '规则引擎报错',
  network_error: '网络异常',
  parse_failed: '解析失败',
  other: '其他异常',
};

/** 失败类型筛选选项（大盘异常任务维度） */
export const FAILURE_TYPE_OPTIONS = Object.entries(FAILURE_TYPE_LABEL).map(
  ([value, label]) => ({ value, label }),
);
