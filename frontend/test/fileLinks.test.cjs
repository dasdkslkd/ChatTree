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

function createMockReact() {
  return {
    createElement: (type, props, ...children) => ({ type, props, children }),
  };
}

function loadFileLinks() {
  const source = fs.readFileSync(path.join(sourceRoot, 'components/markdown/fileLinks.tsx'), 'utf8');
  const transpiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.React,
      esModuleInterop: true,
      importsNotUsedAsValues: ts.ImportsNotUsedAsValues.Remove,
    },
    fileName: 'fileLinks.tsx',
  }).outputText;

  const moduleObj = { exports: {} };
  const requireMock = (name) => {
    if (name === 'react') return createMockReact();
    if (name === 'sonner') return { toast: { error: () => {} } };
    if (name === '../../api/errors') return { getApiErrorMessage: (_error, fallback) => fallback };
    if (name === '../../api/files') return { filesApi: { open: async () => ({ path: '' }) } };
    throw new Error(`unexpected require: ${name}`);
  };
  const fn = new Function('module', 'exports', 'require', 'React', transpiled);
  fn(moduleObj, moduleObj.exports, requireMock, createMockReact());
  return moduleObj.exports;
}

function rangesOf(text) {
  return loadFileLinks().findFilePathRanges(text).map(({ start, end }) => text.slice(start, end));
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
  assert.deepEqual(rangesOf('见 /etc/hosts'), ['/etc/hosts']);
}

function testTrimsTrailingPunctuation() {
  assert.deepEqual(rangesOf('见 C:\\a\\b\\c.py。'), ['C:\\a\\b\\c.py']);
  assert.deepEqual(rangesOf('见 C:\\a\\b\\c.py，'), ['C:\\a\\b\\c.py']);
  assert.deepEqual(rangesOf('(C:\\a\\b\\c.py)'), ['C:\\a\\b\\c.py']);
}

function testIgnoresRelativePathsAndPlainWords() {
  assert.deepEqual(rangesOf('见 src/main.py'), []);
  assert.deepEqual(rangesOf('main.py'), []);
  assert.deepEqual(rangesOf('运行 npm test'), []);
}

function testIgnoresPathsWithSpaces() {
  assert.deepEqual(rangesOf('见 C:\\my folder\\x.txt'), []);
}

// ===== components =====

function testFileLinkTextRendersPathLinks() {
  const { fileLinkComponents } = loadFileLinks();
  const nodes = fileLinkComponents.text({ children: '见 C:\\a\\b\\c.py 文件' });
  assert.ok(Array.isArray(nodes));
  assert.equal(nodes[0], '见 ');
  const link = nodes[1];
  assert.equal(typeof link.type, 'function');
  assert.equal(link.props.path, 'C:\\a\\b\\c.py');
  assert.equal(nodes[2], ' 文件');
}

function testFileOpenLinkDecodesFileScheme() {
  const { fileLinkComponents, FILE_LINK_SCHEME } = loadFileLinks();
  const encoded = FILE_LINK_SCHEME + encodeURIComponent('C:\\a\\b\\c.py');
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
  assert.ok(source.split('mergedComponents').length >= 5, 'all render paths should use merged components');
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
  testIgnoresRelativePathsAndPlainWords();
  testIgnoresPathsWithSpaces();
  testFileLinkTextRendersPathLinks();
  testFileOpenLinkDecodesFileScheme();
  testFileOpenLinkKeepsNormalLinks();
  testMarkdownContentMergesFileLinkComponents();
  testFilesApiPostsToOpenEndpoint();
  testRouterRegistersFilesRoutes();
  console.log('fileLinks tests passed');
}

main();
