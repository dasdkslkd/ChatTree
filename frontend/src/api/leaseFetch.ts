import {
  composeConnectionAbortSignal,
  connectionEpochRuntime,
  StaleConnectionEpochError,
  type ConnectionEpochRuntime,
  type ConnectionEpochToken,
} from '../runtime/connectionEpoch';
import { serverApiUrl } from './client';
import {
  CONNECTION_LEASE_HEADER,
  readConnectionLeaseHeader,
} from './connectionLeaseHeader';
import { parseModernErrorEnvelope } from './errors';

export type LeaseFetchConnectionRuntime = Pick<
  ConnectionEpochRuntime,
  'capture' | 'isCurrent' | 'signalFor' | 'invalidate'
>;

async function cancelResponseBody(response: Response): Promise<void> {
  try {
    await response.body?.cancel();
  } catch {
    // The transport may already have closed or cancelled the body.
  }
}

async function rejectStaleResponse(
  runtime: LeaseFetchConnectionRuntime,
  token: ConnectionEpochToken,
  response?: Response,
): Promise<never> {
  runtime.invalidate(token);
  if (response) await cancelResponseBody(response);
  throw new StaleConnectionEpochError();
}

function requestHeaders(input: RequestInfo | URL, init: RequestInit): Headers {
  const inherited = typeof Request !== 'undefined' && input instanceof Request
    ? input.headers
    : undefined;
  const headers = new Headers(init.headers ?? inherited);
  headers.delete(CONNECTION_LEASE_HEADER);
  return headers;
}

function requestSignal(
  input: RequestInfo | URL,
  init: RequestInit,
): AbortSignal | null | undefined {
  if (init.signal !== undefined) return init.signal;
  return typeof Request !== 'undefined' && input instanceof Request
    ? input.signal
    : undefined;
}

function requestUrl(input: RequestInfo | URL): RequestInfo | URL {
  if (typeof input === 'string' && input.startsWith('/')) {
    return serverApiUrl(input);
  }
  return input;
}

export async function leaseGuardedFetch(
  input: RequestInfo | URL,
  init: RequestInit = {},
  token?: ConnectionEpochToken,
  runtime: LeaseFetchConnectionRuntime = connectionEpochRuntime,
): Promise<Response> {
  const owner = token ?? runtime.capture();
  if (!runtime.isCurrent(owner)) throw new StaleConnectionEpochError();

  const headers = requestHeaders(input, init);
  headers.set(CONNECTION_LEASE_HEADER, owner.connectionLeaseId);
  const callerSignal = requestSignal(input, init);
  const signal = composeConnectionAbortSignal(
    callerSignal,
    runtime.signalFor(owner),
  );

  let response: Response;
  try {
    response = await fetch(requestUrl(input), {
      ...init,
      headers,
      signal,
    });
  } catch (error) {
    if (!runtime.isCurrent(owner)) throw new StaleConnectionEpochError();
    throw error;
  }

  if (!runtime.isCurrent(owner)) {
    return rejectStaleResponse(runtime, owner, response);
  }
  if (readConnectionLeaseHeader(response.headers) !== owner.connectionLeaseId) {
    return rejectStaleResponse(runtime, owner, response);
  }

  if (response.status === 409) {
    let envelope: ReturnType<typeof parseModernErrorEnvelope> = null;
    try {
      const data = await response.clone().json();
      envelope = parseModernErrorEnvelope(response.status, data);
    } catch (error) {
      if (!runtime.isCurrent(owner)) {
        return rejectStaleResponse(runtime, owner, response);
      }
      if (callerSignal?.aborted) {
        await cancelResponseBody(response);
        throw error;
      }
      // A non-JSON 409 remains an ordinary response when the owner is current.
    }
    if (!runtime.isCurrent(owner)) {
      return rejectStaleResponse(runtime, owner, response);
    }
    if (envelope?.code === 'stale_connection_epoch') {
      return rejectStaleResponse(runtime, owner, response);
    }
  }

  return response;
}
