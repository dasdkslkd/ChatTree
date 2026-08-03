const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const sourceRoot = path.join(__dirname, '../src');

function readSource(relativePath) {
  return fs.readFileSync(path.join(__dirname, '..', relativePath), 'utf8');
}

function readRootSource(relativePath) {
  return fs.readFileSync(path.join(__dirname, '../..', relativePath), 'utf8');
}

function transpile(relativePath) {
  const source = fs.readFileSync(path.join(sourceRoot, relativePath), 'utf8');
  return ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.React,
      esModuleInterop: true,
      importsNotUsedAsValues: ts.ImportsNotUsedAsValues.Remove,
    },
    fileName: path.basename(relativePath),
  }).outputText;
}

function createMockReact() {
  const createElement = (type, props, ...children) => ({
    type,
    props: props || {},
    children: children.flat(),
  });
  return { createElement };
}

function loadModule(relativePath, requireMock) {
  const transpiled = transpile(relativePath);
  const moduleObj = { exports: {} };
  const fn = new Function('module', 'exports', 'require', 'React', transpiled);
  fn(moduleObj, moduleObj.exports, requireMock, createMockReact());
  return moduleObj.exports;
}

function loadFileLinkDetection() {
  return loadModule('utils/fileLinkDetection.ts', () => {
    throw new Error('unexpected require in fileLinkDetection');
  });
}

function loadFileLinks() {
  const detection = loadFileLinkDetection();
  const requireMock = (name) => {
    if (name === 'react') return createMockReact();
    if (name === 'sonner') return { toast: { error: () => {} } };
    if (name === '../../api/errors') return { getApiErrorMessage: (_error, fallback) => fallback };
    if (name === '../../api/files') return { filesApi: { open: async () => ({ path: '' }) } };
    if (name === '../../utils/fileLinkDetection') return detection;
    throw new Error(`unexpected require: ${name}`);
  };
  return loadModule('components/markdown/fileLinks.tsx', requireMock);
}

function rangesOf(text) {
  return loadFileLinkDetection().findFilePathRanges(text).map(({ path }) => path);
}

// ===== findFilePathRanges =====

function testMatchesWindowsAbsolutePaths() {
  assert.deepEqual(rangesOf('见 D:\\Workspace\\ChatTree\\main.py 文件'), [
    'D:\\Workspace\\ChatTree\\main.py',
  ]);
}

function testMatchesWindowsForwardSlashPaths() {
  assert.deepEqual(rangesOf('见 C:/Users/foo/a.txt'), ['C:/Users/foo/a.txt']);
}

function testMatchesPosixAbsolutePaths() {
  assert.deepEqual(rangesOf('见 /home/user/file.txt'), ['/home/user/file.txt']);
}

function testTrimsTrailingPunctuation() {
  assert.deepEqual(rangesOf('见 C:\\a\\b\\c.py。'), ['C:\\a\\b\\c.py']);
  assert.deepEqual(rangesOf('见 C:\\a\\b\\c.py，'), ['C:\\a\\b\\c.py']);
  assert.deepEqual(rangesOf('(C:\\a\\b\\c.py)'), ['C:\\a\\b\\c.py']);
}

function testIgnoresPlainWords() {
  assert.deepEqual(rangesOf('main.py'), []);
  assert.deepEqual(rangesOf('运行 npm test'), []);
}

function testIgnoresPathsWithSpaces() {
  assert.deepEqual(rangesOf('见 C:\\my folder\\x.txt'), []);
}

// ===== FileLinkWrapper =====

function testFileLinkWrapperRendersPathLinks() {
  const { fileLinkComponents } = loadFileLinks();
  const nodes = fileLinkComponents.p({ children: '见 C:\\a\\b\\c.py 文件' });
  assert.ok(Array.isArray(nodes));
  assert.equal(nodes[0], '见 ');
  const link = nodes[1];
  assert.equal(typeof link.type, 'function');
  assert.equal(link.props.path, 'C:\\a\\b\\c.py');
  assert.equal(nodes[2], ' 文件');
}

function testFileLinkWrapperProcessesCodeChildren() {
  const { fileLinkComponents } = loadFileLinks();
  const codeElement = { type: 'code', props: { children: 'D:\\x\\y\\z.py' } };
  const result = fileLinkComponents.p({ children: [codeElement] });
  assert.ok(Array.isArray(result));
  const processedCode = result[0];
  assert.equal(processedCode.type, 'code');
  const inner = processedCode.props.children;
  assert.ok(Array.isArray(inner));
  assert.equal(typeof inner[0].type, 'function');
  assert.equal(inner[0].props.path, 'D:\\x\\y\\z.py');
}

// ===== FileOpenLink =====

function testFileOpenLinkDecodesFilePrefix() {
  const { fileLinkComponents, FILE_LINK_PREFIX } = loadFileLinks();
  const { FILE_LINK_PREFIX: detectionPrefix } = loadFileLinkDetection();
  const prefix = detectionPrefix || FILE_LINK_PREFIX;
  const encoded = prefix + encodeURIComponent('C:\\a\\b\\c.py');
  const element = fileLinkComponents.a({ href: encoded, children: ['C:\\a\\b\\c.py'] });
  assert.equal(typeof element.type, 'function');
  assert.equal(element.props.path, 'C:\\a\\b\\c.py');
}

function testFileOpenLinkKeepsNormalLinks() {
  const { fileLinkComponents } = loadFileLinks();
  const element = fileLinkComponents.a({ href: 'https://example.com', children: ['example'] });
  assert.equal(element.type, 'a');
  assert.equal(element.props.href, 'https://example.com');
}

// ===== integration (source-level) =====

function testMarkdownContentMergesFileLinkComponents() {
  const source = readSource('src/components/MarkdownContent.tsx');
  assert.match(source, /fileLinkComponents/);
  assert.match(source, /\{ \.\.\.fileLinkComponents, \.\.\.components \}/);
}

function testFilesApiPostsToOpenEndpoint() {
  const source = readSource('src/api/files.ts');
  assert.match(source, /apiClient\.post\('\/files\/open'/);
}

function testRouterRegistersFilesRoutes() {
  const source = readRootSource('backend/api/router.py');
  assert.match(source, /files\.router/);
}

function main() {
  testMatchesWindowsAbsolutePaths();
  testMatchesWindowsForwardSlashPaths();
  testMatchesPosixAbsolutePaths();
  testTrimsTrailingPunctuation();
  testIgnoresPlainWords();
  testIgnoresPathsWithSpaces();
  testFileLinkWrapperRendersPathLinks();
  testFileLinkWrapperProcessesCodeChildren();
  testFileOpenLinkDecodesFilePrefix();
  testFileOpenLinkKeepsNormalLinks();
  testMarkdownContentMergesFileLinkComponents();
  testFilesApiPostsToOpenEndpoint();
  testRouterRegistersFilesRoutes();
  console.log('fileLinks tests passed');
}

main();
