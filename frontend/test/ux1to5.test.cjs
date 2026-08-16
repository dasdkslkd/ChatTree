const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const chatInput = fs.readFileSync(path.join(__dirname, '../src/components/ChatInput.tsx'), 'utf8');
const mainPage = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');

function testChatInputDedupAndDraftRetention() {
  // UX1：防重复提交 —— isSending 门闩在发送入口最前方
  assert.match(chatInput, /const \[isSending, setIsSending\] = useState\(false\);/);
  assert.match(
    chatInput,
    /const handleSend = async \(\) => \{\s*if \(isSending \|\| !value\.trim\(\)/,
    '发送入口应先拦截重复提交',
  );
  assert.match(
    chatInput,
    /setIsSending\(true\);\s*try \{[\s\S]*?await onSend\(/,
    '发送期间应置位 isSending 并进入 try 块',
  );

  // UX2：发送失败保留输入 —— 清空输入只在成功后执行
  assert.match(
    chatInput,
    /await onSend\([\s\S]*?\);\s*setValue\(''\);/,
    '仅在 onSend 成功后清空输入，失败保留草稿',
  );
  assert.match(
    chatInput,
    /\} catch \(error\) \{\s*toast\.error\(getApiErrorMessage\(error, '发送失败'\)\);/,
    '发送失败应 toast 提示',
  );
  assert.match(
    chatInput,
    /\} finally \{\s*setIsSending\(false\);/,
    '无论成败都应复位 isSending',
  );

  // 发送按钮在发送中禁用并显示 loading 图标
  assert.match(chatInput, /const sendDisabled = !value\.trim\(\) \|\| \(disabled && !isStreaming\) \|\| isSending;/);
  assert.match(
    chatInput,
    /\{isSending \? \(\s*<Loader2 className="h-4 w-4 animate-spin" \/>\s*\) :/,
    '发送中应显示 loading 图标',
  );
}

function testMainPageDeleteConversationTwoStepConfirm() {
  // UX3：删除对话两步确认 —— 不再直接调用 deleteConversation
  assert.match(mainPage, /const \[conversationDeleteTarget, setConversationDeleteTarget\] = useState<string \| null>\(null\);/);
  assert.doesNotMatch(
    mainPage,
    /DropdownMenuItem onClick=\{\(\) => deleteConversation\(c\.id\)\}/,
    '删除项不应直接触发删除',
  );
  assert.match(
    mainPage,
    /DropdownMenuItem onClick=\{\(\) => setConversationDeleteTarget\(c\.id\)\}/,
    '删除项应先进入确认状态',
  );
  assert.match(mainPage, /<Dialog open=\{!!conversationDeleteTarget\}/);
  assert.match(mainPage, /confirmConversationDelete\(\)/);
  const confirm = mainPage.match(/const confirmConversationDelete = async \(\) => \{[\s\S]*?\n  \};/)?.[0] ?? '';
  assert.match(confirm, /setConversationDeleteTarget\(null\);/, '确认后先关闭确认框');
  assert.match(confirm, /await deleteConversation\(id\);/);
  assert.match(confirm, /toast\.error\(getApiErrorMessage\(error, '删除对话失败'\)\);/);
}

function testMainPageRenameFailureKeepsDialogOpen() {
  // UX4：重命名失败不关闭对话框 —— catch 内 return 提前退出
  assert.match(mainPage, /const \[renameDialogOpen, setRenameDialogOpen\] = useState\(false\);/);
  const rename = mainPage.match(/const handleRenameConfirm = async \(\) => \{[\s\S]*?\n  \};/)?.[0] ?? '';
  assert.match(rename, /await updateConversationTitle\(renameConversationId, renameTitle\.trim\(\)\);/);
  assert.match(
    rename,
    /\} catch \(error\) \{\s*toast\.error\(getApiErrorMessage\(error, '重命名对话失败'\)\);\s*return;\s*\}/,
    '失败时应 toast 并提前返回，保持对话框打开',
  );
  assert.match(
    rename,
    /setRenameDialogOpen\(false\);/,
    '关闭对话框的代码只能出现在成功路径之后',
  );
  assert.ok(
    rename.indexOf('setRenameDialogOpen(false);') > rename.indexOf('return;'),
    '关闭对话框必须位于失败提前返回之后',
  );
}

testChatInputDedupAndDraftRetention();
testMainPageDeleteConversationTwoStepConfirm();
testMainPageRenameFailureKeepsDialogOpen();
console.log('ux1-5 tests passed');