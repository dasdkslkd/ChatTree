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
      deleteNode: async () => ({
        deleted_node_id: 'node-2',
        new_current_node_id: 'node-1',
        parent_node_id: 'node-1',
      }),
      getBranches: async () => ({ 'node-1': [] }),
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
      getHistory: async () => [
        { id: 'msg-1', role: 'user', content: '保留的问题', node_id: 'node-1' },
        { id: 'msg-2', role: 'assistant', content: '保留的回答', node_id: 'node-1' },
      ],
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
  assert.equal(getTreeCalls, 1);
  assert.equal(state.currentNodeId, 'node-1');
  assert.equal(state.currentConversation.current_node_id, 'node-1');
  assert.equal(state.treeData.current_node_id, 'node-1');
  assert.deepEqual(state.treeData.nodes.map((node) => node.id), ['root', 'node-1']);
  assert.deepEqual(state.messages.map((message) => message.node_id), ['node-1', 'node-1']);
}

async function main() {
  await testDeleteNodeRefreshesTreeData();
  console.log('conversationStore tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
