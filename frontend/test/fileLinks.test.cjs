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

function loadFileLinks(currentConversation = null) {
  const detection = loadFileLinkDetection();
  const opened = [];
  const requireMock = (name) => {
    if (name === 'react') return createMockReact();
    if (name === 'sonner') return { toast: { error: () => {}, success: () => {}, info: () => {}, warning: () => {}, message: () => {}, loading: () => {}, dismiss: () => {} } };
    if (name === './errorHistory') return { recordError: () => {} };
    if (name === '@/utils/toast') return { toast: { error: () => {} } };
    if (name === '../../api/errors') return { getApiErrorMessage: (_error, fallback) => fallback };
    if (name === '../../api/files') {
      return { filesApi: { open: async (p) => { opened.push(p); return { path: p }; } } };
    }
    if (name === '../../store/conversationStore') {
      return { useConversationStore: { getState: () => ({ currentConversation }) } };
    }
    if (name === '../../utils/fileLinkDetection') return detection;
    throw new Error(`unexpected require: ${name}`);
  };
  return { ...loadModule('components/markdown/fileLinks.tsx', requireMock), opened };
}

function rangesOf(text) {
  return loadFileLinkDetection().findLinkRanges(text);
}

function filesOf(text) {
  return rangesOf(text).filter((range) => range.kind === 'file').map((range) => range.value);
}

function urlsOf(text) {
  return rangesOf(text).filter((range) => range.kind === 'url').map((range) => range.value);
}

// ===== findLinkRanges: 文件路径 =====

function testMatchesWindowsAbsolutePaths() {
  assert.deepEqual(filesOf('见 D:\\Workspace\\ChatTree\\main.py 文件'), [
    'D:\\Workspace\\ChatTree\\main.py',
  ]);
}

function testMatchesWindowsForwardSlashPaths() {
  assert.deepEqual(filesOf('见 C:/Users/foo/a.txt'), ['C:/Users/foo/a.txt']);
}

function testMatchesPosixAbsolutePaths() {
  assert.deepEqual(filesOf('见 /home/user/file.txt'), ['/home/user/file.txt']);
}

function testMatchesRelativePaths() {
  assert.deepEqual(filesOf('保存到 papers/the-social-signal.pdf 完成'), [
    'papers/the-social-signal.pdf',
  ]);
  assert.deepEqual(filesOf('见 ./a/b.c 与 ../d/e.f'), ['./a/b.c', '../d/e.f']);
}

function testTrimsTrailingPunctuation() {
  assert.deepEqual(filesOf('见 C:\\a\\b\\c.py。'), ['C:\\a\\b\\c.py']);
  assert.deepEqual(filesOf('见 C:\\a\\b\\c.py，'), ['C:\\a\\b\\c.py']);
  assert.deepEqual(filesOf('(C:\\a\\b\\c.py)'), ['C:\\a\\b\\c.py']);
}

function testIgnoresPlainWords() {
  assert.deepEqual(filesOf('main.py'), []);
  assert.deepEqual(filesOf('运行 npm test'), []);
}

function testIgnoresPathsWithSpaces() {
  assert.deepEqual(filesOf('见 C:\\my folder\\x.txt'), []);
}

function testIgnoresVersionRangesAndParens() {
  assert.deepEqual(filesOf('pyc (3.10/3.11)'), []);
  assert.deepEqual(filesOf('pyc(3.10/3.11)'), []);
  assert.deepEqual(filesOf('搜索实际的pyc缓存(3.10/3.11)目录'), []);
  assert.deepEqual(filesOf('比例 3.10/3.11 的文本'), []);
  assert.deepEqual(filesOf('data/{symbol}/{date}/ 组织'), []);
}

function testIgnoresDoiLikeText() {
  assert.deepEqual(filesOf('DOI: 10.1016/j.jfineco.2024.103870'), []);
}

// ===== findLinkRanges: 网页链接 =====

function testDetectsUrlInChineseSentence() {
  const text = '核心接口已确认： https://xueqiu.com/statuses/search.json ，按 symbol 分页';
  assert.deepEqual(urlsOf(text), ['https://xueqiu.com/statuses/search.json']);
  assert.deepEqual(filesOf(text), []);
}

function testDetectsUrlWithQueryAndFragment() {
  assert.deepEqual(urlsOf('访问 https://example.com/path?query=1#section 查看'), [
    'https://example.com/path?query=1#section',
  ]);
}

function testTrimsUrlTrailingPunctuation() {
  assert.deepEqual(urlsOf('见 https://example.com. 结尾'), ['https://example.com']);
  assert.deepEqual(urlsOf('见 https://example.com，然后'), ['https://example.com']);
}

function testKeepsBalancedParenthesesInUrl() {
  assert.deepEqual(urlsOf('见 (https://en.wikipedia.org/wiki/Foo_(bar)) 参考'), [
    'https://en.wikipedia.org/wiki/Foo_(bar)',
  ]);
}

function testUrlAdjacentToCjkNotSwallowed() {
  assert.deepEqual(urlsOf('作者在https://www.tonycookson.com/data-and-programs提供了数据'), [
    'https://www.tonycookson.com/data-and-programs',
  ]);
  assert.deepEqual(
    filesOf('作者在https://www.tonycookson.com/data-and-programs提供了数据'),
    [],
  );
}

function testMultipleLinksStayIndependent() {
  const text = 'Link1: https://a.com/x, Link2: https://b.com/y';
  assert.deepEqual(urlsOf(text), ['https://a.com/x', 'https://b.com/y']);
}

// ===== FileLinkWrapper =====

function testFileLinkWrapperRendersPathLinks() {
  const { fileLinkComponents } = loadFileLinks();
  const wrapped = fileLinkComponents.p({ children: '见 C:\\a\\b\\c.py 文件' });
  assert.equal(wrapped.type, 'p');
  const nodes = wrapped.children;
  assert.equal(nodes[0], '见 ');
  const link = nodes[1];
  assert.equal(typeof link.type, 'function');
  assert.equal(link.props.path, 'C:\\a\\b\\c.py');
  assert.equal(nodes[2], ' 文件');
}

function testFileLinkWrapperRendersUrlLinks() {
  const { fileLinkComponents } = loadFileLinks();
  const wrapped = fileLinkComponents.p({ children: '访问 https://example.com/x 吧' });
  assert.equal(wrapped.type, 'p');
  const nodes = wrapped.children;
  assert.equal(nodes[0], '访问 ');
  const link = nodes[1];
  assert.equal(link.type, 'a');
  assert.equal(link.props.href, 'https://example.com/x');
  assert.equal(link.props.target, '_blank');
  assert.equal(link.props.rel, 'noopener noreferrer');
  assert.equal(nodes[2], ' 吧');
}

function testBlockComponentsPreserveTheirTag() {
  const { fileLinkComponents } = loadFileLinks();
  for (const tag of ['h1', 'h2', 'td', 'th', 'li', 'blockquote']) {
    const wrapped = fileLinkComponents[tag]({ children: tag });
    assert.equal(wrapped.type, tag, `expected ${tag} preserved`);
  }
}

function testFileLinkWrapperProcessesInlineCodeChildren() {
  const { fileLinkComponents } = loadFileLinks();
  const codeElement = { type: 'code', props: { children: 'D:\\x\\y\\z.py' } };
  const result = fileLinkComponents.p({ children: [codeElement] });
  assert.equal(result.type, 'p');
  const processedCode = result.children[0];
  assert.equal(processedCode.type, 'code');
  const inner = processedCode.props.children;
  assert.ok(Array.isArray(inner));
  assert.equal(typeof inner[0].type, 'function');
  assert.equal(inner[0].props.path, 'D:\\x\\y\\z.py');
}

function testFileLinkWrapperProcessesFencedCodeBlocks() {
  const { fileLinkComponents } = loadFileLinks();
  const codeElement = { type: 'code', props: { children: '见 https://example.com/x 与 D:\\a\\b\\c.py' } };
  const result = fileLinkComponents.pre({ children: [codeElement] });
  assert.equal(result.type, 'pre');
  const processedCode = result.children[0];
  assert.equal(processedCode.type, 'code');
  const inner = processedCode.props.children;
  assert.ok(Array.isArray(inner));
  assert.equal(inner[0], '见 ');
  assert.equal(inner[1].type, 'a');
  assert.equal(inner[1].props.href, 'https://example.com/x');
  assert.equal(inner[2], ' 与 ');
  assert.equal(typeof inner[3].type, 'function');
  assert.equal(inner[3].props.path, 'D:\\a\\b\\c.py');
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

function testFileOpenLinkConvertsFileProtocol() {
  const { fileLinkComponents } = loadFileLinks();
  const windows = fileLinkComponents.a({ href: 'file:///C:/a/b.txt', children: ['b.txt'] });
  assert.equal(typeof windows.type, 'function');
  assert.equal(windows.props.path, 'C:/a/b.txt');
  const posix = fileLinkComponents.a({ href: 'file:///home/user/x.txt', children: ['x.txt'] });
  assert.equal(posix.props.path, '/home/user/x.txt');
}

function testFileOpenLinkKeepsNormalLinks() {
  const { fileLinkComponents } = loadFileLinks();
  const element = fileLinkComponents.a({ href: 'https://example.com', children: ['example'] });
  assert.equal(element.type, 'a');
  assert.equal(element.props.href, 'https://example.com');
  assert.equal(element.props.target, '_blank');
}

function testFileOpenLinkTrimsSwallowedCjk() {
  const { fileLinkComponents } = loadFileLinks();
  const result = fileLinkComponents.a({
    href: 'https://a.com/data提供了数据',
    children: ['https://a.com/data提供了数据'],
  });
  assert.ok(Array.isArray(result));
  assert.equal(result[0].props.href, 'https://a.com/data');
  assert.equal(result[1], '提供了数据');
}

function testFileOpenLinkKeepsNonHttpLinks() {
  const { fileLinkComponents } = loadFileLinks();
  const element = fileLinkComponents.a({ href: '#top', children: ['top'] });
  assert.equal(element.type, 'a');
  assert.equal(element.props.href, '#top');
  assert.equal(element.props.target, undefined);
}

// ===== FilePathLink 点击跳转 =====

function clickLink(element) {
  element.props.onClick({ preventDefault: () => {} });
}

function testFilePathLinkResolvesRelativeAgainstWorkspace() {
  const { FilePathLink, opened } = loadFileLinks({
    workspace: { cwd: 'D:/proj', workspace_roots: ['D:/proj'] },
  });
  clickLink(FilePathLink({ path: 'papers/x.pdf' }));
  assert.deepEqual(opened, ['D:/proj/papers/x.pdf']);
}

function testFilePathLinkKeepsAbsolutePaths() {
  const { FilePathLink, opened } = loadFileLinks({
    workspace: { cwd: 'D:/proj', workspace_roots: ['D:/proj'] },
  });
  clickLink(FilePathLink({ path: 'C:\\abs\\x.py' }));
  clickLink(FilePathLink({ path: '/home/user/x.txt' }));
  assert.deepEqual(opened, ['C:\\abs\\x.py', '/home/user/x.txt']);
}

function testFilePathLinkSendsRawPathWithoutWorkspace() {
  const { FilePathLink, opened } = loadFileLinks(null);
  clickLink(FilePathLink({ path: 'papers/x.pdf' }));
  assert.deepEqual(opened, ['papers/x.pdf']);
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
  testMatchesRelativePaths();
  testTrimsTrailingPunctuation();
  testIgnoresPlainWords();
  testIgnoresPathsWithSpaces();
  testIgnoresVersionRangesAndParens();
  testIgnoresDoiLikeText();
  testDetectsUrlInChineseSentence();
  testDetectsUrlWithQueryAndFragment();
  testTrimsUrlTrailingPunctuation();
  testKeepsBalancedParenthesesInUrl();
  testUrlAdjacentToCjkNotSwallowed();
  testMultipleLinksStayIndependent();
  testFileLinkWrapperRendersPathLinks();
  testFileLinkWrapperRendersUrlLinks();
  testBlockComponentsPreserveTheirTag();
  testFileLinkWrapperProcessesInlineCodeChildren();
  testFileLinkWrapperProcessesFencedCodeBlocks();
  testFileOpenLinkDecodesFilePrefix();
  testFileOpenLinkConvertsFileProtocol();
  testFileOpenLinkKeepsNormalLinks();
  testFileOpenLinkTrimsSwallowedCjk();
  testFileOpenLinkKeepsNonHttpLinks();
  testFilePathLinkResolvesRelativeAgainstWorkspace();
  testFilePathLinkKeepsAbsolutePaths();
  testFilePathLinkSendsRawPathWithoutWorkspace();
  testMarkdownContentMergesFileLinkComponents();
  testFilesApiPostsToOpenEndpoint();
  testRouterRegistersFilesRoutes();
  console.log('fileLinks tests passed');
}

main();
