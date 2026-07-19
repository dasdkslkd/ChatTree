import axios from 'axios';


const REQUEST_ID_RE = /^[A-Za-z0-9._:-]{1,128}$/;
const GENERIC_UNEXPECTED_RESPONSE = '服务器返回了无法识别的响应';

type ChatTreeApiErrorOptions = {
  status?: number;
  code: string;
  retryable: boolean;
  requestId?: string;
  details?: Record<string, unknown>;
  cause?: unknown;
};

export type ModernErrorEnvelope = Readonly<{
  status: number;
  code: string;
  message: string;
  retryable: boolean;
  requestId: string;
  details?: Record<string, unknown>;
}>;

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

function unexpectedResponse(status: number | undefined, cause: unknown) {
  return new ChatTreeApiError(GENERIC_UNEXPECTED_RESPONSE, {
    status,
    code: 'unexpected_response',
    retryable: false,
    cause,
  });
}

export function unexpectedApiResponse(
  status: number | undefined,
  cause: unknown,
): ChatTreeApiError {
  return unexpectedResponse(status, cause);
}

export function parseModernErrorEnvelope(
  status: number,
  data: unknown,
): ModernErrorEnvelope | null {
  if (
    !Number.isInteger(status)
    || status < 400
    || status > 599
    || !isRecord(data)
    || !isRecord(data.error)
  ) {
    return null;
  }
  const body = data.error;
  if (
    typeof body.code !== 'string'
    || body.code.length === 0
    || typeof body.message !== 'string'
    || body.message.length === 0
    || typeof body.retryable !== 'boolean'
    || !validRequestId(body.request_id)
  ) {
    return null;
  }
  const hasDetails = Object.prototype.hasOwnProperty.call(body, 'details');
  if (hasDetails && !isJsonObject(body.details)) return null;

  return Object.freeze({
    status,
    code: body.code,
    message: body.message,
    retryable: body.retryable,
    requestId: body.request_id,
    details: hasDetails ? body.details as Record<string, unknown> : undefined,
  });
}

function normalizeModernEnvelope(
  status: number,
  data: unknown,
  cause: unknown,
): ChatTreeApiError | undefined {
  const parsed = parseModernErrorEnvelope(status, data);
  if (!parsed) return undefined;
  return new ChatTreeApiError(parsed.message, {
    status: parsed.status,
    code: parsed.code,
    retryable: parsed.retryable,
    requestId: parsed.requestId,
    details: parsed.details,
    cause,
  });
}

export async function apiErrorFromResponse(
  response: Response,
): Promise<ChatTreeApiError> {
  let data: unknown;
  try {
    data = await response.json();
  } catch (cause) {
    return unexpectedResponse(response.status, cause);
  }
  return normalizeModernEnvelope(response.status, data, response)
    ?? unexpectedResponse(response.status, response);
}

export async function requireSuccessfulResponse(
  response: Response,
): Promise<Response> {
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response;
}

function isAbortError(error: unknown): boolean {
  return (
    typeof DOMException !== 'undefined'
    && error instanceof DOMException
    && error.name === 'AbortError'
  ) || (
    error instanceof Error
    && error.name === 'AbortError'
  );
}

export function normalizeFetchError(
  error: unknown,
  signal?: AbortSignal,
): unknown {
  if (
    error instanceof ChatTreeApiError
    || signal?.aborted
    || isAbortError(error)
  ) {
    return error;
  }
  return new ChatTreeApiError('无法连接到服务器', {
    code: 'network_error',
    retryable: true,
    cause: error,
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

  const { status, data } = error.response;
  const modern = normalizeModernEnvelope(status, data, error);
  if (modern) return modern;
  return unexpectedResponse(status, error);
}

export function getApiErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ChatTreeApiError && error.message) return error.message;
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}
