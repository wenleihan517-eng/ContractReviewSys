// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/api/endpoints.ts
/**
 * 接口调用封装（与后端 app/api/v1/endpoints 一一对应）
 *
 * 【文件职责】
 *   把后端 RESTful 接口封装为强类型函数，供 store 层调用。
 *   组件不直接调用 axios，一律经由 store → endpoints 的路径，
 *   保证请求参数拼装与响应类型转换集中可控。
 *
 * 【路径对齐说明】
 *   后端 API_V1_PREFIX 为 /api/v1，client.ts 的 baseURL 已包含该前缀，
 *   因此此处路径只需写业务段（如 /tasks、/review/{id}/result）。
 */

import {
  API_BASE_URL,
  ApiError,
  ERROR_CODE,
  del,
  get,
  getAuthToken,
  post,
  put,
  upload,
} from '@/api/client';
import type {
  ApprovalPendingItem,
  BlockedTaskResolveRequest,
  BlockedTaskResolveResponse,
  ChangePasswordRequest,
  ClauseRecommendRequest,
  ClauseRecommendResponse,
  CommentPreviewResponse,
  CommentWriteBackRequest,
  CommentWriteBackResponse,
  LoginRequest,
  LoginResponse,
  LogoutRequest,
  LogoutResponse,
  PageResult,
  ParseResultResponse,
  ParseTriggerRequest,
  ParseTriggerResponse,
  PermissionCheckRequest,
  PermissionCheckResponse,
  PermissionMatrixResponse,
  RefreshTokenRequest,
  RefreshTokenResponse,
  ReportDownloadResponse,
  ReportFormat,
  ReportGenerateResponse,
  ReviewResultResponse,
  ReviewTriggerResponse,
  RiskItemEditRequest,
  RiskItemEditResponse,
  RuleEnableResponse,
  RuleItemResponse,
  RuleUpsertRequest,
  StandardClauseResponse,
  StandardClauseUpsertRequest,
  TaskCreateRequest,
  TaskCreateResponse,
  TaskDetailResponse,
  TaskListItem,
  TaskListQuery,
  TaskRetryResponse,
  UploadFileResponse,
  UserInfo,
} from '@/types';
import type { ApiResponse } from '@/types';

// ======================================================================
// 一、任务管理（/tasks）
// ======================================================================

/** 任务接口集合 */
export const taskApi = {
  /**
   * 创建审查任务
   *
   * 参数：
   *   body: 任务创建请求
   *
   * 返回：
   *   { task_id, task_no }
   */
  create(body: TaskCreateRequest): Promise<TaskCreateResponse> {
    return post<TaskCreateResponse>('/tasks', body);
  },

  /**
   * 查询任务列表
   *
   * 参数：
   *   query: 分页与状态过滤参数
   *
   * 返回：
   *   分页任务列表
   */
  list(query: TaskListQuery): Promise<PageResult<TaskListItem>> {
    return get<PageResult<TaskListItem>>('/tasks', {
      status: query.status,
      page: query.page ?? 1,
      page_size: query.page_size ?? 20,
      keyword: query.keyword,
    });
  },

  /**
   * 查询任务详情
   *
   * 参数：
   *   taskNo: 任务编号
   *
   * 返回：
   *   任务详情
   */
  detail(taskNo: string): Promise<TaskDetailResponse> {
    return get<TaskDetailResponse>(`/tasks/${encodeURIComponent(taskNo)}`);
  },

  /**
   * 任务重试（重置失败/阻塞任务）
   *
   * 参数：
   *   taskNo: 任务编号
   *
   * 返回：
   *   重试结果
   */
  retry(taskNo: string): Promise<TaskRetryResponse> {
    return post<TaskRetryResponse>(
      `/tasks/${encodeURIComponent(taskNo)}/retry`,
      { task_no: taskNo },
    );
  },

  /**
   * 处理阻塞任务（重跑 / 驳回）
   *
   * 参数：
   *   taskNo: 任务编号
   *   body: 处理动作与说明
   *
   * 返回：
   *   处理结果
   */
  resolveBlocked(
    taskNo: string,
    body: BlockedTaskResolveRequest,
  ): Promise<BlockedTaskResolveResponse> {
    return post<BlockedTaskResolveResponse>(
      `/tasks/${encodeURIComponent(taskNo)}/resolve`,
      body,
    );
  },

  /**
   * 拉取审批待办
   *
   * 参数：
   *   page / pageSize: 分页参数
   *
   * 返回：
   *   待审批任务分页列表
   */
  approvalPending(
    page = 1,
    pageSize = 20,
  ): Promise<PageResult<ApprovalPendingItem>> {
    return get<PageResult<ApprovalPendingItem>>('/tasks/approval-pending', {
      page,
      page_size: pageSize,
    });
  },
};

// ======================================================================
// 二、文档解析（/parse）
// ======================================================================

/** 解析接口集合 */
export const parseApi = {
  /**
   * 上传合同文件
   *
   * 参数：
   *   file: 待上传文件
   *   onProgress: 上传进度回调
   *
   * 返回：
   *   对象键与来源类型
   */
  upload(
    file: File,
    onProgress?: (percent: number) => void,
  ): Promise<UploadFileResponse> {
    return upload<UploadFileResponse>('/parse/upload', file, onProgress);
  },

  /**
   * 触发文档解析
   *
   * 参数：
   *   taskId: 任务 ID
   *   body: 触发参数（对象键 / 来源类型 / 文件名）
   *
   * 返回：
   *   受理结果
   */
  trigger(
    taskId: string,
    body: ParseTriggerRequest,
  ): Promise<ParseTriggerResponse> {
    return post<ParseTriggerResponse>(
      `/parse/${encodeURIComponent(taskId)}/trigger`,
      body,
    );
  },

  /**
   * 查询解析结果
   *
   * 参数：
   *   taskId: 任务 ID
   *
   * 返回：
   *   全文 / 文本块 / 条款 / 元数据
   */
  result(taskId: string): Promise<ParseResultResponse> {
    return get<ParseResultResponse>(
      `/parse/${encodeURIComponent(taskId)}/result`,
    );
  },
};

// ======================================================================
// 三、风险审查（/review）
// ======================================================================

/** 审查接口集合 */
export const reviewApi = {
  /**
   * 触发风险审查
   *
   * 参数：
   *   taskId: 任务 ID
   *
   * 返回：
   *   受理结果
   */
  trigger(taskId: string): Promise<ReviewTriggerResponse> {
    return post<ReviewTriggerResponse>(
      `/review/${encodeURIComponent(taskId)}/trigger`,
      { task_id: taskId },
    );
  },

  /**
   * 查询审查结果
   *
   * 参数：
   *   taskId: 任务 ID
   *
   * 返回：
   *   风险项列表 / 结论 / 等级分布
   */
  result(taskId: string): Promise<ReviewResultResponse> {
    return get<ReviewResultResponse>(
      `/review/${encodeURIComponent(taskId)}/result`,
    );
  },

  /**
   * 人工编辑风险项建议
   *
   * 【权责红线】仅提交建议类字段，不提交任何判定字段。
   *
   * 参数：
   *   riskId: 风险项 ID
   *   body: 建议文本与法律依据
   *
   * 返回：
   *   更新结果
   */
  editRiskItem(
    riskId: string,
    body: RiskItemEditRequest,
  ): Promise<RiskItemEditResponse> {
    return put<RiskItemEditResponse>(
      `/review/risk-items/${encodeURIComponent(riskId)}`,
      body,
    );
  },

  /**
   * 人工修改推荐条款
   *
   * 参数：
   *   riskId: 风险项 ID
   *   body: 修订后的示范条款
   *
   * 返回：
   *   更新结果
   */
  updateRecommend(
    riskId: string,
    body: ClauseRecommendRequest,
  ): Promise<ClauseRecommendResponse> {
    return put<ClauseRecommendResponse>(
      `/review/risk-items/${encodeURIComponent(riskId)}/recommend`,
      body,
    );
  },
};

// ======================================================================
// 四、报告与评论（/report）
// ======================================================================

/** 报告与评论接口集合 */
export const reportApi = {
  /**
   * 生成审查报告
   *
   * 参数：
   *   taskId: 任务 ID
   *   format: 导出格式，默认 md
   *
   * 返回：
   *   报告链接与质量校验结果
   */
  generate(taskId: string, format = 'md'): Promise<ReportGenerateResponse> {
    return post<ReportGenerateResponse>(
      `/report/${encodeURIComponent(taskId)}/generate`,
      { task_id: taskId, format },
    );
  },

  /**
   * 预览审批评论
   *
   * 参数：
   *   taskId: 任务 ID
   *
   * 返回：
   *   评论文本与格式校验结果
   */
  previewComment(taskId: string): Promise<CommentPreviewResponse> {
    return post<CommentPreviewResponse>(
      `/report/${encodeURIComponent(taskId)}/comment/preview`,
      { task_id: taskId },
    );
  },

  /**
   * 回写审批评论
   *
   * 【权责红线】接口仅写入评论区，不改变审批结论。
   *
   * 参数：
   *   taskId: 任务 ID
   *   body: 回写请求（preview 为 true 时仅演练不落库）
   *
   * 返回：
   *   回写状态
   */
  writeBackComment(
    taskId: string,
    body: CommentWriteBackRequest,
  ): Promise<CommentWriteBackResponse> {
    return post<CommentWriteBackResponse>(
      `/report/${encodeURIComponent(taskId)}/comment/write-back`,
      body,
    );
  },

  /**
   * 下载报告内容
   *
   * 参数：
   *   taskId: 任务 ID
   *
   * 返回：
   *   Markdown 内容与预签名链接（JSON 模式，响应结构未变）
   */
  download(taskId: string): Promise<ReportDownloadResponse> {
    return get<ReportDownloadResponse>(
      `/report/${encodeURIComponent(taskId)}/download`,
    );
  },

  /**
   * 下载报告文件（浏览器直接落盘）
   *
   * 说明：
   *   走后端 mode=stream 通道，返回文件流并带 Content-Disposition。
   *   该请求绕开统一 JSON 解包（unwrap），因此直接使用底层 fetch，
   *   避免二进制流被当作 JSON 解析而报错。
   *
   *   必须手动携带 Authorization 头：fetch 不走 axios 拦截器，
   *   令牌不会自动注入。
   *
   * 参数：
   *   taskId: 任务 ID
   *   format: 报告格式，'md'（Markdown，默认）或 'pdf'。
   *           后端 format 参数默认值为 md，因此不传时行为与历史版本完全一致，
   *           旧调用点无需改动。
   *
   * 返回：
   *   Promise<void>，成功时已触发浏览器下载
   *
   * 异常：
   *   ApiError：网络错误、鉴权失败或业务错误码
   */
  async downloadFile(
    taskId: string,
    format: ReportFormat = 'md',
  ): Promise<void> {
    const token = getAuthToken();
    const url = `${API_BASE_URL}/report/${encodeURIComponent(
      taskId,
    )}/download?mode=stream&format=${format}`;

    const resp = await fetch(url, {
      method: 'GET',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });

    if (!resp.ok) {
      // 后端错误体为统一 JSON 结构，尝试解析出业务码与提示
      // 注意：显式标注 number，因为 ERROR_CODE 是 as const，
      // 直接取用会被推断为字面量类型而无法被后续赋值覆盖。
      let code: number = ERROR_CODE.UNKNOWN_ERROR;
      let message = `下载失败（HTTP ${resp.status}）`;
      try {
        const body = (await resp.json()) as ApiResponse<unknown>;
        if (typeof body.code === 'number') {
          code = body.code;
          message = body.message || message;
        }
      } catch {
        // 非 JSON 错误体：保留兜底文案
      }
      throw new ApiError(code, message, resp.status);
    }

    // 从 Content-Disposition 解析文件名，解析不到则用约定命名兜底
    const disposition = resp.headers.get('Content-Disposition') || '';
    const matched = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
    const fileName = matched?.[1]
      ? decodeURIComponent(matched[1])
      : `${taskId}_review_report.${format}`;

    const blob = await resp.blob();
    const objectUrl = window.URL.createObjectURL(blob);
    try {
      const anchor = document.createElement('a');
      anchor.href = objectUrl;
      anchor.download = fileName;
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
    } finally {
      // 必须释放，否则 blob 常驻内存直到页面卸载
      window.URL.revokeObjectURL(objectUrl);
    }
  },
};

// ======================================================================
// 五、规则管理（/rules）
// ======================================================================

/** 规则接口集合 */
export const ruleApi = {
  /**
   * 查询规则列表
   *
   * 参数：
   *   contractType: 合同类型过滤（不传则查全部）
   *   page / pageSize: 分页参数
   *
   * 返回：
   *   规则分页列表
   */
  list(
    contractType?: string,
    page = 1,
    pageSize = 200,
  ): Promise<PageResult<RuleItemResponse>> {
    return get<PageResult<RuleItemResponse>>('/rules', {
      contract_type: contractType,
      page,
      page_size: pageSize,
    });
  },

  /**
   * 创建规则
   *
   * 参数：
   *   body: 规则配置
   *
   * 返回：
   *   { rule_code }
   */
  create(body: RuleUpsertRequest): Promise<{ rule_code: string }> {
    return post<{ rule_code: string }>('/rules', body);
  },

  /**
   * 更新规则
   *
   * 参数：
   *   ruleCode: 规则编码
   *   body: 规则配置
   *
   * 返回：
   *   { rule_code }
   */
  update(
    ruleCode: string,
    body: RuleUpsertRequest,
  ): Promise<{ rule_code: string }> {
    return put<{ rule_code: string }>(
      `/rules/${encodeURIComponent(ruleCode)}`,
      body,
    );
  },

  /**
   * 启用/禁用规则
   *
   * 参数：
   *   ruleCode: 规则编码
   *   enabled: 目标启用状态
   *
   * 返回：
   *   更新后的启用状态
   */
  setEnabled(ruleCode: string, enabled: boolean): Promise<RuleEnableResponse> {
    return put<RuleEnableResponse>(
      `/rules/${encodeURIComponent(ruleCode)}/enabled`,
      { rule_code: ruleCode, enabled },
    );
  },

  /**
   * 删除规则
   *
   * 参数：
   *   ruleCode: 规则编码
   *
   * 返回：
   *   删除结果
   */
  remove(ruleCode: string): Promise<{ rule_code: string }> {
    return del<{ rule_code: string }>(`/rules/${encodeURIComponent(ruleCode)}`);
  },

  /**
   * 查询示范条款库
   *
   * 参数：
   *   clauseType: 条款类型过滤
   *   page / pageSize: 分页参数
   *
   * 返回：
   *   示范条款分页列表
   */
  listStandardClauses(
    clauseType?: string,
    page = 1,
    pageSize = 100,
  ): Promise<PageResult<StandardClauseResponse>> {
    return get<PageResult<StandardClauseResponse>>('/rules/standard-clauses', {
      clause_type: clauseType,
      page,
      page_size: pageSize,
    });
  },

  /**
   * 创建示范条款
   *
   * 参数：
   *   body: 条款内容
   *
   * 返回：
   *   { clause_id }
   */
  createStandardClause(
    body: StandardClauseUpsertRequest,
  ): Promise<{ clause_id: string }> {
    return post<{ clause_id: string }>('/rules/standard-clauses', body);
  },
};

// ======================================================================
// 六、审计日志（/audit）
// ======================================================================

/** 审计接口集合 */
export const auditApi = {
  /**
   * 查询任务操作记录
   *
   * 参数：
   *   taskId: 任务 ID
   *   limit: 返回条数上限
   *
   * 返回：
   *   审计日志列表
   */
  logs(taskId: string, limit = 100): Promise<unknown[]> {
    return get<unknown[]>(
      `/audit/tasks/${encodeURIComponent(taskId)}/logs`,
      { limit },
    );
  },
};

// ======================================================================
// 七、登录鉴权（/auth）
// ======================================================================

/**
 * 鉴权接口集合
 *
 * 【调用顺序约定】
 *   登录    → login()      返回 access_token / refresh_token / user
 *   进应用  → getMe()      用本地 token 换最新用户信息（校验 token 是否仍有效）
 *   令牌过期→ refresh()    用 refresh_token 换新 access_token（同时轮换 refresh_token）
 *   退出    → logout()     服务端递增 token_version，使该用户全部令牌失效
 *   路由守卫→ checkPermission() 按角色或权限点做权威校验
 */
export const authApi = {
  /**
   * 账号密码登录
   *
   * 参数：
   *   body: 账号与密码
   *
   * 返回：
   *   LoginResponse（含双令牌与用户信息）
   *
   * 异常：
   *   ApiError code 60001 —— 账号或密码错误
   *   ApiError code 60007 —— 账号已被禁用
   *   ApiError code 60008 —— 账号已锁定（连续失败达阈值）
   *
   * 说明：
   *   本接口必须处于后端鉴权白名单中，否则未登录状态下无法调用。
   */
  login(body: LoginRequest): Promise<LoginResponse> {
    return post<LoginResponse>('/auth/login', body);
  },

  /**
   * 刷新访问令牌
   *
   * 参数：
   *   refreshToken: 本地保存的刷新令牌
   *
   * 返回：
   *   RefreshTokenResponse（含轮换后的新 refresh_token）
   *
   * 异常：
   *   ApiError code 60006 —— 刷新令牌无效或已过期
   *
   * 说明：
   *   后端采用"刷新即轮换"：旧 refresh_token 用过一次后即失效，
   *   因此调用方**必须**把返回的新 refresh_token 覆盖写入本地，
   *   否则下次刷新会因版本不匹配而失败。
   */
  refresh(refreshToken: string): Promise<RefreshTokenResponse> {
    const body: RefreshTokenRequest = { refresh_token: refreshToken };
    return post<RefreshTokenResponse>('/auth/refresh', body);
  },

  /**
   * 退出登录
   *
   * 参数：
   *   refreshToken: 可选，当前后端按用户整体撤销，传入不改变结果
   *
   * 返回：
   *   LogoutResponse
   *
   * 说明：
   *   后端递增 token_version 使该用户**全部**已签发令牌失效，
   *   因此前端清空本地存储后无需再等待额外响应即可跳转登录页。
   *   该接口失败（如 token 已失效）不应阻塞前端登出流程。
   */
  logout(refreshToken?: string): Promise<LogoutResponse> {
    const body: LogoutRequest = refreshToken
      ? { refresh_token: refreshToken }
      : {};
    return post<LogoutResponse>('/auth/logout', body);
  },

  /**
   * 获取当前登录用户信息
   *
   * 返回：
   *   UserInfo（含角色、角色中文名与权限点清单）
   *
   * 异常：
   *   ApiError code 60002 —— 未携带令牌
   *   ApiError code 60003 —— 令牌非法
   *   ApiError code 60004 —— 令牌已过期
   *
   * 说明：
   *   这是"本地 token 是否仍然有效"的权威判定入口。
   *   应用启动时调用一次，可发现"服务端已撤销但本地仍有 token"的情形。
   */
  getMe(): Promise<UserInfo> {
    return get<UserInfo>('/auth/me');
  },

  /**
   * 权限校验（前端路由守卫使用）
   *
   * 参数：
   *   query.permission: 待校验权限点（与 role 二选一）
   *   query.role:       待校验角色（与 permission 二选一）
   *
   * 返回：
   *   PermissionCheckResponse，其中 passed 为本次校验结论
   *
   * 说明：
   *   两者都不传时后端返回当前用户完整权限清单，用于前端初始化。
   */
  checkPermission(
    query: PermissionCheckRequest = {},
  ): Promise<PermissionCheckResponse> {
    return post<PermissionCheckResponse>('/auth/check-permission', query);
  },

  /**
   * 获取角色权限矩阵
   *
   * 返回：
   *   PermissionMatrixResponse（各角色的权限点清单 + 权限点总数）
   *
   * 说明：
   *   后端是权限矩阵的唯一权威源。前端可用本接口渲染"权限说明"页面，
   *   但**不应**用它本地推导"某路由是否可访问"——路由级判定必须
   *   走后端 checkPermission，避免前端矩阵缓存过期导致越权放行。
   */
  getPermissionMatrix(): Promise<PermissionMatrixResponse> {
    return get<PermissionMatrixResponse>('/auth/permission-matrix');
  },

  /**
   * 修改当前用户密码
   *
   * 参数：
   *   body: 旧密码与新密码
   *
   * 返回：
   *   无业务数据的成功标记
   *
   * 异常：
   *   ApiError code 60001 —— 旧密码错误
   *
   * 说明：
   *   改密成功后后端会递增 token_version，当前令牌立即失效，
   *   前端应主动清空本地存储并跳转登录页要求重新登录。
   */
  changePassword(body: ChangePasswordRequest): Promise<Record<string, never>> {
    return post<Record<string, never>>('/auth/change-password', body);
  },
};
