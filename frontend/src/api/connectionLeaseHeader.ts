import {
  BoundServerLeaseChangedError,
  isCanonicalUuid,
} from '../runtime/connectionIdentity';

export const CONNECTION_LEASE_HEADER = 'X-ChatTree-Connection-Lease-ID';

function matchingEntries(headers: object): Array<[string, unknown]> | null {
  try {
    const ownMatches = Object.entries(headers).filter(
      ([key]) => key.toLowerCase() === CONNECTION_LEASE_HEADER.toLowerCase(),
    );
    if (ownMatches.length > 0) return ownMatches;

    const entries = (headers as { entries?: unknown }).entries;
    if (typeof entries !== 'function') return [];
    const iterable = entries.call(headers) as Iterable<unknown>;
    const iterableMatches: Array<[string, unknown]> = [];
    for (const entry of iterable) {
      if (!Array.isArray(entry) || entry.length < 2 || typeof entry[0] !== 'string') {
        continue;
      }
      if (entry[0].toLowerCase() === CONNECTION_LEASE_HEADER.toLowerCase()) {
        iterableMatches.push([entry[0], entry[1]]);
      }
    }
    return iterableMatches;
  } catch {
    return null;
  }
}

export function readConnectionLeaseHeader(headers: unknown): string | null {
  if (headers === null || typeof headers !== 'object') return null;
  const matches = matchingEntries(headers);
  if (matches === null || matches.length > 1) return null;

  let value: unknown;
  if (matches.length === 1) {
    value = matches[0][1];
  } else {
    try {
      const get = (headers as { get?: unknown }).get;
      if (typeof get !== 'function') return null;
      value = get.call(headers, CONNECTION_LEASE_HEADER);
    } catch {
      return null;
    }
  }
  return isCanonicalUuid(value) ? value : null;
}

export function requireMatchingConnectionLeaseHeader(
  headers: unknown,
  expectedLeaseId: string,
): string {
  const actual = readConnectionLeaseHeader(headers);
  if (!isCanonicalUuid(expectedLeaseId) || actual !== expectedLeaseId) {
    throw new BoundServerLeaseChangedError(
      'Launcher connection changed during Server request',
    );
  }
  return actual;
}
