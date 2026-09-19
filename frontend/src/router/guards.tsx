// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/router/guards.tsx
/**
 * 路由守卫组件（react-router v6）
 *
 * 【文件职责】
 *   1. RequireAuth   —— 未登录用户访问任何受保护页面时跳转登录页
 *   2. RequireRole   —— 已登录但角色无权的用户跳转到自己的默认首页，并提示无权限
 *   3. RedirectIfAuthenticated —— 已登录用户访问登录页时直接送回自己的首页
 *
 * 【为什么用"包裹组件"而不是 loader / middleware】
 *   react-router v6 的 loader 在数据路由模式下才生效，且其返回值语义是"数据"
 *   而非"是否渲染"。本项目采用组件树结构（MainLayout + Outlet），
 *   用包裹组件拦截渲染是最贴合现有结构、也是最容易读懂的做法：
 *   拦截逻辑与它保护的页面在 JSX 中紧邻，不存在"跳转发生在别处"的困惑。
 *
 * 【权限判定的两级策略（重要）】
 *   第一级：本地判定（本文件）。依据 authStore 中缓存的 user.role 与
 *           constants.ROUTE_ALLOWED_ROLES 静态表，同步得出结果。
 *           优点：零延迟，不会出现"页面白屏等待接口"的闪烁。
 *   第二级：服务端判定。后端 /auth/check-permission 才是权限的最终权威。
 *           本地表仅用于**提前拦截**，绝不能作为放行依据——
 *           即：本地说"不行"就一定不行；本地说"行"还可能被后端拒绝（403）。
 *   这样设计既保证了体验，又不把安全边界放在前端。
 */

import { useEffect, useRef } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { App } from 'antd';

import {
  NAV_REASON,
  NO_PERMISSION_NOTICE_SECONDS,
  ROLE_HOME_PATH,
  ROUTE_ALLOWED_ROLES,
  ROUTE_PATH,
  ROUTE_TITLE,
  USER_ROLE_LABEL,
  type UserRole,
} from '@/constants';
import { useAuthStore } from '@/stores/authStore';

// ======================================================================
// 一、路径 → 允许角色 查询
// ======================================================================

/**
 * 查询某个路径允许访问的角色清单
 *
 * 参数：
 *   pathname: 目标路径（如 /workbench/T-001）
 *
 * 返回：
 *   允许的角色数组；该路径未登记时返回 null（表示"任意登录用户可访问"）
 *
 * 说明：
 *   路径可能带子段（如 /workbench/T-001），因此需要按"最长前缀"匹配，
 *   而不是全等比较——否则工作台详情页会因为查不到 /workbench/T-001
 *   这个键而被误判为"任意登录用户可访问"，直接绕过角色限制。
 */
function resolveAllowedRoles(pathname: string): UserRole[] | null {
  // 先试全等（精确登记的路径）
  if (ROUTE_ALLOWED_ROLES[pathname]) {
    return ROUTE_ALLOWED_ROLES[pathname];
  }

  // 再按最长前缀匹配：把 /workbench/T-001 归到 /workbench
  const candidates = Object.keys(ROUTE_ALLOWED_ROLES)
    .filter((key) => pathname === key || pathname.startsWith(`${key}/`))
    // 取最长者，保证 /rules/x 命中 /rules 而非更短的其它键
    .sort((a, b) => b.length - a.length);

  if (candidates.length > 0) {
    return ROUTE_ALLOWED_ROLES[candidates[0]];
  }

  return null;
}

/**
 * 解析路径对应的页面中文名（用于提示语）
 *
 * 参数：
 *   pathname: 目标路径
 *
 * 返回：
 *   页面名；未登记时返回"该页面"
 */
function resolvePathTitle(pathname: string): string {
  if (ROUTE_TITLE[pathname]) return ROUTE_TITLE[pathname];

  const matched = Object.keys(ROUTE_TITLE)
    .filter((key) => pathname === key || pathname.startsWith(`${key}/`))
    .sort((a, b) => b.length - a.length);

  return matched.length > 0 ? ROUTE_TITLE[matched[0]] : '该页面';
}

// ======================================================================
// 二、RequireAuth：登录校验
// ======================================================================

interface RequireAuthProps {
  /** 受保护的页面内容 */
  children: React.ReactNode;
}

/**
 * 登录校验包裹组件
 *
 * 行为：
 *   未登录 → 重定向到 /login，并通过 state 携带来源路径与原因
 *   已登录 → 原样渲染 children
 *
 * 参数：
 *   children: 受保护内容
 *
 * 返回：
 *   React.ReactElement
 *
 * 说明：
 *   `replace` 是必需的——若用 push，用户点"后退"会回到刚才被拦截的页面，
 *   而该页面立即又把他弹回登录页，形成"后退键失效"的观感。
 */
export function RequireAuth({ children }: RequireAuthProps): React.ReactElement {
  const location = useLocation();
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated());

  if (!isAuthenticated) {
    // 把来源路径拼进查询串，登录成功后可直接回跳。
    // 用 search 而非 state：整页跳转（axios 401 处理）会丢失 history state，
    // 而查询串在各种跳转方式下都能保留。
    const from = `${location.pathname}${location.search}`;
    const params = new URLSearchParams({
      reason: NAV_REASON.UNAUTHENTICATED,
      from,
    });
    return <Navigate to={`${ROUTE_PATH.LOGIN}?${params.toString()}`} replace />;
  }

  return <>{children}</>;
}

// ======================================================================
// 三、RequireRole：角色校验
// ======================================================================

interface RequireRoleProps {
  /** 允许访问的角色清单 */
  allow: UserRole[];
  /** 受保护的页面内容 */
  children: React.ReactNode;
}

/**
 * 角色校验包裹组件
 *
 * 行为：
 *   角色命中允许清单 → 渲染 children
 *   角色不命中       → 跳转到该角色的默认首页，并弹出无权限提示
 *
 * 参数：
 *   allow: 允许的角色
 *   children: 受保护内容
 *
 * 返回：
 *   React.ReactElement
 *
 * 【无权限提示为什么放在这里而不是目标页】
 *   跳转是一次性动作，而提示是一次性副作用。若把提示放在目标页，
 *   目标页需要判断"我是被踢过来的吗"，这个状态只能靠 location.state
 *   传递，且目标页可能被其它入口进入（导致误弹）。
 *   放在守卫里，只在"确实发生拦截"这条路径上执行，语义最准确。
 *
 *   用 ref 做一次性守卫：React 18 严格模式下组件会挂载两次，
 *   不加锁会出现同样的提示弹两遍。
 */
export function RequireRole({
  allow,
  children,
}: RequireRoleProps): React.ReactElement {
  const location = useLocation();
  const { message, notification } = App.useApp();
  const user = useAuthStore((state) => state.user);
  const homePath = useAuthStore((state) => state.homePath());
  const role = user?.role ?? null;

  /** 是否本次渲染需要拦截（用于在 effect 中安全地触发提示） */
  const shouldBlock = Boolean(role && !allow.includes(role));
  /** 一次性提示锁，避免严格模式双挂载导致重复提示 */
  const noticedRef = useRef(false);

  useEffect(() => {
    if (!shouldBlock || noticedRef.current) return;
    noticedRef.current = true;

    const pageTitle = resolvePathTitle(location.pathname);
    const roleLabel = role ? USER_ROLE_LABEL[role] : '当前角色';

    // notification 承载较长的说明（含角色与目标页），
    // message 只做一句话摘要会丢失"为什么被拦"的关键信息
    notification.warning({
      message: '无访问权限',
      description: `「${roleLabel}」无权访问${pageTitle}，已跳转至你的默认工作页。如需访问请联系系统管理员调整角色。`,
      placement: 'topRight',
      duration: NO_PERMISSION_NOTICE_SECONDS,
      key: 'no-permission-notice',
    });

    // 同时留一条轻量 toast，保证即使用户立刻切换页面也能感知
    message.warning(`无权访问${pageTitle}，已跳转至默认工作页`);
  }, [shouldBlock, location.pathname, role, message, notification]);

  if (!role) {
    // 理论上不会走到这里（外层 RequireAuth 已拦截），
    // 但仍显式处理，避免"角色为空"时被当作"有权限"而放行
    const params = new URLSearchParams({
      reason: NAV_REASON.UNAUTHENTICATED,
      from: `${location.pathname}${location.search}`,
    });
    return <Navigate to={`${ROUTE_PATH.LOGIN}?${params.toString()}`} replace />;
  }

  if (shouldBlock) {
    return <Navigate to={homePath} replace />;
  }

  return <>{children}</>;
}

// ======================================================================
// 四、RedirectIfAuthenticated：已登录用户访问登录页
// ======================================================================

interface RedirectIfAuthenticatedProps {
  /** 登录页内容 */
  children: React.ReactNode;
}

/**
 * 已登录用户访问登录页时的重定向
 *
 * 行为：
 *   已登录 → 送回该角色的默认首页（避免重复登录造成状态混乱）
 *   未登录 → 正常展示登录页
 *
 * 参数：
 *   children: 登录页内容
 *
 * 返回：
 *   React.ReactElement
 *
 * 【为什么不直接跳大盘】
 *   各角色的默认首页不同（管理员→规则页、法务→工作台）。
 *   统一跳大盘会让管理员每次登录都被丢到一个非本职的页面上。
 */
export function RedirectIfAuthenticated({
  children,
}: RedirectIfAuthenticatedProps): React.ReactElement {
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated());
  const homePath = useAuthStore((state) => state.homePath());

  if (isAuthenticated) {
    return <Navigate to={homePath} replace />;
  }

  return <>{children}</>;
}

// ======================================================================
// 五、页面级权限包裹（按路径自动取角色清单）
// ======================================================================

interface RouteGuardProps {
  /** 受保护内容 */
  children: React.ReactNode;
}

/**
 * 组合式守卫：登录校验 + 按路径的角色校验
 *
 * 参数：
 *   children: 受保护内容
 *
 * 返回：
 *   React.ReactElement
 *
 * 说明：
 *   本组件从当前 location 推导目标路径的角色清单，
 *   因此路由表里只需写 <RouteGuard> 包住页面，
 *   不需要在每个 <Route> 上手写 allow 列表——少写一处就少一处漂移风险。
 */
export function RouteGuard({ children }: RouteGuardProps): React.ReactElement {
  const location = useLocation();

  return (
    <RequireAuth>
      <RoleGateByPath pathname={location.pathname}>
        {children}
      </RoleGateByPath>
    </RequireAuth>
  );
}

/**
 * 按路径判定角色的内部组件
 *
 * 参数：
 *   pathname: 目标路径
 *   children: 内容
 *
 * 返回：
 *   React.ReactElement
 */
function RoleGateByPath({
  pathname,
  children,
}: {
  pathname: string;
  children: React.ReactNode;
}): React.ReactElement {
  const allowed = resolveAllowedRoles(pathname);

  // 未登记角色限制的路径：任意已登录用户可访问
  if (!allowed) {
    return <>{children}</>;
  }

  return <RequireRole allow={allowed}>{children}</RequireRole>;
}

/**
 * 导出内部工具供测试使用
 *
 * 说明：这两个函数是纯函数，单独导出便于在冒烟测试中直接断言
 * 路径→角色的映射正确性，无需渲染组件。
 */
export const __testables = {
  resolveAllowedRoles,
  resolvePathTitle,
  ROLE_HOME_PATH,
};
