import axios from 'axios';


const REQUEST_ID_RE = /^[A-Za-z0-9._:-]{1,128}$/;
const GENERIC_SERVER_ERROR = '服务暂时不可用，请稍后重试';
const GENERIC_UNEXPECTED_RESPONSE = '服务器返回了无法识别的响应';

type ChatTreeApiErrorOptions = {
  status?: number;
  code: string;
  retryable: boolean;
  requestId?: string;
  details?: Record<string, unknown>;
  cause?: unknown;
};

export class ChatTreeApiError extends Error {
  status?: number;
  code: string;
  retryable: boolean;
  requestId?: string;
  details?: Record<string, unknown>;
  override cause?: unknown;

  constructor(message: string, options: ChatTreeApiErrorOptions) {
    super(message);
    this.name = 'ChatTreeApiError';
    this.status = options.status;
    this.code = options.code;
    this.retryable = options.retryable;
    this.requestId = options.requestId;
    this.details = options.details;
    this.cause = options.cause;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function isJsonValue(value: unknown, active: Set<object>): boolean {
  if (
    value === null
    || typeof value === 'string'
    || typeof value === 'boolean'
  ) {
    return true;
  }
  if (typeof value === 'number') return Number.isFinite(value);
  if (typeof value !== 'object') return false;
  if (active.has(value)) return false;

  active.add(value);
  try {
    if (Array.isArray(value)) {
      return value.every((item) => isJsonValue(item, active));
    }
    if (!isRecord(value)) return false;
    return Object.values(value).every((item) => isJsonValue(item, active));
  } finally {
    active.delete(value);
  }
}

function isJsonObject(value: unknown): value is Record<string, unknown> {
  return isRecord(value) && isJsonValue(value, new Set<object>());
}

function validRequestId(value: unknown): value is string {
  return typeof value === 'string' && REQUEST_ID_RE.test(value);
}

function headerValue(headers: unknown, name: string): string | undefined {
  if (headers === null || typeof headers !== 'object') return undefined;
  const get = (headers as { get?: unknown }).get;
  if (typeof get === 'function') {
    const value = get.call(headers, name);
    return typeof value === 'string' ? value : undefined;
  }
  for (const [key, value] of Object.entries(headers)) {
    if (key.toLowerCase() === name.toLowerCase() && typeof value === 'string') {
      return value;
    }
  }
  return undefined;
}

function responseRequestId(headers: unknown): string | undefined {
  const value = headerValue(headers, 'x-request-id');
  return validRequestId(value) ? value : undefined;
}

function unexpectedResponse(status: number | undefined, cause: unknown) {
  return new ChatTreeApiError(GENERIC_UNEXPECTED_RESPONSE, {
    status,
    code: 'unexpected_response',
    retryable: false,
    cause,
  });
}

function normalizeModernEnvelope(
  status: number,
  data: unknown,
  cause: unknown,
): ChatTreeApiError | undefined {
  if (!isRecord(data) || !isRecord(data.error)) return undefined;
  const body = data.error;
  if (
    typeof body.code !== 'string'
    || body.code.length === 0
    || typeof body.message !== 'string'
    || body.message.length === 0
    || typeof body.retryable !== 'boolean'
    || !validRequestId(body.request_id)
  ) {
    return undefined;
  }
  const hasDetails = Object.prototype.hasOwnProperty.call(body, 'details');
  if (hasDetails && !isJsonObject(body.details)) return undefined;

  return new ChatTreeApiError(body.message, {
    status,
    code: body.code,
    retryable: body.retryable,
    requestId: body.request_id,
    details: hasDetails ? body.details as Record<string, unknown> : undefined,
    cause,
  });
}

function isLegacyActiveRunConflict(
  status: number,
  detail: Record<string, unknown>,
): boolean {
  return status === 409
    && isJsonObject(detail)
    && Array.isArray(detail.active_run_ids)
    && detail.active_run_ids.length > 0
    && detail.active_run_ids.every(
      (runId) => typeof runId === 'string' && runId.length > 0,
    );
}

function normalizeLegacy4xx(
  status: number,
  data: unknown,
  headers: unknown,
  cause: unknown,
): ChatTreeApiError | undefined {
  if (!isRecord(data) || !Object.prototype.hasOwnProperty.call(data, 'detail')) {
    return undefined;
  }
  const detail = data.detail;
  const requestId = responseRequestId(headers);
  if (typeof detail === 'string' && detail.length > 0) {
    return new ChatTreeApiError(detail, {
      status,
      code: 'http_error',
      retryable: false,
      requestId,
      cause,
    });
  }
  if (
    !isJsonObject(detail)
    || typeof detail.message !== 'string'
    || detail.message.length === 0
  ) {
    return undefined;
  }

  return new ChatTreeApiError(detail.message, {
    status,
    code: isLegacyActiveRunConflict(status, detail)
      ? 'active_runs_present'
      : 'http_error',
    retryable: isLegacyActiveRunConflict(status, detail),
    requestId,
    details: detail,
    cause,
  });
}

function normalizeLegacy5xx(
  status: number,
  data: unknown,
  headers: unknown,
  cause: unknown,
): ChatTreeApiError | undefined {
  if (!isRecord(data) || !Object.prototype.hasOwnProperty.call(data, 'detail')) {
    return undefined;
  }
  const unavailable = status === 502 || status === 503 || status === 504;
  return new ChatTreeApiError(GENERIC_SERVER_ERROR, {
    status,
    code: unavailable ? 'service_unavailable' : 'internal_error',
    retryable: unavailable,
    requestId: responseRequestId(headers),
    cause,
  });
}

export function normalizeApiError(error: unknown): unknown {
  if (axios.isCancel(error) || error instanceof ChatTreeApiError) return error;
  if (!axios.isAxiosError(error)) return unexpectedResponse(undefined, error);
  if (error.code === 'ECONNABORTED') {
    return new ChatTreeApiError('请求超时，请稍后重试', {
      code: 'request_timeout',
      retryable: true,
      cause: error,
    });
  }
  if (!error.response) {
    return new ChatTreeApiError('无法连接到服务器', {
      code: 'network_error',
      retryable: true,
      cause: error,
    });
  }

  const { status, data, headers } = error.response;
  const modern = normalizeModernEnvelope(status, data, error);
  if (modern) return modern;
  if (status >= 500 && status <= 599) {
    const legacy = normalizeLegacy5xx(status, data, headers, error);
    if (legacy) return legacy;
  }
  if (status >= 400 && status <= 499) {
    const legacy = normalizeLegacy4xx(status, data, headers, error);
    if (legacy) return legacy;
  }
  return unexpectedResponse(status, error);
}

export function getApiErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ChatTreeApiError && error.message) return error.message;
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}
