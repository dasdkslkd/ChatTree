import { useMemo, useState, type ReactNode } from 'react';
import {
  Check,
  ChevronRight,
  ClipboardList,
  Copy,
  FileText,
  FilePlus,
  FileSearch,
  Globe,
  Pencil,
  Search,
  Terminal,
  Wrench,
  X,
  type LucideIcon,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import MarkdownContent from '../../MarkdownContent';
import type { ToolRenderItem } from './AssistantProcessTimeline';

type ToolStatus = ToolRenderItem['status'];

type ToolArgs = Record<string, unknown>;
type ToolResult = Record<string, unknown> | null;

interface ToolSpec {
  icon: LucideIcon;
  summary: (args: ToolArgs, result: ToolResult, status: ToolStatus) => string;
  detail: (args: ToolArgs, result: ToolResult, status: ToolStatus) => ReactNode;
}

function tryParseJSON(text: string | null | undefined): unknown {
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function asObject(value: unknown): ToolResult {
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : null;
}

function asString(value: unknown): string {
  return typeof value === 'string' ? value : value == null ? '' : String(value);
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max - 1)}…`;
}

function singleLine(text: string): string {
  return text.replace(/\s+/g, ' ').trim();
}

function getErrorMessage(result: ToolResult): string | null {
  if (!result) return null;
  const err = result.error;
  if (typeof err === 'object' && err !== null) {
    const message = (err as Record<string, unknown>).message;
    if (typeof message === 'string' && message) return message;
  }
  if (typeof err === 'string' && err) return err;
  return null;
}

function CopyButton({ text, label, variant = 'default' }: { text: string; label: string; variant?: 'default' | 'subtle' }) {
  const [copied, setCopied] = useState(false);
  if (!text) return null;
  const handleCopy = () => {
    void navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };
  return (
    <button
      type="button"
      className={cn('tc-copy', variant === 'subtle' && 'tc-copy-subtle')}
      onClick={handleCopy}
      aria-label={label}
    >
      {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
      <span>{copied ? '已复制' : label}</span>
    </button>
  );
}

function MetaRow({ items }: { items: Array<{ label: string; value: ReactNode; tone?: 'default' | 'error' | 'muted' }> }) {
  const visible = items.filter((item) => item.value !== null && item.value !== undefined && item.value !== '');
  if (visible.length === 0) return null;
  return (
    <div className="tc-meta">
      {visible.map((item, index) => (
        <span key={index} className={cn('tc-meta-item', item.tone === 'error' && 'tc-meta-error', item.tone === 'muted' && 'tc-meta-muted')}>
          <span className="tc-meta-label">{item.label}</span>
          <span className="tc-meta-value">{item.value}</span>
        </span>
      ))}
    </div>
  );
}

function PreBlock({ children, variant = 'output', maxHeight = 240 }: { children: ReactNode; variant?: 'cmd' | 'output' | 'error'; maxHeight?: number }) {
  return (
    <pre
      className={cn('tc-pre', variant === 'cmd' && 'tc-pre-cmd', variant === 'error' && 'tc-pre-error')}
      style={{ maxHeight }}
    >
      {children}
    </pre>
  );
}

function EmptyState({ text }: { text: string }) {
  return <div className="tc-empty">{text}</div>;
}

function shellSpec(): ToolSpec {
  return {
    icon: Terminal,
    summary: (args) => {
      const cmd = asString(args.command);
      if (!cmd) return 'shell';
      return truncate(singleLine(cmd), 80);
    },
    detail: (args, result, status) => {
      const command = asString(args.command);
      const cwd = asString(args.cwd);
      const errorMessage = getErrorMessage(result);
      if (errorMessage) {
        return (
          <>
            {command && (
              <>
                <CopyButton text={command} label="复制命令" />
                <PreBlock variant="cmd">{command}</PreBlock>
              </>
            )}
            <PreBlock variant="error">{errorMessage}</PreBlock>
          </>
        );
      }
      const exitCode = result ? asNumber(result.exit_code) : null;
      const timedOut = result ? Boolean(result.timed_out) : false;
      const stdout = result ? asString(result.stdout) : '';
      const stderr = result ? asString(result.stderr) : '';
      const hasError = exitCode !== null && exitCode !== 0;
      return (
        <>
          {command && (
            <>
              <CopyButton text={command} label="复制命令" />
              <PreBlock variant="cmd">{command}</PreBlock>
            </>
          )}
          {status === 'running' && !result && <EmptyState text="命令执行中..." />}
          {result && (
            <MetaRow
              items={[
                { label: '退出码', value: exitCode === null ? '' : String(exitCode), tone: hasError ? 'error' : 'default' },
                { label: 'cwd', value: cwd && cwd !== '.' ? cwd : '' },
                { label: '超时', value: timedOut ? '是' : '', tone: 'error' },
              ]}
            />
          )}
          {stdout && (
            <>
              <CopyButton text={stdout} label="复制 stdout" variant="subtle" />
              <PreBlock variant="output">{stdout}</PreBlock>
            </>
          )}
          {stderr && (
            <>
              <CopyButton text={stderr} label="复制 stderr" variant="subtle" />
              <PreBlock variant="error">{stderr}</PreBlock>
            </>
          )}
          {result && !stdout && !stderr && !errorMessage && exitCode === 0 && <EmptyState text="命令执行完成（无输出）" />}
        </>
      );
    },
  };
}

function grepSpec(): ToolSpec {
  return {
    icon: Search,
    summary: (args, result) => {
      const pattern = asString(args.pattern);
      const path = asString(args.path);
      const count = result ? asNumber(result.count) : null;
      const output = asString(args.output) || asString(result?.output) || 'files';
      const head = pattern ? truncate(singleLine(pattern), 40) : 'grep';
      const tail = path && path !== '.' ? ` @ ${truncate(path, 30)}` : '';
      if (count !== null) {
        const unit = output === 'content' ? '处匹配' : output === 'count' ? '项统计' : '个文件';
        return `${head}${tail} · ${count} ${unit}`;
      }
      return `${head}${tail}`;
    },
    detail: (args, result, status) => {
      const pattern = asString(args.pattern);
      const path = asString(args.path);
      const output = asString(args.output) || asString(result?.output) || 'files';
      const errorMessage = getErrorMessage(result);
      if (errorMessage) {
        return (
          <>
            {pattern && <PreBlock variant="cmd">/{pattern}/</PreBlock>}
            <PreBlock variant="error">{errorMessage}</PreBlock>
          </>
        );
      }
      const matches = asArray(result?.matches).map((item) => asObject(item) || {});
      const files = asArray(result?.files).map((item) => asString(item));
      const counts = asArray(result?.counts).map((item) => asObject(item) || {});
      const count = asNumber(result?.count) ?? 0;
      const truncated = Boolean(result?.truncated);
      const nextOffset = asNumber(result?.next_offset);
      return (
        <>
          <MetaRow
            items={[
              { label: '正则', value: pattern, tone: 'muted' },
              { label: 'cwd', value: path && path !== '.' ? path : '' },
              { label: '模式', value: output },
              { label: '匹配', value: count > 0 ? String(count) : '', tone: 'muted' },
            ]}
          />
          {status === 'running' && !result && <EmptyState text="搜索中..." />}
          {output === 'content' && matches.length > 0 && (
            <div className="tc-match-list custom-scrollbar">
              {matches.map((match, index) => {
                const matchPath = asString(match.path);
                const matchLine = asNumber(match.line);
                const matchText = asString(match.text);
                const matchType = asString(match.type);
                return (
                  <div key={`${matchPath}:${matchLine ?? index}`} className={cn('tc-match-item', matchType === 'context' && 'tc-match-context')}>
                    <span className="tc-match-path">{matchPath}</span>
                    {matchLine !== null && <span className="tc-match-line">:{matchLine}</span>}
                    <span className="tc-match-text">{matchText}</span>
                  </div>
                );
              })}
            </div>
          )}
          {output === 'files' && files.length > 0 && (
            <div className="tc-file-list custom-scrollbar">
              {files.map((file, index) => (
                <div key={`${file}:${index}`} className="tc-file-item">{file}</div>
              ))}
            </div>
          )}
          {output === 'count' && counts.length > 0 && (
            <div className="tc-file-list custom-scrollbar">
              {counts.map((item, index) => {
                const cPath = asString(item.path);
                const cCount = asNumber(item.count);
                return (
                  <div key={`${cPath}:${index}`} className="tc-file-item">
                    <span className="tc-match-path">{cPath}</span>
                    {cCount !== null && <span className="tc-match-line"> · {cCount}</span>}
                  </div>
                );
              })}
            </div>
          )}
          {result && count === 0 && !errorMessage && <EmptyState text="无匹配结果" />}
          {truncated && nextOffset !== null && (
            <div className="tc-truncated">结果已截断，next_offset={nextOffset}</div>
          )}
        </>
      );
    },
  };
}

function globSpec(): ToolSpec {
  return {
    icon: FileSearch,
    summary: (args, result) => {
      const patterns = asArray(args.patterns).map((item) => asString(item)).filter(Boolean);
      const singlePattern = asString(args.pattern);
      const patternText = singlePattern || patterns.join(', ');
      const path = asString(args.path);
      const count = result ? asNumber(result.count) : null;
      const head = patternText ? truncate(singleLine(patternText), 40) : 'glob';
      const tail = path && path !== '.' ? ` @ ${truncate(path, 30)}` : '';
      if (count !== null) return `${head}${tail} · ${count} 个文件`;
      return `${head}${tail}`;
    },
    detail: (args, result, status) => {
      const patterns = asArray(args.patterns).map((item) => asString(item)).filter(Boolean);
      const singlePattern = asString(args.pattern);
      const path = asString(args.path);
      const sort = asString(args.sort);
      const errorMessage = getErrorMessage(result);
      if (errorMessage) {
        return <PreBlock variant="error">{errorMessage}</PreBlock>;
      }
      const files = asArray(result?.files).map((item) => asString(item)).filter(Boolean);
      const count = asNumber(result?.count) ?? files.length;
      const truncated = Boolean(result?.truncated);
      const nextOffset = asNumber(result?.next_offset);
      const totalKnown = result ? result.total_known : undefined;
      const observed = asNumber(result?.observed_count);
      return (
        <>
          <MetaRow
            items={[
              { label: '模式', value: singlePattern || patterns.join(', '), tone: 'muted' },
              { label: 'cwd', value: path && path !== '.' ? path : '' },
              { label: '排序', value: sort && sort !== 'discovery' ? sort : '' },
              { label: '文件数', value: count > 0 ? String(count) : '', tone: 'muted' },
              { label: '已观察', value: observed !== null && totalKnown === false ? String(observed) : '' },
            ]}
          />
          {status === 'running' && !result && <EmptyState text="搜索中..." />}
          {files.length > 0 && (
            <div className="tc-file-list custom-scrollbar">
              {files.map((file, index) => (
                <div key={`${file}:${index}`} className="tc-file-item">{file}</div>
              ))}
            </div>
          )}
          {result && files.length === 0 && !errorMessage && <EmptyState text="无匹配文件" />}
          {truncated && nextOffset !== null && (
            <div className="tc-truncated">结果已截断，next_offset={nextOffset}</div>
          )}
        </>
      );
    },
  };
}

function readSpec(): ToolSpec {
  return {
    icon: FileText,
    summary: (args) => {
      const targets = asArray(args.targets)
        .map((item) => asObject(item))
        .filter((item): item is Record<string, unknown> => item !== null);
      const targetPaths = targets.map((target) => asString(target.path)).filter(Boolean);
      const singlePath = asString(args.path);
      const path = singlePath || targetPaths[0] || 'read';
      const startLine = asNumber(args.start_line) ?? asNumber(targets[0]?.start_line);
      const lineCount = asNumber(args.line_count) ?? asNumber(targets[0]?.line_count);
      const range = startLine !== null && lineCount !== null ? ` L${startLine}-${startLine + lineCount - 1}` : '';
      return `${truncate(path, 50)}${range}`;
    },
    detail: (_args, result, status) => {
      const errorMessage = getErrorMessage(result);
      if (errorMessage) {
        return <PreBlock variant="error">{errorMessage}</PreBlock>;
      }
      const filesField = result ? asArray(result.files).map((item) => asObject(item)).filter(Boolean) : [];
      const singleFile = result && !Array.isArray(result.files) ? result : null;
      const renderFile = (file: ToolResult, index: number) => {
        const path = asString(file?.path);
        const content = asString(file?.content);
        const startLine = asNumber(file?.start_line);
        const lineCount = asNumber(file?.line_count);
        const fileError = getErrorMessage(file);
        return (
          <div key={`${path}:${index}`} className="tc-file-block">
            {path && (
              <MetaRow
                items={[
                  { label: '文件', value: truncate(path, 80) },
                  { label: '行', value: startLine !== null && lineCount !== null ? `${startLine}-${startLine + lineCount - 1}` : '' },
                ]}
              />
            )}
            {fileError && <PreBlock variant="error">{fileError}</PreBlock>}
            {content && <PreBlock variant="output">{content}</PreBlock>}
            {!content && !fileError && <EmptyState text="文件为空" />}
          </div>
        );
      };
      return (
        <>
          {status === 'running' && !result && <EmptyState text="读取中..." />}
          {filesField.length > 0 && filesField.map((file, index) => renderFile(file, index))}
          {singleFile && !filesField.length && renderFile(singleFile, 0)}
        </>
      );
    },
  };
}

function fileEditSpec(icon: LucideIcon): ToolSpec {
  return {
    icon,
    summary: (args) => {
      const path = asString(args.path) || asString(args.file_path) || 'file';
      return truncate(path, 60);
    },
    detail: (args, result, status) => {
      const path = asString(args.path) || asString(args.file_path);
      const errorMessage = getErrorMessage(result);
      if (errorMessage) {
        return (
          <>
            {path && <MetaRow items={[{ label: '文件', value: truncate(path, 80) }]} />}
            <PreBlock variant="error">{errorMessage}</PreBlock>
          </>
        );
      }
      const content = asString(result?.content);
      const hunks = asArray(result?.hunks);
      const applied = result ? Boolean(result.applied ?? result.success) : null;
      const summary = result ? asString(result.summary) : '';
      return (
        <>
          {path && <MetaRow items={[{ label: '文件', value: truncate(path, 80) }, { label: '状态', value: applied === true ? '已应用' : applied === false ? '未应用' : '', tone: applied === false ? 'error' : 'default' }]} />}
          {status === 'running' && !result && <EmptyState text="执行中..." />}
          {summary && <PreBlock variant="output">{summary}</PreBlock>}
          {hunks.length > 0 && (
            <PreBlock variant="output">{hunks.map((hunk) => asString(asObject(hunk)?.text || asObject(hunk)?.content)).join('\n')}</PreBlock>
          )}
          {content && <PreBlock variant="output">{content}</PreBlock>}
          {result && !content && !summary && hunks.length === 0 && !errorMessage && <PreBlock variant="output">{JSON.stringify(result, null, 2)}</PreBlock>}
        </>
      );
    },
  };
}

function fetchUrlSpec(): ToolSpec {
  return {
    icon: Globe,
    summary: (args) => {
      const url = asString(args.url);
      if (!url) return 'fetch_url';
      return truncate(url, 80);
    },
    detail: (args, result, status) => {
      const url = asString(args.url);
      const errorMessage = getErrorMessage(result);
      if (errorMessage) {
        return (
          <>
            {url && <PreBlock variant="cmd">{url}</PreBlock>}
            <PreBlock variant="error">{errorMessage}</PreBlock>
          </>
        );
      }
      const content = asString(result?.content) || asString(result?.text);
      const title = asString(result?.title);
      const statusCode = asNumber(result?.status_code);
      return (
        <>
          {url && (
            <>
              <CopyButton text={url} label="复制 URL" />
              <PreBlock variant="cmd">{url}</PreBlock>
            </>
          )}
          {status === 'running' && !result && <EmptyState text="抓取中..." />}
          {result && (
            <MetaRow
              items={[
                { label: '状态码', value: statusCode !== null ? String(statusCode) : '' },
                { label: '标题', value: title },
              ]}
            />
          )}
          {content && (
            <>
              <CopyButton text={content} label="复制内容" variant="subtle" />
              <PreBlock variant="output">{content}</PreBlock>
            </>
          )}
        </>
      );
    },
  };
}

function webSearchSpec(): ToolSpec {
  return {
    icon: Search,
    summary: (args, result) => {
      const query = asString(args.query);
      const count = result ? asNumber(result.count) : null;
      if (count !== null) return `${truncate(query, 60)} · ${count} 项`;
      return truncate(query, 80) || 'web_search';
    },
    detail: (args, result, status) => {
      const query = asString(args.query);
      const errorMessage = getErrorMessage(result);
      if (errorMessage) {
        return <PreBlock variant="error">{errorMessage}</PreBlock>;
      }
      const results = asArray(result?.results)
        .map((item) => asObject(item))
        .filter((item): item is Record<string, unknown> => item !== null);
      return (
        <>
          {query && <MetaRow items={[{ label: '查询', value: truncate(query, 80), tone: 'muted' }]} />}
          {status === 'running' && !result && <EmptyState text="搜索中..." />}
          {results.length > 0 && (
            <div className="tc-result-list custom-scrollbar">
              {results.map((item, index) => {
                const title = asString(item.title);
                const url = asString(item.url) || asString(item.link);
                const snippet = asString(item.snippet) || asString(item.description);
                return (
                  <div key={`${url}:${index}`} className="tc-result-item">
                    {title && <div className="tc-result-title">{title}</div>}
                    {url && <div className="tc-result-url">{url}</div>}
                    {snippet && <div className="tc-result-snippet">{snippet}</div>}
                  </div>
                );
              })}
            </div>
          )}
          {result && results.length === 0 && !errorMessage && <EmptyState text="无搜索结果" />}
        </>
      );
    },
  };
}

function planModeSpec(): ToolSpec {
  return {
    icon: ClipboardList,
    summary: (args, _result, status) => {
      const permissionMode = asString(args.permission_mode);
      if (status === 'running') return permissionMode ? `进入计划模式（${permissionMode}）` : '进入计划模式';
      return permissionMode ? `计划模式 · ${permissionMode}` : '计划模式';
    },
    detail: (args, _result, status) => {
      const permissionMode = asString(args.permission_mode);
      const errorMessage = getErrorMessage(_result);
      if (errorMessage) return <PreBlock variant="error">{errorMessage}</PreBlock>;
      return (
        <>
          <MetaRow items={[{ label: '权限模式', value: permissionMode || 'plan', tone: 'muted' }]} />
          {status === 'running' && <EmptyState text="正在进入计划模式..." />}
          {!errorMessage && status !== 'running' && <EmptyState text="已进入计划模式，仅允许只读与计划类工具" />}
        </>
      );
    },
  };
}

function exitPlanModeSpec(): ToolSpec {
  return {
    icon: ClipboardList,
    summary: (args, _result, status) => {
      const plan = asString(args.plan);
      if (!plan) return status === 'running' ? '提交计划中...' : '计划';
      const title = plan.split(/\r?\n/).map((line) => line.trim().replace(/^#+\s*/, '')).find(Boolean) || '计划';
      return truncate(title, 80);
    },
    detail: (args, result, status) => {
      const plan = asString(args.plan);
      const errorMessage = getErrorMessage(result);
      if (errorMessage) {
        return (
          <>
            {plan && (
              <div className="tc-plan-body">
                <MarkdownContent enableMermaid>{plan}</MarkdownContent>
              </div>
            )}
            <PreBlock variant="error">{errorMessage}</PreBlock>
          </>
        );
      }
      const planId = asString(result?.plan_id);
      const planStatus = asString(result?.status);
      return (
        <>
          {plan && (
            <div className="tc-plan-body">
              <MarkdownContent enableMermaid>{plan}</MarkdownContent>
            </div>
          )}
          {result && (
            <MetaRow
              items={[
                { label: 'plan_id', value: planId, tone: 'muted' },
                { label: '状态', value: planStatus === 'awaiting_approval' ? '等待批准' : planStatus === 'approved' ? '已批准' : planStatus === 'rejected' ? '已拒绝' : planStatus },
              ]}
            />
          )}
          {status === 'running' && !result && <EmptyState text="提交计划中..." />}
        </>
      );
    },
  };
}

function defaultSpec(): ToolSpec {
  return {
    icon: Wrench,
    summary: (args, result, status) => {
      const errorMessage = getErrorMessage(result);
      if (errorMessage) return truncate(singleLine(errorMessage), 80);
      const candidate = asString(args.command) || asString(args.pattern) || asString(args.path) || asString(args.query) || asString(args.url);
      if (candidate) return truncate(singleLine(candidate), 80);
      if (status === 'running' && !result) return '执行中...';
      return '工具调用';
    },
    detail: (args, result, status) => {
      const errorMessage = getErrorMessage(result);
      const argsText = Object.keys(args).length > 0 ? JSON.stringify(args, null, 2) : '';
      const resultText = result ? JSON.stringify(result, null, 2) : '';
      return (
        <>
          {argsText && (
            <>
              <CopyButton text={argsText} label="复制参数" />
              <PreBlock variant="cmd">{argsText}</PreBlock>
            </>
          )}
          {status === 'running' && !result && <EmptyState text="执行中..." />}
          {errorMessage && <PreBlock variant="error">{errorMessage}</PreBlock>}
          {resultText && !errorMessage && (
            <>
              <CopyButton text={resultText} label="复制结果" variant="subtle" />
              <PreBlock variant="output">{resultText}</PreBlock>
            </>
          )}
        </>
      );
    },
  };
}

const TOOL_SPECS: Record<string, ToolSpec> = {
  shell: shellSpec(),
  grep: grepSpec(),
  glob: globSpec(),
  read: readSpec(),
  read_file: readSpec(),
  edit: fileEditSpec(Pencil),
  write: fileEditSpec(FilePlus),
  write_file: fileEditSpec(FilePlus),
  patch: fileEditSpec(Pencil),
  apply_patch: fileEditSpec(Pencil),
  fetch_url: fetchUrlSpec(),
  web_search: webSearchSpec(),
  enter_plan_mode: planModeSpec(),
  exit_plan_mode: exitPlanModeSpec(),
};

function getToolSpec(name: string): ToolSpec {
  return TOOL_SPECS[name] || defaultSpec();
}

export function summarizeToolCall(
  name: string,
  argsText: string,
  outputText: string,
  status: ToolStatus,
): string {
  const args = asObject(tryParseJSON(argsText)) || {};
  const result = asObject(tryParseJSON(outputText));
  return getToolSpec(name).summary(args, result, status);
}

export function ToolCallPreview({
  toolName,
  argsText,
  outputText,
}: {
  toolName: string;
  argsText: string;
  outputText?: string | null;
}) {
  const args = asObject(tryParseJSON(argsText)) || {};
  const result = asObject(tryParseJSON(outputText));
  const spec = getToolSpec(toolName);
  return <div className="tc-detail">{spec.detail(args, result, 'done')}</div>;
}

export function ToolCallCard({ item }: { item: ToolRenderItem }) {
  const [expanded, setExpanded] = useState(false);
  const args = useMemo(() => asObject(tryParseJSON(item.argsText)) || {}, [item.argsText]);
  const result = useMemo(() => asObject(tryParseJSON(item.outputText)), [item.outputText]);
  const spec = getToolSpec(item.name);
  const Icon = spec.icon;
  const statusLabel = item.status === 'done' ? '完成' : item.status === 'error' ? '失败' : '运行中';

  return (
    <div className={cn('tool-call', expanded && 'expanded')}>
      <button
        type="button"
        className="tc-header"
        aria-expanded={expanded}
        aria-label={`${item.name} · ${statusLabel}`}
        onClick={() => setExpanded((value) => !value)}
      >
        <Icon className="tc-icon" />
        <span className="tc-name">{item.name}</span>
        <span className="tc-summary">{item.summary}</span>
        <span className="tc-status" aria-label={statusLabel}>
          {item.status === 'done' && <Check className="h-3 w-3" style={{ color: 'var(--icon-accent)' }} />}
          {item.status === 'error' && <X className="h-3 w-3" style={{ color: 'var(--destructive, #ef4444)' }} />}
          {item.status === 'running' && <span className="pulsing-dot" />}
        </span>
        <ChevronRight className="tc-chevron" />
      </button>
      {expanded && (
        <div className="tc-body">
          <div className="tc-body-inner">
            {spec.detail(args, result, item.status)}
          </div>
        </div>
      )}
    </div>
  );
}
