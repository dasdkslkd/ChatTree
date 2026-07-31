const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../src/pages/TreeView.tsx'), 'utf8');
const styles = fs.readFileSync(path.join(__dirname, '../src/App.css'), 'utf8');

function testRootDetectionUsesBackendFlag() {
  assert.match(source, /return node\.is_root === true/);
  assert.doesNotMatch(source, /return !getTreeUserContent\(node\) && !node\.assistant_content/);
}

function testEmptyNonRootNodeGetsPlanContinuationLabel() {
  assert.match(source, /return node\.is_root \? '对话开始' : '计划续跑'/);
  assert.match(source, /getTreeNodePrimaryText\(node\.data\)/);
}

function testActiveChatTargetRefreshesAndRendersInTree() {
  assert.match(source, /run\.kind === 'chat' && runNodeId/);
  assert.match(source, /loadTree\(conversationId\)/);
  assert.match(source, /activeChatRun\?\.content \|\| node\.data\.assistant_content/);
  assert.match(source, /className="tree-streaming-border"/);
  assert.match(source, /回复中/);
  assert.match(styles, /stroke-dasharray: 18 82/);
  assert.match(styles, /animation: tree-streaming-border-flow 1\.5s linear infinite/);
  assert.match(styles, /@media \(prefers-reduced-motion: reduce\)/);
}

testRootDetectionUsesBackendFlag();
testEmptyNonRootNodeGetsPlanContinuationLabel();
testActiveChatTargetRefreshesAndRendersInTree();
console.log('treeView tests passed');
