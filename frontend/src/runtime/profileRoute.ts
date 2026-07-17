export type FrontendRoute =
  | { kind: 'profile'; profileId: string }
  | { kind: 'conversation'; profileId: string; conversationId: string }
  | { kind: 'node'; profileId: string; conversationId: string; nodeId: string }
  | { kind: 'run'; profileId: string; runId: string };

function routeError(): Error {
  return new Error('Frontend route contains an invalid or noncanonical segment');
}

function decodeSegment(value: string): string {
  try {
    const decoded = decodeURIComponent(value);
    if (!decoded || decoded.includes('\0') || decoded === '.' || decoded === '..') {
      throw routeError();
    }
    return decoded;
  } catch {
    throw routeError();
  }
}

function segment(value: string): string {
  if (!value || value.includes('\0') || value === '.' || value === '..') throw routeError();
  return encodeURIComponent(value);
}

export function buildFrontendRoute(route: FrontendRoute): string {
  const root = `/s/${segment(route.profileId)}`;
  if (route.kind === 'profile') return root;
  if (route.kind === 'conversation') return `${root}/c/${segment(route.conversationId)}`;
  if (route.kind === 'node') {
    return `${root}/c/${segment(route.conversationId)}/n/${segment(route.nodeId)}`;
  }
  return `${root}/r/${segment(route.runId)}`;
}

export function parseFrontendRoute(pathname: string): FrontendRoute {
  const parts = pathname.split('/');
  let route: FrontendRoute;
  if (parts.length === 3 && parts[0] === '' && parts[1] === 's') {
    route = { kind: 'profile', profileId: decodeSegment(parts[2]) };
  } else if (parts.length === 5 && parts[0] === '' && parts[1] === 's' && parts[3] === 'c') {
    route = {
      kind: 'conversation',
      profileId: decodeSegment(parts[2]),
      conversationId: decodeSegment(parts[4]),
    };
  } else if (parts.length === 7 && parts[0] === '' && parts[1] === 's'
      && parts[3] === 'c' && parts[5] === 'n') {
    route = {
      kind: 'node',
      profileId: decodeSegment(parts[2]),
      conversationId: decodeSegment(parts[4]),
      nodeId: decodeSegment(parts[6]),
    };
  } else if (parts.length === 5 && parts[0] === '' && parts[1] === 's' && parts[3] === 'r') {
    route = {
      kind: 'run',
      profileId: decodeSegment(parts[2]),
      runId: decodeSegment(parts[4]),
    };
  } else {
    throw new Error('Profile route is required or unsupported');
  }
  if (buildFrontendRoute(route) !== pathname) throw routeError();
  return route;
}
