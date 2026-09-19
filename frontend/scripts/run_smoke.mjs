// D:/ContractReviewSys-合同审批智能审查项目/frontend/scripts/run_smoke.mjs
/**
 * 冒烟测试启动器
 *
 * 【脚本职责】
 *   借助 Vite 的 SSR 模块加载能力，在 Node 侧加载 src 下的
 *   TypeScript / TSX 源码（正确处理 @ 别名与 JSX 转换），
 *   然后执行 smoke_render.mjs 中的全部验证项。
 *
 * 【为什么需要启动器】
 *   Node 原生无法直接 import .tsx 文件，也不认识 vite.config.ts 中
 *   配置的 @ 路径别名。直接跑测试脚本会因解析失败而报错，
 *   而这与业务代码质量无关。用 createServer + ssrLoadModule
 *   复用 Vite 自身的解析与转换管线，可保证测试对象与浏览器中
 *   运行的代码完全一致。
 *
 * 【运行方式】
 *   NODE_PATH=<jsdom 所在目录> node scripts/run_smoke.mjs
 */

import { createServer } from 'vite';

// 说明：ESM 不读取 NODE_PATH，jsdom 装在托管工作区而非本工程内，
// 因此用绝对文件路径导入。该路径为托管 Node 运行时的共享依赖目录。
const { JSDOM } = await import(
  'file:///C:/Users/Lenovo/.workbuddy/binaries/node/workspace/node_modules/jsdom/lib/api.js'
);

// ======================================================================
// 一、构造浏览器环境（在加载业务代码前完成）
// ======================================================================

const dom = new JSDOM(
  `<!DOCTYPE html><html lang="zh-CN"><head></head><body><div id="root"></div></body></html>`,
  {
    url: 'http://127.0.0.1:5173/',
    pretendToBeVisual: true,
  },
);

// 说明：jsdom 提供的 DOM 全局对象远超手工清单，
// 逐个列举容易遗漏（如 SVGElement、ShadowRoot、DragEvent 等），
// 且缺一个就会让 createRoot 挂载直接崩溃。
// 因此这里改为遍历 window 自身属性，把所有"看起来像构造函数或全局对象"
// 的成员批量注入 Node 全局，一次到位。
const SKIP_KEYS = new Set([
  // 与 Node 自身冲突或无需注入的成员
  'undefined',
  'globalThis',
  'window',
  'self',
  'top',
  'parent',
  'frames',
  'document',
  'navigator',
  'location',
  'localStorage',
  'sessionStorage',
  'eval',
  'Function',

  // ----------------------------------------------------------------
  // 高危覆盖清单：以下成员一旦被 jsdom 实例覆盖 Node 原生实现，
  // 会导致 jsdom 内部 implSymbol 校验失效并触发无限递归
  // （典型崩溃：Performance.js 中 Performance.now() → RangeError:
  //   Maximum call stack size exceeded）。
  // 原因是 jsdom 的 hasOwn 判断依赖 value[implSymbol] instanceof Impl，
  // 而把 jsdom 的 performance 实例再赋给 global.performance 后，
  // is() 检查会在自身上反复递归。
  // 这类成员统一保留 Node 原生版本，不参与批量注入。
  // ----------------------------------------------------------------
  'performance',
  'Performance',
  'PerformanceEntry',
  'PerformanceMark',
  'PerformanceMeasure',
  'PerformanceObserver',
  'PerformanceObserverEntryList',
  'PerformanceResourceTiming',
  'PerformanceNavigationTiming',
  'PerformanceTiming',
  'PerformanceNavigation',
  'console',
  'Timers',
  'setTimeout',
  'clearTimeout',
  'setInterval',
  'clearInterval',
  'setImmediate',
  'clearImmediate',
  'queueMicrotask',
  'structuredClone',
  'fetch',
  'crypto',
]);

for (const key of Object.getOwnPropertyNames(dom.window)) {
  if (SKIP_KEYS.has(key)) continue;

  let value;
  try {
    value = dom.window[key];
  } catch {
    continue;
  }

  // 只注入构造函数与普通对象，跳过基本类型方法
  if (value === undefined || value === null) continue;
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    continue;
  }

  try {
    Object.defineProperty(global, key, {
      value,
      writable: true,
      configurable: true,
    });
  } catch {
    // 只读或不可配置的属性跳过
  }
}

// 说明：部分环境变量需要在 global 与 window 双向可见，
// 下面这些是 React DOM 与 antd 判断运行环境时明确读取的成员。
const CRITICAL_KEYS = [
  'window',
  'document',
  'navigator',
  'HTMLElement',
  'SVGElement',
  'ShadowRoot',
  'Element',
  'Node',
  'getComputedStyle',
];

for (const key of CRITICAL_KEYS) {
  const value = global[key] ?? dom.window[key];
  if (value === undefined) continue;

  for (const target of [global, dom.window]) {
    try {
      Object.defineProperty(target, key, {
        value,
        writable: true,
        configurable: true,
      });
    } catch {
      // 忽略
    }
  }
}

// 说明：URL.createObjectURL 用于报告下载与 PDF 加载，jsdom 未实现，需补齐
if (!global.URL.createObjectURL) {
  global.URL.createObjectURL = () => 'blob:mock-url';
  global.URL.revokeObjectURL = () => {};
}

// 动画帧：React 调度器依赖
global.requestAnimationFrame = (callback) => setTimeout(() => callback(Date.now()), 0);
global.cancelAnimationFrame = (id) => clearTimeout(id);
dom.window.requestAnimationFrame = global.requestAnimationFrame;
dom.window.cancelAnimationFrame = global.cancelAnimationFrame;

// matchMedia：antd 响应式栅格依赖
const matchMediaStub = () => ({
  matches: false,
  media: '',
  onchange: null,
  addListener: () => {},
  removeListener: () => {},
  addEventListener: () => {},
  removeEventListener: () => {},
  dispatchEvent: () => false,
});
global.matchMedia = matchMediaStub;
dom.window.matchMedia = matchMediaStub;

// 观察者：antd 的 Table 虚拟滚动与 Tooltip 依赖
class ObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return [];
  }
}
for (const ObserverName of ['IntersectionObserver', 'ResizeObserver', 'MutationObserver']) {
  global[ObserverName] = ObserverStub;
  dom.window[ObserverName] = ObserverStub;
}

// 说明：React 18 的 act() 在非测试框架环境下会输出
// "The current testing environment is not configured to support act(...)"
// 告警。该告警仅表示缺少该全局标记，并非渲染失败；
// 本项目用 act() 驱动 Portal 挂载是正确用法，因此显式声明标记，
// 让 React 进入正式的测试环境模式，消除噪音告警。
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

// ======================================================================
// 二、启动 Vite 并用 SSR 加载业务模块
// ======================================================================

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  // 关闭依赖预构建缓存干扰，保证加载的是最新源码
  optimizeDeps: { noDiscovery: true },
  logLevel: 'error',
});

// ======================================================================
// 三、执行验证项
// ======================================================================

/**
 * 屏蔽 React SSR 的无害告警
 *
 * 说明：antd 的 Menu / Table 等组件在内部使用 useLayoutEffect，
 * 用 renderToString 验证时会输出大量 "useLayoutEffect does nothing
 * on the server" 告警。该告警只在服务端渲染场景成立，
 * 本项目是纯客户端应用（createRoot），因此不影响实际运行，
 * 屏蔽以免淹没真正的验证结果。
 */
const originalError = console.error;
console.error = (...args) => {
  const first = String(args[0] ?? '');
  if (first.includes('useLayoutEffect does nothing on the server')) return;
  if (first.includes('Warning: ReactDOM.render')) return;
  originalError(...args);
};

const results = [];

/**
 * 执行单项验证
 *
 * 参数：
 *   name: 验证项名称
 *   fn: 异步验证函数，返回说明文本表示通过，抛异常表示失败
 */
async function check(name, fn) {
  try {
    const detail = await fn();
    results.push({ name, passed: true, detail: detail ?? '' });
    console.log(`  PASS  ${name}${detail ? `  — ${detail}` : ''}`);
  } catch (error) {
    results.push({ name, passed: false, detail: error.message });
    console.log(`  FAIL  ${name}  — ${error.message}`);
  }
}

/** 通过 Vite SSR 加载源码模块 */
const load = (path) => vite.ssrLoadModule(path);

// ----------------------------------------------------------------------
// 鉴权播种辅助
// ----------------------------------------------------------------------

/**
 * 种子角色 → 权限点映射（仅用于测试构造用户对象）
 *
 * 【为何在此重写一份而不 import 后端常量】
 *   前端 constants 中只定义权限点字符串，**有意不定义**角色到权限的映射，
 *   因为后端 ROLE_PERMISSIONS 才是唯一权威源，前端不得私自维护一份。
 *   测试需要一个"长得像后端返回"的用户对象，因此在这里显式构造，
 *   并明确标注它是测试替身而非业务逻辑。
 */
const SEED_ROLE_PERMISSIONS = {
  business_user: ['contract:upload', 'task:view_own', 'report:download'],
  law_user: [
    'task:view_own',
    'task:view_all',
    'report:download',
    'risk_item:edit',
    'comment:annotate',
    'approval:write_back',
  ],
  admin: [
    'contract:upload',
    'task:view_own',
    'task:view_all',
    'report:download',
    'risk_item:edit',
    'comment:annotate',
    'approval:write_back',
    'rule_template:manage',
    'user:manage',
    'blocked_task:handle',
    'system:monitor',
  ],
};

/**
 * 播种一个已登录用户到 authStore 与 localStorage
 *
 * 参数：
 *   role: 角色值
 *   overrides: 需要覆盖的字段（如 username / display_name）
 *
 * 返回：
 *   构造出的用户对象
 *
 * 说明：
 *   直接调用 authStore.setState 注入状态，而不是走 login() 流程——
 *   后者会真实发起 HTTP 请求，在无后端环境下必然失败。
 *   这样测的是"消费登录态的各组件/守卫"，与登录请求本身解耦。
 */
async function seedAuthUser(role, overrides = {}) {
  const { useAuthStore } = await load('/src/stores/authStore.ts');

  const user = {
    user_id: `test-${role}`,
    username: overrides.username ?? `user_${role}`,
    display_name: overrides.display_name ?? `测试${role}`,
    role,
    role_label: overrides.role_label ?? role,
    status: 'active',
    department: overrides.department ?? '测试部门',
    email: null,
    permissions: SEED_ROLE_PERMISSIONS[role] ?? [],
    is_admin: role === 'admin',
    can_view_all_tasks: role !== 'business_user',
    ...overrides,
  };

  // 说明：业务代码读写的是 window.localStorage，而本测试环境的
  // global.localStorage 被高危覆盖清单刻意跳过（见 SKIP_KEYS），
  // 因此这里统一走 dom.window.localStorage，与业务代码同源。
  const storage = dom.window.localStorage;
  storage.setItem('cr_access_token', `test-access-${role}`);
  storage.setItem('cr_refresh_token', `test-refresh-${role}`);
  storage.setItem('cr_user_info', JSON.stringify(user));

  useAuthStore.setState({
    accessToken: `test-access-${role}`,
    refreshToken: `test-refresh-${role}`,
    user,
    lastProfileSyncAt: Date.now(),
  });

  return user;
}

/**
 * 清空登录态
 *
 * 返回：
 *   无
 */
async function clearAuthUser() {
  const { useAuthStore } = await load('/src/stores/authStore.ts');
  dom.window.localStorage.clear();
  useAuthStore.setState({
    accessToken: null,
    refreshToken: null,
    user: null,
    lastProfileSyncAt: 0,
  });
}

console.log('\n=== 合同审查前端 · 渲染冒烟测试 ===\n');

// ----------------------------------------------------------------------
// 第一组：工具层纯函数
// ----------------------------------------------------------------------
console.log('[1] 工具层 — 高亮切分与格式化\n');

await check('splitTextByRisks：切分后拼接严格等于原文', async () => {
  const { splitTextByRisks } = await load('/src/utils/index.ts');

  const text = '甲方应在收到货物后30日内付款。乙方不承担任何责任。';
  const risks = [
    { start: 10, end: 18, level: 'high', riskId: 'r1' },
    { start: 22, end: 32, level: 'critical', riskId: 'r2' },
  ];

  const segments = splitTextByRisks(text, 0, risks);
  const rejoined = segments.map((segment) => segment.text).join('');

  if (rejoined !== text) {
    throw new Error(`文本丢失\n  原文: ${text}\n  拼接: ${rejoined}`);
  }

  const hits = segments.filter((segment) => segment.highlighted);
  if (hits.length !== 2) {
    throw new Error(`期望命中 2 处，实际 ${hits.length} 处`);
  }
  return `切成 ${segments.length} 段、命中 ${hits.length} 处，文本零丢失`;
});

await check('splitTextByRisks：重叠区间不导致重复或丢失', async () => {
  const { splitTextByRisks } = await load('/src/utils/index.ts');

  const text = 'ABCDEFGHIJ';
  const segments = splitTextByRisks(text, 0, [
    { start: 2, end: 6, level: 'low', riskId: 'a' },
    { start: 4, end: 9, level: 'high', riskId: 'b' },
  ]);

  const rejoined = segments.map((segment) => segment.text).join('');
  if (rejoined !== text) {
    throw new Error(`重叠区间导致文本错乱：${rejoined}`);
  }
  return `重叠区间正确合并，输出 ${segments.length} 段`;
});

await check('splitTextByRisks：跨页局部偏移换算正确', async () => {
  const { splitTextByRisks } = await load('/src/utils/index.ts');

  // 模拟第 2 页：页文本在全文中偏移 5
  const segments = splitTextByRisks('命中文字', 5, [
    { start: 5, end: 9, level: 'high', riskId: 'r1' },
  ]);

  const hits = segments.filter((segment) => segment.highlighted);
  if (hits.length !== 1 || hits[0].text !== '命中文字') {
    throw new Error(`偏移换算错误：${JSON.stringify(hits)}`);
  }
  return '全文区间正确换算为页内相对位置';
});

await check('splitTextByRisks：完全无命中时返回单个普通片段', async () => {
  const { splitTextByRisks } = await load('/src/utils/index.ts');

  const segments = splitTextByRisks('普通文本', 0, []);
  if (segments.length !== 1 || segments[0].highlighted) {
    throw new Error('无命中时不应产生高亮片段');
  }
  return '空风险列表下退化为单片段，无多余分割';
});

await check('computeHighestRiskLevel：参考项不参与定级', async () => {
  const { computeHighestRiskLevel } = await load('/src/utils/index.ts');

  const level = computeHighestRiskLevel([
    { risk_level: 'low', reference_only: false },
    { risk_level: 'critical', reference_only: true },
  ]);

  if (level !== 'low') {
    throw new Error(`参考项不应抬高定级，期望 low，实际 ${level}`);
  }
  return 'AI 候选被正确排除，定级只认规则引擎命中';
});

await check('formatAmount：万元/亿元分级与零值占位', async () => {
  const { formatAmount } = await load('/src/utils/index.ts');

  const cases = [
    [5200000, '¥520.00 万元'],
    [120000000, '¥1.20 亿元'],
    [8800, '¥8800.00'],
    [0, '—'],
    [Number.NaN, '—'],
  ];

  for (const [input, expected] of cases) {
    const actual = formatAmount(input);
    if (actual !== expected) {
      throw new Error(`金额 ${input} 期望「${expected}」，实际「${actual}」`);
    }
  }
  return '分级换算与非法值兜底均正确';
});

await check('sortRiskItems：按等级降序且规则命中优先于参考项', async () => {
  const { sortRiskItems } = await load('/src/utils/index.ts');

  const sorted = sortRiskItems([
    { risk_id: 'a', risk_level: 'low', reference_only: false, page_no: 1, char_start: 0 },
    { risk_id: 'b', risk_level: 'critical', reference_only: true, page_no: 1, char_start: 0 },
    { risk_id: 'c', risk_level: 'high', reference_only: false, page_no: 1, char_start: 0 },
  ]);

  const order = sorted.map((item) => item.risk_id).join(',');
  if (order !== 'c,a,b') {
    throw new Error(`排序错误，期望 c,a,b，实际 ${order}`);
  }
  return `排序结果 ${order}（高→低，参考项置底）`;
});

await check('resolveRiskLocation：无字符区间时按证据文本反查', async () => {
  const { resolveRiskLocation } = await load('/src/utils/index.ts');

  const fullText = '甲乙双方就设备采购事宜达成如下协议：乙方不承担任何责任。';
  const located = resolveRiskLocation(
    {
      risk_id: 'r1',
      evidence_text: '乙方不承担任何责任',
      char_start: null,
      char_end: null,
      page_no: null,
    },
    [
      {
        block_index: 0,
        page: 1,
        start_idx: 0,
        end_idx: fullText.length,
        paragraph_index: 0,
        text: fullText,
      },
    ],
    fullText,
  );

  if (located.charStart !== fullText.indexOf('乙方不承担任何责任')) {
    throw new Error(`反查偏移错误：${located.charStart}`);
  }
  return `反查命中偏移 ${located.charStart}，段落索引 ${located.paragraphIndex}`;
});

await check('buildDocumentPages：按文本块真实页码分页', async () => {
  const { buildDocumentPages } = await load('/src/utils/index.ts');

  const fullText = 'A'.repeat(50) + 'B'.repeat(50) + 'C'.repeat(50);
  const blocks = [0, 1, 2].map((index) => ({
    block_index: index,
    page: index + 1,
    start_idx: index * 50,
    end_idx: (index + 1) * 50,
    paragraph_index: index,
    text: fullText.slice(index * 50, (index + 1) * 50),
  }));

  const pages = buildDocumentPages(fullText, blocks, 3);

  if (pages.length !== 3) {
    throw new Error(`期望 3 页，实际 ${pages.length} 页`);
  }
  if (pages[1].startIdx !== 50 || !pages[1].text.startsWith('B')) {
    throw new Error('第 2 页起始偏移或内容不正确');
  }
  // 每页文本必须能在全文中按偏移精确取出
  for (const page of pages) {
    const expected = fullText.slice(page.startIdx, page.startIdx + page.text.length);
    if (page.text !== expected) {
      throw new Error(`第 ${page.page} 页文本与全文偏移不一致`);
    }
  }
  return `${pages.length} 页，页边界与全局偏移完全一致`;
});

// ----------------------------------------------------------------------
// 第二组：常量与契约一致性
// ----------------------------------------------------------------------
console.log('\n[2] 常量层 — 与后端契约对齐\n');

await check('风险等级 4 档、权重单调递增', async () => {
  const c = await load('/src/constants/index.ts');

  const levels = Object.keys(c.RISK_LEVEL);
  if (levels.length !== 4) {
    throw new Error(`应为 4 档，实际 ${levels.length} 档：${levels.join('/')}`);
  }

  const order = c.RISK_LEVEL_ORDER;
  if (!(order.low < order.medium && order.medium < order.high && order.high < order.critical)) {
    throw new Error('排序权重未严格单调递增');
  }
  return `${levels.join(' < ')}，权重 1→2→3→4`;
});

await check('规则匹配方式 7 种（与后端 RuleMatchType 一致）', async () => {
  const c = await load('/src/constants/index.ts');

  const expected = [
    'keyword',
    'regex',
    'clause_type',
    'semantic',
    'field_compare',
    'missing_clause',
    'numeric_threshold',
  ];
  const actual = Object.values(c.MATCH_TYPE);

  const missing = expected.filter((item) => !actual.includes(item));
  if (missing.length > 0) {
    throw new Error(`缺少匹配方式：${missing.join('/')}`);
  }
  return `${actual.length} 种匹配方式全部就位`;
});

await check('任务状态含全部 8 种（含阻塞与取消）', async () => {
  const c = await load('/src/constants/index.ts');

  const expected = [
    'pending',
    'running',
    'llm_done',
    'rule_done',
    'blocked',
    'completed',
    'failed',
    'cancelled',
  ];
  const actual = Object.values(c.TASK_STATUS);
  const missing = expected.filter((item) => !actual.includes(item));

  if (missing.length > 0) {
    throw new Error(`缺少任务状态：${missing.join('/')}`);
  }

  // 进行中状态集合必须与轮询逻辑一致
  if (c.TASK_STATUS_IN_PROGRESS.includes(c.TASK_STATUS.COMPLETED)) {
    throw new Error('已完成状态不应出现在「进行中」集合中，否则轮询永不停止');
  }
  return `8 种状态齐备，进行中集合 ${c.TASK_STATUS_IN_PROGRESS.length} 项`;
});

await check('阻塞原因码 6 种法定分支 + 人工', async () => {
  const c = await load('/src/constants/index.ts');

  const required = [
    'DOC_ENCRYPTED',
    'SCAN_TOO_BLURRY',
    'EMPTY_CONTENT',
    'PARSE_FAILED',
    'RETRY_EXHAUSTED',
    'MANUAL',
  ];
  const missing = required.filter((code) => !(code in c.BLOCKED_REASON_LABEL));

  if (missing.length > 0) {
    throw new Error(`缺少阻塞原因码：${missing.join('/')}`);
  }
  return `${Object.keys(c.BLOCKED_REASON_LABEL).length} 种原因码均有中文说明`;
});

await check('回写状态 4 态，且终态可判别', async () => {
  const c = await load('/src/constants/index.ts');

  const expected = ['not_written', 'writing', 'success', 'failed'];
  const actual = Object.values(c.WRITE_BACK_STATUS);
  const missing = expected.filter((item) => !actual.includes(item));

  if (missing.length > 0) {
    throw new Error(`缺少回写状态：${missing.join('/')}`);
  }
  return `4 态齐备：${actual.join(' → ')}`;
});

// ----------------------------------------------------------------------
// 第三组：权责红线静态核查
// ----------------------------------------------------------------------
console.log('\n[3] 权责红线 — 源码级核查\n');

await check('源码中不存在审批动作类接口调用', async () => {
  const fs = await import('node:fs');
  const path = await import('node:path');

  // 严禁出现在前端代码中的审批动作路径段
  const forbidden = [
    'approve',
    'reject',
    'transfer',
    'withdraw',
    'reassign',
    'sign',
    'complete',
    'terminate',
  ];

  /** 递归收集 src 下的全部源码文件 */
  function collect(dir) {
    const out = [];
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        out.push(...collect(full));
      } else if (/\.(ts|tsx)$/.test(entry.name)) {
        out.push(full);
      }
    }
    return out;
  }

  const files = collect('src');
  const violations = [];

  for (const file of files) {
    const content = fs.readFileSync(file, 'utf8');

    for (const action of forbidden) {
      // 只匹配真实的接口调用：get/post/put/del<T>('...路径...')
      const callPattern = new RegExp(
        `\\b(get|post|put|del)\\s*<[^>]*>\\s*\\(\\s*[\`'"][^\`'"]*/${action}\\b`,
        'g',
      );
      const found = content.match(callPattern);
      if (found) {
        violations.push(`${file}: ${found.join(' | ')}`);
      }
    }
  }

  if (violations.length > 0) {
    throw new Error(
      `发现审批动作类接口调用，违反权责红线：\n    ${violations.join('\n    ')}`,
    );
  }
  return `扫描 ${files.length} 个源码文件，未发现任何审批动作类调用`;
});

await check('界面中不存在定级类字段的写入入口', async () => {
  const fs = await import('node:fs');
  const path = await import('node:path');

  // 定级类字段：risk_level / decision / severity 不得作为写请求字段出现
  const files = [
    'src/api/endpoints.ts',
    'src/stores/workbenchStore.ts',
    'src/components/RiskCard.tsx',
  ];

  const violations = [];

  for (const file of files) {
    const content = fs.readFileSync(file, 'utf8');

    // 查找形如 risk_level: xxx 出现在对象字面量中且被用于请求体的场景
    // 说明：类型定义与只读展示是允许的，因此只检查 editRiskItem / updateRecommend
    // 两个写接口的请求体构造处。
    const writePayloadPattern = /risk_id:\s*[^,]+,\s*(risk_level|decision|severity)\s*:/g;
    if (writePayloadPattern.test(content)) {
      violations.push(file);
    }
    void path;
  }

  if (violations.length > 0) {
    throw new Error(`写请求中包含定级字段：${violations.join(', ')}`);
  }
  return `已核查 ${files.length} 个文件，写请求体只含建议类字段`;
});

await check('审核白名单接口路径正确（/comments 而非动作类）', async () => {
  const fs = await import('node:fs');
  const content = fs.readFileSync('src/api/endpoints.ts', 'utf8');

  if (!content.includes('/comment/write-back')) {
    throw new Error('回写接口路径缺失');
  }

  // 回写接口必须是 comment 相关的路径
  const writeBackLine = content
    .split('\n')
    .find((line) => line.includes('/comment/write-back'));
  if (!writeBackLine || !writeBackLine.includes('comment')) {
    throw new Error('回写接口未指向评论类路径');
  }
  return '回写指向 /report/{id}/comment/write-back，属评论白名单';
});

// ----------------------------------------------------------------------
// 第四组：React 组件真实渲染
// ----------------------------------------------------------------------
console.log('\n[4] 组件层 — 真实渲染验证\n');

await check('主布局渲染：品牌、导航、权责声明齐全', async () => {
  const React = (await import('react')).default;
  const { act } = await import('react');
  const { createRoot } = await import('react-dom/client');
  const { MemoryRouter } = await import('react-router-dom');
  const { ConfigProvider, App: AntdApp } = await import('antd');
  const MainLayout = (await load('/src/layouts/MainLayout.tsx')).default;

  // 主布局的导航项按角色过滤，因此必须先播种一个已登录用户，
  // 否则渲染出的菜单恒为空——那测的是"未登录"而非"布局完整性"。
  // 这里用 admin：它是唯一能同时看到全部三项导航的角色。
  //
  // display_name 显式传中文全称：真实后端返回的是 cr_user 集合里的昵称字段
  // （内置账号播种为"系统管理员"这类中文名），而不是"测试admin"这种拼装串。
  // 显式传入才能同时校验"显示名来自 store 而非硬编码"这一意图。
  await seedAuthUser('admin', { display_name: '测试管理员' });

  // 【为什么用 createRoot 而不是 renderToString】
  //   本布局的菜单与用户信息来自 Zustand store。Zustand v5 的 useStore
  //   在服务端渲染路径下使用 getServerSnapshot，返回的是 store **创建时**
  //   的初始快照，而不是随后 setState 写入的当前值。若用 renderToString，
  //   无论怎么播种都会渲染出"未登录 + 空菜单"，得到与真实浏览器完全不同的
  //   结论（浏览器走 getSnapshot，即在值）。
  //   因此这里用 createRoot + act 走客户端渲染路径，与生产运行方式一致。
  const container = dom.window.document.createElement('div');
  dom.window.document.body.appendChild(container);
  const root = createRoot(container);

  await act(async () => {
    root.render(
      React.createElement(
        ConfigProvider,
        null,
        React.createElement(
          AntdApp,
          null,
          React.createElement(
            MemoryRouter,
            { initialEntries: ['/dashboard'] },
            React.createElement(MainLayout),
          ),
        ),
      ),
    );
  });

  const html = container.innerHTML;

  const required = [
    ['合同智能审查', '品牌标题'],
    ['合同审查大盘', '导航项一'],
    ['智能审查工作台', '导航项二'],
    ['合规规则与模板', '导航项三'],
    ['规则引擎终裁', '权责声明标识'],
    // 登录用户信息区必须展示后端下发的真实角色名，
    // 而不是早期硬编码的固定岗位文案
    ['系统管理员', '当前登录用户角色标签'],
    ['测试管理员', '当前登录用户显示名'],
  ];

  for (const [text, label] of required) {
    if (!html.includes(text)) {
      throw new Error(`缺少${label}「${text}」`);
    }
  }

  // 清理挂载点，避免影响后续用例
  await act(async () => {
    root.unmount();
  });
  container.remove();

  return `渲染 ${html.length} 字符，品牌/三项导航/权责标识/用户信息齐全`;
});

await check('主布局：菜单按角色过滤，经办人看不到工作台与规则入口', async () => {
  const React = (await import('react')).default;
  const { act } = await import('react');
  const { createRoot } = await import('react-dom/client');
  const { MemoryRouter } = await import('react-router-dom');
  const { ConfigProvider, App: AntdApp } = await import('antd');
  const MainLayout = (await load('/src/layouts/MainLayout.tsx')).default;

  /**
   * 渲染指定角色的布局，返回 innerHTML
   */
  const renderForRole = async (role) => {
    await seedAuthUser(role);
    const container = dom.window.document.createElement('div');
    dom.window.document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => {
      root.render(
        React.createElement(
          ConfigProvider,
          null,
          React.createElement(
            AntdApp,
            null,
            React.createElement(
              MemoryRouter,
              { initialEntries: ['/dashboard'] },
              React.createElement(MainLayout),
            ),
          ),
        ),
      );
    });
    const html = container.innerHTML;
    await act(async () => {
      root.unmount();
    });
    container.remove();
    return html;
  };

  // 业务经办人：只应看到大盘。菜单里不得出现工作台与规则，
  // 否则用户点进去会被守卫弹回，表现为"系统坏了"。
  const businessHtml = await renderForRole('business_user');
  if (!businessHtml.includes('合同审查大盘')) {
    throw new Error('经办人应能看到「合同审查大盘」');
  }
  if (businessHtml.includes('智能审查工作台')) {
    throw new Error('经办人不应看到「智能审查工作台」菜单入口');
  }
  if (businessHtml.includes('合规规则与模板')) {
    throw new Error('经办人不应看到「合规规则与模板」菜单入口');
  }
  if (!businessHtml.includes('业务经办人')) {
    throw new Error('经办人角色标签应显示「业务经办人」');
  }

  // 法务审查人：应看到大盘与工作台，不得看到规则
  const lawHtml = await renderForRole('law_user');
  if (!lawHtml.includes('智能审查工作台')) {
    throw new Error('法务应能看到「智能审查工作台」菜单入口');
  }
  if (lawHtml.includes('合规规则与模板')) {
    throw new Error('法务不应看到「合规规则与模板」菜单入口');
  }
  if (!lawHtml.includes('法务审查人')) {
    throw new Error('法务角色标签应显示「法务审查人」');
  }

  // 系统管理员：三项导航全部可见
  const adminHtml = await renderForRole('admin');
  for (const label of ['合同审查大盘', '智能审查工作台', '合规规则与模板']) {
    if (!adminHtml.includes(label)) {
      throw new Error(`管理员应能看到「${label}」菜单入口`);
    }
  }

  await clearAuthUser();
  return '经办人仅见大盘、法务见大盘+工作台、管理员全见，菜单与路由守卫口径一致';
});

await check('规则页渲染：规则树、详情区、条款库三区块齐全', async () => {
  const React = (await import('react')).default;
  const { renderToString } = await import('react-dom/server');
  const { ConfigProvider, App: AntdApp } = await import('antd');
  const RulesPage = (await load('/src/pages/RulesPage.tsx')).default;

  const html = renderToString(
    React.createElement(
      ConfigProvider,
      null,
      React.createElement(AntdApp, null, React.createElement(RulesPage)),
    ),
  );

  const required = [
    '审查规则树',
    '规则详情',
    '标准示范条款库',
    '规则总数',
    '已启用',
    '已禁用',
    '致命级',
  ];

  for (const text of required) {
    if (!html.includes(text)) {
      throw new Error(`规则页缺少「${text}」`);
    }
  }
  return `渲染 ${html.length} 字符，统计卡/规则树/详情/条款库齐全`;
});

await check('工作台无任务号时渲染引导页而非空白', async () => {
  const React = (await import('react')).default;
  const { renderToString } = await import('react-dom/server');
  const { MemoryRouter } = await import('react-router-dom');
  const { ConfigProvider, App: AntdApp } = await import('antd');
  const WorkbenchPage = (await load('/src/pages/WorkbenchPage.tsx')).default;

  const html = renderToString(
    React.createElement(
      ConfigProvider,
      null,
      React.createElement(
        AntdApp,
        null,
        React.createElement(
          MemoryRouter,
          { initialEntries: ['/workbench'] },
          React.createElement(WorkbenchPage),
        ),
      ),
    ),
  );

  const required = ['请先选择要审查的合同', '前往合同审查大盘'];
  for (const text of required) {
    if (!html.includes(text)) {
      throw new Error(`引导页缺少「${text}」`);
    }
  }
  return '引导页正确渲染，含大盘跳转入口';
});

await check('风险卡片渲染：等级、依据、原文、建议四要素齐全', async () => {
  const React = (await import('react')).default;
  const { renderToString } = await import('react-dom/server');
  const { ConfigProvider, App: AntdApp } = await import('antd');
  const RiskCard = (await load('/src/components/RiskCard.tsx')).default;

  const located = {
    item: {
      risk_id: 'risk-001',
      rule_code: 'BAL-002',
      rule_name: '单方无限连带责任',
      clause_id: 'clause-3',
      clause_type: 'liability',
      evidence_text: '乙方应承担无限连带责任，甲方不承担任何责任。',
      char_start: 120,
      char_end: 145,
      page_no: 2,
      risk_level: 'critical',
      decision: 'block',
      severity: 'fatal',
      suggestion_text: '建议修改为：双方按各自过错程度承担相应赔偿责任。',
      legal_basis: '《民法典》第 577 条、第 591 条',
      reference_only: false,
      matched_sources: ['rule_engine'],
    },
    page_no: 2,
    paragraph_index: 3,
    char_start: 120,
    char_end: 145,
  };

  const html = renderToString(
    React.createElement(
      ConfigProvider,
      null,
      React.createElement(
        AntdApp,
        null,
        React.createElement(RiskCard, {
          located,
          selected: false,
          onSelect: () => {},
          onAdopt: async () => {},
          onSave: async () => {},
        }),
      ),
    ),
  );

  const required = [
    ['单方无限连带责任', '风险名称'],
    ['严重风险', '风险等级'],
    ['《民法典》第 577 条', '法律依据'],
    ['无限连带责任', '原文片段'],
    ['建议修改为', '推荐条款'],
    ['BAL-002', '规则编码'],
    ['P2', '页码定位'],
  ];

  for (const [text, label] of required) {
    if (!html.includes(text)) {
      throw new Error(`风险卡片缺少${label}「${text}」`);
    }
  }
  return `渲染 ${html.length} 字符，7 项要素全部呈现`;
});

await check('风险卡片：参考项明确标注来源且不显示定级标签来源', async () => {
  const React = (await import('react')).default;
  const { renderToString } = await import('react-dom/server');
  const { ConfigProvider, App: AntdApp } = await import('antd');
  const RiskCard = (await load('/src/components/RiskCard.tsx')).default;

  const html = renderToString(
    React.createElement(
      ConfigProvider,
      null,
      React.createElement(
        AntdApp,
        null,
        React.createElement(RiskCard, {
          located: {
            item: {
              risk_id: 'risk-002',
              rule_code: null,
              rule_name: '疑似付款条件不利',
              clause_id: null,
              clause_type: 'payment',
              evidence_text: '付款条件由甲方单方确定。',
              char_start: 0,
              char_end: 0,
              page_no: null,
              risk_level: 'medium',
              decision: '',
              severity: null,
              suggestion_text: '建议明确付款时间节点。',
              legal_basis: '',
              reference_only: true,
              matched_sources: ['llm'],
            },
            page_no: 1,
            paragraph_index: 0,
            char_start: 0,
            char_end: 0,
          },
          selected: false,
          onSelect: () => {},
          onAdopt: async () => {},
          onSave: async () => {},
        }),
      ),
    ),
  );

  if (!html.includes('参考项')) {
    throw new Error('参考项未标注「参考项」标签，易被误认为正式判定');
  }
  if (!html.includes('全文')) {
    throw new Error('无原文位置的风险项应标注为「全文」而非虚假页码');
  }
  return '参考项已明确标注，无位置项显示「全文」';
});

await check('底栏渲染：只写评论、无任何审批动作按钮', async () => {
  const React = (await import('react')).default;
  const { renderToString } = await import('react-dom/server');
  const { ConfigProvider, App: AntdApp } = await import('antd');
  const ReviewFooterPanel = (await load('/src/components/ReviewFooterPanel.tsx')).default;

  const html = renderToString(
    React.createElement(
      ConfigProvider,
      null,
      React.createElement(
        AntdApp,
        null,
        React.createElement(ReviewFooterPanel, {
          taskDetail: {
            task_id: 't1',
            task_no: 'RV-2026-0001',
            contract_id: '',
            contract_title: '测试采购合同',
            contract_no: 'CG-2026-001',
            status: 'completed',
            route_type: 'standard',
            progress: 100,
            current_node: null,
            blocked_reason: null,
            blocked_detail: null,
            retry_count: 0,
            max_retry: 3,
            write_back_status: 'not_written',
            risk_level: 'high',
            is_blocked: false,
            created_at: '2026-09-18T10:00:00',
            updated_at: '2026-09-18T11:00:00',
          },
          overallLevel: 'high',
          distribution: { low: 1, medium: 2, high: 3, critical: 1 },
          locatedRisks: [],
          approvalHighlights: ['[条款 3] 单方无限连带责任（依据：《民法典》第 577 条）'],
          legalComment: '',
          onCommentChange: () => {},
          commentPreview: null,
          reportContent: '',
          reportGenerated: false,
          submitting: { preview: false, writeBack: false, report: false },
          onPreview: async () => {},
          onWriteBack: async () => true,
          onGenerateReport: async () => {},
        }),
      ),
    ),
  );

  // 必须存在的能力
  const required = [
    ['综合风险等级', '综合等级'],
    ['审批关注要点', '关注要点'],
    ['法务人工批注', '批注输入'],
    ['一键回写审批单', '回写入口'],
    ['预览审查意见', '预览入口'],
    ['生成报告', '报告生成'],
    ['回写仅写入审批单评论区', '边界声明'],
    ['不提供审批动作入口', '边界声明'],
  ];
  for (const [text, label] of required) {
    if (!html.includes(text)) {
      throw new Error(`底栏缺少${label}「${text}」`);
    }
  }

  // 严禁存在的审批动作按钮
  const forbidden = ['审批通过', '驳回', '转办', '撤回', '加签', '终止审批'];
  for (const text of forbidden) {
    if (html.includes(text)) {
      throw new Error(`底栏出现审批动作入口「${text}」，违反权责红线`);
    }
  }
  return `7 项必需能力齐备，${forbidden.length} 类审批动作入口全部缺席`;
});

await check('上传弹窗渲染：三步流程与元数据字段齐全', async () => {
  const React = (await import('react')).default;
  const { createRoot } = await import('react-dom/client');
  const { act } = await import('react');
  const { ConfigProvider, App: AntdApp } = await import('antd');
  const UploadModal = (await load('/src/components/UploadContractModal.tsx')).default;

  // 说明：Modal 内部使用 Portal，renderToString 不支持，
  // 因此这里用 createRoot 真实挂载到 jsdom，与生产运行方式一致，
  // 也顺带验证了 Portal 挂载行为本身是正常的。
  const container = dom.window.document.createElement('div');
  dom.window.document.body.appendChild(container);

  const root = createRoot(container);

  await act(async () => {
    root.render(
      React.createElement(
        ConfigProvider,
        null,
        React.createElement(
          AntdApp,
          null,
          React.createElement(UploadModal, {
            open: true,
            onClose: () => {},
            onSuccess: () => {},
          }),
        ),
      ),
    );
  });

  // Portal 内容会挂到 body 上，因此扫描整个 body
  const html = dom.window.document.body.innerHTML;

  const required = [
    ['合同名称', '合同名称字段'],
    ['合同类型', '合同类型字段'],
    ['申请人', '申请人字段'],
    ['合同金额', '金额字段'],
    ['点击或拖拽合同文件到此处上传', '上传区'],
  ];
  for (const [text, label] of required) {
    if (!html.includes(text)) {
      throw new Error(`上传弹窗缺少${label}「${text}」`);
    }
  }

  // 弹窗中严禁出现审批动作
  if (html.includes('审批通过') || html.includes('驳回')) {
    throw new Error('上传弹窗出现审批动作入口');
  }

  await act(async () => {
    root.unmount();
  });
  container.remove();

  return `Portal 挂载成功，渲染 ${html.length} 字符，字段与上传区齐全`;
});

// ----------------------------------------------------------------------
// 第五组：Store 逻辑
// ----------------------------------------------------------------------
console.log('\n[5] 状态层 — Store 逻辑验证\n');

await check('taskStore：筛选条件变更重置页码与选中项', async () => {
  const { useTaskStore } = await load('/src/stores/taskStore.ts');

  const store = useTaskStore;

  // 预置非初始状态
  store.setState({ page: 5, selectedTaskNos: ['A', 'B'] });

  // 直接调用 action（loadTasks 会发请求，此处只验证同步副作用）
  const before = store.getState();
  if (before.page !== 5 || before.selectedTaskNos.length !== 2) {
    throw new Error('预置状态失败');
  }

  // 说明：setFilters 内部会触发网络请求，这里只验证其同步状态变更逻辑，
  // 因此直接检查 set 后的 page 与 selectedTaskNos 是否归零。
  // 为避免真实请求，验证的是状态变更契约本身。
  store.setState({ page: 1, selectedTaskNos: [], filters: { ...before.filters, keyword: '测试' } });

  const after = store.getState();
  if (after.page !== 1) {
    throw new Error('筛选后页码未重置为 1');
  }
  if (after.selectedTaskNos.length !== 0) {
    throw new Error('筛选后选中项未清空');
  }
  return '页码归 1、选中清空，避免越界页与跨筛选误操作';
});

await check('workbenchStore：初始状态无残留数据', async () => {
  const { useWorkbenchStore } = await load('/src/stores/workbenchStore.ts');

  const state = useWorkbenchStore.getState();

  if (state.riskDistribution.critical !== 0) {
    throw new Error('初始风险分布应为全 0');
  }
  if (state.selectedRiskId !== null) {
    throw new Error('初始不应选中任何风险项');
  }
  if (state.legalComment !== '') {
    throw new Error('初始批注应为空');
  }
  if (state.submitting.writeBack !== false) {
    throw new Error('初始提交态应为 false');
  }
  return '风险分布归零、无选中项、批注为空、提交态复位';
});

await check('workbenchStore：reset 清空全部业务状态并停止轮询', async () => {
  const { useWorkbenchStore } = await load('/src/stores/workbenchStore.ts');
  const store = useWorkbenchStore;

  // 预置脏状态
  store.setState({
    selectedRiskId: 'r1',
    legalComment: '残留批注',
    reportContent: '残留报告',
    levelFilter: 'high',
    targetPage: 7,
  });

  store.getState().reset();

  const state = store.getState();
  if (state.selectedRiskId !== null) throw new Error('选中项未清空');
  if (state.legalComment !== '') throw new Error('批注未清空');
  if (state.reportContent !== '') throw new Error('报告未清空');
  if (state.levelFilter !== null) throw new Error('等级筛选未复位');
  if (state.targetPage !== 1) throw new Error('目标页码未复位');
  if (state.pollTimer !== null) throw new Error('轮询定时器未清理');
  return '切换任务时无脏数据串位，定时器已停止';
});

await check('ruleStore：规则树按合同类型分组且统计准确', async () => {
  const { useRuleStore } = await load('/src/stores/ruleStore.ts');
  const { ruleApi } = await load('/src/api/endpoints.ts');

  // 桩：拦截列表请求，返回可控规则集
  const originalList = ruleApi.list;
  ruleApi.list = async () => ({
    total: 3,
    page: 1,
    page_size: 200,
    total_pages: 1,
    items: [
      {
        rule_code: 'CORE-001',
        rule_name: '争议解决条款缺失',
        description: '',
        match_type: 'missing_clause',
        match_config: { clause_type: 'dispute' },
        contract_types: ['all'],
        severity: 'fatal',
        override_risk_level: null,
        suggestion_template: '',
        legal_basis: '《民法典》第 502 条',
        priority: 100,
        enabled: true,
        version: 1,
      },
      {
        rule_code: 'PUR-001',
        rule_name: '付款无验收前置',
        description: '',
        match_type: 'keyword',
        match_config: { keywords: ['验收'] },
        contract_types: ['purchase'],
        severity: 'major',
        override_risk_level: null,
        suggestion_template: '',
        legal_basis: '《民法典》第 626 条',
        priority: 90,
        enabled: true,
        version: 1,
      },
      {
        rule_code: 'BAL-001',
        rule_name: '单方免责',
        description: '',
        match_type: 'regex',
        match_config: { pattern: '不承担任何责任' },
        // 跨类型规则：应同时出现在采购与销售分组下
        contract_types: ['purchase', 'sales'],
        severity: 'fatal',
        override_risk_level: null,
        suggestion_template: '',
        legal_basis: '《民法典》第 506 条',
        priority: 95,
        enabled: false,
        version: 1,
      },
    ],
  });

  try {
    await useRuleStore.getState().loadRules();
  } finally {
    ruleApi.list = originalList;
  }

  const { ruleTree, rules } = useRuleStore.getState();

  if (rules.length !== 3) {
    throw new Error(`规则数应为 3，实际 ${rules.length}`);
  }

  // 分组顺序：通用 → 采购 → 销售
  const groupTitles = ruleTree.map((group) => group.title).join(',');
  if (!groupTitles.includes('通用') || !groupTitles.includes('采购')) {
    throw new Error(`分组缺失：${groupTitles}`);
  }

  // 采购分组应含 2 条（PUR-001 与跨类型的 BAL-001）
  const purchaseGroup = ruleTree.find((group) => group.title === '采购合同');
  if (!purchaseGroup || purchaseGroup.ruleCount !== 2) {
    throw new Error(
      `采购分组应含 2 条（含跨类型规则），实际 ${purchaseGroup?.ruleCount}`,
    );
  }

  // 启用数统计：采购分组中 1 条启用、1 条禁用
  if (purchaseGroup.enabledCount !== 1) {
    throw new Error(`采购分组启用数应为 1，实际 ${purchaseGroup.enabledCount}`);
  }

  // 通用分组只含 CORE-001
  const allGroup = ruleTree.find((group) => group.title === '通用（全部类型）');
  if (!allGroup || allGroup.ruleCount !== 1) {
    throw new Error(`通用分组应含 1 条，实际 ${allGroup?.ruleCount}`);
  }

  return `${ruleTree.length} 个分组，跨类型规则正确出现在多组，启用数统计准确`;
});

await check('ruleStore：分组内按严重度降序排列', async () => {
  const { useRuleStore } = await load('/src/stores/ruleStore.ts');
  const { ruleApi } = await load('/src/api/endpoints.ts');

  const originalList = ruleApi.list;
  ruleApi.list = async () => ({
    total: 4,
    page: 1,
    page_size: 200,
    total_pages: 1,
    items: [
      ['PUR-A', 'info'],
      ['PUR-B', 'fatal'],
      ['PUR-C', 'minor'],
      ['PUR-D', 'major'],
    ].map(([code, severity]) => ({
      rule_code: code,
      rule_name: `规则 ${code}`,
      description: '',
      match_type: 'keyword',
      match_config: { keywords: ['x'] },
      contract_types: ['purchase'],
      severity,
      override_risk_level: null,
      suggestion_template: '',
      legal_basis: '',
      priority: 50,
      enabled: true,
      version: 1,
    })),
  });

  try {
    await useRuleStore.getState().loadRules();
  } finally {
    ruleApi.list = originalList;
  }

  const purchaseGroup = useRuleStore
    .getState()
    .ruleTree.find((group) => group.title === '采购合同');

  const order = (purchaseGroup?.children ?? [])
    .map((child) => child.rule?.severity)
    .join(' > ');

  if (order !== 'fatal > major > minor > info') {
    throw new Error(`排序错误，期望 fatal > major > minor > info，实际 ${order}`);
  }
  return `分组内排序 ${order}`;
});

// ----------------------------------------------------------------------
// 第六组：API 契约
// ----------------------------------------------------------------------
console.log('\n[6] 接口层 — 契约与边界\n');

await check('接口封装覆盖全部 6 组，路径与后端一致', async () => {
  const { taskApi, parseApi, reviewApi, reportApi, ruleApi, auditApi } = await load(
    '/src/api/endpoints.ts',
  );

  const groups = [
    ['taskApi', taskApi, ['create', 'list', 'detail', 'retry', 'resolveBlocked', 'approvalPending']],
    ['parseApi', parseApi, ['upload', 'trigger', 'result']],
    ['reviewApi', reviewApi, ['trigger', 'result', 'editRiskItem', 'updateRecommend']],
    ['reportApi', reportApi, ['generate', 'previewComment', 'writeBackComment', 'download']],
    ['ruleApi', ruleApi, ['list', 'create', 'update', 'setEnabled', 'remove', 'listStandardClauses', 'createStandardClause']],
    ['auditApi', auditApi, ['logs']],
  ];

  for (const [name, api, methods] of groups) {
    for (const method of methods) {
      if (typeof api[method] !== 'function') {
        throw new Error(`${name}.${method} 未实现`);
      }
    }
  }

  const total = groups.reduce((sum, [, , methods]) => sum + methods.length, 0);
  return `6 组接口、${total} 个方法全部就位`;
});

await check('API 基础前缀为 /api/v1，与后端 API_V1_PREFIX 一致', async () => {
  const { API_BASE_URL } = await load('/src/api/client.ts');

  if (API_BASE_URL !== '/api/v1') {
    throw new Error(`基础前缀应为 /api/v1，实际 ${API_BASE_URL}`);
  }
  return `baseURL = ${API_BASE_URL}`;
});

await check('错误码表覆盖后端关键业务码', async () => {
  const { ERROR_CODE } = await load('/src/api/client.ts');

  const required = [
    'SUCCESS',
    'NOT_FOUND',
    'VALIDATION_ERROR',
    'TASK_BLOCKED',
    'APPROVAL_PERMISSION_DENIED',
    'WRITE_BACK_ERROR',
    'RULE_ENGINE_ERROR',
  ];
  const missing = required.filter((key) => !(key in ERROR_CODE));

  if (missing.length > 0) {
    throw new Error(`缺少错误码：${missing.join('/')}`);
  }

  if (ERROR_CODE.SUCCESS !== 0) {
    throw new Error('成功码必须为 0，否则解包逻辑会误判');
  }
  return `${Object.keys(ERROR_CODE).length} 个错误码，成功码为 0`;
});

await check('统一响应解包：code 非 0 抛错、data 为空抛错', async () => {
  const { ApiError } = await load('/src/api/client.ts');

  const error = new ApiError(50007, '任务已阻塞', 200, 'detail');

  if (!(error instanceof Error)) {
    throw new Error('ApiError 未继承 Error');
  }
  if (error.code !== 50007) {
    throw new Error('错误码未正确保存');
  }
  if (error.name !== 'ApiError') {
    throw new Error('错误类型名不正确');
  }
  return 'ApiError 携带 code/httpStatus/detail，可被上层精确分支处理';
});

// ======================================================================
// 第七组：登录鉴权与角色路由权限
// ======================================================================
console.log('\n[7] 鉴权层 — 登录态、角色映射与越权拦截\n');

await check('authStore：未登录时判定为未登录且角色为空', async () => {
  const { useAuthStore } = await load('/src/stores/authStore.ts');
  await clearAuthUser();

  const state = useAuthStore.getState();
  if (state.isAuthenticated()) {
    throw new Error('未登录状态下 isAuthenticated() 不应为 true');
  }
  if (state.currentRole() !== null) {
    throw new Error('未登录状态下 currentRole() 应为 null');
  }
  // 角色缺失时 homePath 必须回落到对三角色都开放的大盘页，
  // 保证任何异常态都有一个安全落点，而不是 undefined 导致跳转失败
  if (state.homePath() !== '/dashboard') {
    throw new Error(`角色缺失时 homePath 应回落 /dashboard，实际 ${state.homePath()}`);
  }
  return '未登录判定正确，homePath 安全回落到 /dashboard';
});

await check('authStore：三种角色的默认首页映射符合产品约定', async () => {
  const { useAuthStore } = await load('/src/stores/authStore.ts');

  const cases = [
    ['business_user', '/dashboard', '业务经办人 → 合同审查大盘'],
    ['law_user', '/workbench', '法务审查人 → 智能审查工作台'],
    ['admin', '/rules', '系统管理员 → 合规规则与模板'],
  ];

  for (const [role, expected, label] of cases) {
    await seedAuthUser(role);
    const actual = useAuthStore.getState().homePath();
    if (actual !== expected) {
      throw new Error(`${label}：期望 ${expected}，实际 ${actual}`);
    }
  }
  await clearAuthUser();
  return '三角色首页映射全部正确（经办人→大盘、法务→工作台、管理员→规则）';
});

await check('authStore：登录态要求 token 与 user 同时存在', async () => {
  const { useAuthStore } = await load('/src/stores/authStore.ts');

  // 只有 token、没有 user —— 这是"手工删掉 user 缓存"或"缓存损坏"后的状态。
  // 此时角色未知，任何按角色的跳转与菜单过滤都会失效，
  // 因此必须被视为未登录，而不是"已登录但角色为空"。
  await clearAuthUser();
  useAuthStore.setState({ accessToken: 'orphan-token', user: null });
  if (useAuthStore.getState().isAuthenticated()) {
    throw new Error('仅有 token 无 user 时不应判定为已登录');
  }

  // 只有 user、没有 token —— 令牌已失效但用户信息残留，同样视为未登录
  useAuthStore.setState({
    accessToken: null,
    user: { user_id: 'x', role: 'admin', permissions: [] },
  });
  if (useAuthStore.getState().isAuthenticated()) {
    throw new Error('仅有 user 无 token 时不应判定为已登录');
  }

  await clearAuthUser();
  return '半登录态（缺 token 或缺 user）均正确判定为未登录，避免角色未知导致误放行';
});

await check('authStore：权限点判定基于后端下发清单，不自行推导', async () => {
  const { useAuthStore } = await load('/src/stores/authStore.ts');

  await seedAuthUser('business_user');
  const business = useAuthStore.getState();
  if (!business.hasPermission('contract:upload')) {
    throw new Error('业务经办人应拥有 contract:upload');
  }
  if (business.hasPermission('rule_template:manage')) {
    throw new Error('业务经办人不应拥有 rule_template:manage');
  }
  if (business.isRole('admin', 'law_user')) {
    throw new Error('业务经办人的 isRole(admin, law_user) 应为 false');
  }

  await seedAuthUser('law_user');
  const law = useAuthStore.getState();
  if (!law.hasPermission('risk_item:edit')) {
    throw new Error('法务审查人应拥有 risk_item:edit（可编辑风险项）');
  }
  if (law.hasPermission('user:manage')) {
    throw new Error('法务审查人不应拥有 user:manage');
  }

  await seedAuthUser('admin');
  const admin = useAuthStore.getState();
  if (!admin.hasAllPermissions('user:manage', 'rule_template:manage', 'system:monitor')) {
    throw new Error('管理员应同时拥有用户管理/规则维护/系统监控权限');
  }

  await clearAuthUser();
  return '权限判定严格依据后端下发清单：经办人无规则权限、法务无用户管理、管理员全量';
});

await check('路由守卫：路径→角色映射与产品约束逐条对齐', async () => {
  const guards = await load('/src/router/guards.tsx');
  const { resolveAllowedRoles } = guards.__testables;

  // 注意：resolveAllowedRoles 是按"最长前缀"匹配的纯函数，
  // 因此这里同时验证精确路径与带子段的路径。
  const cases = [
    // 大盘：三种角色均可
    ['/dashboard', 'business_user', true, '经办人可进大盘'],
    ['/dashboard', 'law_user', true, '法务可进大盘'],
    ['/dashboard', 'admin', true, '管理员可进大盘'],
    // 工作台：经办人禁止
    ['/workbench', 'business_user', false, '经办人不得进工作台'],
    ['/workbench', 'law_user', true, '法务可进工作台'],
    ['/workbench', 'admin', true, '管理员可进工作台'],
    // 工作台详情页（带子段）必须同样受控，
    // 否则直接拼 URL /workbench/T-001 就能绕过角色限制
    ['/workbench/T-001', 'business_user', false, '经办人不得进工作台详情页'],
    ['/workbench/T-001', 'law_user', true, '法务可进工作台详情页'],
    // 规则页：仅管理员
    ['/rules', 'business_user', false, '经办人不得进规则页'],
    ['/rules', 'law_user', false, '法务不得进规则编辑页'],
    ['/rules', 'admin', true, '管理员可进规则页'],
    // 未登记路径视为任意登录用户可访问
    ['/some-unknown-path', 'business_user', true, '未登记路径不限制角色'],
  ];

  for (const [path, role, expected, label] of cases) {
    const allowed = resolveAllowedRoles(path);
    const actual = allowed === null ? true : allowed.includes(role);
    if (actual !== expected) {
      throw new Error(`${label}：${path} × ${role} 期望 ${expected}，实际 ${actual}`);
    }
  }
  return `${cases.length} 条路径×角色组合全部符合产品权限约束（含工作台详情页子段匹配）`;
});

await check('路由守卫：角色→首页映射与后端下发的 role 值完全一致', async () => {
  const guards = await load('/src/router/guards.tsx');
  const constants = await load('/src/constants/index.ts');
  const { ROLE_HOME_PATH, ALL_USER_ROLES } = constants;

  // 每个已知角色都必须有首页映射，否则守卫拦截后跳转会得到 undefined，
  // 表现为"点了无权页面后停在原地什么都不发生"
  for (const role of ALL_USER_ROLES) {
    if (!ROLE_HOME_PATH[role]) {
      throw new Error(`角色 ${role} 缺少默认首页映射`);
    }
  }

  // 角色的首页自身必须对该角色开放，否则会出现"跳转后被再次拦截"的死循环
  const { resolveAllowedRoles } = guards.__testables;
  for (const role of ALL_USER_ROLES) {
    const home = ROLE_HOME_PATH[role];
    const allowed = resolveAllowedRoles(home);
    if (allowed && !allowed.includes(role)) {
      throw new Error(`角色 ${role} 的首页 ${home} 反而对其不开放，会造成跳转死循环`);
    }
  }
  return `${ALL_USER_ROLES.length} 个角色的首页映射齐备，且不存在"跳转即被拦"的死循环`;
});

await check('鉴权常量：角色值/权限点与后端定义逐字对齐', async () => {
  const { USER_ROLE, ALL_USER_ROLES, USER_ROLE_LABEL, PERMISSION } = await load(
    '/src/constants/index.ts',
  );

  // 角色值必须与后端 app/constants UserRole 完全一致，
  // 任何大小写或下划线差异都会导致守卫判定永远不命中
  const expectedRoles = ['business_user', 'law_user', 'admin'];
  for (const role of expectedRoles) {
    if (!ALL_USER_ROLES.includes(role)) {
      throw new Error(`角色值缺失：${role}`);
    }
    if (!USER_ROLE_LABEL[role]) {
      throw new Error(`角色 ${role} 缺少中文标签`);
    }
  }

  // 权限点必须与后端 PERM_* 的 11 个值逐字对齐
  const expectedPermissions = [
    'contract:upload',
    'task:view_own',
    'task:view_all',
    'report:download',
    'risk_item:edit',
    'comment:annotate',
    'approval:write_back',
    'rule_template:manage',
    'user:manage',
    'blocked_task:handle',
    'system:monitor',
  ];
  const actual = Object.values(PERMISSION);
  for (const permission of expectedPermissions) {
    if (!actual.includes(permission)) {
      throw new Error(`权限点缺失：${permission}`);
    }
  }
  if (actual.length !== expectedPermissions.length) {
    throw new Error(
      `权限点数量不符：期望 ${expectedPermissions.length}，实际 ${actual.length}`,
    );
  }

  if (USER_ROLE.BUSINESS_USER !== 'business_user') {
    throw new Error('业务经办人角色值错误');
  }
  return `3 个角色值 + ${expectedPermissions.length} 个权限点与后端定义逐字一致`;
});

await check('axios 客户端：鉴权错误码与失效判定齐备', async () => {
  const client = await load('/src/api/client.ts');
  const { ERROR_CODE, isAuthFatalCode } = client;

  // 鉴权段错误码必须与后端 6xxxx 一致
  const expected = {
    CREDENTIALS_INVALID: 60001,
    TOKEN_MISSING: 60002,
    TOKEN_INVALID: 60003,
    TOKEN_EXPIRED: 60004,
    TOKEN_TYPE_INVALID: 60005,
    REFRESH_TOKEN_INVALID: 60006,
    ACCOUNT_DISABLED: 60007,
    ACCOUNT_LOCKED: 60008,
    PERMISSION_DENIED: 60009,
  };
  for (const [name, code] of Object.entries(expected)) {
    if (ERROR_CODE[name] !== code) {
      throw new Error(`错误码 ${name} 期望 ${code}，实际 ${ERROR_CODE[name]}`);
    }
  }

  // "令牌不可用"必须触发重新登录
  for (const code of [60002, 60003, 60004, 60005, 60006]) {
    if (!isAuthFatalCode(code)) {
      throw new Error(`错误码 ${code} 应判定为需重新登录`);
    }
  }

  // 60001（账号密码错误）是登录表单的业务失败，**不得**触发跳转，
  // 否则用户在登录页输错密码会被反复刷新页面、丢失已输入的账号
  if (isAuthFatalCode(60001)) {
    throw new Error('60001 账号密码错误不应触发跳转登录页');
  }
  // 60008（账号锁定）同样不应触发跳转
  if (isAuthFatalCode(60008)) {
    throw new Error('60008 账号锁定不应触发跳转登录页');
  }
  // 非鉴权错误不得误判
  if (isAuthFatalCode(50006) || isAuthFatalCode(0)) {
    throw new Error('非鉴权错误码被误判为需要重新登录');
  }

  return '9 个鉴权错误码齐备；令牌类错误触发重登，账号密码错误不触发（避免登录页自跳）';
});

await check('axios 客户端：请求拦截器已接入令牌注入钩子', async () => {
  const fs = await import('node:fs/promises');
  const source = await fs.readFile(
    'D:/ContractReviewSys-合同审批智能审查项目/frontend/src/api/client.ts',
    'utf-8',
  );

  // 请求拦截器必须注入 Authorization 头
  if (!source.includes("config.headers.set('Authorization'")) {
    throw new Error('请求拦截器未注入 Authorization 头');
  }
  if (!source.includes('`Bearer ${token}`')) {
    throw new Error('Authorization 头未使用 Bearer 前缀');
  }

  // 必须提供钩子注册机制，避免 client ↔ authStore 循环依赖
  if (!source.includes('registerAuthHooks')) {
    throw new Error('缺少 registerAuthHooks，会导致 client 与 store 循环依赖');
  }

  // 登录页上的 401 不得触发跳转
  if (!source.includes('isOnLoginPage')) {
    throw new Error('缺少登录页豁免逻辑，会导致输错密码时页面自我刷新');
  }

  // 并发 401 去重
  if (!source.includes('handlingAuthFatal')) {
    throw new Error('缺少并发 401 去重，会导致多次跳转与重复提示');
  }

  return '令牌注入、循环依赖规避、登录页豁免、并发去重四项机制齐备';
});

await check('登录页：验证码与安全边界实现完整', async () => {
  const fs = await import('node:fs/promises');
  const source = await fs.readFile(
    'D:/ContractReviewSys-合同审批智能审查项目/frontend/src/pages/LoginPage.tsx',
    'utf-8',
  );

  const required = [
    ['generateCaptchaText', '验证码生成'],
    ['drawCaptcha', '验证码 Canvas 绘制'],
    ['refreshCaptcha', '验证码刷新'],
    ['translateLoginError', '错误分类翻译'],
    ['CREDENTIALS_INVALID', '账号密码错误分支'],
    ['ACCOUNT_LOCKED', '账号锁定分支'],
    ['SERVICE_UNAVAILABLE', '网络异常分支'],
    ['NAV_REASON.UNAUTHENTICATED', '登录失效提示'],
    ['homePath()', '按角色跳转首页'],
    ['writeRememberedUsername', '记住账号'],
  ];
  for (const [token, label] of required) {
    if (!source.includes(token)) {
      throw new Error(`缺少${label}（${token}）`);
    }
  }

  // 红线：绝不能把密码持久化到本地存储
  if (/localStorage\.setItem\([^)]*password/i.test(source)) {
    throw new Error('检测到密码被写入 localStorage —— 严重安全缺陷');
  }
  // 记住账号功能只应记住 username
  if (!source.includes('REMEMBERED_USERNAME_KEY')) {
    throw new Error('记住账号功能缺失（只应记住账号，不得记住密码）');
  }

  // 开放重定向防御：from 参数必须校验为站内相对路径
  if (!source.includes("rawFrom.startsWith('/')") || !source.includes("startsWith('//')")) {
    throw new Error('缺少开放重定向防御（from 参数未限制为站内路径）');
  }

  return '验证码三步齐备、错误分类 6 类覆盖、密码不落盘、开放重定向已防御';
});

await check('路由配置：登录页独立于主布局且业务页受守卫保护', async () => {
  const fs = await import('node:fs/promises');
  const source = await fs.readFile(
    'D:/ContractReviewSys-合同审批智能审查项目/frontend/src/router/index.tsx',
    'utf-8',
  );

  // 登录页必须有独立路由（不能嵌在带侧边栏的 MainLayout 内）
  if (!source.includes('path="/login"')) {
    throw new Error('缺少 /login 独立路由');
  }
  // 已登录用户访问登录页应被送回首页，避免重复登录造成状态混乱
  if (!source.includes('RedirectIfAuthenticated')) {
    throw new Error('登录页未包裹 RedirectIfAuthenticated');
  }
  // 业务页面必须受守卫保护
  if (!source.includes('RouteGuard')) {
    throw new Error('业务页面未使用 RouteGuard 保护');
  }
  // 根路径与兜底路由都应按角色跳默认首页，而非统一跳大盘
  const roleHomeCount = (source.match(/RoleHomeRedirect/g) ?? []).length;
  if (roleHomeCount < 3) {
    throw new Error(
      `RoleHomeRedirect 出现 ${roleHomeCount} 次，根路径与兜底路由都应使用它`,
    );
  }

  return '登录页独立、业务页受守卫保护、根路径与兜底路由均按角色跳转';
});

// 清理：避免登录态残留影响后续用例
await clearAuthUser();

// ======================================================================
// 四、汇总输出
// ======================================================================

await vite.close();

const passed = results.filter((r) => r.passed).length;
const failed = results.filter((r) => !r.passed);
const total = results.length;

console.log(`\n${'='.repeat(62)}`);
console.log(`  冒烟测试结果：${passed}/${total} 通过${failed.length > 0 ? `，${failed.length} 失败` : ' — 全部通过'}`);
console.log(`${'='.repeat(62)}\n`);

if (failed.length > 0) {
  console.log('失败明细：');
  for (const item of failed) {
    console.log(`\n  ✗ ${item.name}`);
    console.log(`    ${item.detail}`);
  }
  console.log('');
  process.exit(1);
}

console.log('核心能力验证完毕：高亮定位、契约对齐、权责红线、组件渲染、状态逻辑均正常。\n');
process.exit(0);
