import {
  composeConnectionAbortSignal,
  connectionScopeRuntime,
  StaleConnectionScopeError,
  type ConnectionScope,
  type ConnectionScopeRuntime,
} from '../runtime/connectionScope';
import { serverApiUrl } from './client';
import {
  CONNECTION_LEASE_HEADER,
  readConnectionLeaseHeader,
} from './connectionLeaseHeader';
import {
  normalizeFetchError,
  parseModernErrorEnvelope,
  requireSuccessfulResponse,
} from './errors';

export type LeaseFetchConnectionRuntime = Pick<
  ConnectionScopeRuntime,
  'current' | 'isActive' | 'invalidate'
>;

async function cancelResponseBody(response: Response): Promise<void> {
  try {
    await response.body?.cancel();
  } catch {
    // The transport may already have closed the body.
  }
}

async function rejectStaleResponse(
  runtime: LeaseFetchConnectionRuntime,
  scope: ConnectionScope,
  response?: Response,
): Promise<never> {
  runtime.invalidate(scope);
  if (response) await cancelResponseBody(response);
  throw new StaleConnectionScopeError();
}

function requestUrl(input: RequestInfo | URL): RequestInfo | URL {
  return typeof input === 'string' && input.startsWith('/')
    ? serverApiUrl(input)
    : input;
}

export async function leaseGuardedFetch(
  input: RequestInfo | URL,
  init: RequestInit = {},
  runtime: LeaseFetchConnectionRuntime = connectionScopeRuntime,
): Promise<Response> {
  const scope = runtime.current();
  const inherited = typeof Request !== 'undefined' && input instanceof Request
    ? input.headers
    : undefined;
  const headers = new Headers(init.headers ?? inherited);
  headers.delete(CONNECTION_LEASE_HEADER);
  headers.set(CONNECTION_LEASE_HEADER, scope.leaseId);
  const inheritedSignal = typeof Request !== 'undefined' && input instanceof Request
    ? input.signal
    : undefined;
  const signal = composeConnectionAbortSignal(
    init.signal ?? inheritedSignal,
    scope.signal,
  );

  let response: Response;
  try {
    response = await fetch(requestUrl(input), { ...init, headers, signal });
  } catch (error) {
    if (!runtime.isActive(scope)) throw new StaleConnectionScopeError();
    throw normalizeFetchError(error, signal);
  }
  if (!runtime.isActive(scope)) return rejectStaleResponse(runtime, scope, response);
  if (readConnectionLeaseHeader(response.headers) !== scope.leaseId) {
    return rejectStaleResponse(runtime, scope, response);
  }
  if (response.status === 409) {
    try {
      const envelope = parseModernErrorEnvelope(
        response.status,
        await response.clone().json(),
      );
      if (envelope?.code === 'stale_connection_epoch') {
        return rejectStaleResponse(runtime, scope, response);
      }
    } catch (error) {
      if (!runtime.isActive(scope)) return rejectStaleResponse(runtime, scope, response);
      if (signal.aborted) {
        await cancelResponseBody(response);
        throw error;
      }
    }
  }
  return requireSuccessfulResponse(response);
}
