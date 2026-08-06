const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const Module = require('module');

const rendererPath = path.join(__dirname, '../src/components/transcript/items/ToolCallRenderer.tsx');
const formattingPath = path.join(__dirname, '../src/components/transcript/items/toolCallFormatting.ts');
const timelinePath = path.join(__dirname, '../src/components/transcript/items/AssistantProcessTimeline.tsx');
const approvalPath = path.join(__dirname, '../src/components/transcript/items/ToolApprovalCard.tsx');
const itemPath = path.join(__dirname, '../src/components/transcript/items/AssistantProcessItem.tsx');
const cssPath = path.join(__dirname, '../src/App.css');

function readSource(relativePath) {
  return fs.readFileSync(path.join(__dirname, '..', relativePath), 'utf8');
}

function ruleBody(selector) {
  const css = fs.readFileSync(cssPath, 'utf8');
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`, 'm'));
  return match?.[1] || '';
}

// ===== Mock modules for transpiled ToolCallRenderer =====
function createMockReact() {
  const React = {
    useState: (initial) => [initial, () => {}],
    useMemo: (factory) => factory(),
    createElement: (type, props, ...children) => ({ type, props, children }),
  };
  return React;
}

function createMockLucide() {
  const icons = [
    'Check', 'ChevronRight', 'ClipboardList', 'Copy', 'FileText', 'FilePlus', 'FileSearch',
    'Globe', 'Pencil', 'Search', 'Terminal', 'Wrench', 'X',
  ];
  const mock = {};
  for (const name of icons) {
    mock[name] = { displayName: name };
  }
  mock.LucideIcon = {};
  return mock;
}

function loadToolCallFormatting() {
  const source = fs.readFileSync(formattingPath, 'utf8');
  const transpiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.React,
      esModuleInterop: true,
      importsNotUsedAsValues: ts.ImportsNotUsedAsValues.Remove,
    },
    fileName: formattingPath,
  }).outputText;

  const moduleObj = { exports: {} };
  const requireMock = (name) => {
    if (name === 'react') return createMockReact();
    if (name === 'lucide-react') return createMockLucide();
    if (name === '@/lib/utils') return { cn: (...classes) => classes.filter(Boolean).join(' ') };
    if (name === './AssistantProcessTimeline') return {};
    if (name === '../../MarkdownContent') return function MockMarkdownContent({ children }) { return { type: 'MockMarkdownContent', props: { children } }; };
    throw new Error(`unexpected require: ${name}`);
  };
  const fn = new Function('module', 'exports', 'require', transpiled);
  fn(moduleObj, moduleObj.exports, requireMock);
  return moduleObj.exports;
}

// ===== Tests =====

function testRegistryContainsAllExpectedTools() {
  const source = readSource('src/components/transcript/items/ToolCallRenderer.tsx');
  const expectedTools = ['shell', 'grep', 'glob', 'read', 'edit', 'web', 'enter_plan_mode', 'exit_plan_mode'];
  for (const tool of expectedTools) {
    assert.match(source, new RegExp(`${tool}\\s*:`), `TOOL_SPECS should register tool: ${tool}`);
  }
  assert.match(source, /TOOL_SPECS\[name\]\s*\|\|\s*defaultSpec\(\)/, 'should fall back to defaultSpec for unknown tools');
}

function testSummarizeForEnterPlanMode() {
  const { summarizeToolCall } = loadToolCallFormatting();
  const args = JSON.stringify({ permission_mode: 'plan' });
  const summary = summarizeToolCall('enter_plan_mode', args, '', 'done');
  assert.equal(summary, '计划模式 · plan');
}

function testSummarizeForEnterPlanModeRunning() {
  const { summarizeToolCall } = loadToolCallFormatting();
  const args = JSON.stringify({ permission_mode: 'plan' });
  const summary = summarizeToolCall('enter_plan_mode', args, '', 'running');
  assert.equal(summary, '进入计划模式（plan）');
}

function testSummarizeForExitPlanMode() {
  const { summarizeToolCall } = loadToolCallFormatting();
  const args = JSON.stringify({ plan: '1. 修改后端\n2. 运行测试' });
  const summary = summarizeToolCall('exit_plan_mode', args, '', 'done');
  assert.equal(summary, '1. 修改后端');
}

function testSummarizeForExitPlanModeEmpty() {
  const { summarizeToolCall } = loadToolCallFormatting();
  const summary = summarizeToolCall('exit_plan_mode', '', '', 'running');
  assert.equal(summary, '提交计划中...');
}

function testSummarizeForShell() {
  const { summarizeToolCall } = loadToolCallFormatting();
  const args = JSON.stringify({ command: 'npm test', cwd: './src' });
  const result = JSON.stringify({ exit_code: 0, stdout: 'ok', stderr: '' });
  assert.equal(summarizeToolCall('shell', args, result, 'done'), 'npm test');
}

function testSummarizeForShellTruncatesLongCommand() {
  const { summarizeToolCall } = loadToolCallFormatting();
  const longCommand = 'git commit -m "' + 'x'.repeat(120) + '"';
  const args = JSON.stringify({ command: longCommand });
  const summary = summarizeToolCall('shell', args, '', 'running');
  assert.ok(summary.length <= 80, `summary should be truncated to 80 chars, got ${summary.length}`);
  assert.ok(summary.endsWith('…'), 'truncated summary should end with ellipsis');
}

function testSummarizeForGrepContent() {
  const { summarizeToolCall } = loadToolCallFormatting();
  const args = JSON.stringify({ pattern: 'function\\s+\\w+', path: './src', output: 'content' });
  const result = JSON.stringify({ count: 5, output: 'content', matches: [] });
  const summary = summarizeToolCall('grep', args, result, 'done');
  assert.equal(summary, 'function\\s+\\w+ @ ./src · 5 处匹配');
}

function testSummarizeForGrepFiles() {
  const { summarizeToolCall } = loadToolCallFormatting();
  const args = JSON.stringify({ pattern: 'TODO', path: '.', output: 'files' });
  const result = JSON.stringify({ count: 3, files: [], output: 'files' });
  const summary = summarizeToolCall('grep', args, result, 'done');
  assert.equal(summary, 'TODO · 3 个文件');
}

function testSummarizeForGlob() {
  const { summarizeToolCall } = loadToolCallFormatting();
  const args = JSON.stringify({ patterns: ['**/*.ts'], path: './src' });
  const result = JSON.stringify({ count: 12, files: [] });
  const summary = summarizeToolCall('glob', args, result, 'done');
  assert.equal(summary, '**/*.ts @ ./src · 12 个文件');
}

function testSummarizeForReadWithLineRange() {
  const { summarizeToolCall } = loadToolCallFormatting();
  const args = JSON.stringify({ path: 'src/foo.ts', start_line: 10, line_count: 50 });
  const summary = summarizeToolCall('read', args, '', 'running');
  assert.equal(summary, 'src/foo.ts L10-59');
}

function testSummarizeForReadWithTargets() {
  const { summarizeToolCall } = loadToolCallFormatting();
  const args = JSON.stringify({ targets: [{ path: 'src/bar.ts', start_line: 1, line_count: 5 }] });
  const summary = summarizeToolCall('read', args, '', 'done');
  assert.equal(summary, 'src/bar.ts L1-5');
}

function testSummarizeForReadUsesActualResultRange() {
  const { summarizeToolCall } = loadToolCallFormatting();
  const args = JSON.stringify({ path: 'src/bar.ts' });
  const result = JSON.stringify({
    files: [{ path: 'src/bar.ts', start_line: 20, line_count: 37, content: '...' }],
  });
  const summary = summarizeToolCall('read', args, result, 'done');
  assert.equal(summary, 'src/bar.ts L20-56');
}

function testSummarizeForBatchReadUsesResultBlocksInOrder() {
  const { summarizeToolCall } = loadToolCallFormatting();
  const args = JSON.stringify({
    path: 'ignored-default.ts',
    targets: [
      { path: 'src/a.ts', start_line: 1, line_count: 20 },
      { path: 'src/b.ts', start_line: 30, line_count: 10 },
    ],
  });
  const result = JSON.stringify({
    files: [
      { path: 'src/a.ts', start_line: 1, line_count: 20, content: 'a' },
      { path: 'src/b.ts', start_line: 30, line_count: 10, content: 'b' },
    ],
  });
  const summary = summarizeToolCall('read', args, result, 'done');
  assert.equal(summary, '2 个文件 · 30 行');
  assert.equal(summarizeToolCall('read', args, '', 'running'), '2 个文件 · 30 行');
}

function testSummarizeForChunkedReadDistinguishesSegments() {
  const { summarizeToolCall } = loadToolCallFormatting();
  const result = JSON.stringify({
    files: [
      { path: 'src/a.ts', start_line: 1, line_count: 20, content: 'a' },
      { path: 'src/a.ts', start_line: 21, line_count: 15, content: 'b' },
    ],
  });
  const summary = summarizeToolCall('read', JSON.stringify({ targets: [] }), result, 'done');
  assert.equal(summary, 'src/a.ts · 2 段 · 35 行');
}

function testSummarizeForWebFetch() {
  const { summarizeToolCall } = loadToolCallFormatting();
  const args = JSON.stringify({ action: 'fetch', url: 'https://example.com/page' });
  const summary = summarizeToolCall('web', args, '', 'running');
  assert.equal(summary, 'https://example.com/page');
}

function testSummarizeForWebSearch() {
  const { summarizeToolCall } = loadToolCallFormatting();
  const args = JSON.stringify({ action: 'search', query: 'how to test react components' });
  const result = JSON.stringify({ count: 7, results: [] });
  const summary = summarizeToolCall('web', args, result, 'done');
  assert.equal(summary, 'how to test react components · 7 项');
}

function testSummarizeForUnknownToolFallsBackToGeneric() {
  const { summarizeToolCall } = loadToolCallFormatting();
  const args = JSON.stringify({ command: 'do something' });
  const summary = summarizeToolCall('unknown_tool', args, '', 'done');
  assert.equal(summary, 'do something');
}

function testSummarizeForUnknownToolWithEmptyArgs() {
  const { summarizeToolCall } = loadToolCallFormatting();
  const summary = summarizeToolCall('mystery_tool', '', '', 'running');
  assert.equal(summary, '执行中...');
}

function testSummarizeHandlesErrorResult() {
  const { summarizeToolCall } = loadToolCallFormatting();
  const args = JSON.stringify({ command: 'bad-command' });
  const result = JSON.stringify({ error: { type: 'not_found', message: 'command not found' } });
  const summary = summarizeToolCall('unknown_tool', args, result, 'error');
  assert.equal(summary, 'command not found');
}

function testSummarizeForShellWithErrorShowsCommand() {
  const { summarizeToolCall } = loadToolCallFormatting();
  const args = JSON.stringify({ command: 'bad-command' });
  const result = JSON.stringify({ error: { type: 'not_found', message: 'command not found' } });
  const summary = summarizeToolCall('shell', args, result, 'error');
  assert.equal(summary, 'bad-command');
}

function testSummarizeHandlesInvalidJson() {
  const { summarizeToolCall } = loadToolCallFormatting();
  const summary = summarizeToolCall('shell', 'not valid json', 'also not json', 'done');
  assert.equal(summary, 'shell');
}

function testToolCallCardStructure() {
  const source = readSource('src/components/transcript/items/ToolCallRenderer.tsx');
  assert.match(source, /export function ToolCallCard\(/);
  assert.match(source, /className=\{cn\('tool-call'/);
  assert.match(source, /aria-expanded=\{expanded\}/);
  assert.match(source, /className="tc-header"/);
  assert.match(source, /className="tc-icon"/);
  assert.match(source, /className="tc-name"/);
  assert.match(source, /className="tc-summary"/);
  assert.match(source, /className="tc-status"/);
  assert.match(source, /className="tc-chevron"/);
  assert.match(source, /className="tc-body"/);
  assert.match(source, /className="tc-body-inner"/);
}

function testToolCallPreviewExported() {
  const source = readSource('src/components/transcript/items/ToolCallRenderer.tsx');
  assert.match(source, /export function ToolCallPreview\(/);
  assert.match(source, /toolName:\s*string/);
  assert.match(source, /argsText:\s*string/);
  assert.match(source, /outputText\?:\s*string \| null/);
}

function testCopyButtonIsRenderedWithClipboardWrite() {
  const source = readSource('src/components/transcript/items/ToolCallRenderer.tsx');
  assert.match(source, /function CopyButton\(/);
  assert.match(source, /navigator\.clipboard\.writeText\(text\)/);
  assert.match(source, /setTimeout\(\(\) => setCopied\(false\), 2000\)/);
}

function testTimelineUsesNewCard() {
  const source = readSource('src/components/transcript/items/AssistantProcessTimeline.tsx');
  assert.match(source, /import \{ ToolCallCard \} from '\.\/ToolCallRenderer'/);
  assert.match(source, /return <ToolCallCard item=\{items\[0\]\} \/>/);
  assert.match(source, /<ToolCallCard key=\{item\.key\} item=\{item\} \/>/);
  assert.doesNotMatch(source, /function GenericToolCallCard/);
  assert.doesNotMatch(source, /<pre className="tc-cmd/);
}

function testAssistantProcessItemUsesSummarizeToolCall() {
  const source = readSource('src/components/transcript/items/AssistantProcessItem.tsx');
  assert.match(source, /import \{ [^}]*summarizeToolCall[^}]*\} from '\.\/toolCallFormatting'/);
  assert.match(source, /summarizeToolCall\(block\.tool_name \|\| 'tool', argsText, outputText, status\)/);
  assert.doesNotMatch(source, /function compactToolSummary/);
}

function testToolBlockToRenderItemMarksResultErrorAsFailed() {
  const source = readSource('src/components/transcript/items/AssistantProcessItem.tsx');
  assert.match(source, /getErrorMessage\(asObject\(tryParseJSON\(outputText\)\)\)/);
  assert.match(source, /block\.status === 'error' \|\| getErrorMessage\(/);
}

function testToolApprovalCardUsesPreview() {
  const source = readSource('src/components/transcript/items/ToolApprovalCard.tsx');
  assert.match(source, /import \{ ToolCallPreview \} from '\.\/ToolCallRenderer'/);
  assert.match(source, /<ToolCallPreview\s+toolName=\{item\.tool_name\}/);
  assert.doesNotMatch(source, /<pre className="tc-cmd/);
  assert.doesNotMatch(source, /<pre className="tc-output/);
}

function testCssDefinesAllRequiredClasses() {
  const css = readSource('src/App.css');
  const requiredClasses = [
    '.tc-icon', '.tc-body', '.tc-body-inner', '.tc-detail',
    '.tc-copy', '.tc-copy-subtle', '.tc-pre', '.tc-pre-cmd', '.tc-pre-error',
    '.tc-meta', '.tc-meta-item', '.tc-meta-label', '.tc-meta-value',
    '.tc-meta-error', '.tc-meta-muted',
    '.tc-empty', '.tc-truncated',
    '.tc-file-list', '.tc-file-item',
    '.tc-match-list', '.tc-match-item', '.tc-match-context', '.tc-match-path', '.tc-match-line', '.tc-match-text',
    '.tc-result-list', '.tc-result-item', '.tc-result-title', '.tc-result-url', '.tc-result-snippet',
    '.tc-file-block',
  ];
  for (const cls of requiredClasses) {
    assert.ok(css.includes(cls), `CSS should define rule for ${cls}`);
  }
}

function testCssUsesMountAnimationForExpand() {
  const body = ruleBody('.tc-body');
  assert.match(body, /animation:\s*tc-fade-in/);
  const css = readSource('src/App.css');
  const keyframes = css.match(/@keyframes\s+tc-fade-in\s*\{[\s\S]*?\n\}/);
  assert.ok(keyframes, 'CSS should define @keyframes tc-fade-in');
  assert.match(keyframes[0], /opacity:\s*0/);
  assert.match(keyframes[0], /opacity:\s*1/);
}

function testBodyIsConditionallyRendered() {
  const source = readSource('src/components/transcript/items/ToolCallRenderer.tsx');
  assert.match(source, /\{expanded && \(/);
  assert.match(source, /<div className="tc-body">/);
  // 折叠态下 tc-body 不应常驻 DOM
  const cardBlock = source.match(/export function ToolCallCard\([\s\S]*?\n\}/);
  assert.ok(cardBlock, 'ToolCallCard should be exported');
  assert.match(cardBlock[0], /\{expanded && \(\s*<div className="tc-body">/);
}

function testCssHasResponsiveBreakpoint() {
  const css = readSource('src/App.css');
  assert.match(css, /@media \(max-width: 640px\)/);
}

function testCssHasScrollableListsForLargeResults() {
  const css = readSource('src/App.css');
  const listSection = css.match(/\.tc-file-list,[\s\S]*?\}/);
  assert.ok(listSection, 'CSS should define .tc-file-list rule');
  assert.match(listSection[0], /overflow-y:\s*auto/);
  assert.match(listSection[0], /max-height:\s*260px/);
}

// ===== Run all tests =====

function run() {
  testRegistryContainsAllExpectedTools();
  testSummarizeForEnterPlanMode();
  testSummarizeForEnterPlanModeRunning();
  testSummarizeForExitPlanMode();
  testSummarizeForExitPlanModeEmpty();
  testSummarizeForShell();
  testSummarizeForShellTruncatesLongCommand();
  testSummarizeForGrepContent();
  testSummarizeForGrepFiles();
  testSummarizeForGlob();
  testSummarizeForReadWithLineRange();
  testSummarizeForReadWithTargets();
  testSummarizeForReadUsesActualResultRange();
  testSummarizeForBatchReadUsesResultBlocksInOrder();
  testSummarizeForChunkedReadDistinguishesSegments();
  testSummarizeForWebFetch();
  testSummarizeForWebSearch();
  testSummarizeForUnknownToolFallsBackToGeneric();
  testSummarizeForUnknownToolWithEmptyArgs();
  testSummarizeHandlesErrorResult();
  testSummarizeForShellWithErrorShowsCommand();
  testSummarizeHandlesInvalidJson();
  testToolCallCardStructure();
  testToolCallPreviewExported();
  testCopyButtonIsRenderedWithClipboardWrite();
  testTimelineUsesNewCard();
  testAssistantProcessItemUsesSummarizeToolCall();
  testToolBlockToRenderItemMarksResultErrorAsFailed();
  testToolApprovalCardUsesPreview();
  testCssDefinesAllRequiredClasses();
  testCssUsesMountAnimationForExpand();
  testBodyIsConditionallyRendered();
  testCssHasResponsiveBreakpoint();
  testCssHasScrollableListsForLargeResults();
  console.log('tool call rendering tests passed');
}

run();
