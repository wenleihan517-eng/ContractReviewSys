// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/types/index.ts
/**
 * 接口契约类型定义（与后端 app/schemas 严格对齐）
 *
 * 【文件职责】
 *   把后端 Pydantic 响应模型翻译为 TypeScript 类型，作为前端类型安全的唯一来源。
 *   所有 API 调用与组件 props 的类型都从这里派生。
 *
 * 【对齐原则】
 *   字段名与后端 model_dump() 输出的 JSON key 逐字一致，不重命名、不做驼峰转换，
 *   避免"前端改名→后端改字段→静默 undefined"这类难以排查的问题。
 */

import type {
  ContractType,
  MatchType,
  Permission,
  RiskLevel,
  RuleSeverity,
  TaskStatus,
  UserRole,
  WriteBackStatus,
} from '@/constants';

// ======================================================================
// 一、统一响应封装
// ======================================================================

/**
 * 后端统一响应体
 *
 * code 为 0 表示成功，非 0 为业务错误码（见 ERROR_CODE）
 */
export interface ApiResponse<T> {
  code: number;
  message: string;
  data: T | null;
  request_id: string | null;
}

/**
 * 分页结果容器
 *
 * total_pages 由后端 page_response 计算后一并返回，前端无需自行推算
 */
export interface PageResult<T> {
  total: number;
  page: number;
  page_size: number;
  items: T[];
  total_pages: number;
}

// ======================================================================
// 二、任务管理（app/schemas/task.py）
// ======================================================================

/** 创建审查任务请求 */
export interface TaskCreateRequest {
  contract_id: string;
  contract_title?: string;
  contract_no?: string;
  submit_dept?: string;
  applicant?: string;
  counterparty?: string;
  contract_amount?: number;
  currency?: string;
  performance_period?: string;
  business_type?: string;
  contract_type?: string;
  source_type?: string;
  file_object_key?: string;
}

/** 创建任务响应 */
export interface TaskCreateResponse {
  task_id: string;
  task_no: string;
}

/**
 * 任务列表项
 *
 * 注意：后端 TaskListItem 不含 applicant / business_type / contract_amount
 * 与 risk_level，这些字段由前端在详情接口补齐（见 TaskDetailResponse）。
 * 大盘列表通过并发拉取详情聚合出完整行数据。
 */
export interface TaskListItem {
  task_id: string;
  task_no: string;
  contract_title: string;
  status: TaskStatus;
  route_type: string;
  progress: number;
  blocked_reason: string | null;
  created_at: string | null;
  failure_type?: string | null;
  failure_reason?: string | null;
  created_by?: string;
}

/** 任务详情响应 */
export interface TaskDetailResponse {
  task_id: string;
  task_no: string;
  contract_id: string;
  contract_title: string;
  contract_no: string;
  status: TaskStatus;
  route_type: string;
  progress: number;
  current_node: string | null;
  blocked_reason: string | null;
  blocked_detail: string | null;
  retry_count: number;
  max_retry: number;
  write_back_status: WriteBackStatus;
  risk_level: RiskLevel | null;
  is_blocked: boolean;
  created_at: string | null;
  updated_at: string | null;
  failure_type?: string | null;
  failure_reason?: string | null;
  created_by?: string;
}

/**
 * 大盘行数据（前端聚合模型）
 *
 * 说明：后端任务列表接口未返回申请人/业务类型/金额，
 * 前端在列表加载后按 task_id 并发拉取任务详情与审查结果聚合而成，
 * 保证大盘展示的字段完整且与后端数据同源。
 */
export interface DashboardRow {
  /** 任务标识 */
  task_id: string;
  task_no: string;
  /** 合同名称 */
  contract_title: string;
  /** 合同编号 */
  contract_no: string;
  /** 申请人 */
  applicant: string;
  /** 送审部门 */
  submit_dept: string;
  /** 业务类型 */
  business_type: string;
  /** 合同类型 */
  contract_type: ContractType | string;
  /** 合同金额（元） */
  contract_amount: number;
  /** 币种 */
  currency: string;
  /** 合同相对方 */
  counterparty: string;
  /** 任务状态 */
  status: TaskStatus;
  /** 综合风险等级（规则引擎判定，只读） */
  risk_level: RiskLevel | null;
  /** 审查进度百分比 */
  progress: number;
  /** 路由类型（fast/standard/deep） */
  route_type: string;
  /** 是否被阻塞 */
  is_blocked: boolean;
  /** 阻塞原因码 */
  blocked_reason: string | null;
  /** 重试次数 */
  retry_count: number;
  /** 重试上限 */
  max_retry: number;
  /** 回写状态 */
  write_back_status: WriteBackStatus;
  /** 创建时间 */
  created_at: string | null;
  /** 失败类型（FailureType，2026-09-20 交互优化） */
  failure_type?: string | null;
  /** 失败原因摘要（脱敏，悬浮提示用） */
  failure_reason?: string | null;
  /** 提交经办人（重新上传权限判定） */
  created_by?: string;
}

/** 任务列表查询参数 */
export interface TaskListQuery {
  status?: string;
  page?: number;
  page_size?: number;
  keyword?: string;
  failure_type?: string;
}

/** 任务重试响应 */
export interface TaskRetryResponse {
  task_no: string;
  resumed: boolean;
  message: string;
}

/** 处理阻塞任务请求 */
export interface BlockedTaskResolveRequest {
  task_no: string;
  action: 'retry' | 'dismiss';
  reason?: string;
}

/** 处理阻塞任务响应 */
export interface BlockedTaskResolveResponse {
  task_no: string;
  action: string;
  message: string;
}

/** 审批待办项 */
export interface ApprovalPendingItem {
  task_no: string;
  contract_title: string;
  risk_level: RiskLevel | null;
  conclusion_text: string;
  write_back_status: string;
}

// ======================================================================
// 三、文档解析（app/schemas/parse.py）
// ======================================================================

/** 文件上传响应 */
export interface UploadFileResponse {
  object_key: string;
  file_name: string;
  file_size: number;
  file_ext: string;
  source_type: 'digital' | 'scanned';
}

/** 触发解析请求 */
export interface ParseTriggerRequest {
  task_id: string;
  object_key: string;
  source_type?: string;
  file_name?: string;
}

/** 触发解析响应 */
export interface ParseTriggerResponse {
  task_id: string;
  accepted: boolean;
  message: string;
}

/**
 * 文本块
 *
 * start_idx / end_idx 为全文字符区间（左闭右开），
 * 与风险项 char_start / char_end 同一坐标系，是左右栏联动的关键。
 */
export interface TextBlockItem {
  block_index: number;
  page: number;
  start_idx: number;
  end_idx: number;
  paragraph_index: number;
  text: string;
}

/** 条款项 */
export interface ClauseItem {
  clause_id: string;
  clause_index: number;
  clause_type: string;
  clause_title: string;
  content: string;
}

/** 解析结果响应 */
export interface ParseResultResponse {
  task_id: string;
  parse_status: string;
  full_text: string;
  page_count: number;
  total_chars: number;
  parse_engine: string;
  source_type: string;
  text_blocks: TextBlockItem[];
  clauses: ClauseItem[];
  metadata: Record<string, unknown>;
  parse_error: string;
}

// ======================================================================
// 四、风险审查（app/schemas/review.py）
// ======================================================================

/**
 * 风险项
 *
 * 【权责红线】risk_level / decision / severity 由后端规则引擎产出，
 * 前端只读展示，不提供修改入口。
 */
export interface RiskItemResponse {
  risk_id: string;
  rule_code: string | null;
  rule_name: string;
  clause_id: string | null;
  clause_type: string | null;
  evidence_text: string;
  char_start: number | null;
  char_end: number | null;
  page_no: number | null;
  risk_level: RiskLevel;
  decision: string;
  severity: string | null;
  suggestion_text: string;
  legal_basis: string;
  reference_only: boolean;
  matched_sources: string[];
}

/** 审查结果响应 */
export interface ReviewResultResponse {
  task_id: string;
  rule_evaluated: boolean;
  risk_level: RiskLevel | null;
  is_blocked: boolean;
  risk_distribution: Record<string, number>;
  conclusion_text: string;
  risk_items: RiskItemResponse[];
}

/** 触发审查响应 */
export interface ReviewTriggerResponse {
  task_id: string;
  accepted: boolean;
  message: string;
}

/** 人工编辑风险项请求（仅建议类字段，不含判定字段） */
export interface RiskItemEditRequest {
  risk_id: string;
  suggestion_text?: string;
  legal_basis?: string;
}

/** 人工编辑风险项响应 */
export interface RiskItemEditResponse {
  risk_id: string;
  updated: boolean;
  message: string;
}

/** 人工修改推荐条款请求 */
export interface ClauseRecommendRequest {
  risk_id: string;
  recommended_text: string;
}

/** 人工修改推荐条款响应 */
export interface ClauseRecommendResponse {
  risk_id: string;
  updated: boolean;
  message: string;
}

// ======================================================================
// 五、报告与评论（app/schemas/report.py）
// ======================================================================

/**
 * 报告文件格式
 *
 * md  —— Markdown 纯文本（始终可用）
 * pdf —— PDF 正式版式（需后端 PDF_ENABLED 开启且依赖 PyMuPDF）
 */
export type ReportFormat = 'md' | 'pdf';

/** 生成报告响应 */
export interface ReportGenerateResponse {
  task_id: string;
  report_url: string;
  report_object_key: string;
  quality_passed: boolean;
  risk_item_count: number;
  /** PDF 在 MinIO 的对象键（后端可选返回） */
  pdf_object_key?: string;
  /** PDF 预签名下载链接（后端可选返回） */
  pdf_url?: string;
  /** PDF 文件大小（字节） */
  pdf_size_bytes?: number;
  /** PDF 页数 */
  pdf_page_count?: number;
  /** PDF 渲染引擎标识，固定为 pymupdf */
  pdf_render_engine?: string;
  /** 本次是否为复用已有 PDF（true 表示命中幂等，未重复渲染上传） */
  pdf_reused?: boolean;
}

/** 下载报告响应 */
export interface ReportDownloadResponse {
  task_id: string;
  /** 报告内容。注意：format=pdf 时后端固定返回空串（二进制不入 JSON） */
  content: string;
  report_url: string;
  /** MinIO 对象键（后端新增，可选） */
  report_object_key?: string;
  /** 建议下载文件名，形如 {task_id}_review_report.md */
  file_name?: string;
  /** 报告格式：md / pdf */
  report_format?: string;
  /** 报告生成时间（ISO 8601） */
  report_generated_at?: string | null;
  /** 报告是否可用 */
  report_available?: boolean;
}

/** 评论预览响应 */
export interface CommentPreviewResponse {
  task_id: string;
  comment_text: string;
  api_path: string;
  risk_item_count: number;
  validation_passed: boolean;
  validation_issues: string[];
}

/** 评论回写请求 */
export interface CommentWriteBackRequest {
  task_id: string;
  preview: boolean;
}

/** 评论回写响应 */
export interface CommentWriteBackResponse {
  task_id: string;
  write_back_status: WriteBackStatus;
  preview: boolean;
  http_status: number | null;
  response_code: number | null;
  error: string;
}

// ======================================================================
// 六、规则管理（app/schemas/rule.py）
// ======================================================================

/** 创建/更新规则请求 */
export interface RuleUpsertRequest {
  rule_code: string;
  rule_name: string;
  description?: string;
  match_type: MatchType | string;
  match_config: Record<string, unknown>;
  contract_types?: string[];
  severity: RuleSeverity | string;
  override_risk_level?: RiskLevel | null;
  suggestion_template?: string;
  legal_basis?: string;
  priority?: number;
  enabled?: boolean;
}

/** 规则模板响应 */
export interface RuleItemResponse {
  rule_code: string;
  rule_name: string;
  description: string;
  match_type: MatchType | string;
  match_config: Record<string, unknown>;
  contract_types: string[];
  severity: RuleSeverity | string;
  override_risk_level: RiskLevel | null;
  suggestion_template: string;
  legal_basis: string;
  priority: number;
  enabled: boolean;
  version: number;
}

/** 启用/禁用规则响应 */
export interface RuleEnableResponse {
  rule_code: string;
  enabled: boolean;
  message: string;
}

/** 创建/更新示范条款请求 */
export interface StandardClauseUpsertRequest {
  clause_id?: string | null;
  clause_title: string;
  clause_text: string;
  clause_type?: string;
  contract_types?: string[];
}

/** 示范条款响应 */
export interface StandardClauseResponse {
  clause_id: string;
  clause_title: string;
  clause_text: string;
  clause_type: string;
  contract_types: string[];
}

// ======================================================================
// 七、审计日志（app/schemas/audit.py 对应）
// ======================================================================

/** 审计日志项 */
export interface AuditLogItem {
  log_id: string;
  task_id: string;
  action: string;
  node_name: string | null;
  sequence: number;
  message: string;
  detail: Record<string, unknown>;
  created_at: string | null;
}

// ======================================================================
// 八、前端内部视图模型
// ======================================================================

/**
 * 带页码定位的文本片段
 *
 * 说明：左右栏联动需要"风险项 → 原文片段 → 页码 + 段落索引"三要素。
 * 后端风险项已给出 page_no / char_start / char_end，但段落索引需从
 * 文本块反查，本模型承载反查结果。
 */
export interface LocatedRisk {
  /** 风险项原数据 */
  item: RiskItemResponse;
  /** 命中所在页码（后端给出，缺失时由文本块反查补齐） */
  page_no: number;
  /** 命中所在段落索引（由文本块反查补齐） */
  paragraph_index: number;
  /** 证据文本在合同全文中的区间 */
  char_start: number;
  char_end: number;
}

/** 规则树节点（按合同类型分组的树状结构） */
export interface RuleTreeNode {
  /** 节点唯一 key */
  key: string;
  /** 节点标题 */
  title: string;
  /** 节点类型：分组或规则 */
  nodeType: 'group' | 'rule';
  /** 分组节点：该组下的规则数量 */
  ruleCount?: number;
  /** 分组节点：该组中启用的规则数量 */
  enabledCount?: number;
  /** 规则节点：规则原始数据 */
  rule?: RuleItemResponse;
  /** 子节点 */
  children?: RuleTreeNode[];
}

// ======================================================================
// 十三、登录鉴权（app/schemas/auth.py）
// ======================================================================

/**
 * 登录请求
 *
 * 字段名与后端 LoginRequest 严格一致。
 * 后端校验：username 3–50 字符且以字母开头（仅字母/数字/下划线）；
 * password 1–64 字符。
 */
export interface LoginRequest {
  /** 登录账号 */
  username: string;
  /** 登录密码（明文仅存在于内存与本请求体，绝不写入 localStorage） */
  password: string;
}

/**
 * 登录用户信息
 *
 * 【安全边界】本结构对应后端 UserInfo，是字段白名单式定义，
 * 不含 password_hash / token_version / failed_login_count 等内部字段。
 * 前端也不得自行扩展这些字段。
 */
export interface UserInfo {
  /** 用户 ID */
  user_id: string;
  /** 登录账号 */
  username: string;
  /** 展示名称 */
  display_name: string;
  /** 角色标识 */
  role: UserRole;
  /** 角色中文名（由后端下发，前端不自行拼装） */
  role_label: string;
  /** 账号状态：active / disabled / locked */
  status: string;
  /** 所属部门 */
  department: string | null;
  /** 邮箱 */
  email: string | null;
  /**
   * 该角色拥有的权限点清单
   *
   * 由后端权限矩阵实时计算下发，前端不得硬编码权限-角色映射。
   */
  permissions: Permission[];
  /** 是否系统管理员 */
  is_admin: boolean;
  /** 是否可查看全部任务（业务经办人为 false） */
  can_view_all_tasks: boolean;
}

/** 令牌信息（登录与刷新接口共用返回体结构） */
export interface TokenInfo {
  /** 访问令牌 */
  access_token: string;
  /** 刷新令牌（令牌轮换：每次刷新都会签发新的） */
  refresh_token: string;
  /** 令牌类型，固定 Bearer */
  token_type: string;
  /** 访问令牌有效期（秒） */
  expires_in: number;
  /** 刷新令牌有效期（秒） */
  refresh_expires_in: number;
}

/** 登录响应：令牌信息 + 用户信息 */
export interface LoginResponse extends TokenInfo {
  /** 登录用户信息（含角色与权限点） */
  user: UserInfo;
}

/** 刷新令牌请求 */
export interface RefreshTokenRequest {
  refresh_token: string;
}

/** 刷新令牌响应（含轮换后的新 refresh_token） */
export interface RefreshTokenResponse extends TokenInfo {
  // 刷新响应不再返回 user，用户信息由 /auth/me 单独获取，
  // 避免每个刷新周期都重复传输静态用户数据
}

/** 退出登录请求 */
export interface LogoutRequest {
  /** 刷新令牌（可选，当前后端实现按用户整体撤销） */
  refresh_token?: string;
}

/** 退出登录响应 */
export interface LogoutResponse {
  /** 已登出的账号 */
  username: string;
  /** 令牌撤销时间（ISO 8601） */
  revoked_at: string;
  /** 提示信息 */
  message: string;
}

/**
 * 权限校验请求（前端路由守卫使用）
 *
 * 【二选一】传 permission 校验权限点，传 role 校验角色；
 * 两者都不传时返回当前用户完整权限清单（用于前端初始化）。
 */
export interface PermissionCheckRequest {
  /** 待校验的权限点，如 rule_template:manage */
  permission?: string;
  /** 待校验的角色，如 law_user */
  role?: UserRole;
}

/** 权限校验响应 */
export interface PermissionCheckResponse {
  user_id: string;
  username: string;
  role: UserRole;
  role_label: string;
  /** 当前用户拥有的全部权限点 */
  permissions: Permission[];
  /** 本次校验是否通过（未指定校验目标时恒为 true） */
  passed: boolean;
  /** 本次校验类型：role / permission / none */
  checked_type: 'role' | 'permission' | 'none';
  /** 被校验的角色或权限点值 */
  checked_value: string;
  /** 校验结果说明 */
  message: string;
}

/** 角色权限矩阵单项 */
export interface RolePermissionItem {
  role: UserRole;
  role_label: string;
  permissions: Permission[];
}

/** 角色权限矩阵响应（后端为权限矩阵唯一权威源） */
export interface PermissionMatrixResponse {
  roles: RolePermissionItem[];
  total_permissions: number;
}

/** 修改密码请求 */
export interface ChangePasswordRequest {
  old_password: string;
  new_password: string;
}

/** 站内通知条目 */
export interface NotificationItem {
  notify_id: string;
  type: string;
  level: 'info' | 'critical';
  title: string;
  content: string;
  task_no: string;
  read: boolean;
  created_at?: string;
}

/** 未读数量响应 */
export interface UnreadCountResponse {
  unread_count: number;
}

/** 任务列表项新增的失败分类字段 */
export interface TaskFailureMeta {
  failure_type?: string | null;
  failure_reason?: string | null;
}
