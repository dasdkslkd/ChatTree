const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

require.extensions['.ts'] = function loadTs(module, filename) {
  const source = fs.readFileSync(filename, 'utf8');
  const output = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    fileName: filename,
  }).outputText;
  module._compile(output, filename);
};

const {
  mergeLiveRunTranscriptItems,
  normalizeTranscriptItems,
} = require(path.join(__dirname, '../src/utils/transcriptItems.ts'));

function testNormalizeKeepsBackendOrderAndFiltersHidden() {
  const items = normalizeTranscriptItems([
    { id: 'a', type: 'user_message', visibility: 'main' },
    { id: 'b', type: 'plan_card', visibility: 'hidden' },
    { id: 'c', type: 'run_draft', visibility: 'main' },
  ]);
  assert.deepEqual(items.map((item) => item.id), ['a', 'c']);
}

function testNormalizeOnlyKeepsMainVisibilityInOrder() {
  const items = normalizeTranscriptItems([
    { id: 'a', type: 'user_message' },
    { id: 'b', type: 'plan_card', visibility: 'side_panel' },
    { id: 'c', type: 'run_draft', visibility: 'main' },
    { id: 'd', type: 'tool_call', visibility: 'drawer' },
    { id: 'e', type: 'assistant_message', visibility: 'main' },
  ]);
  assert.deepEqual(items.map((item) => item.id), ['a', 'c', 'e']);
}

function testNormalizeUsesBackendItemTypeWithoutReordering() {
  const items = normalizeTranscriptItems([
    { id: 'process-1', item_type: 'assistant_process', visibility: 'main' },
    { id: 'tool-1', item_type: 'tool_group', visibility: 'main' },
    { id: 'answer-1', item_type: 'assistant_answer', visibility: 'main' },
    { id: 'draft-1', item_type: 'run_draft', visibility: 'main' },
    { id: 'answer-2', item_type: 'assistant_answer', visibility: 'main' },
  ]);

  assert.deepEqual(items.map((item) => item.id), [
    'process-1',
    'tool-1',
    'answer-1',
    'draft-1',
    'answer-2',
  ]);
  assert.deepEqual(items.map((item) => item.type), [
    'assistant_process',
    'tool_group',
    'assistant_answer',
    'run_draft',
    'assistant_answer',
  ]);
}

function testNormalizeDoesNotProjectStandalonePlanCards() {
  const items = normalizeTranscriptItems([
    { id: 'user-1', type: 'user_message', visibility: 'main' },
    { id: 'legacy-plan-1', type: 'plan_card', visibility: 'main' },
    { id: 'legacy-plan-2', item_type: 'plan_card', visibility: 'main' },
    { id: 'process-1', item_type: 'assistant_process', visibility: 'main' },
  ]);

  assert.deepEqual(items.map((item) => item.id), ['user-1', 'process-1']);
  assert.equal(items.some((item) => item.type === 'plan_card'), false);
}

function testNormalizeDoesNotGroupProcessToolAndAnswerBlocks() {
  const items = normalizeTranscriptItems([
    { id: 'p1', type: 'assistant_process', local_order: 30 },
    { id: 'a1', type: 'assistant_answer', local_order: 10 },
    { id: 't1', type: 'tool_group', local_order: 20 },
    { id: 'p2', type: 'assistant_process', local_order: 40 },
  ]);

  assert.deepEqual(items.map((item) => `${item.type}:${item.id}`), [
    'assistant_process:p1',
    'assistant_answer:a1',
    'tool_group:t1',
    'assistant_process:p2',
  ]);
}

function testLiveRunOverlayReplacesPersistedDraftWithSingleProcessItemAtOriginalPosition() {
  const items = mergeLiveRunTranscriptItems(
    [
      { id: 'user-1', type: 'user_message', node_id: 'node-1' },
      { id: 'draft-1', type: 'run_draft', run_id: 'run-1', node_id: 'node-1' },
      { id: 'plan-1', type: 'plan_card', node_id: 'node-1' },
    ],
    [
      {
        runId: 'run-1',
        nodeId: 'node-1',
        targetNodeId: 'node-1',
        anchorNodeId: 'parent-1',
        items: [
          {
            id: 'live-run-process',
            type: 'assistant_process',
            run_id: 'run-1',
            props: { live_process: true },
          },
        ],
      },
    ],
  );

  assert.deepEqual(items.map((item) => item.id), [
    'user-1',
    'live-run-process',
  ]);
  assert.equal(items[1].type, 'assistant_process');
  assert.equal(items[1].props.live_process, true);
}

function testRunDraftHiddenWhenAssistantProcessExistsForSameRun() {
  const source = fs.readFileSync(path.join(__dirname, '../src/utils/transcriptItems.ts'), 'utf8');
  assert.match(source, /filterStaleRunDraftItems/);
  assert.match(source, /item\.type === 'run_draft'/);
  assert.match(source, /item\.type === 'assistant_process'/);

  const items = normalizeTranscriptItems([
    { id: 'draft-1', type: 'run_draft', run_id: 'run-1', visibility: 'main' },
    { id: 'process-1', type: 'assistant_process', run_id: 'run-1', visibility: 'main' },
    { id: 'draft-2', type: 'run_draft', run_id: 'run-2', visibility: 'main' },
  ]);
  assert.deepEqual(items.map((item) => item.id), ['process-1', 'draft-2']);
}

function testRunDraftHiddenWhenAssistantProcessMatchesSameNodeWithoutRunId() {
  const items = normalizeTranscriptItems([
    { id: 'draft-actual', type: 'run_draft', run_id: 'run-actual', node_id: 'node-1', visibility: 'main' },
    { id: 'process-actual', type: 'assistant_process', node_id: 'node-1', visibility: 'main' },
    { id: 'draft-other', type: 'run_draft', run_id: 'run-other', node_id: 'node-2', visibility: 'main' },
  ]);
  assert.deepEqual(items.map((item) => item.id), ['process-actual', 'draft-other']);
}

function testLiveRunOverlayAnchorsToBranchInsteadOfAppendingToTail() {
  const items = mergeLiveRunTranscriptItems(
    [
      { id: 'anchor-user', type: 'user_message', node_id: 'anchor-node' },
      { id: 'task-progress', type: 'task_progress', node_id: 'unrelated-node' },
    ],
    [
      {
        runId: 'run-2',
        nodeId: null,
        targetNodeId: null,
        anchorNodeId: 'anchor-node',
        items: [
          { id: 'live-answer-2', type: 'assistant_answer', run_id: 'run-2' },
        ],
      },
    ],
  );

  assert.deepEqual(items.map((item) => item.id), [
    'anchor-user',
    'live-answer-2',
    'task-progress',
  ]);
}

function testLiveRunOverlayAnchorsAfterApprovedPlanProposal() {
  const items = mergeLiveRunTranscriptItems(
    [
      { id: 'user-1', type: 'user_message', node_id: 'node-user' },
      {
        id: 'process-plan',
        type: 'assistant_process',
        node_id: 'node-plan',
        props: {
          timeline: [
            {
              type: 'plan_proposal',
              status: 'approved',
              plan_id: 'plan-1',
              proposal_id: 'proposal-1',
              tool_call_id: 'call-1',
              plan: '# Plan',
            },
          ],
        },
      },
      { id: 'answer-old', type: 'assistant_answer', node_id: 'node-old' },
    ],
    [
      {
        runId: 'run-implementation',
        nodeId: null,
        targetNodeId: null,
        anchorNodeId: 'node-plan',
        items: [
          { id: 'live-implementation', type: 'run_draft', run_id: 'run-implementation' },
        ],
      },
    ],
  );

  assert.deepEqual(items.map((item) => item.id), [
    'user-1',
    'process-plan',
    'live-implementation',
    'answer-old',
  ]);
}

function testPlanApprovalLiveRunMergesIntoPlanProcess() {
  const items = mergeLiveRunTranscriptItems(
    [
      { id: 'user-1', type: 'user_message', node_id: 'node-user' },
      {
        id: 'process-plan',
        type: 'assistant_process',
        node_id: 'node-plan',
        status: 'completed',
        props: {
          timeline: [
            {
              type: 'tool_call',
              tool_call: {
                id: 'call-exit',
                function: { name: 'exit_plan_mode', arguments: '{}' },
              },
              tool_result: null,
            },
          ],
        },
      },
    ],
    [
      {
        runId: 'run-implementation',
        nodeId: null,
        targetNodeId: 'node-impl',
        anchorNodeId: 'node-plan',
        items: [
          {
            id: 'live-implementation',
            type: 'assistant_process',
            run_id: 'run-implementation',
            node_id: 'node-impl',
            anchor_node_id: 'node-plan',
            status: 'streaming',
            props: {
              continuation_of_node_id: 'node-plan',
              continuation_marker: '计划已批准，开始实现',
              timeline: [
                {
                  type: 'tools',
                  key: 'tools-implementation',
                  items: [{
                    key: 'call-create-task',
                    name: 'create_task',
                    summary: 'create_task',
                    argsText: '',
                    outputText: '',
                    status: 'running',
                    resultEnvelope: null,
                  }],
                },
              ],
            },
          },
        ],
      },
    ],
  );

  assert.deepEqual(items.map((item) => item.id), ['user-1', 'process-plan']);
  assert.equal(items[1].status, 'streaming');
  assert.deepEqual(items[1].props.timeline.map((block) => block.type), [
    'tool_call',
    'marker',
    'tools',
  ]);
  assert.equal(items[1].props.timeline[1].content, '计划已批准，开始实现');
}

function testLiveRunPendingBubbleHiddenWhenUserMessageAlreadyLanded() {
  const items = mergeLiveRunTranscriptItems(
    [
      { id: 'user-real', type: 'user_message', node_id: 'node-1', visibility: 'main' },
    ],
    [
      {
        runId: 'run-1',
        nodeId: 'node-1',
        targetNodeId: 'node-1',
        anchorNodeId: null,
        items: [
          {
            id: 'live-run-process',
            type: 'assistant_process',
            run_id: 'run-1',
            node_id: 'node-1',
            visibility: 'main',
            props: {
              live_process: true,
              pendingUserMessage: 'hello',
              showPendingBubble: true,
              showStreamBlock: true,
              timeline: [],
            },
          },
        ],
      },
    ],
  );

  assert.deepEqual(items.map((item) => item.id), ['user-real', 'live-run-process']);
  assert.equal(items[1].props.showPendingBubble, false);
  assert.equal(items[1].props.pendingUserMessage, null);
}

function testPersistedPlanContinuationStaysInsideParentProcess() {
  const items = normalizeTranscriptItems([
    {
      id: 'process-plan',
      type: 'assistant_process',
      node_id: 'node-plan',
      props: {
        timeline: [{ type: 'tool_call', tool_call: { function: { name: 'exit_plan_mode' } } }],
        continuations: [
          {
            marker: '计划已批准，开始实现',
            timeline: [{ type: 'tool_call', tool_call: { function: { name: 'create_task' } } }],
          },
        ],
      },
    },
  ]);

  assert.equal(items.length, 1);
  assert.deepEqual(items[0].props.timeline.map((block) => block.type), [
    'tool_call',
    'marker',
    'tool_call',
  ]);
}

function testMainPageDelegatesTranscriptOrderingToTranscriptList() {
  const source = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
  assert.match(source, /<TranscriptList/);
  assert.doesNotMatch(source, /renderTaskLedgerStrip\(\)/);
  assert.doesNotMatch(source, /renderPlanApprovalCard\(\)/);
  assert.doesNotMatch(source, /renderPlanQuestionCard/);
  assert.doesNotMatch(source, /activeRunDrafts\.map\(/);
}

function testMainPageOutlineJumpsToTranscriptAnchors() {
  const source = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');

  assert.match(source, /function findTranscriptAnchorElement/);
  assert.match(source, /data-transcript-message-id=\{item\.message_id \|\| undefined\}/);
  assert.match(source, /data-transcript-node-id=\{nodeId \|\| undefined\}/);
  assert.match(source, /messageId: m\.id/);
  assert.match(source, /nodeId: m\.node_id/);
  assert.match(source, /findTranscriptAnchorElement\(historyRef\.current,\s*target\)/);
  assert.match(source, /findTranscriptAnchorElement\(historyRef\.current,\s*\{\s*nodeId: pendingScrollNodeId/);
}

function testMainPageUsesLiveTranscriptOverlayWithSharedProcessRendering() {
  const source = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
  const processTimeline = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/AssistantProcessTimeline.tsx'), 'utf8');

  assert.doesNotMatch(source, /\bgetAssistantTimeline\b/);
  assert.match(source, /\bgetSideRunAssistantTimeline\b/);
  assert.match(source, /\bcreateLiveRunTranscriptItems\b/);
  assert.match(source, /\bcreateLiveAssistantTranscriptItems\b/);
  assert.match(source, /\bliveMainTranscriptRunOverlays\b/);
  assert.match(source, /\bmergeLiveRunTranscriptItems\b/);
  assert.doesNotMatch(source, /\brenderLiveRunDraftTranscriptItem\b/);
  assert.match(processTimeline, /cn\('processed-fold', processExpanded && 'expanded'\)/);
  assert.match(processTimeline, /onClick=\{\(\) => setProcessExpanded/);
  assert.match(processTimeline, /streaming=\{block\.key === props\.activeReasoningKey\}/);
  assert.match(processTimeline, /<ToolCallGroup key=\{block\.key\} items=\{block\.items\} \/>/);
  assert.match(source, /const sideRunDrafts = useMemo/);
  assert.doesNotMatch(source, /activeRunDrafts\.map\(/);
}

function testPlanActionsAreRealCallbacks() {
  const mainPage = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
  const renderer = fs.readFileSync(path.join(__dirname, '../src/components/transcript/TranscriptItemRenderer.tsx'), 'utf8');
  const planCard = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/PlanCardItem.tsx'), 'utf8');

  assert.match(mainPage, /<TranscriptList[\s\S]*onApprovePlan=\{handleApprovePlan\}/);
  assert.match(mainPage, /<TranscriptList[\s\S]*onRejectPlan=\{handleRejectPlan\}/);
  assert.doesNotMatch(mainPage, /data-plan-actions/);
  assert.match(renderer, /onApprovePlan/);
  assert.match(renderer, /onRejectPlan/);
  assert.match(planCard, /onApprovePlan/);
  assert.match(planCard, /onRejectPlan/);
  assert.match(planCard, /onClick=\{\(\) => onApprovePlan\?\.\(item\)\}/);
  assert.match(planCard, /onClick=\{\(\) => onRejectPlan\?\.\(item\)\}/);
}

function testPlanQuestionIsRenderedAndAnsweredFromTranscriptItem() {
  const mainPage = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
  const renderer = fs.readFileSync(path.join(__dirname, '../src/components/transcript/TranscriptItemRenderer.tsx'), 'utf8');
  const planCard = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/PlanCardItem.tsx'), 'utf8');

  assert.match(mainPage, /<TranscriptList[\s\S]*onAnswerPlanQuestion=\{handleAnswerPlanQuestion\}/);
  assert.doesNotMatch(mainPage, /\{renderPlanQuestionCard\(\)\}/);
  assert.match(renderer, /onAnswerPlanQuestion/);
  assert.match(planCard, /status === 'awaiting_question'/);
  assert.match(planCard, /onAnswerPlanQuestion\?\.\(item,\s*answer\)/);
}

function testPlanActionsUseTranscriptItemPlanIdInsteadOfActivePlanFallback() {
  const source = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');

  assert.match(source, /const handleApprovePlan = useCallback\(async \(item: TranscriptItem\)/);
  assert.match(source, /const handleRejectPlan = useCallback\(async \(item: TranscriptItem\)/);
  assert.match(source, /const planId = item\.plan_id \|\| ''/);
  assert.match(source, /isTranscriptItemVisibleNow\(item,\s*currentConversation\?\.id \?\? null,\s*selectedBranchTipId\)/);
  assert.doesNotMatch(source, /const planId = activePlan\.plan_id \|\| activePlan\.id \|\| ''/);
}

function testPlanQuestionAnswerUsesTranscriptItemPlanIdInsteadOfActivePlanFallback() {
  const source = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
  const handlerMatch = source.match(/const handleAnswerPlanQuestion = useCallback\(async \(item: TranscriptItem,\s*answerOverride\?: string\) => \{[\s\S]*?\n  \}, \[/);

  assert.ok(handlerMatch, 'handleAnswerPlanQuestion handler should accept the transcript item');
  assert.match(handlerMatch[0], /isTranscriptItemVisibleNow\(item,\s*currentConversation\?\.id \?\? null,\s*selectedBranchTipId\)/);
  assert.match(handlerMatch[0], /const planId = item\.plan_id \|\| ''/);
  assert.match(handlerMatch[0], /const actionNodeId = getTranscriptItemNodeId\(item\) \|\| selectedBranchTipId/);
  assert.doesNotMatch(handlerMatch[0], /activePlan/);
  assert.doesNotMatch(handlerMatch[0], /selectedBranchTipId,\s*\)/);
}

function testTranscriptFallbackAndCopySurfacesAreVisible() {
  const renderer = fs.readFileSync(path.join(__dirname, '../src/components/transcript/TranscriptItemRenderer.tsx'), 'utf8');
  const list = fs.readFileSync(path.join(__dirname, '../src/components/transcript/TranscriptList.tsx'), 'utf8');
  const userMessage = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/UserMessageItem.tsx'), 'utf8');
  const assistantAnswer = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/AssistantAnswerItem.tsx'), 'utf8');
  const mainPage = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');

  assert.doesNotMatch(renderer, /default:\s*return null/);
  assert.match(renderer, /UnknownTranscriptItem/);
  assert.match(list, /transcript-empty/);
  assert.match(list, /transcript-error/);
  assert.match(userMessage, /onCopy/);
  assert.match(userMessage, /aria-label="复制消息"/);
  assert.match(assistantAnswer, /onCopy/);
  assert.match(assistantAnswer, /aria-label="复制消息"/);
  assert.match(mainPage, /onCopyItem=\{handleCopyTranscriptItem\}/);
}

function testTranscriptMessageItemsUseLegacyChatBubbleStyling() {
  const list = fs.readFileSync(path.join(__dirname, '../src/components/transcript/TranscriptList.tsx'), 'utf8');
  const userMessage = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/UserMessageItem.tsx'), 'utf8');
  const assistantAnswer = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/AssistantAnswerItem.tsx'), 'utf8');
  const assistantProcess = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/AssistantProcessItem.tsx'), 'utf8');
  const assistantProcessTimeline = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/AssistantProcessTimeline.tsx'), 'utf8');
  const toolGroup = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/ToolGroupItem.tsx'), 'utf8');

  assert.match(userMessage, /chat-message-row w-full my-2 flex flex-col group items-end/);
  assert.match(userMessage, /rounded-2xl rounded-br-sm/);
  assert.match(userMessage, /self-end justify-end/);

  assert.match(assistantAnswer, /chat-message-row w-full flex flex-col group items-start/);
  assert.match(assistantAnswer, /compactAfterProcess \? 'mt-0 mb-2' : 'my-2'/);
  assert.match(assistantAnswer, /rounded-2xl leading-relaxed/);
  assert.match(assistantAnswer, /self-start justify-start/);

  assert.match(list, /applyProcessAnswerCompaction/);
  assert.match(list, /compact_with_next_answer:\s*true/);
  assert.match(list, /compact_after_process:\s*true/);
  assert.match(assistantProcessTimeline, /className=\{cn\('thought'/);
  assert.match(assistantProcessTimeline, /className="thought-head"/);
  assert.match(assistantProcessTimeline, /rounded-lg px-3 py-2 text-sm leading-relaxed/);
  assert.match(assistantProcessTimeline, /rounded-lg px-3 py-2 text-sm prose prose-sm prose-invert/);
  assert.doesNotMatch(assistantProcessTimeline, /rounded-2xl rounded-br-sm/);
  assert.match(assistantProcessTimeline, /props\.compactWithNextAnswer \? 'mt-2 mb-0' : 'my-2'/);
  assert.match(assistantProcess, /tool_interactions/);
  assert.match(assistantProcessTimeline, /type: 'content'/);
  assert.match(assistantProcessTimeline, /renderTimelineBlock/);
  assert.doesNotMatch(assistantProcessTimeline, /PlanProposalCard/);
  assert.doesNotMatch(assistantProcess, /transcript-assistant-process/);

  assert.match(toolGroup, /className=\{cn\('tool-group'/);
  assert.match(toolGroup, /className="tool-group-header"/);
  assert.doesNotMatch(toolGroup, /transcript-tool-group/);
}

function testAssistantProcessRendersIntermediateTextButNoCopy() {
  const assistantProcess = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/AssistantProcessItem.tsx'), 'utf8');
  const assistantProcessTimeline = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/AssistantProcessTimeline.tsx'), 'utf8');
  const assistantAnswer = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/AssistantAnswerItem.tsx'), 'utf8');

  assert.match(assistantProcessTimeline, /type: 'content'/);
  assert.match(assistantProcessTimeline, /renderTimelineBlock/);
  assert.doesNotMatch(assistantProcessTimeline, /PlanProposalCard/);
  assert.doesNotMatch(assistantProcess, /aria-label="复制消息"/);
  assert.doesNotMatch(assistantProcessTimeline, /aria-label="复制消息"/);
  assert.match(assistantAnswer, /aria-label="复制消息"/);
}

function testStoppedStatusBelongsAfterAssistantAnswerWhenProcessIsCompacted() {
  const assistantProcessTimeline = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/AssistantProcessTimeline.tsx'), 'utf8');
  const assistantAnswer = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/AssistantAnswerItem.tsx'), 'utf8');

  assert.match(assistantProcessTimeline, /props\.compactWithNextAnswer \|\| props\.showStatusLabel === false \? null : getStreamStatusLabel/);
  assert.match(assistantAnswer, /getStreamStatusText/);
  assert.match(assistantAnswer, /stream_status/);
  assert.match(assistantAnswer, /statusLabel &&/);
  assert.match(assistantAnswer, /text-destructive/);
}

function testAssistantProcessUsesToolResultOnToolCallTimelineBlocks() {
  const assistantTimeline = fs.readFileSync(path.join(__dirname, '../src/utils/assistantTimeline.ts'), 'utf8');

  assert.match(
    assistantTimeline,
    /type === 'tool_call'[\s\S]*makeToolItem\(\s*normalizeTimelineToolCall\(record\),\s*normalizeTimelineToolMessage\(record\),\s*key\s*\)/,
  );
}

function testStreamStateContentStaysFinalAnswerOnly() {
  const streamManager = fs.readFileSync(path.join(__dirname, '../src/services/streamManager.ts'), 'utf8');
  assert.match(streamManager, /if \(chunk\.content && !isAggregateResultEvent\(chunk\) && !isCommandEvent\(chunk\)\) \{\s*next\.content \+= chunk\.content;\s*next\.reasoningActive = false;\s*\}/);
  assert.match(
    streamManager,
    /chunk\.event_type === 'tool_calls_committed'[\s\S]*?appendToolCalls\(\s*next\.toolInteractions,\s*toolCalls,\s*next\.content,\s*next\.reasoning,\s*toolRoundId,\s*toolRound,\s*true,\s*\)/,
  );
  assert.match(streamManager, /if \(toolCalls\.length > 0\) \{\s*next\.content = '';\s*next\.reasoning = '';\s*next\.reasoningActive = false;\s*\}/);
}

function testPlanApprovalDoesNotRenderControlEvents() {
  const mainPage = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
  const transcriptItems = fs.readFileSync(path.join(__dirname, '../src/utils/transcriptItems.ts'), 'utf8');
  assert.match(transcriptItems, /visibility === 'main'/);
  assert.doesNotMatch(mainPage, /control_event/);
  assert.doesNotMatch(mainPage, /PlanCardItem/);
  assert.match(transcriptItems, /rawType === 'plan_card'/);
  assert.match(transcriptItems, /status === 'awaiting_approval'/);
}

function testCopyHandlerOnlyReachesRealTranscriptMessages() {
  const renderer = fs.readFileSync(path.join(__dirname, '../src/components/transcript/TranscriptItemRenderer.tsx'), 'utf8');
  const planCard = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/PlanCardItem.tsx'), 'utf8');
  const runDraft = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/RunDraftItem.tsx'), 'utf8');
  const assistantProcess = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/AssistantProcessItem.tsx'), 'utf8');
  const toolGroup = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/ToolGroupItem.tsx'), 'utf8');

  assert.match(renderer, /case 'user_message':[\s\S]*<UserMessageItem[\s\S]*onCopy=\{onCopyItem\}/);
  assert.match(renderer, /case 'assistant_answer':\s*return <AssistantAnswerItem item=\{item\} onCopy=\{onCopyItem\} \/>/);
  const nonCopyableCases = renderer.match(/case 'assistant_process':[\s\S]*?case 'task_notification':/)?.[0] || '';
  assert.ok(nonCopyableCases, 'renderer should expose the non-copyable transcript cases');
  assert.doesNotMatch(nonCopyableCases, /\bonCopy=/);
  assert.doesNotMatch(nonCopyableCases, /\bonCopyItem\b/);
  assert.doesNotMatch(planCard, /aria-label="复制消息"|onCopy/);
  assert.doesNotMatch(runDraft, /aria-label="复制消息"|onCopy/);
  assert.doesNotMatch(assistantProcess, /aria-label="复制消息"|onCopy/);
  assert.doesNotMatch(toolGroup, /aria-label="复制消息"|onCopy/);
}

function testUserMessageEditAndDeleteActionsAreWired() {
  const types = fs.readFileSync(path.join(__dirname, '../src/types/transcript.ts'), 'utf8');
  const list = fs.readFileSync(path.join(__dirname, '../src/components/transcript/TranscriptList.tsx'), 'utf8');
  const renderer = fs.readFileSync(path.join(__dirname, '../src/components/transcript/TranscriptItemRenderer.tsx'), 'utf8');
  const userMessage = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/UserMessageItem.tsx'), 'utf8');
  const chatInput = fs.readFileSync(path.join(__dirname, '../src/components/ChatInput.tsx'), 'utf8');
  const mainPage = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');

  assert.match(types, /TranscriptUserMessageActionHandler/);
  assert.match(types, /onEditUserMessage\?:/);
  assert.match(types, /onDeleteUserMessage\?:/);
  assert.match(list, /onEditUserMessage=\{onEditUserMessage\}/);
  assert.match(list, /onDeleteUserMessage=\{onDeleteUserMessage\}/);
  assert.match(renderer, /<UserMessageItem[\s\S]*onEdit=\{onEditUserMessage\}[\s\S]*onDelete=\{onDeleteUserMessage\}/);
  assert.match(userMessage, /aria-label="编辑消息"/);
  assert.match(userMessage, /aria-label="删除消息"/);
  assert.match(userMessage, /Pencil/);
  assert.match(userMessage, /Trash2/);
  assert.match(mainPage, /const handleEditUserMessage = useCallback\(async \(item: TranscriptItem,\s*text: string\)/);
  assert.match(mainPage, /isTranscriptItemOnCurrentBranch\(item,\s*currentConversation\?\.id \?\? null,\s*currentBranchNodeIds\)/);
  assert.match(mainPage, /const parentNodeId = getEditableUserMessageParentNodeId\(item,\s*messages\)/);
  assert.match(mainPage, /const inheritedToolPermissionMode = liveBranchToolPermissionMode \?\? currentBranchToolPermissionMode \?\? null/);
  assert.match(mainPage, /setEditTargetNodeId\(parentNodeId\)/);
  assert.match(mainPage, /setEditToolPermissionMode\(inheritedToolPermissionMode\)/);
  assert.match(mainPage, /const attachmentRefs = getEditableUserMessageAttachmentRefs\(item,\s*messages\)/);
  assert.match(mainPage, /setAttachedFiles\(attachmentRefs\.importFiles\)/);
  assert.match(mainPage, /setAttachedImageRefs\(attachmentRefs\.imageRefs\)/);
  assert.match(mainPage, /await switchNode\(parentNodeId\)/);
  assert.match(mainPage, /const handleCancelEdit = useCallback\(async \(\) => \{/);
  assert.match(mainPage, /await switchNode\(returnNodeId\)/);
  assert.match(mainPage, /isEditing=\{Boolean\(editTargetNodeId\)\}/);
  assert.match(mainPage, /onCancelEdit=\{handleCancelEdit\}/);
  assert.match(chatInput, /isEditing\?: boolean/);
  assert.match(chatInput, /onCancelEdit\?: \(\) => void/);
  assert.match(chatInput, /aria-label="取消编辑"/);
  assert.match(mainPage, /tool_permission_mode: toolPermissionMode \?\? \(editTargetNodeId \? editToolPermissionMode \?\? undefined : undefined\)/);
  assert.match(mainPage, /const isProtectedEditAttachment = editProtectedAttachmentNames\.includes\(filename\)/);
  assert.match(mainPage, /const handleDeleteUserMessage = useCallback\(async \(item: TranscriptItem\)/);
  assert.match(mainPage, /deleteNode\(nodeId\)/);
  assert.match(mainPage, /onEditUserMessage=\{handleEditUserMessage\}/);
  assert.match(mainPage, /onDeleteUserMessage=\{handleDeleteUserMessage\}/);
}

testNormalizeKeepsBackendOrderAndFiltersHidden();
testNormalizeOnlyKeepsMainVisibilityInOrder();
testNormalizeUsesBackendItemTypeWithoutReordering();
testNormalizeDoesNotProjectStandalonePlanCards();
testNormalizeDoesNotGroupProcessToolAndAnswerBlocks();
testLiveRunOverlayReplacesPersistedDraftWithSingleProcessItemAtOriginalPosition();
testRunDraftHiddenWhenAssistantProcessExistsForSameRun();
testRunDraftHiddenWhenAssistantProcessMatchesSameNodeWithoutRunId();
testLiveRunOverlayAnchorsToBranchInsteadOfAppendingToTail();
testLiveRunOverlayAnchorsAfterApprovedPlanProposal();
testPlanApprovalLiveRunMergesIntoPlanProcess();
testLiveRunPendingBubbleHiddenWhenUserMessageAlreadyLanded();
testPersistedPlanContinuationStaysInsideParentProcess();
testMainPageDelegatesTranscriptOrderingToTranscriptList();
testMainPageOutlineJumpsToTranscriptAnchors();
testMainPageUsesLiveTranscriptOverlayWithSharedProcessRendering();
testPlanActionsAreRealCallbacks();
testPlanQuestionIsRenderedAndAnsweredFromTranscriptItem();
testPlanActionsUseTranscriptItemPlanIdInsteadOfActivePlanFallback();
testPlanQuestionAnswerUsesTranscriptItemPlanIdInsteadOfActivePlanFallback();
testTranscriptFallbackAndCopySurfacesAreVisible();
testTranscriptMessageItemsUseLegacyChatBubbleStyling();
testAssistantProcessRendersIntermediateTextButNoCopy();
testStoppedStatusBelongsAfterAssistantAnswerWhenProcessIsCompacted();
testAssistantProcessUsesToolResultOnToolCallTimelineBlocks();
testStreamStateContentStaysFinalAnswerOnly();
testPlanApprovalDoesNotRenderControlEvents();
testCopyHandlerOnlyReachesRealTranscriptMessages();
testUserMessageEditAndDeleteActionsAreWired();
console.log('transcriptItems tests passed');
