const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const mainPage = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
const indexCss = fs.readFileSync(path.join(__dirname, '../src/index.css'), 'utf8');

function testMainPageHasAccessibleResizeSeparators() {
  assert.match(mainPage, /aria-label="调整左侧栏宽度"/);
  assert.match(mainPage, /aria-label="调整右侧栏宽度"/);
  assert.match(mainPage, /role="separator"/);
  assert.match(mainPage, /onPointerDown=\{\(event\) => beginSidebarResize\(event, 'left'\)\}/);
  assert.match(mainPage, /onPointerDown=\{\(event\) => beginSidebarResize\(event, 'right'\)\}/);
}

function testResizeHandlesHaveDedicatedStyles() {
  assert.match(indexCss, /\.sidebar-resize-handle/);
  assert.match(indexCss, /\.sidebar-resize-handle-left/);
  assert.match(indexCss, /\.sidebar-resize-handle-right/);
  assert.match(indexCss, /\.is-sidebar-resizing/);
}

function testVisibleResizeRuleSpansFullPanelHeight() {
  const visibleRule = indexCss.match(/\.sidebar-resize-handle::after\s*\{[^}]+\}/)?.[0] || '';
  assert.match(visibleRule, /top:\s*0;/);
  assert.match(visibleRule, /bottom:\s*0;/);
}

function testRightPanelUsesOneWidthAcrossViews() {
  assert.doesNotMatch(mainPage, /rightPanelView === 'side'\s*\?\s*rightSidePanelWidth\s*:\s*rightOutlinePanelWidth/);
  assert.doesNotMatch(mainPage, /rightOutlinePanelWidth|rightSidePanelWidth/);
  assert.match(mainPage, /rightPanelWidth/);
  assert.match(mainPage, /RIGHT_PANEL_WIDTH_STORAGE_KEY/);
}

function main() {
  testMainPageHasAccessibleResizeSeparators();
  testResizeHandlesHaveDedicatedStyles();
  testVisibleResizeRuleSpansFullPanelHeight();
  testRightPanelUsesOneWidthAcrossViews();
  console.log('sidebarResizeIntegration tests passed');
}

main();
