const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const sourceRoot = path.join(__dirname, '../src');
const sourceExtensions = new Set(['.ts', '.tsx']);

function listSourceFiles(dir) {
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  const files = [];

  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...listSourceFiles(fullPath));
    } else if (sourceExtensions.has(path.extname(entry.name))) {
      files.push(fullPath);
    }
  }

  return files;
}

function findNativeTitleAttributes(source, fileName = 'fixture.tsx') {
  const sourceFile = ts.createSourceFile(
    fileName,
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  const matches = [];

  function visit(node) {
    if (ts.isJsxAttribute(node) && node.name.text === 'title') {
      const openingElement = node.parent?.parent;
      if (ts.isJsxOpeningElement(openingElement) || ts.isJsxSelfClosingElement(openingElement)) {
        const tagName = openingElement.tagName;
        if (ts.isIdentifier(tagName) && /^[a-z]/.test(tagName.text)) {
          const { line, character } = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile));
          matches.push(`${fileName}:${line + 1}:${character + 1}`);
        }
      }
    }
    ts.forEachChild(node, visit);
  }

  visit(sourceFile);
  return matches;
}

function testDetectorRecognizesNativeTitleAttributes() {
  assert.deepEqual(
    findNativeTitleAttributes('<button title="Native help">Run</button>', 'native.tsx'),
    ['native.tsx:1:9'],
  );
}

function testDetectorIgnoresCustomComponentTitleProps() {
  assert.deepEqual(
    findNativeTitleAttributes('<CapabilityGroup\n  title="Skill"\n/>', 'custom.tsx'),
    [],
  );
}

function testNoNativeTitleAttributesRemain() {
  const matches = [];

  for (const file of listSourceFiles(sourceRoot)) {
    if (path.extname(file) !== '.tsx') continue;

    const source = fs.readFileSync(file, 'utf8');
    matches.push(...findNativeTitleAttributes(source, path.relative(sourceRoot, file)));
  }

  assert.deepEqual(matches, []);
}

function main() {
  testDetectorRecognizesNativeTitleAttributes();
  testDetectorIgnoresCustomComponentTitleProps();
  testNoNativeTitleAttributesRemain();
  console.log('noNativeTitleTooltip tests passed');
}

main();
