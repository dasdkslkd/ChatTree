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

const { parseFileMention, stripFileMention } = require(path.join(__dirname, '../src/utils/fileMention.ts'));

const MENTION_PREFIX = "'''USER MENTIONED FILES: a.txt b.txt '''\n\n这是附件说明\n---\n\n";
const PLAIN_CONTENT = '你好，世界';

function testParseFileMentionExtractsNamesAndCleanContent() {
  const result = parseFileMention(`${MENTION_PREFIX}${PLAIN_CONTENT}`);
  assert.deepEqual(result, { fileNames: ['a.txt', 'b.txt'], cleanContent: PLAIN_CONTENT });
}

function testParseFileMentionReturnsNullForPlainContent() {
  assert.equal(parseFileMention(PLAIN_CONTENT), null);
  assert.equal(parseFileMention(''), null);
}

function testParseFileMentionHandlesSingleFile() {
  const result = parseFileMention("'''USER MENTIONED FILES: c.png '''\n\n说明\n---\n\n正文");
  assert.deepEqual(result, { fileNames: ['c.png'], cleanContent: '正文' });
}

function testStripFileMentionRemovesPrefixOrKeepsContent() {
  assert.equal(stripFileMention(`${MENTION_PREFIX}${PLAIN_CONTENT}`), PLAIN_CONTENT);
  assert.equal(stripFileMention(PLAIN_CONTENT), PLAIN_CONTENT);
  assert.equal(stripFileMention(''), '');
}

(async () => {
  testParseFileMentionExtractsNamesAndCleanContent();
  testParseFileMentionReturnsNullForPlainContent();
  testParseFileMentionHandlesSingleFile();
  testStripFileMentionRemovesPrefixOrKeepsContent();
  console.log('file mention tests passed');
})().catch((error) => {
  console.error(error);
  process.exit(1);
});