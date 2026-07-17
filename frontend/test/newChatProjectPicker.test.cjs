const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
const epochHelperSource = fs.readFileSync(
  path.join(__dirname, '../src/runtime/projectWorkspaceEpoch.ts'),
  'utf8',
);

function testProjectPickerClosesAfterSelectingProject() {
  assert.match(
    source,
    /const \[projectPickerOpen, setProjectPickerOpen\] = useState\(false\);/,
    'project picker open state should be controlled',
  );
  assert.match(
    source,
    /<DropdownMenu\s+open=\{projectPickerOpen\}\s+onOpenChange=\{handleProjectPickerOpenChange\}>/,
    'project picker dropdown should use controlled open state',
  );
  assert.match(
    source,
    /setSelectedProjectId\(group\.id\);[\s\S]*setProjectPickerOpen\(false\);/,
    'selecting a project should close the dropdown',
  );
}

function testNewChatTitleUsesSelectedWorkspaceLabel() {
  assert.match(
    source,
    /const newChatProjectLabel = selectedNewConversationWorkspace\.label \|\| '默认项目';/,
    'new chat title should derive the project label from selected workspace',
  );
  assert.match(
    source,
    /<h1 className="new-chat-title">\{`我们应该在 \$\{newChatProjectLabel\} 中做些什么？`\}<\/h1>/,
    'new chat title should render the selected project label',
  );
  assert.doesNotMatch(
    source,
    /<h1 className="new-chat-title">我们应该在 ChatTree 中构建什么？<\/h1>/,
    'new chat title should not be hard-coded to ChatTree',
  );
}

function testProjectFolderResolutionUsesOneEpochOwner() {
  assert.match(
    source,
    /const token = captureConnectionEpoch\(\);[\s\S]*resolveProjectWorkspaceForEpoch\(token, \{/,
    'project folder submit should capture one epoch before starting remote work',
  );
  assert.match(
    source,
    /onSuccess: \(workspace\) => \{[\s\S]*rememberProjectWorkspace\(workspace\);[\s\S]*setProjectFolderDialogMode\(null\);/,
    'workspace persistence and dialog success state should share the guarded success callback',
  );
  assert.match(
    source,
    /onFinally: \(\) => setProjectFolderSubmitting\(false\)/,
    'submitting state should be released only through the epoch helper',
  );
  assert.match(
    epochHelperSource,
    /if \(!epochSource\.isCurrent\(token\)\) return null;[\s\S]*callbacks\.onSuccess\(value\)/,
    'the helper should check token currency before success commits',
  );
  assert.match(
    epochHelperSource,
    /finally \{[\s\S]*if \(epochSource\.isCurrent\(token\)\) callbacks\.onFinally\(\);/,
    'the helper should suppress stale finally commits',
  );
}

testProjectPickerClosesAfterSelectingProject();
testNewChatTitleUsesSelectedWorkspaceLabel();
testProjectFolderResolutionUsesOneEpochOwner();

console.log('PASS newChatProjectPicker');
