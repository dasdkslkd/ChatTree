export const PROFILE_RENDERER_LOCK_PREFIX = 'chattree-profile-renderer:';

type ProfileRendererLockManager = Readonly<{
  request(
    name: string,
    options: Readonly<{ mode: 'exclusive'; ifAvailable: true }>,
    callback: (lock: unknown | null) => Promise<void>,
  ): Promise<void>;
}>;

type ActiveClaim = Readonly<{
  profileId: string;
  ready: Promise<void>;
}>;

let activeClaim: ActiveClaim | null = null;

export class ProfileRendererOwnershipError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = 'ProfileRendererOwnershipError';
  }
}

function browserLockManager(): ProfileRendererLockManager {
  try {
    const manager = navigator.locks as ProfileRendererLockManager | undefined;
    if (!manager || typeof manager.request !== 'function') {
      throw new Error('Web Locks API is unavailable');
    }
    return manager;
  } catch (cause) {
    if (cause instanceof ProfileRendererOwnershipError) throw cause;
    throw new ProfileRendererOwnershipError(
      'Profile renderer ownership is unavailable',
      { cause },
    );
  }
}

export function acquireProfileRendererOwnership(
  profileId: string,
  lockManager?: ProfileRendererLockManager,
): Promise<void> {
  if (!profileId) {
    return Promise.reject(new ProfileRendererOwnershipError('Profile ID is required'));
  }
  if (activeClaim) {
    if (activeClaim.profileId !== profileId) {
      return Promise.reject(new ProfileRendererOwnershipError(
        'Profile renderer ownership cannot change inside one page',
      ));
    }
    return activeClaim.ready;
  }

  let resolveReady!: () => void;
  let rejectReady!: (reason: unknown) => void;
  const ready = new Promise<void>((resolve, reject) => {
    resolveReady = resolve;
    rejectReady = reject;
  });
  activeClaim = { profileId, ready };

  let manager: ProfileRendererLockManager;
  try {
    manager = lockManager ?? browserLockManager();
  } catch (cause) {
    rejectReady(cause);
    return ready;
  }

  const heldForPageLifetime = new Promise<void>(() => {});
  try {
    const request = manager.request(
      `${PROFILE_RENDERER_LOCK_PREFIX}${encodeURIComponent(profileId)}`,
      { mode: 'exclusive', ifAvailable: true },
      async (lock) => {
        if (lock === null) {
          rejectReady(new ProfileRendererOwnershipError(
            'Profile is already open in another tab',
          ));
          return;
        }
        resolveReady();
        await heldForPageLifetime;
      },
    );
    void Promise.resolve(request).catch((cause) => {
      rejectReady(new ProfileRendererOwnershipError(
        'Profile renderer ownership is unavailable',
        { cause },
      ));
    });
  } catch (cause) {
    rejectReady(new ProfileRendererOwnershipError(
      'Profile renderer ownership is unavailable',
      { cause },
    ));
  }
  return ready;
}
