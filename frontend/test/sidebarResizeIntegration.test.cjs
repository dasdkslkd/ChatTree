const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const mainPage = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
const indexCss = fs.readFileSync(path.join(__dirname, '../src/index.css'), 'utf8');
const projectStorage = fs.readFileSync(path.join(__dirname, '../src/utils/projectStorage.ts'), 'utf8');

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
  assert.match(mainPage, /PROFILE_RIGHT_PANEL_STORAGE_KEY/);
}

function testEveryMainPagePersistenceKeyIsProfileScoped() {
  assert.match(mainPage, /const PROFILE_ID = getProfileContext\(\)\.profileId;/);
  for (const key of [
    'LEFT_SIDEBAR_STORAGE_KEY',
    'RIGHT_PANEL_STORAGE_KEY',
  ]) {
    assert.match(
      mainPage,
      new RegExp(`profileStorageKey\\(PROFILE_ID, ${key}\\)`),
      `${key} must be scoped by the immutable route Profile`,
    );
  }
  assert.match(projectStorage, /const PROFILE_ID = getProfileContext\(\)\.profileId;/);
  for (const key of [
    'MANUAL_PROJECTS_STORAGE_KEY',
    'PROJECT_ORDER_STORAGE_KEY',
  ]) {
    assert.match(
      projectStorage,
      new RegExp(`profileStorageKey\\(PROFILE_ID, ${key}\\)`),
      `${key} must be scoped by the immutable route Profile`,
    );
  }
  for (const key of [
    'PROFILE_MANUAL_PROJECTS_STORAGE_KEY',
    'PROFILE_PROJECT_ORDER_STORAGE_KEY',
  ]) {
    assert.match(projectStorage, new RegExp(`localStorage\\.getItem\\(${key}\\)`));
    assert.match(projectStorage, new RegExp(`localStorage\\.setItem\\(${key},`));
  }
  for (const key of [
    'PROFILE_LEFT_SIDEBAR_STORAGE_KEY',
    'PROFILE_RIGHT_PANEL_STORAGE_KEY',
  ]) {
    assert.match(
      mainPage,
      new RegExp(`readStoredSidebarWidth\\(getBrowserStorage\\(\\),\\s*${key},`),
    );
    assert.match(mainPage, new RegExp(`storageKey:\\s*${key}`));
    assert.match(
      mainPage,
      new RegExp(`writeStoredSidebarWidth\\(\\s*getBrowserStorage\\(\\),\\s*${key},`),
    );
  }
  assert.doesNotMatch(
    mainPage,
    /(?:getItem|setItem)\((?:MANUAL_PROJECTS_STORAGE_KEY|PROJECT_ORDER_STORAGE_KEY)/,
  );
  assert.doesNotMatch(
    projectStorage,
    /(?:getItem|setItem)\((?:MANUAL_PROJECTS_STORAGE_KEY|PROJECT_ORDER_STORAGE_KEY)/,
  );
}

function main() {
  testMainPageHasAccessibleResizeSeparators();
  testResizeHandlesHaveDedicatedStyles();
  testVisibleResizeRuleSpansFullPanelHeight();
  testRightPanelUsesOneWidthAcrossViews();
  testEveryMainPagePersistenceKeyIsProfileScoped();
  console.log('sidebarResizeIntegration tests passed');
}

main();
