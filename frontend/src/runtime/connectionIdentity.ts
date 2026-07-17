const CANONICAL_UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export const EXPECTED_PROTOCOL_VERSION = 1;

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

export type BoundServerContext = Readonly<{
  profileId: string;
  serverInstanceId: string;
  connectionEpoch: number;
  connectionLeaseId: string;
  apiBase: string;
}>;

export function isCanonicalUuid(value: unknown): value is string {
  return typeof value === 'string' && CANONICAL_UUID_RE.test(value);
}

export function isFatalBoundServerError(error: unknown): boolean {
  return error instanceof BoundServerIdentityError
    || error instanceof BoundServerProtocolError;
}
