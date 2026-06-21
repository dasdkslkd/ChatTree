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
  extractToolResultEnvelope,
  formatToolArguments,
  formatToolOutput,
  isToolResultError,
  summarizeToolCall,
} = require(path.join(__dirname, '../src/utils/toolDisplay.ts'));

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

function testExtractsContentEnvelopeAndFormatsPreview() {
  const toolMessage = {
    content: JSON.stringify({
      envelope: {
        tool_result_id: 'result-123',
        read_more: true,
        preview: JSON.stringify({
          command: 'node demo.js',
          exit_code: 0,
          stdout: 'full preview ok\n',
          stderr: '',
        }),
        total_chars: 24000,
        truncated: true,
      },
    }),
  };

  assert.equal(formatToolOutput(toolMessage), 'full preview ok\n');
  assert.deepEqual(extractToolResultEnvelope(toolMessage), {
    toolResultId: 'result-123',
    readMore: true,
    preview: JSON.stringify({
      command: 'node demo.js',
      exit_code: 0,
      stdout: 'full preview ok\n',
      stderr: '',
    }),
    totalChars: 24000,
    truncated: true,
  });
}

function testExtractsToolResultIdFromLegacyReadMore() {
  const toolMessage = {
    content: JSON.stringify({
      preview: 'short preview',
      read_more: 'read_tool_result({"tool_result_id":"legacy-42","offset":8000})',
    }),
  };

  assert.deepEqual(extractToolResultEnvelope(toolMessage), {
    toolResultId: 'legacy-42',
    readMore: 'read_tool_result({"tool_result_id":"legacy-42","offset":8000})',
    preview: 'short preview',
    totalChars: undefined,
    truncated: true,
  });
}

function testFormatsRawContentWhileExtractingModelEnvelope() {
  const toolMessage = {
    raw_content: JSON.stringify({
      command: 'pytest -q',
      exit_code: 0,
      stdout: 'raw stdout\n',
      stderr: '',
      timed_out: false,
    }),
    content: JSON.stringify({
      tool_result_id: 'result-raw',
      total_chars: 1200,
      truncated: false,
      preview: 'Command: pytest -q\nExit code: 0',
    }),
  };

  assert.equal(formatToolOutput(toolMessage), 'raw stdout\n');
  assert.deepEqual(extractToolResultEnvelope(toolMessage), {
    toolResultId: 'result-raw',
    readMore: undefined,
    preview: 'Command: pytest -q\nExit code: 0',
    totalChars: 1200,
    truncated: false,
  });
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

function testSummarizesCommonToolCalls() {
  assert.equal(
    summarizeToolCall('read_file', '{"path":"src/main.py"}'),
    '读取 src/main.py',
  );
  assert.equal(
    summarizeToolCall('run_command', '{"command":"pytest -q","timeout_seconds":30}'),
    '运行 pytest -q',
  );
  assert.equal(
    summarizeToolCall('edit_file', '{"path":"a.py","old_string":"x","new_string":"y"}'),
    '编辑 a.py · 精确替换',
  );
  assert.equal(
    summarizeToolCall('search_files', '{"query":"ToolManager","path":"backend"}'),
    '搜索 "ToolManager"',
  );
}

function testSummarizesPatchAndCompactArguments() {
  const patch = [
    '--- a/a.py',
    '+++ b/a.py',
    '@@ -1 +1 @@',
    '-old',
    '+new',
    '--- a/b.py',
    '+++ b/b.py',
    '@@ -1 +1 @@',
    '-old',
    '+new',
  ].join('\n');

  assert.equal(
    summarizeToolCall('apply_patch', JSON.stringify({ patch })),
    '应用补丁 · 2 个文件',
  );
  assert.equal(
    summarizeToolCall('run_command', 'pytest test/test_code_tools.py -q'),
    '运行 pytest test/test_code_tools.py -q',
  );
}

function testFormatsToolArguments() {
  assert.equal(
    formatToolArguments('{"path":"src/main.py","limit":20}'),
    '{\n  "path": "src/main.py",\n  "limit": 20\n}',
  );
}

function main() {
  testFormatsRunCommandStdout();
  testUnwrapsPreviewThenFormatsStdout();
  testExtractsContentEnvelopeAndFormatsPreview();
  testExtractsToolResultIdFromLegacyReadMore();
  testFormatsRawContentWhileExtractingModelEnvelope();
  testShowsStructuredErrorMessage();
  testSummarizesCommonToolCalls();
  testSummarizesPatchAndCompactArguments();
  testFormatsToolArguments();
  console.log('toolDisplay tests passed');
}

main();
