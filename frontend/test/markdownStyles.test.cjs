const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const css = fs.readFileSync(path.join(__dirname, '../src/App.css'), 'utf8');

function ruleBody(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`, 'm'));
  return match?.[1] || '';
}

function testCodeBlocksAreConstrainedInsideMessageWidth() {
  const wrapper = ruleBody('.code-block-wrapper');
  const highlighterPre = ruleBody('.prose .code-block-wrapper > pre');

  assert.match(wrapper, /max-width:\s*100%/);
  assert.match(wrapper, /box-sizing:\s*border-box/);
  assert.match(highlighterPre, /max-width:\s*100%/);
  assert.match(highlighterPre, /overflow-x:\s*auto/);
}

function testMathBlocksAreConstrainedInsideMessageWidth() {
  const katexDisplay = ruleBody('.prose .katex-display');

  assert.match(katexDisplay, /max-width:\s*100%/);
  assert.match(katexDisplay, /overflow-x:\s*auto/);
  assert.match(katexDisplay, /overflow-y:\s*hidden/);
}

function testBoxedMathUsesSoftThemeStyling() {
  const fbox = ruleBody('.prose .katex .fbox');
  const boxpad = ruleBody('.prose .katex .boxpad');

  assert.match(fbox, /border-color:\s*rgba\(217,\s*119,\s*87,\s*0\.42\)/);
  assert.match(fbox, /border-radius:\s*0\.28em/);
  assert.match(fbox, /box-shadow:\s*0 0 0 2px rgba\(217,\s*119,\s*87,\s*0\.08\)/);
  assert.doesNotMatch(boxpad, /background:/);
}

function main() {
  testCodeBlocksAreConstrainedInsideMessageWidth();
  testMathBlocksAreConstrainedInsideMessageWidth();
  testBoxedMathUsesSoftThemeStyling();
  console.log('markdownStyles tests passed');
}

main();
