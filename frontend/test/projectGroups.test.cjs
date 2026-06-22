const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

require.extensions['.ts'] = function loadTs(module, filename) {
  const source = fs.readFileSync(filename, 'utf8');
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      esModuleInterop: true,
      importsNotUsedAsValues: ts.ImportsNotUsedAsValues.Remove,
    },
    fileName: filename,
  }).outputText;
  module._compile(output, filename);
};

const {
  DEFAULT_VISIBLE_HISTORY_COUNT,
  groupConversationsByProject,
  getVisibleProjectConversations,
  getWorkspaceForNewConversation,
} = require(path.join(__dirname, '../src/utils/projectGroups.ts'));

function conv(id, title, updatedAt, cwd, label) {
  return {
    id,
    title,
    created_at: updatedAt,
    updated_at: updatedAt,
    model: '',
    model_id: '',
    provider_id: '',
    current_node_id: 'root',
    total_tokens: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
    workspace: cwd ? {
      cwd,
      workspace_roots: [cwd],
      protected_paths: ['.git'],
      label,
    } : undefined,
  };
}

function testGroupsByWorkspaceAndSortsByRecentConversation() {
  const groups = groupConversationsByProject([
    conv('old-a', 'Old A', 1, 'D:/Projects/A', 'A'),
    conv('new-b', 'New B', 10, 'D:/Projects/B', 'B'),
    conv('new-a', 'New A', 20, 'D:/Projects/A', 'A'),
  ], {
    defaultWorkspace: {
      cwd: 'D:/Default',
      workspace_roots: ['D:/Default'],
      protected_paths: ['.git'],
      label: 'Default',
    },
  });

  assert.equal(groups.length, 2);
  assert.equal(groups[0].path, 'D:/Projects/A');
  assert.deepEqual(groups[0].conversations.map((item) => item.id), ['new-a', 'old-a']);
  assert.equal(groups[1].path, 'D:/Projects/B');
}

function testProjectOrderOverridesRecentConversationSort() {
  const groups = groupConversationsByProject([
    conv('a', 'A', 30, 'D:/Projects/A', 'A'),
    conv('b', 'B', 20, 'D:/Projects/B', 'B'),
    conv('c', 'C', 10, 'D:/Projects/C', 'C'),
  ], {
    projectOrder: [
      encodeURIComponent('D:/Projects/C'),
      encodeURIComponent('D:/Projects/A'),
    ],
  });

  assert.deepEqual(groups.map((group) => group.path), [
    'D:/Projects/C',
    'D:/Projects/A',
    'D:/Projects/B',
  ]);
}

function testUnknownProjectsSinkAfterSavedOrder() {
  const groups = groupConversationsByProject([
    conv('recent-new', 'New', 100, 'D:/Projects/New', 'New'),
    conv('ordered', 'Ordered', 1, 'D:/Projects/Ordered', 'Ordered'),
  ], {
    projectOrder: [encodeURIComponent('D:/Projects/Ordered')],
  });

  assert.deepEqual(groups.map((group) => group.path), [
    'D:/Projects/Ordered',
    'D:/Projects/New',
  ]);
}

function testDefaultVisibleHistoryCountAndExpansion() {
  const conversations = Array.from({ length: 7 }, (_, index) =>
    conv(`c-${index}`, `Title ${index}`, 100 - index, 'D:/Projects/A', 'A')
  );
  const [group] = groupConversationsByProject(conversations, {
    defaultWorkspace: conversations[0].workspace,
  });

  const collapsed = getVisibleProjectConversations(group);
  assert.equal(DEFAULT_VISIBLE_HISTORY_COUNT, 5);
  assert.equal(collapsed.items.length, 5);
  assert.equal(collapsed.hiddenCount, 2);
  assert.equal(collapsed.canExpand, true);
  assert.equal(collapsed.canCollapse, false);

  const expanded = getVisibleProjectConversations({ ...group, isHistoryExpanded: true });
  assert.equal(expanded.items.length, 7);
  assert.equal(expanded.hiddenCount, 0);
  assert.equal(expanded.canExpand, false);
  assert.equal(expanded.canCollapse, true);
}

function testSearchKeepsGroupsAndStillLimitsToFive() {
  const conversations = Array.from({ length: 6 }, (_, index) =>
    conv(`match-${index}`, `needle ${index}`, 100 - index, 'D:/Projects/A', 'A')
  ).concat([
    conv('miss', 'other', 1, 'D:/Projects/B', 'B'),
  ]);

  const groups = groupConversationsByProject(conversations, {
    defaultWorkspace: conversations[0].workspace,
    searchQuery: 'needle',
  });
  assert.equal(groups.length, 1);
  assert.equal(groups[0].conversations.length, 6);

  const visible = getVisibleProjectConversations(groups[0]);
  assert.equal(visible.items.length, 5);
  assert.equal(visible.hiddenCount, 1);
}

function testNewConversationWorkspaceFallsBackInPriorityOrder() {
  const defaultWorkspace = {
    cwd: 'D:/Default',
    workspace_roots: ['D:/Default'],
    protected_paths: ['.git'],
    label: 'Default',
  };
  const groups = groupConversationsByProject([
    conv('recent', 'Recent', 20, 'D:/Projects/Recent', 'Recent'),
    conv('older', 'Older', 1, 'D:/Projects/Older', 'Older'),
  ], { defaultWorkspace });

  assert.equal(getWorkspaceForNewConversation(groups, groups[1].id, defaultWorkspace).cwd, 'D:/Projects/Older');
  assert.equal(getWorkspaceForNewConversation(groups, 'missing', defaultWorkspace).cwd, 'D:/Projects/Recent');
  assert.equal(getWorkspaceForNewConversation([], null, defaultWorkspace).cwd, 'D:/Default');
}

function testExtraWorkspacesShowAsEmptyProjectGroups() {
  const groups = groupConversationsByProject([
    conv('existing', 'Existing', 20, 'D:/Projects/Existing', 'Existing'),
  ], {
    extraWorkspaces: [{
      cwd: 'D:/Projects/New',
      workspace_roots: ['D:/Projects/New'],
      protected_paths: ['.git'],
      label: 'New',
    }],
  });

  assert.equal(groups.length, 2);
  const extra = groups.find((group) => group.path === 'D:/Projects/New');
  assert.ok(extra);
  assert.equal(extra.label, 'New');
  assert.deepEqual(extra.conversations, []);
  assert.equal(getWorkspaceForNewConversation(groups, extra.id).cwd, 'D:/Projects/New');
}

testGroupsByWorkspaceAndSortsByRecentConversation();
testProjectOrderOverridesRecentConversationSort();
testUnknownProjectsSinkAfterSavedOrder();
testDefaultVisibleHistoryCountAndExpansion();
testSearchKeepsGroupsAndStillLimitsToFive();
testNewConversationWorkspaceFallsBackInPriorityOrder();
testExtraWorkspacesShowAsEmptyProjectGroups();

console.log('PASS projectGroups');
