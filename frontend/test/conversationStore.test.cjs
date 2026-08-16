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
const transcriptServiceModule = path.join(__dirname, '../src/services/transcript.ts');
const modelStoreModule = path.join(__dirname, '../src/store/modelStore.ts');
const storeModule = path.join(__dirname, '../src/store/conversationStore.ts');
const { ChatTreeApiError } = require('../src/api/errors.ts');
require('../src/runtime/profileContext.ts').initializeProfileContext(
  'http://127.0.0.1:18100/s/local',
);

const localStore = new Map();
global.localStorage = {
  getItem: (key) => localStore.get(key) ?? null,
  setItem: (key, value) => localStore.set(key, String(value)),
  removeItem: (key) => localStore.delete(key),
};

let transcriptResponse = {
  conversation_id: 'conv-1',
  node_id: 'node-1',
  revision: 0,
  items: [
    { id: 'message:msg-1', type: 'user_message', conversation_id: 'conv-1', content: '保留的问题', node_id: 'node-1', message_id: 'msg-1' },
    { id: 'message:msg-2', type: 'assistant_answer', conversation_id: 'conv-1', content: '保留的回答', node_id: 'node-1', message_id: 'msg-2', status: 'complete' },
  ],
};
let branchesResponse = { 'node-1': [] };
let getBranchesCalls = 0;
let getBranchesIds = [];
let branchesByConversation = {};
let branchesGate = null;
let treeGate = null;
let createCalls = [];
let createdConversation = null;
let updateModelCalls = [];
let listCalls = 0;
let listResponse = [];
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
      create: async (request) => {
        createCalls.push(request);
        return createdConversation;
      },
      updateModel: async (...args) => {
        updateModelCalls.push(args);
      },
      list: async () => {
        listCalls += 1;
        return listResponse;
      },
      getBranches: async (conversationId) => {
        getBranchesCalls += 1;
        getBranchesIds.push(conversationId);
        if (branchesGate) await branchesGate;
        return branchesByConversation[conversationId] ?? branchesResponse;
      },
      getTree: async () => {
        getTreeCalls += 1;
        if (treeGate) await treeGate;
        return refreshedTree;
      },
    },
  },
};

require.cache[require.resolve(transcriptServiceModule)] = {
  id: transcriptServiceModule,
  filename: transcriptServiceModule,
  loaded: true,
  exports: {
    transcriptService: {
      fetchBranchSnapshot: async () => transcriptResponse,
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
}

async function testDeleteNodeRetriesForceWhenActiveRunBlocksDeletion() {
  getTreeCalls = 0;
  deleteNodeCalls = [];
  let attempts = 0;
  deleteNodeHandler = async () => {
    attempts += 1;
    if (attempts === 1) {
      throw new ChatTreeApiError('Active runs are present', {
        status: 409,
        code: 'active_runs_present',
        retryable: false,
        requestId: 'delete-tree',
        details: { active_run_ids: ['run-1'] },
      });
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

async function testRefreshMessagesUsesTranscriptTipInsteadOfMessageHistory() {
  getBranchesCalls = 0;
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
  transcriptResponse = {
    conversation_id: 'conv-1',
    node_id: 'node-hello',
    revision: 0,
    items: [
      { id: 'message:msg-a', type: 'user_message', conversation_id: 'conv-1', content: '你好', node_id: 'node-hello', message_id: 'msg-a' },
      { id: 'message:msg-b', type: 'assistant_answer', conversation_id: 'conv-1', content: '你好回复', node_id: 'node-hello', message_id: 'msg-b', status: 'complete' },
    ],
  };
  branchesResponse = {};

  useConversationStore.setState({
    conversations: [currentConversation],
    currentConversation,
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
  assert.equal(getBranchesCalls, 0);
}

async function testRefreshMessagesReturnsFalseUntilAwaitedUserLands() {
  getBranchesCalls = 0;
  const currentConversation = {
    id: 'conv-1',
    title: '乐观消息刷新测试',
    created_at: 1,
    updated_at: 1,
    model: '',
    model_id: '',
    provider_id: '',
    current_node_id: 'node-old',
    total_tokens: {},
  };
  transcriptResponse = {
    conversation_id: 'conv-1',
    node_id: 'node-new',
    revision: 0,
    items: [
      { id: 'message:msg-old-user', type: 'user_message', conversation_id: 'conv-1', content: '旧问题', node_id: 'node-old', message_id: 'msg-old-user' },
      { id: 'message:msg-old-assistant', type: 'assistant_answer', conversation_id: 'conv-1', content: '旧回答', node_id: 'node-old', message_id: 'msg-old-assistant', status: 'complete' },
    ],
  };
  branchesResponse = {};

  useConversationStore.setState({
    conversations: [currentConversation],
    currentConversation,
    branches: {},
    currentNodeId: 'node-new',
    loading: false,
    error: null,
  });

  const ok = await useConversationStore.getState().refreshMessages('conv-1', {
    awaitNodeId: 'node-new',
    awaitRole: 'user',
    retries: 0,
  });

  const state = useConversationStore.getState();
  assert.equal(ok, false);
  assert.equal(state.currentNodeId, 'node-new');
  assert.equal(getBranchesCalls, 0);
}

async function testRefreshBranchesUpdatesBranchesOnce() {
  getBranchesCalls = 0;
  branchesResponse = { 'node-hello': ['node-alt'] };
  const currentConversation = {
    id: 'conv-1',
    title: '分支刷新测试',
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
    branches: {},
    currentNodeId: 'node-hello',
    loading: false,
    error: null,
  });

  const ok = await useConversationStore.getState().refreshBranches('conv-1');

  const state = useConversationStore.getState();
  assert.equal(ok, true);
  assert.equal(getBranchesCalls, 1);
  assert.deepEqual(state.branches, branchesResponse);
}

async function testSwitchNodeUpdatesCurrentConversationSnapshot() {
  switchNodeCalls = [];
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


async function testCreateConversationInsertsLocallyWithoutRelisting() {
  createCalls = [];
  updateModelCalls = [];
  listCalls = 0;
  createdConversation = {
    id: 'conv-new',
    title: '新对话',
    created_at: 3,
    updated_at: 3,
    model: '',
    model_id: '',
    provider_id: '',
    current_node_id: 'root',
    total_tokens: {},
  };
  const existing = {
    id: 'conv-1',
    title: '旧对话',
    created_at: 1,
    updated_at: 1,
    model: '',
    model_id: '',
    provider_id: '',
    current_node_id: 'node-1',
    total_tokens: {},
  };

  useConversationStore.setState({
    conversations: [existing],
    currentConversation: existing,
    branches: {},
    treeData: null,
    currentNodeId: 'node-1',
    loading: false,
    error: null,
  });

  const result = await useConversationStore.getState().createConversation({ title: '新对话' });

  const state = useConversationStore.getState();
  assert.equal(result?.id, 'conv-new');
  assert.deepEqual(createCalls, [{ title: '新对话' }]);
  assert.equal(listCalls, 0, 'create must not reload the full conversation list');
  assert.deepEqual(state.conversations.map((conversation) => conversation.id), ['conv-new', 'conv-1']);
  assert.equal(state.currentConversation?.id, 'conv-new');
  assert.equal(state.loading, false);
}

async function testSelectConversationSwitchesUiBeforeBranchesLoad() {
  getBranchesCalls = 0;
  getBranchesIds = [];
  branchesByConversation = { 'conv-2': ['branch-2'] };
  let releaseBranches;
  const branchesBlocked = new Promise((resolve) => {
    releaseBranches = resolve;
  });
  branchesGate = branchesBlocked;
  const conv1 = {
    id: 'conv-1',
    title: '会话一',
    created_at: 1,
    updated_at: 1,
    model: '',
    model_id: '',
    provider_id: '',
    current_node_id: 'node-1',
    total_tokens: {},
  };
  const conv2 = {
    id: 'conv-2',
    title: '会话二',
    created_at: 2,
    updated_at: 2,
    model: '',
    model_id: '',
    provider_id: '',
    current_node_id: 'node-2',
    total_tokens: {},
  };

  useConversationStore.setState({
    conversations: [conv1, conv2],
    currentConversation: conv1,
    branches: [],
    treeData: null,
    currentNodeId: 'node-1',
    loading: false,
    error: null,
  });

  const promise = useConversationStore.getState().selectConversation('conv-2');
  // 本地会话立即切换，不等 getBranches 返回
  assert.equal(useConversationStore.getState().currentConversation?.id, 'conv-2');
  assert.equal(useConversationStore.getState().currentNodeId, 'node-2');
  assert.equal(getBranchesCalls, 1);

  releaseBranches();
  await promise;
  assert.deepEqual(useConversationStore.getState().branches, ['branch-2']);
  branchesGate = null;
}

async function testSelectConversationDoesNotOverwriteBranchesAfterSwitchAway() {
  getBranchesCalls = 0;
  getBranchesIds = [];
  branchesByConversation = { 'conv-2': ['branch-2'], 'conv-3': ['branch-3'] };
  let releaseBranches;
  const branchesBlocked = new Promise((resolve) => {
    releaseBranches = resolve;
  });
  branchesGate = branchesBlocked;
  const makeConversation = (id, nodeId) => ({
    id,
    title: id,
    created_at: 1,
    updated_at: 1,
    model: '',
    model_id: '',
    provider_id: '',
    current_node_id: nodeId,
    total_tokens: {},
  });
  const conv1 = makeConversation('conv-1', 'node-1');
  const conv2 = makeConversation('conv-2', 'node-2');
  const conv3 = makeConversation('conv-3', 'node-3');

  useConversationStore.setState({
    conversations: [conv1, conv2, conv3],
    currentConversation: conv1,
    branches: [],
    treeData: null,
    currentNodeId: 'node-1',
    loading: false,
    error: null,
  });

  const slow = useConversationStore.getState().selectConversation('conv-2');
  assert.equal(useConversationStore.getState().currentConversation?.id, 'conv-2');
  // 用户在 conv-2 branches 返回前切到 conv-3 并完成
  branchesGate = null;
  await useConversationStore.getState().selectConversation('conv-3');
  releaseBranches();
  await slow;

  const state = useConversationStore.getState();
  assert.equal(state.currentConversation?.id, 'conv-3');
  assert.deepEqual(state.branches, ['branch-3'], 'slow conversation branches must not overwrite current');
}

async function testRefreshMessagesBacksOffExponentially() {
  const originalSetTimeout = global.setTimeout;
  const originalClearTimeout = global.clearTimeout;
  const delays = [];
  global.setTimeout = (callback, delay) => {
    delays.push(delay);
    queueMicrotask(callback);
    return {};
  };
  global.clearTimeout = () => {};
  try {
    const currentConversation = {
      id: 'conv-1',
      title: '退避测试',
      created_at: 1,
      updated_at: 1,
      model: '',
      model_id: '',
      provider_id: '',
      current_node_id: 'node-old',
      total_tokens: {},
    };
    transcriptResponse = {
      conversation_id: 'conv-1',
      node_id: 'node-old',
      revision: 0,
      items: [
        { id: 'message:msg-old', type: 'assistant_answer', conversation_id: 'conv-1', content: '旧回答', node_id: 'node-old', message_id: 'msg-old', status: 'complete' },
      ],
    };
    branchesResponse = {};

    useConversationStore.setState({
      conversations: [currentConversation],
      currentConversation,
      branches: {},
      currentNodeId: 'node-old',
      loading: false,
      error: null,
    });

    const ok = await useConversationStore.getState().refreshMessages('conv-1', {
      awaitNodeId: 'node-new',
      awaitRole: 'assistant',
      retries: 3,
    });

    assert.equal(ok, false);
    assert.deepEqual(delays, [150, 300, 600]);
  } finally {
    global.setTimeout = originalSetTimeout;
    global.clearTimeout = originalClearTimeout;
  }
}

async function testLoadTreeDeduplicatesConcurrentRequests() {
  getTreeCalls = 0;
  let releaseTree;
  const treeBlocked = new Promise((resolve) => {
    releaseTree = resolve;
  });
  treeGate = treeBlocked;
  const currentConversation = {
    id: 'conv-1',
    title: '树去重测试',
    created_at: 1,
    updated_at: 1,
    model: '',
    model_id: '',
    provider_id: '',
    current_node_id: 'node-1',
    total_tokens: {},
  };

  useConversationStore.setState({
    conversations: [currentConversation],
    currentConversation,
    branches: {},
    treeData: null,
    currentNodeId: 'node-1',
    loading: false,
    error: null,
  });

  const first = useConversationStore.getState().loadTree('conv-1');
  const second = useConversationStore.getState().loadTree('conv-1');
  assert.equal(getTreeCalls, 1, 'concurrent loadTree calls must share one request');

  releaseTree();
  await Promise.all([first, second]);
  assert.equal(getTreeCalls, 1);
  assert.equal(useConversationStore.getState().treeData?.current_node_id, 'node-1');
  treeGate = null;

  // in-flight 清理后允许再次加载
  await useConversationStore.getState().loadTree('conv-1');
  assert.equal(getTreeCalls, 2);
}
async function main() {
  await testDeleteNodeRefreshesTreeData();
  await testDeleteNodeRetriesForceWhenActiveRunBlocksDeletion();
  await testRefreshMessagesUsesTranscriptTipInsteadOfMessageHistory();
  await testRefreshMessagesReturnsFalseUntilAwaitedUserLands();
  await testRefreshBranchesUpdatesBranchesOnce();
  await testSwitchNodeUpdatesCurrentConversationSnapshot();
  await testUpdateMultiAgentModeSyncsConversationSnapshots();
  testSetCurrentNodeIdLocalKeepsSnapshotsInSync();
  await testCreateConversationInsertsLocallyWithoutRelisting();
  await testSelectConversationSwitchesUiBeforeBranchesLoad();
  await testSelectConversationDoesNotOverwriteBranchesAfterSwitchAway();
  await testRefreshMessagesBacksOffExponentially();
  await testLoadTreeDeduplicatesConcurrentRequests();
  console.log('conversationStore tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
