import { readFrontendRouteLocation } from './profileRoute';

export type ProfileContext = Readonly<{
  profileId: string;
  apiBase: string;
}>;

let context: ProfileContext | null = null;

export function buildProfileApiBase(profileId: string): string {
  if (!profileId || profileId.includes('\0') || profileId === '.' || profileId === '..') {
    throw new Error('Profile ID is invalid');
  }
  return `/p/${encodeURIComponent(profileId)}/api/v1`;
}

export function initializeProfileContext(href = window.location.href): ProfileContext {
  const route = readFrontendRouteLocation({ href });
  const next = Object.freeze({
    profileId: route.profileId,
    apiBase: buildProfileApiBase(route.profileId),
  });
  if (context && (
    context.profileId !== next.profileId
    || context.apiBase !== next.apiBase
  )) {
    throw new Error('Profile route cannot change inside one page instance');
  }
  context = context || next;
  return context;
}

export function getProfileContext(): ProfileContext {
  if (!context) throw new Error('Profile route has not been initialized');
  return context;
}
