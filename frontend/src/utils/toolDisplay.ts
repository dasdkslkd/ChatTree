type ToolMessageLike = {
  content?: unknown;
  raw_content?: unknown;
  model_visible_content?: unknown;
  output?: unknown;
  result?: unknown;
  error?: unknown;
  envelope?: unknown;
  tool_result_id?: unknown;
};

export type ToolResultEnvelope = {
  toolResultId: string;
  readMore: unknown;
  preview: unknown;
  totalChars: number | undefined;
  truncated: boolean | undefined;
};

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function parseJsonString(value: unknown): unknown {
  if (typeof value !== 'string') return value;
  const trimmed = value.trim();
  if (!trimmed) return value;
  try {
    return JSON.parse(trimmed);
  } catch {
    return value;
  }
}

function stringifyDisplay(value: unknown): string {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function formatToolArguments(value: unknown): string {
  return stringifyDisplay(parseJsonString(value));
}

export function summarizeToolCall(toolName: string, rawArguments: unknown): string {
  const name = toolName || 'tool';
  const args = normalizeToolArgs(name, rawArguments);
  const path = stringArg(args, 'path');
  const command = stringArg(args, 'command');
  const query = stringArg(args, 'query');

  switch (name) {
    case 'read_file':
      return path ? `读取 ${path}` : '读取文件';
    case 'list_files':
      return summarizeListFiles(args);
    case 'search_files':
      return query ? `搜索 "${compact(query, 60)}"` : '搜索文件';
    case 'edit_file':
      return summarizeEditFile(args, path);
    case 'write_file':
      return summarizeWriteFile(args, path);
    case 'apply_patch':
      return summarizePatch(args);
    case 'run_command':
      return command ? `运行 ${compact(command, 80)}` : '运行命令';
    case 'web_search':
      return query ? `搜索网页 "${compact(query, 60)}"` : '搜索网页';
    case 'fetch_url':
      return stringArg(args, 'url') ? `读取 URL ${compact(stringArg(args, 'url') || '', 80)}` : '读取 URL';
    case 'read_tool_result':
      return '读取完整工具结果';
    case 'list_available_tools':
      return '列出可用工具';
    default:
      return summarizeFallback(name, args);
  }
}

function normalizeToolArgs(toolName: string, rawArguments: unknown): Record<string, unknown> {
  const parsed = parseJsonString(rawArguments);
  if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
    const record = { ...(parsed as Record<string, unknown>) };
    if (Object.keys(record).length === 1 && typeof record.arguments === 'string') {
      const nested = parseJsonString(record.arguments);
      if (nested && typeof nested === 'object' && !Array.isArray(nested)) {
        return normalizeToolArgs(toolName, nested);
      }
      return compactRecordForTool(toolName, record.arguments);
    }
    renameFirst(record, 'path', ['file_path', 'filepath', 'file']);
    renameFirst(record, 'command', ['cmd', 'script']);
    renameFirst(record, 'query', ['q', 'pattern']);
    renameFirst(record, 'old_string', ['old', 'oldString', 'old_text']);
    renameFirst(record, 'new_string', ['new', 'newString', 'new_text']);
    return record;
  }
  if (typeof parsed === 'string') return compactRecordForTool(toolName, parsed);
  return {};
}

function compactRecordForTool(toolName: string, value: string): Record<string, unknown> {
  if (toolName === 'run_command') return { command: value };
  if (toolName === 'read_file' || toolName === 'list_files') return { path: value };
  if (toolName === 'search_files' || toolName === 'web_search') return { query: value };
  if (toolName === 'apply_patch') return { patch: value };
  return { arguments: value };
}

function renameFirst(record: Record<string, unknown>, canonical: string, aliases: string[]): void {
  if (record[canonical] !== undefined) return;
  for (const alias of aliases) {
    if (record[alias] !== undefined) {
      record[canonical] = record[alias];
      delete record[alias];
      return;
    }
  }
}

function stringArg(record: Record<string, unknown>, key: string): string | null {
  const value = record[key];
  return typeof value === 'string' && value.trim() ? value : null;
}

function boolArg(record: Record<string, unknown>, key: string): boolean {
  return record[key] === true;
}

function compact(value: string, limit: number): string {
  const singleLine = value.replace(/\s+/g, ' ').trim();
  if (singleLine.length <= limit) return singleLine;
  return `${singleLine.slice(0, limit - 3)}...`;
}

function summarizeListFiles(args: Record<string, unknown>): string {
  const path = stringArg(args, 'path') || '.';
  const glob = stringArg(args, 'glob') || stringArg(args, 'pattern');
  const recursive = boolArg(args, 'recursive');
  const parts = [`列出 ${path}`];
  if (glob && glob !== '*') parts.push(glob);
  if (recursive) parts.push('递归');
  return parts.join(' · ');
}

function summarizeEditFile(args: Record<string, unknown>, path: string | null): string {
  const target = path || '文件';
  return boolArg(args, 'replace_all') ? `编辑 ${target} · 替换全部` : `编辑 ${target} · 精确替换`;
}

function summarizeWriteFile(args: Record<string, unknown>, path: string | null): string {
  const content = stringArg(args, 'content') || '';
  const suffix = content ? ` · ${content.length} 字符` : '';
  return `写入 ${path || '文件'}${suffix}`;
}

function summarizePatch(args: Record<string, unknown>): string {
  const patch = stringArg(args, 'patch') || '';
  const fileCount = new Set(
    patch
      .split('\n')
      .filter((line) => line.startsWith('+++ '))
      .map((line) => line.slice(4).replace(/^[ab]\//, '').trim())
      .filter(Boolean),
  ).size;
  return fileCount > 0 ? `应用补丁 · ${fileCount} 个文件` : '应用补丁';
}

function summarizeFallback(toolName: string, args: Record<string, unknown>): string {
  const path = stringArg(args, 'path');
  const query = stringArg(args, 'query');
  const command = stringArg(args, 'command');
  if (path) return `${toolName} · ${path}`;
  if (query) return `${toolName} · "${compact(query, 60)}"`;
  if (command) return `${toolName} · ${compact(command, 80)}`;
  return `调用 ${toolName}`;
}

function rawToolPayload(toolMessage?: ToolMessageLike | null): unknown {
  if (!toolMessage) return null;
  return toolMessage.raw_content ?? toolMessage.output ?? toolMessage.result ?? toolMessage.content ?? toolMessage.error;
}

function modelVisiblePayload(toolMessage?: ToolMessageLike | null): unknown {
  if (!toolMessage) return null;
  return toolMessage.model_visible_content ?? toolMessage.content;
}

function readMoreToolResultId(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const jsonStyle = value.match(/["']tool_result_id["']\s*:\s*["']([^"']+)["']/);
  if (jsonStyle?.[1]) return jsonStyle[1];
  const namedStyle = value.match(/tool_result_id\s*=\s*["']([^"']+)["']/);
  return namedStyle?.[1] ?? null;
}

function normalizeEnvelope(record: Record<string, unknown>): ToolResultEnvelope | null {
  const readMore = record.read_more;
  const toolResultId =
    (typeof record.tool_result_id === 'string' && record.tool_result_id) ||
    (typeof record.toolResultId === 'string' && record.toolResultId) ||
    readMoreToolResultId(readMore);
  const hasEnvelopeShape =
    Object.prototype.hasOwnProperty.call(record, 'preview') ||
    Object.prototype.hasOwnProperty.call(record, 'read_more') ||
    Object.prototype.hasOwnProperty.call(record, 'tool_result_id') ||
    Object.prototype.hasOwnProperty.call(record, 'total_chars') ||
    Object.prototype.hasOwnProperty.call(record, 'truncated');
  if (!hasEnvelopeShape || !toolResultId) return null;

  return {
    toolResultId,
    readMore,
    preview: record.preview,
    totalChars: typeof record.total_chars === 'number' ? record.total_chars : undefined,
    truncated: typeof record.truncated === 'boolean' ? record.truncated : Boolean(readMore) || undefined,
  };
}

function findToolResultEnvelope(value: unknown): ToolResultEnvelope | null {
  const parsed = parseJsonString(value);
  const record = asRecord(parsed);
  if (!record) return null;

  const direct = normalizeEnvelope(record);
  if (direct) return direct;

  const nested = asRecord(parseJsonString(record.envelope));
  return nested ? normalizeEnvelope(nested) : null;
}

function formatStructuredError(error: unknown): string {
  const record = asRecord(error);
  if (!record) return stringifyDisplay(error);
  const type = typeof record.type === 'string' && record.type ? record.type : 'Error';
  const message = typeof record.message === 'string' ? record.message : '';
  const toolName = typeof record.tool_name === 'string' ? record.tool_name : '';
  const command = typeof record.command === 'string' ? record.command : '';
  const target = toolName ? `${toolName} failed` : 'tool failed';
  const suffix = command ? ` while running \`${command}\`` : '';
  return message ? `${type}: ${message}` : `${type}: ${target}${suffix}`;
}

function formatCommandRecord(record: Record<string, unknown>): string | null {
  const hasCommandShape =
    Object.prototype.hasOwnProperty.call(record, 'stdout') ||
    Object.prototype.hasOwnProperty.call(record, 'stderr') ||
    Object.prototype.hasOwnProperty.call(record, 'exit_code') ||
    Object.prototype.hasOwnProperty.call(record, 'timed_out');
  if (!hasCommandShape) return null;

  const stdout = typeof record.stdout === 'string' ? record.stdout : '';
  const stderr = typeof record.stderr === 'string' ? record.stderr : '';
  const exitCode = record.exit_code;
  const timedOut = record.timed_out === true;

  if (stdout && stderr) return `${stdout}\n[stderr]\n${stderr}`;
  if (stdout) return stdout;
  if (stderr) return stderr;
  if (timedOut) return 'Command timed out.';
  if (typeof exitCode === 'number') return `exit_code: ${exitCode}`;
  return '';
}

function formatParsedPayload(payload: unknown, depth = 0): string {
  if (depth > 4) return stringifyDisplay(payload);
  const parsed = parseJsonString(payload);
  const record = asRecord(parsed);
  if (!record) return stringifyDisplay(parsed);

  if (Object.prototype.hasOwnProperty.call(record, 'error')) {
    return formatStructuredError(record.error);
  }

  if (Object.prototype.hasOwnProperty.call(record, 'envelope')) {
    return formatParsedPayload(record.envelope, depth + 1);
  }

  const commandOutput = formatCommandRecord(record);
  if (commandOutput !== null) return commandOutput;

  if (Object.prototype.hasOwnProperty.call(record, 'preview')) {
    return formatParsedPayload(record.preview, depth + 1);
  }
  if (Object.prototype.hasOwnProperty.call(record, 'content')) {
    return formatParsedPayload(record.content, depth + 1);
  }
  if (Object.prototype.hasOwnProperty.call(record, 'output')) {
    return formatParsedPayload(record.output, depth + 1);
  }
  if (Object.prototype.hasOwnProperty.call(record, 'result')) {
    return formatParsedPayload(record.result, depth + 1);
  }

  return stringifyDisplay(record);
}

export function formatToolOutput(toolMessage?: ToolMessageLike | null): string {
  return formatParsedPayload(rawToolPayload(toolMessage));
}

export function extractToolResultEnvelope(toolMessage?: ToolMessageLike | null): ToolResultEnvelope | null {
  if (!toolMessage) return null;
  return (
    findToolResultEnvelope(toolMessage.envelope) ||
    findToolResultEnvelope(modelVisiblePayload(toolMessage)) ||
    findToolResultEnvelope(rawToolPayload(toolMessage)) ||
    findToolResultEnvelope(toolMessage)
  );
}

export function isToolResultError(toolMessage?: ToolMessageLike | null): boolean {
  const parsed = parseJsonString(rawToolPayload(toolMessage));
  const record = asRecord(parsed);
  if (!record) return Boolean(toolMessage?.error);
  if (record.error || record.exception || record.traceback || record.success === false) return true;
  if (record.timed_out === true) return true;
  return typeof record.exit_code === 'number' && record.exit_code !== 0;
}
