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

const { normalizeMathDelimiters } = require(path.join(__dirname, '../src/utils/normalizeMathDelimiters.ts'));

function testConvertsParenAndBracketDelimiters() {
  assert.equal(normalizeMathDelimiters('inline \\(x + y\\)'), 'inline $x + y$');
  assert.equal(
    normalizeMathDelimiters('display\n\\[\n\\frac{1}{2}\n\\]'),
    'display\n$$\n\\frac{1}{2}\n$$',
  );
}

function testLeavesCodeSegmentsUnchanged() {
  const markdown = [
    '`\\(not math\\)`',
    '',
    '```md',
    '\\[not math\\]',
    '```',
    '',
    '\\(real\\)',
  ].join('\n');

  assert.equal(
    normalizeMathDelimiters(markdown),
    [
      '`\\(not math\\)`',
      '',
      '```md',
      '\\[not math\\]',
      '```',
      '',
      '$real$',
    ].join('\n'),
  );
}

function testKeepsLatexLineBreakSpacingInsideMath() {
  const markdown = '\\[\n\\begin{aligned}\nx &= 1 \\\\[4pt]\ny &= 2\n\\end{aligned}\n\\]';
  const normalized = normalizeMathDelimiters(markdown);

  assert.match(normalized, /\\\\\[4pt\]/);
  assert.equal(normalized.startsWith('$$\n'), true);
  assert.equal(normalized.endsWith('\n$$'), true);
}

function main() {
  testConvertsParenAndBracketDelimiters();
  testLeavesCodeSegmentsUnchanged();
  testKeepsLatexLineBreakSpacingInsideMath();
  console.log('normalizeMathDelimiters tests passed');
}

main();
