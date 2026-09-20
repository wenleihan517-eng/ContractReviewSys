// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/hooks/useResponsive.ts
/**
 * 响应式断点 Hook
 *
 * 【职责】
 *   封装 antd 的 Grid.useBreakpoint，向上层组件提供语义化的设备类型判断，
 *   避免在多个页面里重复写 `!screens.md` 这类"魔法断点"判断，
 *   统一"手机 / 平板 / 桌面"三档口径。
 *
 * 【断点口径】
 *   antd 响应式断点（基于视口最小宽度）：
 *     xs < 576 < sm < 768 < md < 992 < lg < 1200 < xl < 1600 < xxl
 *   本 Hook 约定：
 *     isMobile  = 视口 < md（即 < 768px，典型手机竖屏）
 *     isTablet  = md ≤ 视口 < lg（平板 / 手机横屏）
 *     isDesktop = 视口 ≥ lg（桌面）
 *
 * 【返回值】
 *   screens : 原始断点字典（xs/sm/md/lg/xl/xxl 布尔值），供需要细粒度判断时直接使用
 *   isMobile / isTablet / isDesktop : 语义化布尔值
 *
 * 说明：
 *   该 Hook 依赖 antd 内部媒体查询，无需额外 Provider，
 *   但必须在 React 组件内调用（遵循 Hook 规则）。
 */

import { Grid } from 'antd';

const { useBreakpoint } = Grid;

/** 响应式状态 */
export interface ResponsiveState {
  /** antd 原始断点字典 */
  screens: ReturnType<typeof useBreakpoint>;
  /** 手机（视口 < 768px） */
  isMobile: boolean;
  /** 平板（768px ≤ 视口 < 992px） */
  isTablet: boolean;
  /** 桌面（视口 ≥ 992px） */
  isDesktop: boolean;
}

/**
 * 获取当前视口对应的响应式状态
 *
 * 返回：
 *   ResponsiveState —— 含原始断点与三档语义化布尔值
 */
export function useResponsive(): ResponsiveState {
  const screens = useBreakpoint();

  const isMobile = !screens.md;
  const isTablet = Boolean(screens.md) && !screens.lg;
  const isDesktop = Boolean(screens.lg);

  return { screens, isMobile, isTablet, isDesktop };
}
