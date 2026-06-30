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

  assert.equal(output, '命令: echo hello\n退出码: 0\n输出:\nhello');
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

  assert.equal(output, '命令: python demo.py\n退出码: 0\n输出:\ndemo ok');
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

  assert.equal(formatToolOutput(toolMessage), '命令: node demo.js\n退出码: 0\n输出:\nfull preview ok');
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

  assert.equal(formatToolOutput(toolMessage), '命令: pytest -q\n退出码: 0\n输出:\nraw stdout');
  assert.deepEqual(extractToolResultEnvelope(toolMessage), {
    toolResultId: 'result-raw',
    readMore: undefined,
    preview: 'Command: pytest -q\nExit code: 0',
    totalChars: 1200,
    truncated: false,
  });
}

function testFormatsSlimmedToolResultEnvelopeWithoutRawFields() {
  const toolMessage = {
    name: 'run_command',
    tool_call_id: 'call-1',
    tool_result_id: 'result-slim',
    content: JSON.stringify({
      tool_result_id: 'result-slim',
      total_chars: 36000,
      truncated: true,
      preview: JSON.stringify({
        command: 'npm test',
        exit_code: 0,
        stdout: 'slim preview ok\n',
        stderr: '',
      }),
    }),
  };

  assert.equal(formatToolOutput(toolMessage), '命令: npm test\n退出码: 0\n输出:\nslim preview ok');
  assert.deepEqual(extractToolResultEnvelope(toolMessage), {
    toolResultId: 'result-slim',
    readMore: undefined,
    preview: JSON.stringify({
      command: 'npm test',
      exit_code: 0,
      stdout: 'slim preview ok\n',
      stderr: '',
    }),
    totalChars: 36000,
    truncated: true,
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
    'path: src/main.py\nlimit: 20',
  );
}

function testFormatsSearchResultsAsReadableSummary() {
  const output = formatToolOutput({
    content: JSON.stringify({
      preview: JSON.stringify({
        results: [
          {
            title: 'OpenAI Docs',
            url: 'https://platform.openai.com/docs',
            content: 'Build with OpenAI models and tools.',
          },
          {
            title: 'API Reference',
            link: 'https://platform.openai.com/docs/api-reference',
            snippet: 'Reference for endpoints.',
          },
        ],
      }),
      tool_result_id: 'search-1',
      total_chars: 5000,
      truncated: true,
    }),
  });

  assert.equal(
    output,
    [
      '结果 1: OpenAI Docs',
      'https://platform.openai.com/docs',
      'Build with OpenAI models and tools.',
      '',
      '结果 2: API Reference',
      'https://platform.openai.com/docs/api-reference',
      'Reference for endpoints.',
    ].join('\n'),
  );
}

function testFormatsUnknownObjectAsKeyValueSummary() {
  const output = formatToolOutput({
    content: JSON.stringify({
      preview: JSON.stringify({
        filename: 'report.csv',
        rows: 42,
        columns: ['date', 'close'],
        nested: { ignored: true },
      }),
      tool_result_id: 'unknown-1',
      total_chars: 1000,
      truncated: false,
    }),
  });

  assert.equal(output, 'filename: report.csv\nrows: 42\ncolumns: date, close\nnested: {"ignored":true}');
}

function main() {
  testFormatsRunCommandStdout();
  testUnwrapsPreviewThenFormatsStdout();
  testExtractsContentEnvelopeAndFormatsPreview();
  testExtractsToolResultIdFromLegacyReadMore();
  testFormatsRawContentWhileExtractingModelEnvelope();
  testFormatsSlimmedToolResultEnvelopeWithoutRawFields();
  testShowsStructuredErrorMessage();
  testSummarizesCommonToolCalls();
  testSummarizesPatchAndCompactArguments();
  testFormatsToolArguments();
  testFormatsSearchResultsAsReadableSummary();
  testFormatsUnknownObjectAsKeyValueSummary();
  console.log('toolDisplay tests passed');
}

main();
