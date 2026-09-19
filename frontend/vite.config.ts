// D:/ContractReviewSys-合同审批智能审查项目/frontend/vite.config.ts
/**
 * Vite 构建配置
 *
 * 职责：
 *   1. 配置 React 插件
 *   2. 配置路径别名 @ 指向 src 目录
 *   3. 配置开发服务器端口与后端接口代理，解决开发期跨域
 *   4. 支持通过 .env.local 切换"局域网预览"模式（手机/同网段访问）
 *
 * 返回值说明：
 *   defineConfig 返回 Vite 配置对象，由 Vite 在构建/启动时消费
 *
 * 【环境变量来源说明】
 *   本文件通过 loadEnv 显式读取 .env / .env.local，保证 VITE_API_PROXY_TARGET、
 *   VITE_DEV_HOST 等配置 reliably 生效（直接读 process.env 在 Vite 6 下
 *   不会自动携带 .env.local 的字段）。
 */

import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig(({ mode }) => {
  // 显式加载项目根目录下的 .env / .env.local（'' 表示不过滤前缀，全部读入）。
  // 说明：本机 8000 端口可能被 VMware NAT 等程序占用，本地试用时通过
  //       frontend/.env.local 覆盖代理目标，无需改动本文件，
  //       从而保持仓库默认配置与部署文档一致。
  const env = loadEnv(mode, process.cwd(), '');
  const proxyTarget = env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000';

  // 开发服务器监听地址（VITE_DEV_HOST）。
  // 默认值 '127.0.0.1' 仅本机可访问——这是仓库的安全默认，不暴露合同原文。
  // 如需手机/同网段设备访问（局域网预览），在 frontend/.env.local 中设置：
  //   VITE_DEV_HOST=0.0.0.0
  // 并配合 VITE_API_PROXY_TARGET 指向后端实际端口（如 8100）。
  // 注意：绑定 0.0.0.0 后，明文 HTTP 会把合同原文/风险命中暴露给同一网段，
  //       仅限可信 WiFi 下的临时试用；正式部署必须走 HTTPS + Nginx 反代。
  const devHost = env.VITE_DEV_HOST || '127.0.0.1';
  // 仅当显式切换到非本机地址时才放开 allowedHosts（Vite 6 默认只放行 localhost）。
  // 否则手机用局域网 IP 访问会被拦截 "Blocked request. This host is not allowed."
  const lanMode = devHost !== '127.0.0.1' && devHost !== 'localhost';

  return {
    // React 插件：启用 Fast Refresh
    plugins: [react()],

    resolve: {
      alias: {
        // 路径别名：@ 等价于 src 目录，简化深层导入
        '@': path.resolve(__dirname, './src'),
      },
    },

    server: {
      // 开发服务器监听端口，与后端 CORS 白名单保持一致
      port: 5173,
      // 监听地址：默认本机回环，局域网预览时由 VITE_DEV_HOST 覆盖为 0.0.0.0。
      // 说明：不直接写死 0.0.0.0，是为了让仓库默认保持"仅本机"的安全基线。
      host: devHost,
      // 局域网模式下放开 Host 校验，否则手机经局域网 IP 访问会被 Vite 拦截。
      // 本项仅当 lanMode 为真时注入，避免提交即暴露开发服务器。
      ...(lanMode ? { allowedHosts: true as const } : {}),
      // 接口代理：前端以 /api 开头发起的请求转发到后端，规避浏览器跨域
      // 目标地址优先取环境变量 VITE_API_PROXY_TARGET，未设置时回退 8000。
      proxy: {
        '/api': {
          target: proxyTarget,
          changeOrigin: true,
        },
      },
    },

    build: {
      // 构建产物输出目录
      outDir: 'dist',
      // 关闭 sourcemap，减小产物体积（生产环境需要排查时可临时开启）
      sourcemap: false,
      // 单包体积告警阈值（KB），pdfjs-dist 体积较大，适当放宽
      chunkSizeWarningLimit: 1500,
    },
  };
});
