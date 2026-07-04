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

const { createPlansService } = require(path.join(__dirname, '../src/services/plans.ts'));

function fakeClient() {
  const calls = [];
  return {
    calls,
    client: {
      async get(url, config) {
        calls.push({ method: 'get', url, config });
        return { data: { plan: { plan_id: 'plan-1', conversation_id: 'conv-1', status: 'awaiting_approval', plan: '# Plan' } } };
      },
      async post(url, payload) {
        calls.push({ method: 'post', url, payload });
        return { data: { ok: true } };
      },
    },
  };
}

async function testFetchActivePlanUsesConversationQuery() {
  const { client, calls } = fakeClient();
  const service = createPlansService(client);

  const plan = await service.fetchActive('conv-1');

  assert.equal(plan.id, 'plan-1');
  assert.equal(plan.plan_id, 'plan-1');
  assert.deepEqual(calls[0], {
    method: 'get',
    url: '/conversations/conv-1/plans/current',
    config: undefined,
  });
}

async function testApproveAndRejectPayloads() {
  const { client, calls } = fakeClient();
  const service = createPlansService(client);

  await service.approve('conv-1', 'plan-1');
  await service.reject('conv-1', 'plan-1', '请补充测试计划');
  await service.answer('conv-1', 'plan-1', '默认显示');

  assert.deepEqual(calls[0], {
    method: 'post',
    url: '/conversations/conv-1/plans/plan-1/approve',
    payload: {},
  });
  assert.deepEqual(calls[1], {
    method: 'post',
    url: '/conversations/conv-1/plans/plan-1/reject',
    payload: { feedback: '请补充测试计划' },
  });
  assert.deepEqual(calls[2], {
    method: 'post',
    url: '/conversations/conv-1/plans/plan-1/answer',
    payload: { answer: '默认显示' },
  });
}

async function main() {
  await testFetchActivePlanUsesConversationQuery();
  await testApproveAndRejectPayloads();
  console.log('plansService tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
