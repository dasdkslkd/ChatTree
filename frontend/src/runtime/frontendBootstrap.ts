import { readFrontendRouteLocation } from './profileRoute';

export type FrontendBootstrap = Readonly<{ profileId: string; apiBase: string }>;
export type BootstrapSource = Readonly<{ injected?: unknown; href: string }>;

declare global {
  interface Window {
    __CHATTREE_BOOTSTRAP__?: FrontendBootstrap;
  }
}

let initializedBootstrap: FrontendBootstrap | null = null;

export function buildProfileApiBase(profileId: string): string {
  if (!profileId || profileId.includes('\0') || profileId === '.' || profileId === '..') {
    throw new Error('Profile ID is invalid');
  }
  return `/p/${encodeURIComponent(profileId)}/api/v1`;
}

function rawHttpPathname(value: string): string | null {
  const schemeSeparator = value.indexOf('://');
  if (schemeSeparator <= 0) return null;
  const authorityStart = schemeSeparator + 3;
  const pathStart = value.indexOf('/', authorityStart);
  return pathStart === -1 ? '/' : value.slice(pathStart);
}

function validateInjectedBootstrap(injected: unknown): FrontendBootstrap | undefined {
  if (injected === undefined) return undefined;
  if (injected === null || typeof injected !== 'object' || Array.isArray(injected)) {
    throw new Error('Injected frontend bootstrap must be a plain object');
  }
  const prototype = Object.getPrototypeOf(injected);
  if (prototype !== Object.prototype && prototype !== null) {
    throw new Error('Injected frontend bootstrap must be a plain object');
  }
  const keys = Reflect.ownKeys(injected);
  if (keys.length !== 2 || !keys.includes('profileId') || !keys.includes('apiBase')) {
    throw new Error('Injected frontend bootstrap must contain exactly profileId and apiBase');
  }
  const values = injected as Record<string, unknown>;
  if (typeof values.profileId !== 'string' || !values.profileId
      || typeof values.apiBase !== 'string' || !values.apiBase) {
    throw new Error('Injected frontend bootstrap fields must be non-empty strings');
  }
  return { profileId: values.profileId, apiBase: values.apiBase };
}

function validateApiBase(value: string, profileId: string, trustedInjected: boolean): string {
  const canonicalPath = buildProfileApiBase(profileId);
  if (value === canonicalPath) return value;
  if (!trustedInjected
      || value.startsWith('/')
      || value !== value.trim()
      || value.includes('?')
      || value.includes('#')
      || value.includes('\\')) {
    throw new Error('Frontend apiBase must use the bound Launcher Profile proxy');
  }

  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error('Frontend apiBase must be an absolute Launcher HTTP URL');
  }
  if ((url.protocol !== 'http:' && url.protocol !== 'https:')
      || !url.host
      || url.username
      || url.password
      || url.search
      || url.hash
      || rawHttpPathname(value) !== url.pathname
      || url.pathname !== canonicalPath) {
    throw new Error('Frontend apiBase must use the bound Launcher Profile proxy');
  }
  return `${url.origin}${canonicalPath}`;
}

export function resolveFrontendBootstrap(source: BootstrapSource): FrontendBootstrap {
  const route = readFrontendRouteLocation({ href: source.href });
  const candidate = validateInjectedBootstrap(source.injected);
  const profileId = candidate === undefined ? route.profileId : candidate.profileId;
  if (profileId !== route.profileId) {
    throw new Error('URL Profile does not match bootstrap Profile');
  }
  const apiBase = validateApiBase(
    candidate === undefined ? buildProfileApiBase(profileId) : candidate.apiBase,
    profileId,
    candidate !== undefined,
  );
  return Object.freeze({ profileId, apiBase });
}

export function initializeFrontendBootstrap(): FrontendBootstrap {
  const next = resolveFrontendBootstrap({
    injected: window.__CHATTREE_BOOTSTRAP__,
    href: window.location.href,
  });
  if (initializedBootstrap) {
    if (initializedBootstrap.profileId !== next.profileId
        || initializedBootstrap.apiBase !== next.apiBase) {
      throw new Error('Frontend bootstrap cannot change inside one page instance');
    }
    return initializedBootstrap;
  }
  initializedBootstrap = next;
  return initializedBootstrap;
}

export function getFrontendBootstrap(): FrontendBootstrap {
  if (!initializedBootstrap) {
    throw new Error('Frontend bootstrap has not been initialized');
  }
  return initializedBootstrap;
}
