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
const errorsModule = path.join(__dirname, '../src/api/errors.ts');
const messageApiModule = path.join(__dirname, '../src/api/message.ts');
const modelStoreModule = path.join(__dirname, '../src/store/modelStore.ts');
const epochModule = path.join(__dirname, '../src/runtime/connectionEpoch.ts');
const bootstrapModule = path.join(__dirname, '../src/runtime/frontendBootstrap.ts');
const profileStorageModule = path.join(__dirname, '../src/runtime/profileStorage.ts');
const storeModule = path.join(__dirname, '../src/store/conversationStore.ts');

const CONTEXT_A = Object.freeze({
  profileId: 'profile-a',
  apiBase: '/p/profile-a/api/v1',
  serverInstanceId: '11111111-1111-4111-8111-111111111111',
  connectionEpoch: 1,
  connectionLeaseId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
});

const storage = new Map();
global.localStorage = {
  getItem: (key) => storage.get(key) ?? null,
  setItem: (key, value) => storage.set(key, String(value)),
  removeItem: (key) => storage.delete(key),
};
global.window = {
  location: {
    href: 'http://127.0.0.1:5173/s/profile-a',
    pathname: '/s/profile-a',
  },
  localStorage: global.localStorage,
};
require(bootstrapModule).initializeFrontendBootstrap();
const { profileStorageKey } = require(profileStorageModule);
const boundConversationStorageKey = profileStorageKey('profile-a', 'conversation-storage');
const otherConversationStorageKey = profileStorageKey('profile-b', 'conversation-storage');
const persistedConversation = {
  id: 'profile-a-conversation',
  title: 'Profile A only',
  created_at: 1,
  updated_at: 1,
  model: '',
  model_id: '',
  provider_id: '',
  current_node_id: 'root',
  total_tokens: {},
};
storage.set('conversation-storage', JSON.stringify({
  state: { conversations: [{ ...persistedConversation, id: 'legacy-conversation' }] },
  version: 0,
}));
storage.set(otherConversationStorageKey, JSON.stringify({
  state: { conversations: [{ ...persistedConversation, id: 'profile-b-conversation' }] },
  version: 0,
}));
storage.set(boundConversationStorageKey, JSON.stringify({
  state: { conversations: [persistedConversation] },
  version: 0,
}));

let historyResponse = [
  { id: 'msg-1', role: 'user', content: '保留的问题', node_id: 'node-1' },
  { id: 'msg-2', role: 'assistant', content: '保留的回答', node_id: 'node-1' },
];
let branchesResponse = { 'node-1': [] };
let getBranchesCalls = 0;
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
let epochGate = null;
let resetToDefaultCalls = [];
let resetToDefaultHandler = async () => {};

function afterEpochGate(value) {
  return epochGate ? epochGate.promise.then(() => value) : Promise.resolve(value);
}

require.cache[require.resolve(conversationApiModule)] = {
  id: conversationApiModule,
  filename: conversationApiModule,
  loaded: true,
  exports: {
    conversationApi: {
      list: async () => afterEpochGate([]),
      create: async () => afterEpochGate({
        id: 'conv-created',
        title: 'created',
        created_at: 1,
        updated_at: 1,
        model: '',
        model_id: '',
        provider_id: '',
        current_node_id: 'root',
        total_tokens: {},
      }),
      delete: async () => afterEpochGate(undefined),
      updateTitle: async () => afterEpochGate(undefined),
      updateModel: async () => afterEpochGate(undefined),
      switchNode: async (conversationId, nodeId) => {
        switchNodeCalls.push({ conversationId, nodeId });
        return afterEpochGate({ current_node_id: nodeId });
      },
      updateMultiAgentMode: async (conversationId, mode) => {
        updateMultiAgentModeCalls.push({ conversationId, mode });
        return afterEpochGate(undefined);
      },
      deleteNode: async (...args) => {
        deleteNodeCalls.push(args);
        if (epochGate) return afterEpochGate({
          deleted_node_id: args[1],
          new_current_node_id: 'node-1',
          parent_node_id: 'node-1',
        });
        return deleteNodeHandler(...args);
      },
      getBranches: async () => {
        getBranchesCalls += 1;
        return afterEpochGate(branchesResponse);
      },
      getTree: async () => {
        getTreeCalls += 1;
        return afterEpochGate(refreshedTree);
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
      getHistory: async () => afterEpochGate(historyResponse),
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
        resetToDefault: async (token) => {
          resetToDefaultCalls.push(token);
          return resetToDefaultHandler(token);
        },
        syncFromConversation: async () => {},
      }),
    },
  },
};

const { normalizeApiError } = require(errorsModule);
const { connectionEpochRuntime } = require(epochModule);
connectionEpochRuntime.install(CONTEXT_A);
const { useConversationStore } = require(storeModule);

function testPersistUsesProfileScopedKey() {
  assert.deepEqual(
    useConversationStore.getState().conversations.map((conversation) => conversation.id),
    ['profile-a-conversation'],
    'rehydration reads only the immutable bootstrap Profile',
  );
  storage.delete(boundConversationStorageKey);
  storage.delete('conversation-storage');
  useConversationStore.setState({ conversations: [] });
  assert.equal(storage.has(boundConversationStorageKey), true);
  assert.equal(storage.has('conversation-storage'), false);
  assert.match(storage.get(otherConversationStorageKey), /profile-b-conversation/);
}

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
  const normalize = (data, headers = {}) => {
    const error = new Error('Conflict');
    error.isAxiosError = true;
    error.response = { status: 409, data, headers };
    return normalizeApiError(error);
  };
  const blockedErrors = [
    normalize({
      error: {
        code: 'active_runs_present',
        message: 'active run',
        retryable: true,
        request_id: 'req-modern',
        details: { active_run_ids: ['run-modern'] },
      },
    }),
    normalize(
      { detail: { message: 'active run', active_run_ids: ['run-legacy'] } },
      { 'x-request-id': 'req-legacy' },
    ),
  ];
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

  for (const blockedError of blockedErrors) {
    getTreeCalls = 0;
    deleteNodeCalls = [];
    let attempts = 0;
    deleteNodeHandler = async () => {
      attempts += 1;
      if (attempts === 1) throw blockedError;
      return {
        deleted_node_id: 'node-2',
        new_current_node_id: 'node-1',
        parent_node_id: 'node-1',
      };
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
    assert.equal(attempts, 2);
    assert.equal(getTreeCalls, 1);
    assert.equal(state.error, null);
    assert.equal(state.currentNodeId, 'node-1');
  }
}

async function testRefreshMessagesUsesHistoryTipInsteadOfStaleConversationList() {
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
  assert.equal(getBranchesCalls, 0);
}

async function testRefreshMessagesKeepsOptimisticMessagesUntilAwaitedUserLands() {
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
  historyResponse = [
    { id: 'msg-old-user', role: 'user', content: '旧问题', node_id: 'node-old' },
    { id: 'msg-old-assistant', role: 'assistant', content: '旧回答', node_id: 'node-old' },
  ];
  branchesResponse = {};

  useConversationStore.setState({
    conversations: [currentConversation],
    currentConversation,
    messages: [
      ...historyResponse,
      { id: 'stream-user-node-new', role: 'user', content: '新问题', node_id: 'node-new' },
    ],
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
  assert.equal(state.messages.some((message) => message.id === 'stream-user-node-new'), true);
  assert.equal(state.messages.some((message) => message.content === '新问题'), true);
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
    messages: [],
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

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

async function testUpdateConversationModelIsImmutable() {
  const conversation = {
    id: 'conv-1',
    title: 'immutable model',
    created_at: 1,
    updated_at: 1,
    model: '',
    model_id: 'old-model',
    provider_id: 'old-provider',
    current_node_id: 'node-1',
    total_tokens: {},
  };
  epochGate = null;
  useConversationStore.setState({
    conversations: [conversation],
    currentConversation: conversation,
    loading: false,
    error: null,
  });

  assert.equal(await useConversationStore.getState().updateConversationModel(
    'conv-1',
    'new-model',
    'new-provider',
    'high',
    true,
  ), true);

  const state = useConversationStore.getState();
  assert.equal(conversation.model_id, 'old-model');
  assert.equal(conversation.provider_id, 'old-provider');
  assert.notEqual(state.conversations[0], conversation);
  assert.notEqual(state.currentConversation, conversation);
  assert.equal(state.conversations[0].model_id, 'new-model');
  assert.equal(state.currentConversation.provider_id, 'new-provider');
}

async function testEveryAsyncActionKeepsItsOriginalEpoch() {
  const conversation = {
    id: 'conv-1',
    title: 'epoch matrix',
    created_at: 1,
    updated_at: 1,
    model: '',
    model_id: 'old-model',
    provider_id: 'old-provider',
    current_node_id: 'node-1',
    multi_agent_mode: 'explicit_request_only',
    total_tokens: {},
  };
  historyResponse = [];
  branchesResponse = { old: ['branch'] };
  epochGate = null;
  useConversationStore.setState({
    conversations: [conversation],
    currentConversation: conversation,
    messages: [{ id: 'old', role: 'user', content: 'old', node_id: 'node-1' }],
    branches: branchesResponse,
    treeData: refreshedTree,
    currentNodeId: 'node-1',
    loading: false,
    error: 'old-error',
  });

  const retry = useConversationStore.getState().refreshMessages('conv-1', {
    awaitNodeId: 'never-landed',
    retries: 1,
  });
  await Promise.resolve();
  await Promise.resolve();

  epochGate = deferred();
  const actions = useConversationStore.getState();
  const pending = [
    actions.loadConversations(),
    actions.createConversation(),
    actions.selectConversation('conv-1'),
    actions.deleteConversation('conv-1'),
    actions.updateConversationTitle('conv-1', 'stale-title'),
    actions.updateConversationModel('conv-1', 'stale-model', 'stale-provider'),
    actions.updateMultiAgentMode('conv-1', 'proactive'),
    actions.switchNode('stale-node'),
    actions.refreshBranches('conv-1'),
    actions.deleteNode('node-1'),
    actions.loadTree('conv-1'),
  ];
  const atInvalidation = useConversationStore.getState();
  connectionEpochRuntime.invalidate(connectionEpochRuntime.capture());
  epochGate.resolve();
  const results = await Promise.all([...pending, retry]);

  const after = useConversationStore.getState();
  assert.equal(results[5], false);
  assert.equal(results.at(-1), false);
  assert.equal(after.conversations, atInvalidation.conversations);
  assert.equal(after.currentConversation, atInvalidation.currentConversation);
  assert.equal(after.messages, atInvalidation.messages);
  assert.equal(after.branches, atInvalidation.branches);
  assert.equal(after.treeData, atInvalidation.treeData);
  assert.equal(after.currentNodeId, atInvalidation.currentNodeId);
  assert.equal(after.loading, atInvalidation.loading);
  assert.equal(after.error, atInvalidation.error);

  const callsBeforeCaptureFailures = {
    deleteNode: deleteNodeCalls.length,
    branches: getBranchesCalls,
    tree: getTreeCalls,
  };
  await Promise.all([
    useConversationStore.getState().loadConversations(),
    useConversationStore.getState().createConversation(),
    useConversationStore.getState().selectConversation('conv-1'),
    useConversationStore.getState().deleteConversation('conv-1'),
    useConversationStore.getState().updateConversationTitle('conv-1', 'never'),
    useConversationStore.getState().updateConversationModel('conv-1', 'never', 'never'),
    useConversationStore.getState().updateMultiAgentMode('conv-1', 'proactive'),
    useConversationStore.getState().switchNode('never'),
    useConversationStore.getState().refreshMessages('conv-1'),
    useConversationStore.getState().refreshBranches('conv-1'),
    useConversationStore.getState().deleteNode('node-1'),
    useConversationStore.getState().loadTree('conv-1'),
  ]);
  assert.deepEqual({
    deleteNode: deleteNodeCalls.length,
    branches: getBranchesCalls,
    tree: getTreeCalls,
  }, callsBeforeCaptureFailures);
}

async function testClearCurrentConversationAwaitsTokenOwnedModelReset() {
  let releaseReset;
  const resetGate = new Promise((resolve) => {
    releaseReset = resolve;
  });
  resetToDefaultCalls = [];
  resetToDefaultHandler = async () => resetGate;
  useConversationStore.setState({
    currentConversation: { ...persistedConversation, id: 'conv-clear' },
    messages: [{ id: 'message-clear' }],
    currentNodeId: 'node-clear',
  });
  const token = connectionEpochRuntime.capture();
  let settled = false;
  const clearing = useConversationStore.getState().clearCurrentConversation(token)
    .then(() => { settled = true; });

  assert.equal(useConversationStore.getState().currentConversation, null);
  assert.equal(useConversationStore.getState().currentNodeId, null);
  await Promise.resolve();
  assert.equal(settled, false, 'clear must keep its owner pending until model reset settles');
  assert.deepEqual(resetToDefaultCalls, [token]);
  releaseReset();
  await clearing;
  assert.equal(settled, true);
  resetToDefaultHandler = async () => {};
}

async function main() {
  testPersistUsesProfileScopedKey();
  await testDeleteNodeRefreshesTreeData();
  await testDeleteNodeRetriesForceWhenActiveRunBlocksDeletion();
  await testRefreshMessagesUsesHistoryTipInsteadOfStaleConversationList();
  await testRefreshMessagesKeepsOptimisticMessagesUntilAwaitedUserLands();
  await testRefreshBranchesUpdatesBranchesOnce();
  await testSwitchNodeUpdatesCurrentConversationSnapshot();
  await testUpdateMultiAgentModeSyncsConversationSnapshots();
  testSetCurrentNodeIdLocalKeepsSnapshotsInSync();
  testPatchAssistantMessageFromStreamUpsertsCurrentNode();
  await testUpdateConversationModelIsImmutable();
  await testClearCurrentConversationAwaitsTokenOwnedModelReset();
  await testEveryAsyncActionKeepsItsOriginalEpoch();
  console.log('conversationStore tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
