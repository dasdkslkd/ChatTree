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
    },
  }).outputText;
  module._compile(output, filename);
};

const clientPath = path.join(__dirname, '../src/api/client.ts');
const agentsPath = path.join(__dirname, '../src/api/agents.ts');
const workflowsPath = path.join(__dirname, '../src/api/workflows.ts');
const requests = [];

require.cache[require.resolve(clientPath)] = {
  id: clientPath,
  filename: clientPath,
  loaded: true,
  exports: {
    apiClient: {
      async post(url, data, options) {
        requests.push({ url, data, options });
        return {
          data: {
            run_id: `run-${requests.length}`,
            created: true,
            status: 'running',
          },
        };
      },
    },
  },
};

const { agentsApi } = require(agentsPath);
const { workflowsApi } = require(workflowsPath);

async function testAgentRunStartUsesRequiredIdempotencyContract() {
  requests.length = 0;
  const controller = new AbortController();
  const body = { input: 'inspect' };

  const response = await agentsApi.startRun(
    'conv/1',
    'review agent',
    body,
    'agent-key-1',
    controller.signal,
  );

  assert.deepEqual(response, { run_id: 'run-1', created: true, status: 'running' });
  assert.deepEqual(requests[0], {
    url: '/conversations/conv%2F1/agents/review%20agent/runs',
    data: body,
    options: {
      headers: { 'Idempotency-Key': 'agent-key-1' },
      signal: controller.signal,
    },
  });
}

async function testWorkflowRunStartUsesRequiredIdempotencyContract() {
  requests.length = 0;
  const controller = new AbortController();
  const body = { script: 'return 1' };

  const response = await workflowsApi.startRun(
    'conv/2',
    body,
    'workflow-key-1',
    controller.signal,
  );

  assert.deepEqual(response, { run_id: 'run-1', created: true, status: 'running' });
  assert.deepEqual(requests[0], {
    url: '/conversations/conv%2F2/workflows/runs',
    data: body,
    options: {
      headers: { 'Idempotency-Key': 'workflow-key-1' },
      signal: controller.signal,
    },
  });
}

(async () => {
  await testAgentRunStartUsesRequiredIdempotencyContract();
  await testWorkflowRunStartUsesRequiredIdempotencyContract();
  console.log('run start API contract tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
