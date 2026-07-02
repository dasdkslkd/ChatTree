import assert from 'node:assert/strict';
import { mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'vite';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(__dirname, '..');
const tempDir = path.join(projectRoot, 'node_modules', '.tmp', 'markdown-rich-test');
const entryPath = path.join(tempDir, 'entry.tsx');
const outputDir = path.join(tempDir, 'dist');

await rm(tempDir, { recursive: true, force: true });
await mkdir(tempDir, { recursive: true });
await writeFile(
  entryPath,
  [
    "import MarkdownRich from '@/components/markdown/MarkdownRich';",
    "export { MarkdownRich };",
    '',
  ].join('\n'),
  'utf8',
);

await build({
  root: projectRoot,
  logLevel: 'silent',
  configFile: false,
  plugins: [(await import('@vitejs/plugin-react')).default()],
  build: {
    ssr: entryPath,
    outDir: outputDir,
    emptyOutDir: true,
    rollupOptions: {
      external: ['react', 'react-dom', 'react-dom/server'],
      output: {
        format: 'esm',
        entryFileNames: 'entry.mjs',
      },
    },
  },
  resolve: {
    alias: {
      '@': path.join(projectRoot, 'src'),
    },
  },
});

const bundle = await import(pathToFileURL(path.join(outputDir, 'entry.mjs')).href);
const html = renderToStaticMarkup(
  React.createElement(bundle.MarkdownRich, null, [
    'Inline $\\int x\\,dx$',
    '',
    '\\[',
    '\\frac{1}{2}',
    '\\]',
    '',
    '<script>alert("x")</script><details><summary>ok</summary>body</details>',
  ].join('\n')),
);

assert.match(html, /katex/);
assert.doesNotMatch(html, /<script/i);
assert.match(html, /details/i);

const css = await readFile(path.join(projectRoot, 'src/App.css'), 'utf8');
assert.match(css, /\.prose \.katex-display/);

await rm(tempDir, { recursive: true, force: true });
console.log('markdownRichRender tests passed');
