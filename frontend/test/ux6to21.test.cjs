const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

function read(relativePath) {
  return fs.readFileSync(path.join(__dirname, '..', relativePath), 'utf8');
}

const mainPage = read('src/pages/MainPage.tsx');
const treeView = read('src/pages/TreeView.tsx');
const userMsg = read('src/components/transcript/items/UserMessageItem.tsx');
const assistant = read('src/components/transcript/items/AssistantAnswerItem.tsx');
const store = read('src/store/conversationStore.ts');
const renderer = read('src/components/transcript/TranscriptItemRenderer.tsx');
const planCard = read('src/components/transcript/items/PlanApprovalCard.tsx');
const toolCard = read('src/components/transcript/items/ToolApprovalCard.tsx');

function testUx5ErrorFieldRemoved() {
  // UX5：conversationStore 不再维护无人消费的 error 字段
  assert.doesNotMatch(store, /clearError/, '不应残留 clearError');
  assert.doesNotMatch(store, /set\(\{\s*error:/, '不应再向 store 写入 error 死状态');
}

function testUx6TreeViewEscClosesMenu() {
  // UX6：TreeView 右键菜单带 role="menu"（全局 Esc 豁免停止流式）且可被 Esc 关闭
  assert.match(treeView, /role="menu"/, '菜单容器应声明 role="menu"');
  assert.match(treeView, /event\.key === 'Escape'/, '菜单打开时应监听 Esc 关闭');
  assert.match(treeView, /window\.removeEventListener\('keydown', onKeyDown\)/, 'Esc 监听应随菜单关闭卸载');
}

function testUx7GlobalShortcutImeGuard() {
  // UX7：全局快捷键在输入框/IME 组合输入期间不触发
  assert.match(mainPage, /if \(isEditable \|\| event\.isComposing\) return;/, '快捷键入口应统一早退');
}

function testUx11EditDraftGuardsConversationSwitch() {
  // UX11：编辑消息草稿未完成时，切换/新建对话被阻断
  assert.match(mainPage, /const handleSelectConversation = async \(id: string\) => \{\s*if \(editTargetNodeId\)/, '切换对话应先检查编辑态');
  assert.match(mainPage, /const handleNewConversation = \(\) => \{\s*if \(editTargetNodeId\)/, '新建对话应先检查编辑态');
  assert.match(mainPage, /toast\.info\('请先完成或取消当前消息编辑'\)/, '阻断时应有提示');
}

function testUx13ImageFailurePlaceholder() {
  // UX13：图片加载失败显示占位而不是无限 spinner
  assert.match(userMsg, /failedImages/, '应有图片失败记录');
  assert.match(userMsg, /图片加载失败/, '失败缩略图与预览应显示占位文案');
  assert.match(userMsg, /setPreviewText\(target\.isImage \? '图片加载失败' : '附件内容加载失败'\)/, '预览失败应内联提示');
}

function testUx14CopyFailureNoFalseSuccess() {
  // UX14：复制失败 toast + 重抛，成功态仅在写入成功后显示
  assert.match(mainPage, /toast\.error\(getApiErrorMessage\(error, '复制失败'\)\);\s*throw error;/, 'MainPage 复制失败应 toast 并重抛');
  assert.match(userMsg, /try \{\s*await onCopy\?\.\(item, text\);\s*setCopied\(true\);/, 'UserMessage 仅在成功后置 copied');
  assert.match(userMsg, /\} catch \{\s*\/\/ onCopy 失败已由调用方 toast，这里保持未复制状态/, 'UserMessage 失败应保持未复制状态');
  assert.match(assistant, /try \{\s*await onCopy\?\.\(item, text\);\s*setCopied\(true\);/, 'AssistantAnswer 仅在成功后置 copied');
  assert.match(assistant, /\} catch \{\s*\/\/ onCopy 失败已由调用方 toast，这里保持未复制状态/, 'AssistantAnswer 失败应保持未复制状态');
}

function testUx15DeleteMessageAndBranchConfirmDialog() {
  // UX15：删除消息/分支改为应用内确认 Dialog
  assert.doesNotMatch(mainPage, /window\.confirm\('确定删除这条消息及其后续分支\?'\)/, 'MainPage 不再使用原生 confirm');
  assert.match(mainPage, /const \[deleteMessageTarget, setDeleteMessageTarget\] = useState<UserMessageItem \| null>\(null\);/);
  assert.match(mainPage, /setDeleteMessageTarget\(item\)/, '删除消息应先进入确认状态');
  assert.match(mainPage, /<Dialog open=\{!!deleteMessageTarget\}/);
  assert.match(mainPage, /void confirmDeleteMessage\(\)/);
  assert.doesNotMatch(treeView, /confirm\(`确定删除/, 'TreeView 不再使用原生 confirm');
  assert.match(treeView, /deleteBranchTarget/, '删除分支应先进入确认状态');
  assert.match(treeView, /void confirmDeleteBranch\(\)/);
}

function testUx16RenameDialogAutoFocusAndSubmitLock() {
  // UX16：重命名对话框 autoFocus + 全选 + 提交期间禁用按钮
  const renameInputs = mainPage.match(/autoFocus[\s\S]*?placeholder="请输入/g) ?? [];
  assert.equal(renameInputs.length, 2, '对话与项目重命名 Input 都应有 autoFocus');
  assert.match(mainPage, /onFocus=\{\(e\) => e\.currentTarget\.select\(\)\}/, '聚焦时全选文本');
  assert.match(mainPage, /const \[renameSubmitting, setRenameSubmitting\] = useState\(false\);/);
  assert.match(mainPage, /const \[projectRenameSubmitting, setProjectRenameSubmitting\] = useState\(false\);/);
  assert.match(mainPage, /disabled=\{renameSubmitting\}/);
  assert.match(mainPage, /disabled=\{projectRenameSubmitting\}/);
  assert.match(mainPage, /if \(renameSubmitting\) return;/, '重命名提交应防重复');
  assert.match(mainPage, /if \(projectRenameSubmitting\) return;/, '项目重命名提交应防重复');
}

function testUx20ErrorsBoundToItemId() {
  // UX20：计划/工具审批错误按 item.id 归属到具体卡片
  assert.match(mainPage, /useState<Record<string, string>>\(\{\}\)/, '错误 state 应为按 item 的映射');
  assert.match(mainPage, /setPlanError\(\{ \[item\.id\]:/, '计划错误按 item.id 写入');
  assert.match(mainPage, /setToolApprovalError\(\{ \[item\.id\]:/, '工具审批错误按 item.id 写入');
  assert.match(mainPage, /planErrorByItem=\{planError\}/);
  assert.match(mainPage, /toolApprovalErrorByItem=\{toolApprovalError\}/);
  assert.match(renderer, /planErrorByItem\[item\.id\]|planErrorByItem=\{planErrorByItem\}/, '渲染链透传按 item 的错误映射');
  assert.match(planCard, /planErrorByItem\[item\.id\]/, '计划卡只显示本卡错误');
  assert.match(toolCard, /toolApprovalErrorByItem\[item\.id\]/, '工具卡只显示本卡错误');
}

function testUx21ProjectLoadFailureKeepsOldData() {
  // UX21：项目配置加载失败保留旧值并 toast
  assert.doesNotMatch(mainPage, /setProjectConfigs\(\{\}\)/, '失败不应清空项目配置');
  assert.doesNotMatch(mainPage, /setProjectWorkspaces\(\[\]\)/, '失败不应清空工作区列表');
  assert.match(mainPage, /toast\.error\(getApiErrorMessage\(error, '加载项目配置失败'\)\);/, '失败应 toast 提示');
}

testUx5ErrorFieldRemoved();
testUx6TreeViewEscClosesMenu();
testUx7GlobalShortcutImeGuard();
testUx11EditDraftGuardsConversationSwitch();
testUx13ImageFailurePlaceholder();
testUx14CopyFailureNoFalseSuccess();
testUx15DeleteMessageAndBranchConfirmDialog();
testUx16RenameDialogAutoFocusAndSubmitLock();
testUx20ErrorsBoundToItemId();
testUx21ProjectLoadFailureKeepsOldData();
console.log('ux6-21 tests passed');
