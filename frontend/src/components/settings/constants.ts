import type { ToolsConfig } from '@/types/model';

export const DEFAULT_TOOLS_CONFIG: ToolsConfig = {
  enabled: true,
  max_rounds: 5,
  max_result_length: 8000,
  default_permission_mode: 'auto_approve',
  builtin: {
    enabled: true,
    exposure: 'coding',
    code: {
      enabled: true,
      groups: ['read', 'search', 'edit', 'shell'],
    },
  },
  web_search: {
    enabled: true,
    searxng: {
      searxng_url: 'http://localhost:8888',
      language: 'zh-CN',
      max_results: 10,
      timeout: 15,
    },
  },
  mcp: {
    enabled: false,
    servers: {},
  },
};

export function normalizeToolsConfig(raw?: ToolsConfig): ToolsConfig {
  return {
    ...DEFAULT_TOOLS_CONFIG,
    ...(raw || {}),
    builtin: {
      ...(DEFAULT_TOOLS_CONFIG.builtin || {}),
      ...(raw?.builtin || {}),
      code: {
        ...(DEFAULT_TOOLS_CONFIG.builtin?.code || {}),
        ...(raw?.builtin?.code || {}),
      },
    },
    web_search: {
      ...(DEFAULT_TOOLS_CONFIG.web_search || {}),
      ...(raw?.web_search || {}),
      searxng: {
        ...(DEFAULT_TOOLS_CONFIG.web_search?.searxng || {}),
        ...(raw?.web_search?.searxng || {}),
      },
    },
    mcp: {
      ...(DEFAULT_TOOLS_CONFIG.mcp || {}),
      ...(raw?.mcp || {}),
      servers: { ...(raw?.mcp?.servers || {}) },
    },
  };
}

export function parseNumber(value: string, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}
