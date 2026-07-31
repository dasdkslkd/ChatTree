export type ToolStatus = 'done' | 'error' | 'running';
export type ToolArgs = Record<string, unknown>;
export type ToolResult = Record<string, unknown> | null;

export function tryParseJSON(text: string | null | undefined): unknown {
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

export function asObject(value: unknown): ToolResult {
  return typeof value === 'object' && value !== null ? value as Record<string, unknown> : null;
}

export function asString(value: unknown): string {
  return typeof value === 'string' ? value : value == null ? '' : String(value);
}

export function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

export function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max - 1)}…`;
}

export function singleLine(text: string): string {
  return text.replace(/\s+/g, ' ').trim();
}

export function getErrorMessage(result: ToolResult): string | null {
  if (!result) return null;
  const error = result.error;
  if (typeof error === 'object' && error !== null) {
    const message = (error as Record<string, unknown>).message;
    if (typeof message === 'string' && message) return message;
  }
  return typeof error === 'string' && error ? error : null;
}

export function summarizeToolCall(
  name: string,
  argsText: string,
  outputText: string,
  status: ToolStatus,
): string {
  const args = asObject(tryParseJSON(argsText)) || {};
  const result = asObject(tryParseJSON(outputText));

  if (name === 'shell') {
    const command = asString(args.command);
    return command ? truncate(singleLine(command), 80) : 'shell';
  }

  if (name === 'grep') {
    const pattern = asString(args.pattern);
    const path = asString(args.path);
    const count = result ? asNumber(result.count) : null;
    const output = asString(args.output) || asString(result?.output) || 'files';
    const head = pattern ? truncate(singleLine(pattern), 40) : 'grep';
    const tail = path && path !== '.' ? ` @ ${truncate(path, 30)}` : '';
    if (count === null) return `${head}${tail}`;
    const unit = output === 'content' ? '处匹配' : output === 'count' ? '项统计' : '个文件';
    return `${head}${tail} · ${count} ${unit}`;
  }

  if (name === 'glob') {
    const patterns = asArray(args.patterns).map((item) => asString(item)).filter(Boolean);
    const patternText = asString(args.pattern) || patterns.join(', ');
    const path = asString(args.path);
    const count = result ? asNumber(result.count) : null;
    const head = patternText ? truncate(singleLine(patternText), 40) : 'glob';
    const tail = path && path !== '.' ? ` @ ${truncate(path, 30)}` : '';
    return count === null ? `${head}${tail}` : `${head}${tail} · ${count} 个文件`;
  }

  if (name === 'read') {
    const targets = asArray(args.targets)
      .map((item) => asObject(item))
      .filter((item): item is Record<string, unknown> => item !== null);
    const targetPaths = targets.map((target) => asString(target.path)).filter(Boolean);
    const path = asString(args.path) || targetPaths[0] || 'read';
    const startLine = asNumber(args.start_line) ?? asNumber(targets[0]?.start_line);
    const lineCount = asNumber(args.line_count) ?? asNumber(targets[0]?.line_count);
    const range = startLine !== null && lineCount !== null
      ? ` L${startLine}-${startLine + lineCount - 1}`
      : '';
    return `${truncate(path, 50)}${range}`;
  }

  if (name === 'edit') {
    return truncate(asString(args.path) || asString(args.file_path) || 'file', 60);
  }

  if (name === 'web') {
    if (asString(args.action) === 'fetch' || (asString(args.url) && !asString(args.query))) {
      return truncate(asString(args.url), 80) || 'web';
    }
    const query = asString(args.query);
    const count = result ? asNumber(result.count) : null;
    return count === null ? truncate(query, 80) || 'web' : `${truncate(query, 60)} · ${count} 项`;
  }

  if (name === 'enter_plan_mode') {
    const permissionMode = asString(args.permission_mode);
    if (status === 'running') return permissionMode ? `进入计划模式（${permissionMode}）` : '进入计划模式';
    return permissionMode ? `计划模式 · ${permissionMode}` : '计划模式';
  }

  if (name === 'exit_plan_mode') {
    const plan = asString(args.plan);
    if (!plan) return status === 'running' ? '提交计划中...' : '计划';
    const title = plan
      .split(/\r?\n/)
      .map((line) => line.trim().replace(/^#+\s*/, ''))
      .find(Boolean) || '计划';
    return truncate(title, 80);
  }

  const errorMessage = getErrorMessage(result);
  if (errorMessage) return truncate(singleLine(errorMessage), 80);
  const candidate = asString(args.command)
    || asString(args.pattern)
    || asString(args.path)
    || asString(args.query)
    || asString(args.url);
  if (candidate) return truncate(singleLine(candidate), 80);
  return status === 'running' && !result ? '执行中...' : '工具调用';
}
