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
  LEFT_SIDEBAR_WIDTH,
  RIGHT_PANEL_WIDTH,
  clampSidebarWidth,
  getPointerResizedSidebarWidth,
  getKeyboardResizedSidebarWidth,
  readStoredSidebarWidth,
  writeStoredSidebarWidth,
} = require(path.join(__dirname, '../src/utils/sidebarResize.ts'));

function createMemoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem(key) {
      return values.has(key) ? values.get(key) : null;
    },
    setItem(key, value) {
      values.set(key, String(value));
    },
  };
}

function testClampsConfiguredWidths() {
  assert.equal(clampSidebarWidth(120, LEFT_SIDEBAR_WIDTH), 220);
  assert.equal(clampSidebarWidth(640, LEFT_SIDEBAR_WIDTH), 520);
  assert.equal(clampSidebarWidth(360, LEFT_SIDEBAR_WIDTH), 360);
}

function testPointerResizeDirectionMatchesSidebarEdge() {
  assert.equal(getPointerResizedSidebarWidth('left', 300, 100, 148, LEFT_SIDEBAR_WIDTH), 348);
  assert.equal(getPointerResizedSidebarWidth('left', 300, 100, 52, LEFT_SIDEBAR_WIDTH), 252);
  assert.equal(getPointerResizedSidebarWidth('right', 280, 500, 452, RIGHT_PANEL_WIDTH), 328);
  assert.equal(getPointerResizedSidebarWidth('right', 280, 500, 548, RIGHT_PANEL_WIDTH), 240);
}

function testKeyboardResizeDirectionMatchesSidebarEdge() {
  assert.equal(getKeyboardResizedSidebarWidth('left', 'ArrowRight', 300, LEFT_SIDEBAR_WIDTH), 316);
  assert.equal(getKeyboardResizedSidebarWidth('left', 'ArrowLeft', 300, LEFT_SIDEBAR_WIDTH), 284);
  assert.equal(getKeyboardResizedSidebarWidth('right', 'ArrowLeft', 280, RIGHT_PANEL_WIDTH), 296);
  assert.equal(getKeyboardResizedSidebarWidth('right', 'ArrowRight', 280, RIGHT_PANEL_WIDTH), 264);
  assert.equal(getKeyboardResizedSidebarWidth('right', 'Home', 300, RIGHT_PANEL_WIDTH), 240);
  assert.equal(getKeyboardResizedSidebarWidth('right', 'End', 300, RIGHT_PANEL_WIDTH), 680);
  assert.equal(getKeyboardResizedSidebarWidth('right', 'Enter', 300, RIGHT_PANEL_WIDTH), 300);
}

function testStoredWidthFallsBackForMissingOrInvalidValues() {
  const storage = createMemoryStorage({
    badText: 'wide',
    badNumber: '-10',
    tooLarge: '9999',
    valid: '444',
  });

  assert.equal(readStoredSidebarWidth(storage, 'missing', RIGHT_PANEL_WIDTH), 280);
  assert.equal(readStoredSidebarWidth(storage, 'badText', RIGHT_PANEL_WIDTH), 280);
  assert.equal(readStoredSidebarWidth(storage, 'badNumber', RIGHT_PANEL_WIDTH), 280);
  assert.equal(readStoredSidebarWidth(storage, 'tooLarge', RIGHT_PANEL_WIDTH), 680);
  assert.equal(readStoredSidebarWidth(storage, 'valid', RIGHT_PANEL_WIDTH), 444);
}

function testWriteStoredWidthPersistsClampedInteger() {
  const storage = createMemoryStorage();
  writeStoredSidebarWidth(storage, 'width', 519.7, LEFT_SIDEBAR_WIDTH);
  assert.equal(storage.getItem('width'), '520');
}

function main() {
  testClampsConfiguredWidths();
  testPointerResizeDirectionMatchesSidebarEdge();
  testKeyboardResizeDirectionMatchesSidebarEdge();
  testStoredWidthFallsBackForMissingOrInvalidValues();
  testWriteStoredWidthPersistsClampedInteger();
  console.log('sidebarResize tests passed');
}

main();
