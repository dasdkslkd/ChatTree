type ToolMessageLike = {
  content?: unknown;
  output?: unknown;
  result?: unknown;
  error?: unknown;
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

function rawToolPayload(toolMessage?: ToolMessageLike | null): unknown {
  if (!toolMessage) return null;
  return toolMessage.content ?? toolMessage.output ?? toolMessage.result ?? toolMessage.error;
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

export function isToolResultError(toolMessage?: ToolMessageLike | null): boolean {
  const parsed = parseJsonString(rawToolPayload(toolMessage));
  const record = asRecord(parsed);
  if (!record) return Boolean(toolMessage?.error);
  if (record.error || record.exception || record.traceback || record.success === false) return true;
  if (record.timed_out === true) return true;
  return typeof record.exit_code === 'number' && record.exit_code !== 0;
}
