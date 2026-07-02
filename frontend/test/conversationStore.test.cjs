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

const conversationApiModule = path.join(__dirname, '../src/api/conversation.ts');
const messageApiModule = path.join(__dirname, '../src/api/message.ts');
const modelStoreModule = path.join(__dirname, '../src/store/modelStore.ts');
const storeModule = path.join(__dirname, '../src/store/conversationStore.ts');

const storage = new Map();
global.localStorage = {
  getItem: (key) => storage.get(key) ?? null,
  setItem: (key, value) => storage.set(key, String(value)),
  removeItem: (key) => storage.delete(key),
};

let historyResponse = [
  { id: 'msg-1', role: 'user', content: '保留的问题', node_id: 'node-1' },
  { id: 'msg-2', role: 'assistant', content: '保留的回答', node_id: 'node-1' },
];
let branchesResponse = { 'node-1': [] };
let switchNodeCalls = [];
let updateMultiAgentModeCalls = [];
let deleteNodeCalls = [];
let deleteNodeHandler = async () => ({
  deleted_node_id: 'node-2',
  new_current_node_id: 'node-1',
  parent_node_id: 'node-1',
});

const refreshedTree = {
  root_node_id: 'root',
  current_node_id: 'node-1',
  nodes: [
    {
      id: 'root',
      parent_id: null,
      children_ids: ['node-1'],
      user_content: '',
      assistant_content: '',
      model_id: null,
      timestamp: 1,
      is_current: false,
      is_root: true,
    },
    {
      id: 'node-1',
      parent_id: 'root',
      children_ids: [],
      user_content: '保留的问题',
      assistant_content: '保留的回答',
      model_id: 'model-a',
      timestamp: 2,
      is_current: true,
      is_root: false,
    },
  ],
};

let getTreeCalls = 0;

require.cache[require.resolve(conversationApiModule)] = {
  id: conversationApiModule,
  filename: conversationApiModule,
  loaded: true,
  exports: {
    conversationApi: {
      switchNode: async (conversationId, nodeId) => {
        switchNodeCalls.push({ conversationId, nodeId });
        return { current_node_id: nodeId };
      },
      updateMultiAgentMode: async (conversationId, mode) => {
        updateMultiAgentModeCalls.push({ conversationId, mode });
      },
      deleteNode: async (...args) => {
        deleteNodeCalls.push(args);
        return deleteNodeHandler(...args);
      },
      getBranches: async () => branchesResponse,
      getTree: async () => {
        getTreeCalls += 1;
        return refreshedTree;
      },
    },
  },
};

require.cache[require.resolve(messageApiModule)] = {
  id: messageApiModule,
  filename: messageApiModule,
  loaded: true,
  exports: {
    messageApi: {
      getHistory: async () => historyResponse,
    },
  },
};

require.cache[require.resolve(modelStoreModule)] = {
  id: modelStoreModule,
  filename: modelStoreModule,
  loaded: true,
  exports: {
    useModelStore: {
      getState: () => ({
        resetToDefault: async () => {},
        syncFromConversation: async () => {},
      }),
    },
  },
};

const { useConversationStore } = require(storeModule);

async function testDeleteNodeRefreshesTreeData() {
  getTreeCalls = 0;
  deleteNodeCalls = [];
  deleteNodeHandler = async () => ({
    deleted_node_id: 'node-2',
    new_current_node_id: 'node-1',
    parent_node_id: 'node-1',
  });
  const currentConversation = {
    id: 'conv-1',
    title: '树测试',
    created_at: 1,
    updated_at: 1,
    model: '',
    model_id: '',
    provider_id: '',
    current_node_id: 'node-2',
    total_tokens: {},
  };

  useConversationStore.setState({
    conversations: [currentConversation],
    currentConversation,
    messages: [{ id: 'old', role: 'user', content: '待删除', node_id: 'node-2' }],
    branches: {},
    treeData: {
      root_node_id: 'root',
      current_node_id: 'node-2',
      nodes: [...refreshedTree.nodes, {
        id: 'node-2',
        parent_id: 'node-1',
        children_ids: [],
        user_content: '待删除',
        assistant_content: '待删除回答',
        model_id: 'model-a',
        timestamp: 3,
        is_current: true,
        is_root: false,
      }],
    },
    currentNodeId: 'node-2',
    loading: false,
    error: null,
  });

  await useConversationStore.getState().deleteNode('node-2');

  const state = useConversationStore.getState();
  assert.deepEqual(deleteNodeCalls, [['conv-1', 'node-2']]);
  assert.equal(getTreeCalls, 1);
  assert.equal(state.currentNodeId, 'node-1');
  assert.equal(state.currentConversation.current_node_id, 'node-1');
  assert.equal(state.treeData.current_node_id, 'node-1');
  assert.deepEqual(state.treeData.nodes.map((node) => node.id), ['root', 'node-1']);
  assert.deepEqual(state.messages.map((message) => message.node_id), ['node-1', 'node-1']);
}

async function testDeleteNodeRetriesForceWhenActiveRunBlocksDeletion() {
  getTreeCalls = 0;
  deleteNodeCalls = [];
  let attempts = 0;
  deleteNodeHandler = async () => {
    attempts += 1;
    if (attempts === 1) {
      const error = new Error('Conflict');
      error.response = {
        status: 409,
        data: { detail: { message: 'active run', active_run_ids: ['run-1'] } },
      };
      throw error;
    }
    return {
      deleted_node_id: 'node-2',
      new_current_node_id: 'node-1',
      parent_node_id: 'node-1',
    };
  };
  const currentConversation = {
    id: 'conv-1',
    title: '强制删除测试',
    created_at: 1,
    updated_at: 1,
    model: '',
    model_id: '',
    provider_id: '',
    current_node_id: 'node-2',
    total_tokens: {},
  };

  useConversationStore.setState({
    conversations: [currentConversation],
    currentConversation,
    messages: [{ id: 'old', role: 'user', content: '待删除', node_id: 'node-2' }],
    branches: {},
    treeData: null,
    currentNodeId: 'node-2',
    loading: false,
    error: null,
  });

  await useConversationStore.getState().deleteNode('node-2');

  const state = useConversationStore.getState();
  assert.deepEqual(deleteNodeCalls, [
    ['conv-1', 'node-2'],
    ['conv-1', 'node-2', { force: true }],
  ]);
  assert.equal(getTreeCalls, 1);
  assert.equal(state.error, null);
  assert.equal(state.currentNodeId, 'node-1');
}

async function testRefreshMessagesUsesHistoryTipInsteadOfStaleConversationList() {
  const currentConversation = {
    id: 'conv-1',
    title: '刷新测试',
    created_at: 1,
    updated_at: 1,
    model: '',
    model_id: '',
    provider_id: '',
    current_node_id: 'root',
    total_tokens: {},
  };
  historyResponse = [
    { id: 'msg-a', role: 'user', content: '你好', node_id: 'node-hello' },
    { id: 'msg-b', role: 'assistant', content: '你好回复', node_id: 'node-hello' },
  ];
  branchesResponse = {};

  useConversationStore.setState({
    conversations: [currentConversation],
    currentConversation,
    messages: [],
    branches: {},
    currentNodeId: 'root',
    loading: false,
    error: null,
  });

  const ok = await useConversationStore.getState().refreshMessages('conv-1', {
    awaitNodeId: 'node-hello',
    awaitRole: 'assistant',
    retries: 0,
  });

  const state = useConversationStore.getState();
  assert.equal(ok, true);
  assert.equal(state.currentNodeId, 'node-hello');
  assert.equal(state.currentConversation.current_node_id, 'node-hello');
}

async function testSwitchNodeUpdatesCurrentConversationSnapshot() {
  switchNodeCalls = [];
  historyResponse = [];
  branchesResponse = {};
  const currentConversation = {
    id: 'conv-1',
    title: '编辑测试',
    created_at: 1,
    updated_at: 1,
    model: '',
    model_id: '',
    provider_id: '',
    current_node_id: 'node-hello',
    total_tokens: {},
  };

  useConversationStore.setState({
    conversations: [currentConversation],
    currentConversation,
    messages: [{ id: 'old', role: 'user', content: '你好', node_id: 'node-hello' }],
    branches: {},
    currentNodeId: 'node-hello',
    loading: false,
    error: null,
  });

  await useConversationStore.getState().switchNode('root');

  const state = useConversationStore.getState();
  assert.deepEqual(switchNodeCalls, [{ conversationId: 'conv-1', nodeId: 'root' }]);
  assert.equal(state.currentNodeId, 'root');
  assert.equal(state.currentConversation.current_node_id, 'root');
}

async function testUpdateMultiAgentModeSyncsConversationSnapshots() {
  updateMultiAgentModeCalls = [];
  const currentConversation = {
    id: 'conv-1',
    title: 'agent 模式测试',
    created_at: 1,
    updated_at: 1,
    model: '',
    model_id: '',
    provider_id: '',
    current_node_id: 'node-hello',
    multi_agent_mode: 'explicit_request_only',
    total_tokens: {},
  };

  useConversationStore.setState({
    conversations: [currentConversation, { ...currentConversation, id: 'conv-2' }],
    currentConversation,
    messages: [],
    branches: {},
    currentNodeId: 'node-hello',
    loading: false,
    error: null,
  });

  await useConversationStore.getState().updateMultiAgentMode('conv-1', 'proactive');

  const state = useConversationStore.getState();
  assert.deepEqual(updateMultiAgentModeCalls, [{ conversationId: 'conv-1', mode: 'proactive' }]);
  assert.equal(state.currentConversation.multi_agent_mode, 'proactive');
  assert.equal(state.conversations.find((conversation) => conversation.id === 'conv-1').multi_agent_mode, 'proactive');
  assert.equal(state.conversations.find((conversation) => conversation.id === 'conv-2').multi_agent_mode, 'explicit_request_only');
}

function testSetCurrentNodeIdLocalKeepsSnapshotsInSync() {
  const currentConversation = {
    id: 'conv-1',
    title: '本地切换测试',
    created_at: 1,
    updated_at: 1,
    model: '',
    model_id: '',
    provider_id: '',
    current_node_id: 'node-hello',
    total_tokens: {},
  };

  useConversationStore.setState({
    conversations: [currentConversation],
    currentConversation,
    messages: [{ id: 'msg-a', role: 'user', content: '你好', node_id: 'node-hello' }],
    branches: {},
    currentNodeId: 'node-hello',
    loading: false,
    error: null,
  });

  useConversationStore.getState().setCurrentNodeIdLocal('node-openai');

  const state = useConversationStore.getState();
  assert.equal(state.currentNodeId, 'node-openai');
  assert.equal(state.currentConversation.current_node_id, 'node-openai');
  assert.equal(state.conversations[0].current_node_id, 'node-openai');
}

function testPatchAssistantMessageFromStreamUpsertsCurrentNode() {
  const currentConversation = {
    id: 'conv-1',
    title: '流式补丁测试',
    created_at: 1,
    updated_at: 1,
    model: '',
    model_id: '',
    provider_id: '',
    current_node_id: 'node-old',
    total_tokens: {},
  };

  useConversationStore.setState({
    conversations: [currentConversation],
    currentConversation,
    messages: [],
    branches: {},
    currentNodeId: 'node-old',
    loading: false,
    error: null,
  });

  const patched = useConversationStore.getState().patchAssistantMessageFromStream('conv-1', {
    id: 'stream-run-node-new',
    role: 'assistant',
    content: '流式完成回答',
    node_id: 'node-new',
    timestamp: 2,
    generation_info: {
      duration_ms: 1000,
      status: 'completed',
    },
  }, '问题');

  const state = useConversationStore.getState();
  assert.equal(patched, true);
  assert.equal(state.messages.length, 2);
  assert.equal(state.messages[0].role, 'user');
  assert.equal(state.messages[0].content, '问题');
  assert.equal(state.messages[1].role, 'assistant');
  assert.equal(state.messages[1].content, '流式完成回答');
  assert.equal(state.currentNodeId, 'node-new');
  assert.equal(state.currentConversation.current_node_id, 'node-new');
  assert.equal(state.conversations[0].current_node_id, 'node-new');

  useConversationStore.getState().patchAssistantMessageFromStream('conv-1', {
    id: 'stream-run-node-new',
    role: 'assistant',
    content: '后端前的最终回答',
    node_id: 'node-new',
    timestamp: 3,
    generation_info: {
      duration_ms: 1200,
      status: 'completed',
    },
  });

  const replaced = useConversationStore.getState();
  assert.equal(replaced.messages.length, 2);
  assert.equal(replaced.messages[1].content, '后端前的最终回答');
}

async function main() {
  await testDeleteNodeRefreshesTreeData();
  await testDeleteNodeRetriesForceWhenActiveRunBlocksDeletion();
  await testRefreshMessagesUsesHistoryTipInsteadOfStaleConversationList();
  await testSwitchNodeUpdatesCurrentConversationSnapshot();
  await testUpdateMultiAgentModeSyncsConversationSnapshots();
  testSetCurrentNodeIdLocalKeepsSnapshotsInSync();
  testPatchAssistantMessageFromStreamUpsertsCurrentNode();
  console.log('conversationStore tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
