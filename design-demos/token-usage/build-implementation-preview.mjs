import { build } from '../../web/node_modules/esbuild/lib/main.js'
import { fileURLToPath } from 'node:url'
import { writeFile } from 'node:fs/promises'

const root = fileURLToPath(new URL('.', import.meta.url))
await build({
  entryPoints: [root + 'implementation-preview.tsx'],
  outfile: root + 'implementation-preview.js',
  bundle: true,
  format: 'esm',
  jsx: 'automatic',
  nodePaths: [fileURLToPath(new URL('../../web/node_modules', import.meta.url))],
  define: { 'process.env.NODE_ENV': '"production"' },
})
await writeFile(root + 'implementation-preview.html', `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>实际 Token 统计组件</title><link rel="stylesheet" href="implementation-preview.css"><style>*{box-sizing:border-box}:root{--app-font-ratio:1;--text-primary:#2b2a28;--text-secondary:#605e58;--text-muted:#6c6963;--rag-text:#2b2a28;--rag-bg-panel:#fdfdfc;--rag-border-strong:#d9d8d3;--rag-shadow:0 16px 35px #33333312}body{margin:0;background:#f7f6f3;font-family:'Segoe UI','Microsoft YaHei',sans-serif}button{padding:8px 12px;border:1px solid #c9cec9;border-radius:6px;color:#4a5f50;background:#fff;font:inherit}button[aria-pressed=true]{background:#e5eee7}</style><div id="root"></div><script type="module" src="implementation-preview.js"></script></html>`, 'utf8')
