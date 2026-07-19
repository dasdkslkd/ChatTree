const CANONICAL_UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export const MIN_PROTOCOL_VERSION = 1;

export class BoundServerNotReadyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'BoundServerNotReadyError';
  }
}

export class BoundServerLeaseChangedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'BoundServerLeaseChangedError';
  }
}

export class BoundServerIdentityError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'BoundServerIdentityError';
  }
}

export class BoundServerProtocolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'BoundServerProtocolError';
  }
}

export class BoundServerStatusError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  readonly details?: Record<string, unknown>;

  constructor(
    code: string,
    message: string,
    retryable: boolean,
    details?: Record<string, unknown>,
  ) {
    super(message);
    this.name = 'BoundServerStatusError';
    this.code = code;
    this.retryable = retryable;
    this.details = details;
  }
}

export type BoundServerContext = Readonly<{
  profileId: string;
  serverInstanceId: string;
  connectionEpoch: number;
  connectionLeaseId: string;
  apiBase: string;
}>;

export function sameBoundServerContext(
  left: BoundServerContext,
  right: BoundServerContext,
): boolean {
  return left.profileId === right.profileId
    && left.apiBase === right.apiBase
    && left.serverInstanceId === right.serverInstanceId
    && left.connectionEpoch === right.connectionEpoch
    && left.connectionLeaseId === right.connectionLeaseId;
}

export function isCanonicalUuid(value: unknown): value is string {
  return typeof value === 'string' && CANONICAL_UUID_RE.test(value);
}

export function isFatalBoundServerError(error: unknown): boolean {
  return error instanceof BoundServerIdentityError
    || error instanceof BoundServerProtocolError
    || (error instanceof BoundServerStatusError && !error.retryable);
}
