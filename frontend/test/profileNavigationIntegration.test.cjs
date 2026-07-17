const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const mainPage = fs.readFileSync(
  path.join(__dirname, '../src/pages/MainPage.tsx'),
  'utf8',
);
const treeView = fs.readFileSync(
  path.join(__dirname, '../src/pages/TreeView.tsx'),
  'utf8',
);

function sliceBetween(source, start, end) {
  const startIndex = source.indexOf(start);
  const endIndex = source.indexOf(end, startIndex + start.length);
  assert.notEqual(startIndex, -1, `missing source marker: ${start}`);
  assert.notEqual(endIndex, -1, `missing source marker: ${end}`);
  return source.slice(startIndex, endIndex);
}

function assertOrdered(source, fragments) {
  let cursor = -1;
  for (const fragment of fragments) {
    const next = source.indexOf(fragment, cursor + 1);
    assert.notEqual(next, -1, `missing ordered fragment: ${fragment}`);
    assert.ok(next > cursor, `fragment is out of order: ${fragment}`);
    cursor = next;
  }
}

assert.equal(
  (mainPage.match(/new BoundRouteRestorer\(/g) || []).length,
  1,
  'MainPage must construct one serialized route owner',
);
assert.match(mainPage, /if \(!routeMountTokenRef\.current\) \{\s*routeMountTokenRef\.current = captureConnectionEpoch\(\);/);

const routeOwner = sliceBetween(
  mainPage,
  'const restorer = new BoundRouteRestorer({',
  'useEffect(() => {\n    const updateLocalStreamingIds',
);
assertOrdered(routeOwner, [
  'const restorer = new BoundRouteRestorer({',
  'routeRestorerRef.current = restorer;',
  'const initialRoute = restorer.submit(',
  'prepare: async (ownerToken) => {',
  'await useConversationStore.getState().loadConversations(ownerToken);',
  'connectionEpochRuntime.assertCurrent(ownerToken);',
  'bindBoundFrontendPopstate(',
  'restorer,',
  "(route) => navigateBoundFrontend(route, 'replace'),",
  'void initialRoute.catch(',
  'restorer.dispose();',
]);
assert.match(routeOwner, /selectConversation: async \(id, ownerToken\)[\s\S]*await useConversationStore\.getState\(\)\.selectConversation\(id, ownerToken\);[\s\S]*currentConversation\?\.id === id;/);
assert.match(routeOwner, /switchNode: async \(id, ownerToken\)[\s\S]*await useConversationStore\.getState\(\)\.switchNode\(id, ownerToken\);[\s\S]*state\.currentNodeId === id/);

const conversationSelection = sliceBetween(
  mainPage,
  'const handleSelectConversation = async',
  'const handleDeleteConversation = async',
);
assertOrdered(conversationSelection, [
  'await submitBoundRoute(',
  "{ kind: 'conversation', profileId, conversationId: id },",
  "'push',",
]);
assert.doesNotMatch(conversationSelection, /await selectConversation\(/);

const taskInspection = sliceBetween(
  mainPage,
  'const handleInspectTaskNotification = useCallback',
  'const handleCopyTranscriptItem = useCallback',
);
assert.match(taskInspection, /await submitBoundRoute\([\s\S]*kind: 'run'[\s\S]*result\.conversationId !== conversationId/);
assert.doesNotMatch(taskInspection, /setSelectedSideRunId\(/);
assert.doesNotMatch(taskInspection, /runsApi\.(?:listConversation|events)|resumeStream|restoreRunFromEvents/);

const interactiveRunPanelStart = mainPage.indexOf('const renderSideRunActions =');
assert.notEqual(interactiveRunPanelStart, -1, 'missing side-run action renderer');
const interactiveRunPanel = mainPage.slice(interactiveRunPanelStart);
assert.match(interactiveRunPanel, /closeSideRun\(\);/);
assert.match(interactiveRunPanel, /openSideRun\(step\.run\.runId\)/);
assert.match(interactiveRunPanel, /if \(nextRunId\) openSideRun\(nextRunId\);/);
assert.match(interactiveRunPanel, /onClick=\{\(\) => openSideRun\(item\.run\.runId\)\}/);
assert.doesNotMatch(interactiveRunPanel, /setSelectedSideRunId\(/);

assert.match(treeView, /onSelectNode: \(nodeId: string\) => Promise<void>/);
assert.match(treeView, /onDeleteNode: \(nodeId: string\) => Promise<void>/);
assert.match(treeView, /await onSelectNode\(nodeId\);/);
assert.match(treeView, /await onDeleteNode\(contextMenu\.nodeId\);/);
assert.doesNotMatch(treeView, /switchNode\(|navigateBoundFrontend\(/);

const nodeNavigation = sliceBetween(
  mainPage,
  'const deleteNodeAndMaintainRoute = useCallback',
  'const handleDeleteUserMessage = useCallback',
);
assert.match(nodeNavigation, /runBoundRouteIntent\(async \(token\)/);
assertOrdered(nodeNavigation, [
  'deleteNode(nodeId, token)',
  'connectionEpochRuntime.assertCurrent(token);',
  "navigateBoundFrontend(getCurrentConversationRoute(), 'replace');",
  'const handleSelectTreeNode = useCallback',
  'await submitBoundRoute({',
  "kind: 'node',",
  "}, 'replace');",
]);

assert.match(mainPage, /await useConversationStore\.getState\(\)\.clearCurrentConversation\(ownerToken\);/);
assert.match(mainPage, /await waitForRouteReadiness\([\s\S]*ROUTE_TRANSCRIPT_READY_TIMEOUT_MS[\s\S]*cancelActive/);
assert.match(mainPage, /const ROUTE_TRANSCRIPT_READY_TIMEOUT_MS = 10_000;/);
const restoredRouteApply = sliceBetween(
  mainPage,
  'const applyRestoredRoute = async',
  'const restorer = new BoundRouteRestorer({',
);
assert.match(restoredRouteApply, /await waitForRouteRender\([\s\S]*ROUTE_TRANSCRIPT_RENDER_TIMEOUT_MS[\s\S]*connectionEpochRuntime\.signalFor\(ownerToken\)[\s\S]*routeOwnerSignal/);
assert.doesNotMatch(restoredRouteApply, /new Promise<void>\(\(resolve\) => window\.requestAnimationFrame/);
assert.match(mainPage, /const openSideRun = useCallback[\s\S]*submitBoundRoute\(\{ kind: 'run'/);
assert.match(mainPage, /const closeSideRun = useCallback[\s\S]*runBoundRouteIntent\(/);
assert.match(mainPage, /<TreeView[\s\S]*onSelectNode=\{handleSelectTreeNode\}[\s\S]*onDeleteNode=\{handleDeleteTreeNode\}/);

console.log('profile navigation integration tests passed');
