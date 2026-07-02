import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

const apiProxyTarget = process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8001'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    // Mermaid 的 ELK/mindmap 图类型由 markdown 渲染路径懒加载，属于非首屏功能块。
    // 保持明确分包，同时把告警阈值调到覆盖这些已知懒加载图模块，避免构建日志误报。
    chunkSizeWarningLimit: 1600,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined;
          if (id.includes('react-syntax-highlighter') || id.includes('prismjs')) {
            return 'syntax';
          }
          if (id.includes('katex')) {
            return 'markdown-math';
          }
          if (id.includes('react-markdown') || id.includes('remark-') || id.includes('rehype-') || id.includes('micromark') || id.includes('mdast') || id.includes('hast') || id.includes('unified')) {
            return 'markdown';
          }
          if (id.includes('flowchart-elk') || id.includes('elkjs')) {
            return 'mermaid-elk';
          }
          if (id.includes('mindmap')) {
            return 'mermaid-mindmap';
          }
          if (id.includes('mermaid') || id.includes('d3-') || id.includes('dagre') || id.includes('graphlib')) {
            return 'mermaid';
          }
          if (id.includes('react') || id.includes('react-dom')) {
            return 'react';
          }
          return undefined;
        },
      },
    },
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    proxy: {
      '/api': {
        target: apiProxyTarget,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
