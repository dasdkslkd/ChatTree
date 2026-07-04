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

function testPlanMarkdownInlineCodeCanWrapInsideCard() {
  const planPanel = ruleBody('.plan-markdown-panel');
  const inlineCode = ruleBody('.plan-markdown-panel :not(pre) > code');

  assert.match(planPanel, /max-width:\s*100%/);
  assert.match(planPanel, /overflow-x:\s*hidden/);
  assert.match(inlineCode, /white-space:\s*normal/);
  assert.match(inlineCode, /overflow-wrap:\s*anywhere/);
  assert.match(inlineCode, /word-break:\s*break-word/);
  assert.match(inlineCode, /box-decoration-break:\s*clone/);
  assert.match(inlineCode, /-webkit-box-decoration-break:\s*clone/);
  assert.match(inlineCode, /border-radius:\s*0\.34em/);
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
  testPlanMarkdownInlineCodeCanWrapInsideCard();
  testBoxedMathUsesSoftThemeStyling();
  console.log('markdownStyles tests passed');
}

main();
