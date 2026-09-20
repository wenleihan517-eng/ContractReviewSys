// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/api/client.ts
/**
 * HTTP 客户端封装
 *
 * 【文件职责】
 *   1. 创建 axios 实例，统一 baseURL / 超时 / 请求 ID 透传
 *   2. 解包后端统一响应体 {code, message, data, request_id}
 *   3. 把非 0 业务码与网络异常统一收敛为 ApiError，供上层 catch 处理
 *   4. **鉴权自动化**：请求自动携带 Authorization 头；令牌失效自动清空并跳登录页
 *
 * 【为什么解包放在这一层】
 *   后端所有接口都返回 {code, message, data} 三层结构。若让每个调用点自己判断
 *   code，一旦漏判就会把错误响应当成功数据处理（例如把 null 当列表渲染），
 *   这类缺陷很难排查。因此在此处统一拦断：code !== 0 一律抛 ApiError。
 *
 * 【为什么鉴权也放在这一层】
 *   令牌注入与 401 处理是"每个请求都要做"的横切关注点。
 *   若靠各调用点手写，漏掉一处就是一条"未带令牌"的裸奔请求；
 *   401 处理若分散在各页面，则必然出现"有的页面会踢回登录页、有的不会"。
 *   集中在此处可保证全站行为一致。
 *
 * 返回值说明：
 *   导出的 request 系列方法直接返回业务数据（已解包），失败时抛出 ApiError。
 */

import axios, { AxiosError, type AxiosInstance, type AxiosRequestConfig } from 'axios';

import type { ApiResponse } from '@/types';

// ======================================================================
// 一、错误类型
// ======================================================================

/**
 * 业务错误
 *
 * 承载后端返回的错误码与提示语，供组件展示与分支判断。
 */
export class ApiError extends Error {
  /** 业务错误码，0 为成功（不会出现在本异常中） */
  readonly code: number;

  /** HTTP 状态码，网络层异常时为 0 */
  readonly httpStatus: number;

  /** 详细错误上下文（仅开发环境由后端返回） */
  readonly detail: string;

  /**
   * 构造业务错误
   *
   * 参数：
   *   code: 业务错误码
   *   message: 面向用户的提示语
   *   httpStatus: HTTP 状态码
   *   detail: 详细上下文
   */
  constructor(code: number, message: string, httpStatus = 0, detail = '') {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.httpStatus = httpStatus;
    this.detail = detail;
  }
}

/** 与后端 app/core/exceptions.py ErrorCode 对齐的常用错误码 */
export const ERROR_CODE = {
  SUCCESS: 0,
  UNKNOWN_ERROR: 10000,
  INTERNAL_ERROR: 10001,
  SERVICE_UNAVAILABLE: 10002,
  NOT_FOUND: 10003,
  VALIDATION_ERROR: 20000,
  MONGO_ERROR: 30000,
  MINIO_ERROR: 30001,
  MILVUS_ERROR: 30002,
  LLM_ERROR: 40000,
  EMBEDDING_ERROR: 40002,
  FILE_UPLOAD_ERROR: 50000,
  FILE_TYPE_NOT_ALLOWED: 50001,
  CONTRACT_PARSE_ERROR: 50003,
  RULE_ENGINE_ERROR: 50005,
  REVIEW_TASK_ERROR: 50006,
  TASK_BLOCKED: 50007,
  APPROVAL_PERMISSION_DENIED: 50008,
  WRITE_BACK_ERROR: 50009,
  // ---------------- 鉴权段（与后端 6xxxx 对齐） ----------------
  AUTH_ERROR: 60000,
  /** 账号或密码错误（也用于账号枚举防御：用户名不存在时返回同一码） */
  CREDENTIALS_INVALID: 60001,
  /** 未携带令牌 */
  TOKEN_MISSING: 60002,
  /** 令牌非法（签名错误 / 篡改） */
  TOKEN_INVALID: 60003,
  /** 令牌已过期 */
  TOKEN_EXPIRED: 60004,
  /** 令牌类型不符（如用 refresh 当 access） */
  TOKEN_TYPE_INVALID: 60005,
  /** 刷新令牌无效 */
  REFRESH_TOKEN_INVALID: 60006,
  /** 账号已被禁用 */
  ACCOUNT_DISABLED: 60007,
  /** 账号已被锁定（连续登录失败达阈值） */
  ACCOUNT_LOCKED: 60008,
  /** 权限不足 */
  PERMISSION_DENIED: 60009,
} as const;

/**
 * 判定是否为"需要重新登录"的鉴权类错误码
 *
 * 【为什么用集合判定而非逐个 if】
 *   新增鉴权错误码时只需在此处补一项，所有调用方自动获得新行为。
 *   若散落在各处写 if，漏掉一处就会出现"某类失效不踢回登录页"，
 *   用户会卡在一个永远加载失败的页面上。
 */
const AUTH_FATAL_CODES: ReadonlySet<number> = new Set<number>([
  ERROR_CODE.TOKEN_MISSING,
  ERROR_CODE.TOKEN_INVALID,
  ERROR_CODE.TOKEN_EXPIRED,
  ERROR_CODE.TOKEN_TYPE_INVALID,
  ERROR_CODE.REFRESH_TOKEN_INVALID,
]);

/**
 * 判定错误码是否表示"登录态已不可用"
 *
 * 参数：
 *   code: 业务错误码
 *
 * 返回：
 *   true 表示需要清空登录态并要求重新登录
 */
export function isAuthFatalCode(code: number): boolean {
  return AUTH_FATAL_CODES.has(code);
}

// ======================================================================
// 二、请求链路 ID
// ======================================================================

/**
 * 生成前端请求链路 ID
 *
 * 说明：与后端 X-Request-Id 中间件配合，便于把前端日志与后端日志串起来。
 * 后端若已生成会以其为准，此 ID 仅作为前端侧兜底标识。
 *
 * 返回：
 *   形如 web-1712345678901-a1b2c3 的标识串
 */
function generateRequestId(): string {
  const ts = Date.now();
  const rand = Math.random().toString(36).slice(2, 8);
  return `web-${ts}-${rand}`;
}

// ======================================================================
// 三、鉴权钩子（避免 client 与 store 循环依赖）
// ======================================================================

/**
 * 鉴权钩子容器
 *
 * 【为什么用注册式而不是直接 import authStore】
 *   authStore 依赖 endpoints，endpoints 依赖 client —— 若 client 反过来
 *   直接 import authStore，就形成 `client → authStore → endpoints → client`
 *   的循环依赖。ESM 的循环依赖在运行时表现为"某一侧拿到 undefined"，
 *   且报错信息与实际代码位置无关，排查成本极高。
 *
 *   解法：由 authStore 在模块初始化时把实现"注册"进来，
 *   client 只持有可空的函数引用，运行时调用。这样依赖方向始终是单向的。
 */
interface AuthHooks {
  /** 读取访问令牌；未登录返回 null */
  getToken: () => string | null;
  /** 令牌失效时的清空动作（只清本地，不调后端） */
  onAuthFatal: () => void;
  /** 跳转到登录页，并携带来源与原因 */
  redirectToLogin: (reason: 'unauthenticated') => void;
}

let authHooks: AuthHooks | null = null;

/**
 * 注册鉴权钩子
 *
 * 参数：
 *   hooks: 钩子实现
 *
 * 返回：
 *   无
 *
 * 说明：
 *   由 src/stores/authStore.ts 在模块加载末尾调用一次。
 *   重复注册以最后一次为准，便于测试环境替换实现。
 */
export function registerAuthHooks(hooks: AuthHooks): void {
  authHooks = hooks;
}

/**
 * 读取当前访问令牌
 *
 * 【用途】
 *   axios 拦截器会自动注入 Authorization，但直接使用 fetch 的场景
 *   （如报告文件流下载，需绕开 JSON 解包）拿不到这层自动注入，
 *   必须显式取令牌。
 *
 * 【为什么不做成导出 authStore 的引用】
 *   保持 client 对 authStore 的单向依赖不变：此处只读取已注册的钩子，
 *   不 import 任何 store。
 *
 * 返回：
 *   令牌字符串；未登录或未注册钩子时返回 null
 */
export function getAuthToken(): string | null {
  return authHooks ? authHooks.getToken() : null;
}

/**
 * 判断当前是否处于登录页
 *
 * 说明：登录页自身发起的请求（如提交账号密码）若返回 401，
 * 属于"密码错误"而非"登录态失效"。此时若也执行跳转，
 * 会造成刷新登录页、丢失用户已输入的账号，体验极差。
 * 因此登录页上的 401 只抛错、不跳转。
 *
 * 返回：
 *   true 表示当前位于登录页
 */
function isOnLoginPage(): boolean {
  try {
    return window.location.pathname === '/login';
  } catch {
    return false;
  }
}

/**
 * 执行登录态失效处理
 *
 * 参数：
 *   reason: 固定为 unauthenticated（预留扩展：未来可加 'expired' 区分文案）
 *
 * 返回：
 *   无
 *
 * 说明：
 *   以"是否已登录"做去重，避免并发多个请求同时 401 时
 *   连续触发多次跳转与多次提示。
 */
let handlingAuthFatal = false;

function handleAuthFatal(reason: 'unauthenticated'): void {
  if (!authHooks) return;
  // 登录页上的 401 是"凭据错误"，不触发跳转
  if (isOnLoginPage()) return;
  // 去重：一次失效只处理一次
  if (handlingAuthFatal) return;

  handlingAuthFatal = true;
  try {
    authHooks.onAuthFatal();
    authHooks.redirectToLogin(reason);
  } finally {
    // 用微任务复位，保证同一轮事件循环内的并发 401 只处理一次，
    // 又不会永久锁死后续真实的失效事件
    window.setTimeout(() => {
      handlingAuthFatal = false;
    }, 0);
  }
}

// ======================================================================
// 四、axios 实例
// ======================================================================

/** 接口基础前缀，与后端 API_V1_PREFIX 保持一致 */
export const API_BASE_URL = '/api/v1';

const http: AxiosInstance = axios.create({
  baseURL: API_BASE_URL,
  // 文档解析/大模型审查链路耗时较长，超时放宽到 60 秒
  timeout: 60000,
  headers: {
    'Content-Type': 'application/json',
  },
});

// ----------------------------------------------------------------------
// 请求拦截：注入链路 ID 与鉴权令牌
// ----------------------------------------------------------------------
http.interceptors.request.use((config) => {
  config.headers.set('X-Request-Id', generateRequestId());

  // 自动携带令牌。
  // 已手工指定 Authorization 的请求（如调试或特殊场景）不覆盖，
  // 保证调用方有最终决定权。
  if (authHooks && !config.headers.has('Authorization')) {
    const token = authHooks.getToken();
    if (token) {
      config.headers.set('Authorization', `Bearer ${token}`);
    }
  }

  return config;
});

// ----------------------------------------------------------------------
// 响应拦截：解包统一响应体 + 鉴权失效处理
// ----------------------------------------------------------------------
http.interceptors.response.use(
  (response) => response,
  (error: AxiosError<ApiResponse<unknown>>) => {
    // 后端返回了结构化错误体（HTTP 4xx/5xx 且 body 符合统一响应结构）
    const body = error.response?.data;
    if (body && typeof body.code === 'number') {
      // 鉴权类错误：清空登录态并跳登录页。
      // 注意这里只处理"令牌不可用"，不处理 60001（账号密码错误）——
      // 后者是登录表单的业务失败，应由登录页自行提示。
      if (isAuthFatalCode(body.code)) {
        handleAuthFatal('unauthenticated');
      }

      // HTTP 401 但业务码不是已知鉴权码（如后端新增了码值）：
      // 兜底按登录态失效处理，保证不会出现"401 却什么都不做"的静默失败
      if (error.response?.status === 401 && !isAuthFatalCode(body.code)) {
        handleAuthFatal('unauthenticated');
      }

      return Promise.reject(
        new ApiError(
          body.code,
          body.message || '请求失败',
          error.response?.status ?? 0,
          '',
        ),
      );
    }

    // 无结构化错误体但 HTTP 为 401（如网关/代理直接拒绝）：
    // 同样视为登录态失效
    if (error.response?.status === 401) {
      handleAuthFatal('unauthenticated');
      return Promise.reject(
        new ApiError(
          ERROR_CODE.TOKEN_INVALID,
          '登录状态已失效，请重新登录',
          401,
        ),
      );
    }

    // 网络层异常：无响应体
    if (error.code === 'ECONNABORTED') {
      return Promise.reject(
        new ApiError(ERROR_CODE.SERVICE_UNAVAILABLE, '请求超时，请稍后重试', 0),
      );
    }

    if (!error.response) {
      return Promise.reject(
        new ApiError(
          ERROR_CODE.SERVICE_UNAVAILABLE,
          '无法连接到服务器，请检查后端服务是否已启动',
          0,
        ),
      );
    }

    return Promise.reject(
      new ApiError(
        ERROR_CODE.UNKNOWN_ERROR,
        error.message || '请求失败',
        error.response.status,
      ),
    );
  },
);

// ======================================================================
// 五、统一请求方法（自动解包）
// ======================================================================

/**
 * 解包统一响应体并返回业务数据
 *
 * 参数：
 *   promise: axios 请求 Promise
 *
 * 返回：
 *   业务数据（data 字段）
 *
 * 异常：
 *   ApiError：code !== 0 或 data 缺失时抛出
 */
async function unwrap<T>(promise: Promise<{ data: ApiResponse<T> }>): Promise<T> {
  const response = await promise;
  const body = response.data;

  if (body.code !== ERROR_CODE.SUCCESS) {
    throw new ApiError(body.code, body.message || '请求失败', 200);
  }

  if (body.data === null || body.data === undefined) {
    // data 为空在列表/详情场景是异常信号，显式报错优于静默返回 null
    throw new ApiError(
      ERROR_CODE.INTERNAL_ERROR,
      '服务端返回数据为空，请稍后重试',
      200,
    );
  }

  return body.data;
}

/**
 * GET 请求
 *
 * 参数：
 *   url: 接口路径（不含 baseURL）
 *   params: 查询参数
 *
 * 返回：
 *   业务数据
 */
export function get<T>(
  url: string,
  params?: Record<string, unknown>,
): Promise<T> {
  return unwrap<T>(http.get<ApiResponse<T>>(url, { params }));
}

/**
 * POST 请求
 *
 * 参数：
 *   url: 接口路径
 *   data: 请求体
 *   config: 附加 axios 配置（如文件上传时的 headers 覆盖）
 *
 * 返回：
 *   业务数据
 */
export function post<T>(
  url: string,
  data?: unknown,
  config?: AxiosRequestConfig,
): Promise<T> {
  return unwrap<T>(http.post<ApiResponse<T>>(url, data, config));
}

/**
 * PUT 请求
 *
 * 参数：
 *   url: 接口路径
 *   data: 请求体
 *
 * 返回：
 *   业务数据
 */
export function put<T>(url: string, data?: unknown): Promise<T> {
  return unwrap<T>(http.put<ApiResponse<T>>(url, data));
}

/**
 * DELETE 请求
 *
 * 参数：
 *   url: 接口路径
 *
 * 返回：
 *   业务数据
 */
export function del<T>(url: string): Promise<T> {
  return unwrap<T>(http.delete<ApiResponse<T>>(url));
}

/**
 * 上传文件
 *
 * 说明：单独封装而非复用 post，因为需要构造 multipart/form-data 并让
 * 浏览器自动补全 boundary（手动设置 Content-Type 会丢失 boundary 导致后端解析失败）。
 *
 * 参数：
 *   url: 接口路径
 *   file: 待上传文件
 *   onProgress: 上传进度回调（0-100）
 *
 * 返回：
 *   业务数据
 */
export function upload<T>(
  url: string,
  file: File,
  onProgress?: (percent: number) => void,
): Promise<T> {
  const formData = new FormData();
  formData.append('file', file);

  return unwrap<T>(
    http.post<ApiResponse<T>>(url, formData, {
      // 超时放宽：大文件上传需要更长时间
      timeout: 300000,
      // 【关键修复】必须显式声明 multipart/form-data，覆盖 http 实例默认的
      //   'Content-Type: application/json'。
      // 原因：axios 1.20 的 transformRequest 在检测到 data 是 FormData 且
      //   Content-Type 含 application/json 时，会把 FormData 序列化成 JSON
      //   （JSON.stringify(formDataToJSON(data))），导致 File 对象在 JSON 中
      //   丢失，后端收到 file 字段缺失而报 422「请求参数校验失败」。
      // 声明为 multipart/form-data 后：transformRequest 保留 FormData 原样，
      //   xhr/fetch adapter 会移除无 boundary 的 multipart 头并让浏览器自动
      //   补全 boundary（见 axios fetch adapter 的 delete('content-type') 逻辑）。
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress: (event) => {
        if (!onProgress || !event.total) return;
        const percent = Math.round((event.loaded / event.total) * 100);
        onProgress(percent);
      },
    }),
  );
}

export default http;
