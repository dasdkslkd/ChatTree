const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');

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

function testProjectFolderUsesNativePickerWithoutPathInput() {
  assert.match(
    source,
    /window\.electronAPI\.selectProjectFolder\(\)/,
    'adding a project should open the desktop folder picker',
  );
  assert.match(
    source,
    /setProjectWorkspaces\(data\.projects\.map\(\(project\) => project\.workspace\)\)/,
    'project groups should come from Server project metadata',
  );
  assert.doesNotMatch(source, /projectFolderPath|请输入文件夹路径|D:\\\\Projects\\\\ChatTree/);
}

function testDeletingConversationDoesNotReloadProjectCatalog() {
  assert.match(source, /onClick=\{\(\) => setConversationDeleteTarget\(c\.id\)\}/);
  assert.doesNotMatch(
    source,
    /deleteConversation\(c\.id\);[\s\S]{0,120}loadProjects\(\)/,
    'deleting a conversation must not reload the independent project catalog',
  );
}

testProjectPickerClosesAfterSelectingProject();
testNewChatTitleUsesSelectedWorkspaceLabel();
testProjectFolderUsesNativePickerWithoutPathInput();
testDeletingConversationDoesNotReloadProjectCatalog();

console.log('PASS newChatProjectPicker');
