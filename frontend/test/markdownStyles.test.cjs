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

function main() {
  testCodeBlocksAreConstrainedInsideMessageWidth();
  console.log('markdownStyles tests passed');
}

main();
