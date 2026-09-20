// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/stores/authStore.ts
/**
 * 登录状态管理（鉴权中枢）
 *
 * 【文件职责】
 *   全局唯一的登录态来源，负责：
 *     1. 令牌与用户信息的持久化（localStorage）与读取
 *     2. 登录 / 登出 / 刷新令牌 / 拉取最新用户信息
 *     3. 权限判定：角色判定（isRole）与权限点判定（hasPermission）
 *     4. "本地 token 是否可信"的启动期校验
 *
 * 【为什么把令牌放在这里而不是组件里】
 *   axios 拦截器需要在不依赖 React 上下文的情况下读取令牌
 *   （拦截器在 React 组件树之外执行）。若令牌存在组件 state 中，
 *   拦截器就只能通过 window 全局变量或事件总线间接获取，极易失同步。
 *   放在本模块的模块级存储 + Zustand 状态中，拦截器直接 import 即可。
 *
 * 【安全说明（重要）】
 *   - localStorage 中的令牌可被同源 XSS 脚本读取。这是项目既定的演示方案，
 *     生产环境应改为 HttpOnly + Secure + SameSite 的 Cookie 方案，
 *     使 JS 无法读取令牌，从根上消除 XSS 窃取令牌的风险。
 *   - **明文密码绝不落盘**。登录成功后仅保存后端返回的令牌与用户信息，
 *     密码变量在 login() 作用域内结束即被回收。
 *   - 本地缓存的用户信息仅用于首屏渲染加速，**不作为权限判定的最终依据**。
 *     路由级判定走后端 /auth/check-permission（见 router/guards.tsx 说明）。
 */

import { create } from 'zustand';

import { authApi } from '@/api/endpoints';
import { ApiError, ERROR_CODE, registerAuthHooks } from '@/api/client';
import {
  ALL_USER_ROLES,
  ROLE_HOME_PATH,
  ROUTE_PATH,
  STORAGE_KEY,
  USER_INFO_STALE_MS,
  type NavReason,
  type Permission,
  type UserRole,
} from '@/constants';
import type { LoginRequest, UserInfo } from '@/types';

// ======================================================================
// 一、本地存储读写（防御式）
// ======================================================================

/**
 * 安全读取 localStorage
 *
 * 说明：localStorage 在隐私模式或被策略禁用时会抛异常，
 * 且存值可能被用户手工改坏（非合法 JSON）。
 * 因此所有读取都必须包在 try/catch 中，失败一律视为"无数据"，
 * 而不是让异常冒泡导致整个应用白屏。
 *
 * 参数：
 *   key: 存储键
 *
 * 返回：
 *   字符串值；不存在或读取失败时返回 null
 */
function safeGetItem(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

/**
 * 安全写入 localStorage
 *
 * 参数：
 *   key: 存储键
 *   value: 待写入字符串
 *
 * 返回：
 *   无
 */
function safeSetItem(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // 存储配额耗尽或被禁用：登录态仅存在于内存中，
    // 刷新页面即失效，但不影响本次会话使用
  }
}

/**
 * 安全移除 localStorage 项
 *
 * 参数：
 *   key: 存储键
 *
 * 返回：
 *   无
 */
function safeRemoveItem(key: string): void {
  try {
    window.localStorage.removeItem(key);
  } catch {
    // 忽略：删除失败不影响内存态清理
  }
}

/**
 * 清空全部鉴权相关的本地存储
 *
 * 说明：登出、token 失效、改密后调用。
 * 集中一处清理，避免遗漏某个键导致"看似已登出实际仍带旧令牌"。
 *
 * 返回：
 *   无
 */
function clearAuthStorage(): void {
  safeRemoveItem(STORAGE_KEY.ACCESS_TOKEN);
  safeRemoveItem(STORAGE_KEY.REFRESH_TOKEN);
  safeRemoveItem(STORAGE_KEY.USER_INFO);
  safeRemoveItem(STORAGE_KEY.TOKEN_EXPIRE_AT);
}

/**
 * 解析本地缓存的用户信息
 *
 * 返回：
 *   UserInfo；无缓存或缓存损坏时返回 null
 *
 * 说明：
 *   解析失败（手工改坏 / 旧版本结构）时返回 null 并顺带清理，
 *   保证不会把半截对象喂给界面导致渲染崩溃。
 */
function readStoredUserInfo(): UserInfo | null {
  const raw = safeGetItem(STORAGE_KEY.USER_INFO);
  if (!raw) return null;

  try {
    const parsed = JSON.parse(raw) as UserInfo;
    // 最低限度结构校验：角色值必须在已知集合内，
    // 否则后续 ROLE_HOME_PATH[role] 会取到 undefined，跳转目标落空
    if (!parsed || typeof parsed !== 'object') return null;
    if (!ALL_USER_ROLES.includes(parsed.role)) {
      clearAuthStorage();
      return null;
    }
    // permissions 缺失时补空数组，避免 hasPermission 里 .includes 抛异常
    if (!Array.isArray(parsed.permissions)) {
      parsed.permissions = [];
    }
    return parsed;
  } catch {
    clearAuthStorage();
    return null;
  }
}

// ======================================================================
// 二、对外暴露的令牌访问器（供 axios 拦截器与守卫使用）
// ======================================================================

/**
 * 读取当前访问令牌
 *
 * 说明：axios 请求拦截器直接调用本函数注入 Authorization 头。
 * 不经过 Zustand 的 hook（getState），避免拦截器与 React 渲染时序耦合。
 *
 * 返回：
 *   访问令牌字符串；未登录时返回 null
 */
export function getAccessToken(): string | null {
  return safeGetItem(STORAGE_KEY.ACCESS_TOKEN);
}

/**
 * 读取当前刷新令牌
 *
 * 返回：
 *   刷新令牌字符串；未登录时返回 null
 */
export function getRefreshToken(): string | null {
  return safeGetItem(STORAGE_KEY.REFRESH_TOKEN);
}

/**
 * 清空本地登录态（不调用后端）
 *
 * 使用场景：
 *   axios 响应拦截器检测到 401 时调用——此时令牌已失效，
 *   再调用后端登出接口只会再次 401，且会让错误处理递归，因此只做本地清理。
 *
 * 返回：
 *   无
 */
export function purgeLocalAuth(): void {
  clearAuthStorage();
  // 同步清空内存态，否则守卫仍认为已登录、
  // 会把用户放行进业务页再被接口 401 弹回，体验上表现为"闪一下又跳走"
  useAuthStore.setState({
    accessToken: null,
    refreshToken: null,
    user: null,
    lastProfileSyncAt: 0,
  });
}

/**
 * 判断访问令牌是否已在本地过期
 *
 * 说明：仅依据本地记录的过期时间戳做粗判，用于"临过期前提前刷新"，
 * **不作为是否放行的依据**（放行判定必须由后端响应决定）。
 *
 * 参数：
 *   skewMs: 提前量（毫秒），默认提前 30 秒视为已过期，
 *           避免"刚好卡在过期瞬间发出请求"导致必然失败
 *
 * 返回：
 *   true 表示已过期或即将过期
 */
export function isAccessTokenExpired(skewMs = 30_000): boolean {
  const raw = safeGetItem(STORAGE_KEY.TOKEN_EXPIRE_AT);
  if (!raw) {
    // 无过期时间记录时保守地认为"未过期"，
    // 交由后端 401 做权威判定；否则老版本存储会导致必然要求重新登录
    return false;
  }
  const expireAt = Number(raw);
  if (!Number.isFinite(expireAt)) return true;
  return Date.now() >= expireAt - skewMs;
}

// ======================================================================
// 三、Store 状态与动作定义
// ======================================================================

interface AuthState {
  // ---------------- 数据 ----------------
  /** 访问令牌（与 localStorage 同步） */
  accessToken: string | null;
  /** 刷新令牌（与 localStorage 同步） */
  refreshToken: string | null;
  /** 当前登录用户；null 表示未登录 */
  user: UserInfo | null;
  /** 登录请求进行中 */
  loggingIn: boolean;
  /** 登出请求进行中 */
  loggingOut: boolean;
  /** 最近一次向后端校验用户信息的时间戳（毫秒） */
  lastProfileSyncAt: number;

  // ---------------- 派生 ----------------
  /**
   * 是否已登录
   *
   * 【为什么要求 user 与 token 同时存在】
   *   只有 token 而 user 为 null 时，角色未知，
   *   任何基于角色的跳转与菜单过滤都会失效。
   *   这种"半登录"状态（如手工删除 user 缓存）应被视为未登录。
   */
  isAuthenticated: () => boolean;

  /** 当前角色；未登录时为 null */
  currentRole: () => UserRole | null;

  /** 当前角色的默认首页路径 */
  homePath: () => string;

  // ---------------- 权限判定 ----------------
  /**
   * 判断当前用户是否属于指定角色之一
   *
   * 参数：
   *   roles: 允许的角色列表
   *
   * 返回：
   *   有角色且命中任一角色时为 true
   */
  isRole: (...roles: UserRole[]) => boolean;

  /**
   * 判断当前用户是否拥有指定权限点
   *
   * 参数：
   *   permission: 权限点
   *
   * 返回：
   *   拥有时为 true
   *
   * 说明：
   *   基于后端下发的 user.permissions 判定，前端不自行推导。
   *   该判定用于**按钮级/组件级**显隐；路由级放行另走后端接口。
   */
  hasPermission: (permission: Permission) => boolean;

  /**
   * 判断当前用户是否拥有全部指定权限点
   *
   * 参数：
   *   permissions: 权限点列表
   *
   * 返回：
   *   全部拥有时为 true
   */
  hasAllPermissions: (...permissions: Permission[]) => boolean;

  // ---------------- 动作 ----------------
  /**
   * 登录
   *
   * 参数：
   *   payload: 账号与密码
   *
   * 返回：
   *   UserInfo：登录成功后的用户信息（调用方据此决定跳转目标）
   *
   * 异常：
   *   ApiError：业务失败时抛出（含 60001 账号密码错误 / 60007 禁用 / 60008 锁定），
   *             由登录页统一捕获并转成可读提示
   */
  login: (payload: LoginRequest) => Promise<UserInfo>;

  /**
   * 登出
   *
   * 返回：
   *   Promise<void>
   *
   * 说明：
   *   先调后端（尽力而为，失败不阻塞），再清本地并回落未登录态。
   *   之所以不因后端失败而中断：用户点了"退出"就必须退出，
   *   不能因为网络问题把人留在已登录界面。
   */
  logout: () => Promise<void>;

  /**
   * 拉取最新用户信息
   *
   * 参数：
   *   force: 为 true 时忽略缓存保鲜期强制刷新
   *
   * 返回：
   *   UserInfo；未登录或请求失败时返回 null
   *
   * 说明：
   *   应用启动时调用，用于发现"服务端已撤销令牌但本地仍有缓存"的情形。
   *   失败时若为鉴权类错误则清空登录态（由拦截器统一处理）。
   */
  fetchProfile: (force?: boolean) => Promise<UserInfo | null>;

  /**
   * 用刷新令牌换取新的访问令牌
   *
   * 返回：
   *   true 表示刷新成功
   *
   * 说明：
   *   后端刷新即轮换，成功后必须覆盖写入**两个**令牌。
   */
  refreshSession: () => Promise<boolean>;

  /**
   * 记录一次跳转原因，供登录页展示提示
   *
   * 参数：
   *   reason: 跳转原因
   *
   * 返回：
   *   无
   */
  noticeReason: NavReason | null;
  setNoticeReason: (reason: NavReason | null) => void;
}

// ======================================================================
// 四、Store 实现
// ======================================================================

/**
 * 初始状态
 *
 * 说明：直接从 localStorage 恢复，使刷新页面后登录态得以保持。
 * 恢复的是"本地记录"，其有效性仍需由后端校验（fetchProfile）。
 */
const initialAccessToken = getAccessToken();
const initialRefreshToken = getRefreshToken();
const initialUser = readStoredUserInfo();

export const useAuthStore = create<AuthState>((set, get) => ({
  // ---------------- 初始数据 ----------------
  accessToken: initialAccessToken,
  refreshToken: initialRefreshToken,
  user: initialUser,
  loggingIn: false,
  loggingOut: false,
  lastProfileSyncAt: 0,
  noticeReason: null,

  // ---------------- 派生 ----------------
  isAuthenticated: () => {
    const { accessToken, user } = get();
    return Boolean(accessToken && user);
  },

  currentRole: () => get().user?.role ?? null,

  homePath: () => {
    const role = get().user?.role;
    // 角色缺失时回落到大盘页：它是对三角色都开放的唯一页面，
    // 保证任何异常态下都有一个安全且可用的落点
    if (!role) return ROLE_HOME_PATH.business_user;
    return ROLE_HOME_PATH[role] ?? ROLE_HOME_PATH.business_user;
  },

  // ---------------- 权限判定 ----------------
  isRole: (...roles) => {
    const role = get().user?.role;
    if (!role) return false;
    return roles.includes(role);
  },

  hasPermission: (permission) => {
    const permissions = get().user?.permissions;
    if (!Array.isArray(permissions)) return false;
    return permissions.includes(permission);
  },

  hasAllPermissions: (...permissions) => {
    const owned = get().user?.permissions;
    if (!Array.isArray(owned)) return false;
    return permissions.every((item) => owned.includes(item));
  },

  setNoticeReason: (reason) => set({ noticeReason: reason }),

  // ---------------- 登录 ----------------
  login: async (payload) => {
    set({ loggingIn: true });
    try {
      const resp = await authApi.login(payload);

      // 计算本地过期时间戳，用于"临过期提前刷新"的粗判
      const expireAt = Date.now() + resp.expires_in * 1000;

      // 先写存储再改内存态：若存储写入失败（隐私模式），
      // 内存态仍然可用，本次会话正常工作，仅刷新后失效
      safeSetItem(STORAGE_KEY.ACCESS_TOKEN, resp.access_token);
      safeSetItem(STORAGE_KEY.REFRESH_TOKEN, resp.refresh_token);
      safeSetItem(STORAGE_KEY.USER_INFO, JSON.stringify(resp.user));
      safeSetItem(STORAGE_KEY.TOKEN_EXPIRE_AT, String(expireAt));

      set({
        accessToken: resp.access_token,
        refreshToken: resp.refresh_token,
        user: resp.user,
        lastProfileSyncAt: Date.now(),
        noticeReason: null,
      });

      return resp.user;
    } finally {
      // 无论成功失败都要复位 loading，否则失败一次后按钮永远转圈
      set({ loggingIn: false });
    }
  },

  // ---------------- 登出 ----------------
  logout: async () => {
    set({ loggingOut: true });
    try {
      const refreshToken = getRefreshToken();
      // 尽力而为：后端不可达或令牌已失效都不应阻止用户退出
      await authApi.logout(refreshToken ?? undefined);
    } catch {
      // 静默吞掉：登出是"用户意志"而非"服务端许可"，
      // 后端调用失败时仍必须完成本地登出
    } finally {
      clearAuthStorage();
      set({
        accessToken: null,
        refreshToken: null,
        user: null,
        lastProfileSyncAt: 0,
        loggingOut: false,
      });
    }
  },

  // ---------------- 拉取用户信息 ----------------
  fetchProfile: async (force = false) => {
    const { accessToken, lastProfileSyncAt } = get();
    if (!accessToken) return null;

    // 缓存未过期则直接用本地数据，避免每次切换路由都打接口
    if (
      !force &&
      lastProfileSyncAt > 0 &&
      Date.now() - lastProfileSyncAt < USER_INFO_STALE_MS
    ) {
      return get().user;
    }

    try {
      const user = await authApi.getMe();
      safeSetItem(STORAGE_KEY.USER_INFO, JSON.stringify(user));
      set({ user, lastProfileSyncAt: Date.now() });
      return user;
    } catch (error) {
      // 鉴权类错误由 axios 拦截器统一清空登录态并跳登录页；
      // 此处只需保证不把错误用户信息写回状态即可
      if (
        error instanceof ApiError &&
        (error.code === ERROR_CODE.TOKEN_MISSING ||
          error.code === ERROR_CODE.TOKEN_INVALID ||
          error.code === ERROR_CODE.TOKEN_EXPIRED ||
          error.code === ERROR_CODE.TOKEN_TYPE_INVALID ||
          error.code === ERROR_CODE.REFRESH_TOKEN_INVALID)
      ) {
        return null;
      }
      // 网络类错误：保留本地缓存继续可用，离线/后端未启动时不至于被迫登出
      return get().user;
    }
  },

  // ---------------- 刷新令牌 ----------------
  refreshSession: async () => {
    const refreshToken = getRefreshToken();
    if (!refreshToken) return false;

    try {
      const resp = await authApi.refresh(refreshToken);
      const expireAt = Date.now() + resp.expires_in * 1000;

      // 刷新即轮换：两个令牌都要覆盖写入，
      // 否则旧 refresh_token 在服务端已失效，下次刷新必然失败
      safeSetItem(STORAGE_KEY.ACCESS_TOKEN, resp.access_token);
      safeSetItem(STORAGE_KEY.REFRESH_TOKEN, resp.refresh_token);
      safeSetItem(STORAGE_KEY.TOKEN_EXPIRE_AT, String(expireAt));

      set({
        accessToken: resp.access_token,
        refreshToken: resp.refresh_token,
      });
      return true;
    } catch {
      // 刷新失败（refresh_token 也已过期/被撤销）→ 彻底登出
      clearAuthStorage();
      set({
        accessToken: null,
        refreshToken: null,
        user: null,
        lastProfileSyncAt: 0,
      });
      return false;
    }
  },
}));

// ======================================================================
// 五、把实现注册给 axios 客户端（见 api/client.ts 的循环依赖说明）
// ======================================================================

/**
 * 注册时机说明
 *
 * 本模块被 import 时立即执行注册。由于 main.tsx → router → 页面/守卫
 * 的加载链路一定会先加载本模块，因此首个业务请求发出前钩子必然就绪。
 *
 * 注册的是"函数"而非"令牌值"：
 *   getToken 每次调用都从 localStorage 实时读取，
 *   保证拦截器拿到的永远是最新令牌，不会因为模块初始化快照而过期。
 */
registerAuthHooks({
  /** 读取最新访问令牌 */
  getToken: () => getAccessToken(),

  /** 令牌失效：清空本地登录态（不调用后端，避免 401 递归） */
  onAuthFatal: () => purgeLocalAuth(),

  /**
   * 跳转登录页
   *
   * 参数：
   *   reason: 跳转原因，登录页据此展示不同提示
   *
   * 说明：
   *   用 window.location.replace 而非 history.pushState：
   *     - replace 不产生历史记录，用户按"后退"不会回到失效页面
   *     - 整页跳转会重新初始化应用，确保所有 store 与内存态被彻底重置，
   *       避免残留的旧角色数据导致菜单闪烁
   *   同时通过查询串回传来源路径，登录成功后可直接回到原页面。
   */
  redirectToLogin: (reason) => {
    const from = `${window.location.pathname}${window.location.search}`;
    const params = new URLSearchParams({ reason, from });
    window.location.replace(`${ROUTE_PATH.LOGIN}?${params.toString()}`);
  },
});

