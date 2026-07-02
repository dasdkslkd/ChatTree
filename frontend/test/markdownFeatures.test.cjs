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

const { detectMarkdownFeatures } = require(path.join(__dirname, '../src/utils/markdownFeatures.ts'));

function testDetectsCommonMathDelimiters() {
  assert.deepEqual(detectMarkdownFeatures('inline $\\int x\\,dx$').hasMath, true);
  assert.deepEqual(detectMarkdownFeatures('block\n$$\n\\frac{1}{2}\n$$').hasMath, true);
  assert.deepEqual(detectMarkdownFeatures('inline \\(x + y\\)').hasMath, true);
  assert.deepEqual(detectMarkdownFeatures('display\n\\[\n\\begin{aligned}x&=1\\end{aligned}\n\\]').hasMath, true);
}

function testIgnoresMathInsideCode() {
  const markdown = [
    'Plain text.',
    '',
    '`$not_math$`',
    '',
    '```ts',
    'const value = "$also_not_math$";',
    '\\[not math\\]',
    '```',
  ].join('\n');

  assert.equal(detectMarkdownFeatures(markdown).hasMath, false);
}

function testDetectsMermaidAndRawHtml() {
  assert.equal(detectMarkdownFeatures('```mermaid\ngraph TD\nA-->B\n```').hasMermaid, true);
  assert.equal(detectMarkdownFeatures('<details><summary>x</summary>body</details>').hasRawHtml, true);
}

function testDoesNotTreatPlainAngleBracketsAsRawHtml() {
  assert.equal(detectMarkdownFeatures('1 < 2 and x > y').hasRawHtml, false);
}

function main() {
  testDetectsCommonMathDelimiters();
  testIgnoresMathInsideCode();
  testDetectsMermaidAndRawHtml();
  testDoesNotTreatPlainAngleBracketsAsRawHtml();
  console.log('markdownFeatures tests passed');
}

main();
