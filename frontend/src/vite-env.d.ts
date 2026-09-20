// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/vite-env.d.ts
/**
 * 环境与模块类型声明
 *
 * 【文件职责】
 *   为 Vite 特有导入与第三方库缺失的类型入口补充声明，
 *   使 TypeScript 严格模式下能够正确推断这些模块的类型。
 *
 * 【需要本文件的原因（已实测确认）】
 *   1. pdfjs-dist 的 Worker 资源通过 Vite 的 `?url` 后缀导入，
 *      TypeScript 不认识该后缀，必须显式声明其返回字符串 URL。
 *      若缺少此声明，打包后 Worker 路径会失效导致 PDF 无法渲染。
 *   2. antd 5.29 的 package.json 未声明 exports 映射，其语言包
 *      `antd/locale/zh_CN` 只有 .js 没有配套 .d.ts，
 *      在 moduleResolution: bundler 下无法解析类型。
 *
 * 【为什么不声明 antd/es/* 深层路径】
 *   antd 顶层已导出 ColumnsType / UploadFile / TableProps 等全部
 *   业务需要的类型，业务代码一律从 'antd' 导入。
 *   为深层路径写 declare module 会覆盖 antd 原有类型，反而丢类型安全，
 *   因此这里不做深层路径声明，业务侧改用顶层导出。
 */

/// <reference types="vite/client" />

// ======================================================================
// 一、Vite 资源导入后缀声明
// ======================================================================

/**
 * `?url` 后缀导入
 *
 * 说明：Vite 会把资源处理为可访问的 URL 字符串。
 * pdfjs-dist 的 Worker 脚本必须通过这种方式引入。
 */
declare module '*?url' {
  const url: string;
  export default url;
}

/**
 * `?raw` 后缀导入，返回文件文本内容
 */
declare module '*?raw' {
  const content: string;
  export default content;
}

/**
 * pdfjs-dist Worker 的具体路径声明
 *
 * 说明：通配 `*?url` 已覆盖该场景，此处显式列出以便阅读时明确意图，
 * 同时兼容部分编辑器对通配模块解析不完善的场景。
 */
declare module 'pdfjs-dist/build/pdf.worker.min.mjs?url' {
  const url: string;
  export default url;
}

// ======================================================================
// 二、antd 语言包声明
// ======================================================================

/**
 * antd 中文语言包
 *
 * 说明：antd 的 locale 目录仅提供 .js 实现，无配套 .d.ts。
 * 此处声明其结构，使 ConfigProvider 的 locale 属性类型正确。
 */
declare module 'antd/locale/zh_CN' {
  const locale: {
    locale: string;
    [key: string]: unknown;
  };
  export default locale;
}
