// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/router/index.tsx
/**
 * 路由配置（含登录页与角色权限守卫）
 *
 * 【文件职责】
 *   集中声明前端路由表，并完成三件事：
 *     1. 登录页独立于主布局（不能嵌在带侧边栏的 MainLayout 里）
 *     2. 所有业务页面包裹 RouteGuard —— 未登录跳登录页、角色无权跳默认首页
 *     3. 根路径按角色落到各自默认首页（管理员≠大盘）
 *
 * 【为何使用懒加载】
 *   工作台页面依赖 pdfjs-dist（体积较大），规则页也只在需要时访问。
 *   通过 React.lazy 拆分，可使登录页与大盘页首屏只加载必要代码，
 *   避免为了一次列表浏览而下载整个 PDF 渲染引擎。
 *
 * 【路由清单与访问角色】
 *   /login               登录页（任意人可访问；已登录则被送回默认首页）
 *   /                    按角色重定向到默认首页
 *   /dashboard           合同审查大盘      —— 三种角色均可
 *   /workbench           智能审查工作台    —— 法务 / 管理员
 *   /workbench/:taskNo   智能审查工作台    —— 法务 / 管理员
 *   /rules               合规规则与模板    —— 仅管理员
 *   *                    兜底：按角色回默认首页
 *
 * 【为什么不用 loader 做鉴权】
 *   见 guards.tsx 的文件头说明——本项目为组件树结构，
 *   用包裹组件拦截最贴合现有组织方式，也最容易排查"为什么被跳走"。
 */

import { lazy, Suspense } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import { Spin } from 'antd';

import MainLayout from '@/layouts/MainLayout';
import { RedirectIfAuthenticated, RouteGuard } from '@/router/guards';
import { useAuthStore } from '@/stores/authStore';

// ---------------- 页面懒加载 ----------------
const LoginPage = lazy(() => import('@/pages/LoginPage'));
const DashboardPage = lazy(() => import('@/pages/DashboardPage'));
const WorkbenchPage = lazy(() => import('@/pages/WorkbenchPage'));
const RulesPage = lazy(() => import('@/pages/RulesPage'));

/**
 * 页面加载占位
 *
 * 说明：懒加载切换瞬间展示，避免出现空白屏。
 * 这是加载态而非功能占位，属于交互完整性的一部分。
 */
function PageLoading(): React.ReactElement {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        minHeight: 320,
        flexDirection: 'column',
        gap: 12,
      }}
    >
      <Spin size="large" />
      <span style={{ color: '#8c8c8c', fontSize: 13 }}>页面加载中…</span>
    </div>
  );
}

/**
 * 把页面包进懒加载的 Suspense
 *
 * 参数：
 *   node: 懒加载页面元素
 *
 * 返回：
 *   带 fallback 的元素
 *
 * 说明：抽成函数避免每个 Route 重复写 5 行 Suspense 包裹。
 */
function lazyPage(node: React.ReactNode): React.ReactElement {
  return <Suspense fallback={<PageLoading />}>{node}</Suspense>;
}

/**
 * 按角色重定向到默认首页
 *
 * 返回：
 *   Navigate 元素
 *
 * 说明：
 *   未登录时 homePath() 会回落到大盘路径，但该页面本身受守卫保护，
 *   会被 RequireAuth 再拦一次并带去登录页，因此不会出现"未登录却渲染大盘"。
 */
function RoleHomeRedirect(): React.ReactElement {
  const homePath = useAuthStore((state) => state.homePath());
  return <Navigate to={homePath} replace />;
}

export default function AppRouter(): React.ReactElement {
  return (
    <Routes>
      {/* ---------------- 登录页（独立路由，不带主布局） ---------------- */}
      <Route
        path="/login"
        element={
          <RedirectIfAuthenticated>{lazyPage(<LoginPage />)}</RedirectIfAuthenticated>
        }
      />

      {/* ---------------- 主布局内的业务页面 ---------------- */}
      <Route
        path="/"
        element={
          <RouteGuard>
            <MainLayout />
          </RouteGuard>
        }
      >
        {/* 根路径按角色落到各自默认首页 */}
        <Route index element={<RoleHomeRedirect />} />

        {/* 页面一：合同审查大盘（三种角色均可访问） */}
        <Route path="dashboard" element={lazyPage(<DashboardPage />)} />

        {/* 页面二：智能审查工作台（法务 / 管理员；经办人会被守卫拦回大盘） */}
        <Route path="workbench" element={lazyPage(<WorkbenchPage />)} />
        <Route path="workbench/:taskNo" element={lazyPage(<WorkbenchPage />)} />

        {/* 页面三：合规规则与模板（仅管理员） */}
        <Route path="rules" element={lazyPage(<RulesPage />)} />

        {/* 兜底：未知路径按角色回默认首页 */}
        <Route path="*" element={<RoleHomeRedirect />} />
      </Route>
    </Routes>
  );
}
