// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/main.tsx
/**
 * 应用入口
 *
 * 【文件职责】
 *   挂载 React 根节点，注入全局 Provider（Ant Design 主题、路由、国际化）。
 *
 * 【Ant Design 主题定制策略】
 *   不逐组件覆盖样式，而是通过 ConfigProvider 的 token 统一调校：
 *     - 主色沿用系统蓝，符合企业级审批系统的视觉惯例
 *     - 圆角收敛为 6px，避免默认 8px 在大表格中显得松散
 *     - 紧凑密度，使大盘表格能在一屏内承载更多行
 *
 * 【中文国际化】
 *   Ant Design 组件内置文案（分页、日期、空状态等）默认英文，
 *   必须显式注入 zhCN，否则会出现中英混排。
 */

import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { App as AntdApp, ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import dayjs from 'dayjs';
import 'dayjs/locale/zh-cn';

import AppRouter from '@/router';

// dayjs 全局中文，保证时间格式化的星期/月份为中文
dayjs.locale('zh-cn');

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error('未找到根节点 #root，请检查 index.html 挂载点配置');
}

ReactDOM.createRoot(rootElement).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        // token 为全局设计变量，所有组件共享
        token: {
          colorPrimary: '#1677ff',
          // 圆角收敛为 6px，避免默认 8px 在大表格中显得松散
          borderRadius: 6,
          fontSize: 14,
        },
        components: {
          // 表格紧凑化：企业审批系统列表信息密度高于常规站点
          Table: {
            cellPaddingBlock: 10,
            headerBg: '#fafafa',
          },
          Layout: {
            headerHeight: 64,
          },
        },
      }}
    >
      {/* AntdApp 提供 message / modal / notification 的上下文，
          使这些静态方法能正确继承主题与国际化配置 */}
      <AntdApp>
        <BrowserRouter>
          <AppRouter />
        </BrowserRouter>
      </AntdApp>
    </ConfigProvider>
  </React.StrictMode>,
);
