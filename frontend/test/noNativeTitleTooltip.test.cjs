const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

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

function testNoNativeTitleAttributesRemain() {
  const matches = [];

  for (const file of listSourceFiles(sourceRoot)) {
    if (path.extname(file) !== '.tsx') continue;

    const source = fs.readFileSync(file, 'utf8');
    source.split(/\r?\n/).forEach((line, index) => {
      if (/\btitle=/.test(line) && !/<CapabilityGroup\b/.test(line)) {
        matches.push(`${path.relative(sourceRoot, file)}:${index + 1}: ${line.trim()}`);
      }
    });
  }

  assert.deepEqual(matches, []);
}

function main() {
  testNoNativeTitleAttributesRemain();
  console.log('noNativeTitleTooltip tests passed');
}

main();
