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

const { formatToolOutput, isToolResultError } = require(path.join(__dirname, '../src/utils/toolDisplay.ts'));

function testFormatsRunCommandStdout() {
  const output = formatToolOutput({
    content: JSON.stringify({
      command: 'echo hello',
      cwd: '.',
      exit_code: 0,
      stdout: 'hello\n',
      stderr: '',
      timed_out: false,
    }),
  });

  assert.equal(output, 'hello\n');
}

function testUnwrapsPreviewThenFormatsStdout() {
  const output = formatToolOutput({
    content: JSON.stringify({
      preview: JSON.stringify({
        command: 'python demo.py',
        exit_code: 0,
        stdout: 'demo ok\n',
        stderr: '',
      }),
    }),
  });

  assert.equal(output, 'demo ok\n');
}

function testShowsStructuredErrorMessage() {
  const output = formatToolOutput({
    content: JSON.stringify({
      error: {
        type: 'NotImplementedError',
        message: '',
        tool_name: 'run_command',
        command: 'pwsh -c test',
      },
    }),
  });

  assert.equal(output, 'NotImplementedError: run_command failed while running `pwsh -c test`');
  assert.equal(isToolResultError({
    content: JSON.stringify({
      error: { type: 'NotImplementedError', tool_name: 'run_command' },
    }),
  }), true);
}

function main() {
  testFormatsRunCommandStdout();
  testUnwrapsPreviewThenFormatsStdout();
  testShowsStructuredErrorMessage();
  console.log('toolDisplay tests passed');
}

main();
