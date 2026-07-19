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
  collectPendingToolApprovalPrompts,
} = require(path.join(__dirname, '../src/utils/toolApprovals.ts'));

function run(overrides = {}) {
  return {
    runId: 'run-chat',
    kind: 'chat',
    pendingUserMessage: null,
    pendingApprovals: {},
    ...overrides,
  };
}

function testCollectsPendingApprovalsFromMainDetachedAndChildRuns() {
  const prompts = collectPendingToolApprovalPrompts([
    run({
      runId: 'run-chat',
      kind: 'chat',
      pendingUserMessage: 'normal chat',
      pendingApprovals: {
        approval_chat: {
          id: 'approval_chat',
          status: 'pending',
          tool_name: 'read',
          arguments_preview: '{"path":"frontend/src/pages/MainPage.tsx"}',
        },
      },
    }),
    run({
      runId: 'run-workflow',
      kind: 'workflow',
      pendingUserMessage: '/workflow release-check',
      pendingApprovals: {
        approval_workflow: {
          id: 'approval_workflow',
          status: 'pending',
          tool_name: 'shell',
          arguments_preview: '{"command":"npm run build"}',
        },
      },
    }),
    run({
      runId: 'run-fork',
      kind: 'subagent',
      pendingUserMessage: '/fork inspect approvals',
      pendingApprovals: {
        approval_done: { id: 'approval_done', status: 'approved', tool_name: 'read' },
        approval_fork: {
          id: 'approval_fork',
          status: 'pending',
          tool_name: 'shell',
          arguments_preview: '{"command":"node frontend/test/slashRuntime.test.cjs"}',
        },
      },
    }),
    run({
      runId: 'run-child',
      kind: 'workflow_step',
      pendingApprovals: {
        approval_child: {
          id: 'approval_child',
          status: 'pending',
          tool_name: 'edit',
          arguments_preview: '{"path":"tmp/out.txt","operation":"create","content":"ok"}',
        },
      },
    }),
  ]);

  assert.deepEqual(prompts.map((item) => item.approval.id), [
    'approval_chat',
    'approval_workflow',
    'approval_fork',
    'approval_child',
  ]);
  assert.equal(prompts[1].runLabel, 'workflow');
  assert.equal(prompts[0].sourceLabel, '主对话');
  assert.equal(prompts[0].toolSummary, '读取 frontend/src/pages/MainPage.tsx');
  assert.equal(prompts[1].sourceLabel, 'Workflow');
  assert.equal(prompts[1].sourceSummary, '/workflow release-check');
  assert.equal(prompts[1].toolSummary, '运行 npm run build');
  assert.equal(prompts[2].runLabel, 'fork');
  assert.equal(prompts[2].sourceLabel, 'Subagent');
  assert.equal(prompts[3].sourceLabel, 'Workflow 子任务');
  assert.equal(prompts[3].sourceSummary, 'workflow_step · run-child');
  assert.equal(prompts[3].toolSummary, '写入 tmp/out.txt · 2 字符');
}

function testDeduplicatesApprovalsWhenRunsAppearInMultipleSurfaces() {
  const sharedRun = run({
    runId: 'run-shared',
    kind: 'subagent',
    pendingUserMessage: '/fork shared approval',
    pendingApprovals: {
      approval_shared: {
        id: 'approval_shared',
        status: 'pending',
        tool_name: 'shell',
        arguments_preview: '{"command":"echo shared"}',
      },
    },
  });

  const prompts = collectPendingToolApprovalPrompts([
    sharedRun,
    sharedRun,
    run({
      runId: 'run-other',
      kind: 'chat',
      pendingApprovals: {
        approval_shared: {
          id: 'approval_shared',
          status: 'pending',
          tool_name: 'read',
        },
      },
    }),
  ]);

  assert.deepEqual(prompts.map((item) => `${item.runId}:${item.approval.id}`), [
    'run-shared:approval_shared',
    'run-other:approval_shared',
  ]);
}

function testFiltersToServerConfirmedPendingApprovals() {
  const prompts = collectPendingToolApprovalPrompts([
    run({
      runId: 'run-restored',
      kind: 'subagent',
      pendingApprovals: {
        stale_approval: {
          id: 'stale_approval',
          status: 'pending',
          tool_name: 'shell',
        },
      },
    }),
    run({
      runId: 'run-live',
      kind: 'subagent',
      pendingApprovals: {
        live_approval: {
          id: 'live_approval',
          status: 'pending',
          tool_name: 'shell',
        },
      },
    }),
  ], new Set(['live_approval']));

  assert.deepEqual(prompts.map((item) => item.approval.id), ['live_approval']);
}

function testUsesUnknownToolFallbackWithoutLosingSourceContext() {
  const prompts = collectPendingToolApprovalPrompts([
    run({
      runId: 'run-unknown',
      kind: 'side_question',
      pendingUserMessage: '/ask inspect custom tool',
      pendingApprovals: {
        approval_unknown: {
          id: 'approval_unknown',
          status: 'pending',
          tool_name: 'custom_probe',
          arguments_preview: '{"opaque":true}',
        },
      },
    }),
  ]);

  assert.equal(prompts.length, 1);
  assert.equal(prompts[0].sourceLabel, '侧边提问');
  assert.equal(prompts[0].sourceSummary, '/ask inspect custom tool');
  assert.equal(prompts[0].toolSummary, '调用 custom_probe');
}

testCollectsPendingApprovalsFromMainDetachedAndChildRuns();
testDeduplicatesApprovalsWhenRunsAppearInMultipleSurfaces();
testFiltersToServerConfirmedPendingApprovals();
testUsesUnknownToolFallbackWithoutLosingSourceContext();

console.log('toolApprovals tests passed');
