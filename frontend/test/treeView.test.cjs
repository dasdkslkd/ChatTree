const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../src/pages/TreeView.tsx'), 'utf8');

function testRootDetectionUsesBackendFlag() {
  assert.match(source, /return node\.is_root === true/);
  assert.doesNotMatch(source, /return !getTreeUserContent\(node\) && !node\.assistant_content/);
}

function testEmptyNonRootNodeGetsPlanContinuationLabel() {
  assert.match(source, /return node\.is_root \? '对话开始' : '计划续跑'/);
  assert.match(source, /getTreeNodePrimaryText\(node\.data\)/);
}

testRootDetectionUsesBackendFlag();
testEmptyNonRootNodeGetsPlanContinuationLabel();
console.log('treeView tests passed');
