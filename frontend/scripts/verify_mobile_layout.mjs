// D:/ContractReviewSys-合同审批智能审查项目/frontend/scripts/verify_mobile_layout.mjs
/**
 * 移动端响应式布局验证（真渲染冒烟）
 *
 * 【为什么需要这个脚本】
 *   「局域网 + 移动端适配」改造把桌面固定侧边栏改为：
 *     - 桌面（>= md）：左 Sider 常驻
 *     - 手机（< md）：Sider 收起为汉堡按钮触发的 Drawer
 *   这类"按断点切换结构"的改动，tsc / vite build 都不会报错，
 *   但极易出现「断点取反写错 / Sider 没条件渲染 / 汉堡没接 onClick」等
 *   静默缺陷。必须让组件在真实 DOM、模拟不同视口宽度下挂载，才能回归。
 *
 * 【做法】
 *   复用 Vite 模块管线（ssrLoadModule）+ JSDOM + React act。
 *   通过可切换的 matchMedia 桩模拟视口；关键在于：antd 的 useBreakpoint
 *   会在模块加载时一次性注册 matchMedia 监听并缓存结果，no-op 的
 *   addEventListener 不会二次回调，因此**必须在加载 antd 之前**就确定视口
 *   模式。故本脚本按 LAYOUT_MODE 环境变量分两次独立运行（desktop / mobile），
 *   每次都是干净的模块图，避免单进程内切换断点无法传播的问题。
 *
 * 【验证项】
 *   desktop：渲染 .ant-layout-sider（左导航常驻），无汉堡按钮，含权责声明
 *   mobile ：不渲染 .ant-layout-sider，出现汉堡按钮；点击汉堡唤起 Drawer；
 *            顶栏隐藏宽幅权责声明（避免横向溢出）
 */

const { JSDOM } = await import(
  'file:///C:/Users/Lenovo/.workbuddy/binaries/node/workspace/node_modules/jsdom/lib/api.js'
);
import { createServer } from 'vite';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = resolve(__dirname, '..');

// 运行模式：desktop | mobile（由环境变量决定，加载 antd 前即固定）
const MOBILE = process.env.LAYOUT_MODE === 'mobile';

// ======================================================================
// 一、构造浏览器环境（必须在加载业务代码之前完成）
// ======================================================================

const dom = new JSDOM(
  `<!DOCTYPE html><html lang="zh-CN"><head></head><body><div id="root"></div></body></html>`,
  { url: 'http://127.0.0.1:5173/', pretendToBeVisual: true },
);

const SKIP_KEYS = new Set([
  'undefined', 'globalThis', 'window', 'self', 'top', 'parent', 'frames',
  'document', 'navigator', 'location', 'localStorage', 'sessionStorage',
  'eval', 'Function',
  'performance', 'Performance',
  'console', 'Timers',
  'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval',
  'setImmediate', 'clearImmediate', 'queueMicrotask',
  'structuredClone', 'fetch', 'crypto',
]);

for (const key of Object.getOwnPropertyNames(dom.window)) {
  if (SKIP_KEYS.has(key)) continue;
  let value;
  try {
    value = dom.window[key];
  } catch {
    continue;
  }
  if (value === undefined || value === null) continue;
  if (['string', 'number', 'boolean'].includes(typeof value)) continue;
  try {
    Object.defineProperty(global, key, {
      value,
      writable: true,
      configurable: true,
    });
  } catch {
    /* 只读或不可配置，跳过 */
  }
}

for (const key of [
  'window', 'document', 'navigator',
  'HTMLElement', 'SVGElement', 'ShadowRoot', 'Element', 'Node',
  'getComputedStyle',
]) {
  const value = global[key] ?? dom.window[key];
  if (value === undefined) continue;
  for (const target of [global, dom.window]) {
    try {
      Object.defineProperty(target, key, { value, writable: true, configurable: true });
    } catch {
      /* 忽略 */
    }
  }
}

if (!global.URL.createObjectURL) {
  global.URL.createObjectURL = () => 'blob:mock-url';
  global.URL.revokeObjectURL = () => {};
}

global.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 0);
global.cancelAnimationFrame = (id) => clearTimeout(id);
dom.window.requestAnimationFrame = global.requestAnimationFrame;
dom.window.cancelAnimationFrame = global.cancelAnimationFrame;

/**
 * matchMedia 桩（视口在加载 antd 前即固定为 MOBILE）
 *
 * antd 的 useBreakpoint 对 md(768)/lg(992) 等查询 (min-width: Npx)。
 * 手机视口（宽 390px）下这些均应 matches=false → isMobile=true；
 * 桌面视口下全部 matches=true → isMobile=false。
 */
const PHONE_WIDTH = 390;
function makeMatchMedia() {
  return (query) => {
    const m = query.match(/min-width:\s*(\d+)px/);
    const minW = m ? parseInt(m[1], 10) : 0;
    const matches = MOBILE ? minW <= PHONE_WIDTH : true;
    return {
      matches,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    };
  };
}
global.matchMedia = makeMatchMedia();
dom.window.matchMedia = global.matchMedia;

class ObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return [];
  }
}
for (const name of ['IntersectionObserver', 'ResizeObserver', 'MutationObserver']) {
  global[name] = ObserverStub;
  dom.window[name] = ObserverStub;
}

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

// ======================================================================
// 二、启动 Vite 管线并加载被测组件
// ======================================================================

const vite = await createServer({
  root: PROJECT_ROOT,
  server: { middlewareMode: true },
  appType: 'custom',
  optimizeDeps: { noDiscovery: true },
  logLevel: 'error',
});

const React = (await import('react')).default;
const { createRoot } = await import('react-dom/client');
const { act } = await import('react');
const { MemoryRouter, Routes, Route } = await import('react-router-dom');

const MainLayoutMod = await vite.ssrLoadModule('/src/layouts/MainLayout.tsx');
const MainLayout = MainLayoutMod.default;
// 说明：不单独 ssrLoadModule('antd')——antd 的 es 构建在 Node ESM 下因
// extensionless 深导入会解析失败。MainLayout 经 Vite 转换内部引入 antd 走的是
// Vite 的解析管线，工作正常。App.useApp() 在缺少 <App> 包裹时会回退到全局
// 静态方法，不会抛错，足以支撑结构断言。

// ======================================================================
// 三、断言工具
// ======================================================================

let passed = 0;
let failed = 0;
const failures = [];

function check(name, condition, detail = '') {
  if (condition) {
    passed += 1;
    console.log(`  √ ${name}`);
  } else {
    failed += 1;
    failures.push(name + (detail ? `  (${detail})` : ''));
    console.log(`  × ${name}${detail ? `  (${detail})` : ''}`);
  }
}

// ======================================================================
// 四、执行渲染验证
// ======================================================================

console.log(`\n[verify_mobile_layout] 响应式布局验证（模式=${MOBILE ? 'mobile' : 'desktop'}）`);
console.log('='.repeat(64));

const container = document.createElement('div');
document.body.appendChild(container);
const root = createRoot(container);

await act(async () => {
  root.render(
    React.createElement(
      MemoryRouter,
      { initialEntries: ['/dashboard'] },
      React.createElement(
        Routes,
        null,
        React.createElement(Route, {
          path: '/dashboard',
          element: React.createElement(MainLayout),
        }),
      ),
    ),
  );
  await new Promise((r) => setTimeout(r, 60));
});

const html = document.body.innerHTML;
const siderCount = document.querySelectorAll('.ant-layout-sider').length;
const hamburgerPresent = html.includes('打开导航菜单');

if (!MOBILE) {
  // ---------------- 桌面视口 ----------------
  check('桌面视口：左导航 Sider 常驻渲染', siderCount >= 1, `sider=${siderCount}`);
  check('桌面视口：不出现汉堡按钮', hamburgerPresent === false);
  check('桌面视口：出现「规则引擎终裁」权责声明', html.includes('规则引擎终裁'));
} else {
  // ---------------- 手机视口 ----------------
  check('手机视口：左导航 Sider 不再渲染', siderCount === 0, `sider=${siderCount}`);
  check('手机视口：出现汉堡按钮（aria-label=打开导航菜单）', hamburgerPresent);
  check(
    '手机视口：顶栏隐藏宽幅权责声明',
    html.includes('规则引擎终裁') === false,
    '避免横向溢出',
  );

  // 点击汉堡唤起抽屉
  const hamburgerBtn = [...document.querySelectorAll('button')].find((btn) =>
    (btn.getAttribute('aria-label') || '').includes('打开导航菜单'),
  );
  let drawerOpens = false;
  if (hamburgerBtn) {
    await act(async () => {
      hamburgerBtn.dispatchEvent(
        new dom.window.MouseEvent('click', { bubbles: true }),
      );
      await new Promise((r) => setTimeout(r, 60));
    });
    drawerOpens =
      document.querySelector('.ant-drawer-open') !== null ||
      document.querySelector('.ant-drawer') !== null;
  }
  check('手机视口：点击汉堡唤起导航抽屉', Boolean(hamburgerBtn) && drawerOpens);
}

// ======================================================================
// 五、收尾
// ======================================================================

await act(async () => {
  root.unmount();
});
container.remove();
await vite.close();

console.log('='.repeat(64));
console.log(`结果：${passed} 通过 / ${failed} 失败`);
if (failures.length > 0) {
  console.log('\n失败项：');
  for (const f of failures) console.log(`  - ${f}`);
}
process.exit(failed === 0 ? 0 : 1);
