export const CONVERSATION_STORAGE_KEY = 'conversation-storage';
export const PROJECT_ORDER_STORAGE_KEY = 'chattree.projectOrder';
export const LEFT_SIDEBAR_STORAGE_KEY = 'chattree.leftSidebarWidth';
export const RIGHT_PANEL_STORAGE_KEY = 'chattree.rightPanelWidth';

export const ALL_PROFILE_STORAGE_KEYS = [
  CONVERSATION_STORAGE_KEY,
  PROJECT_ORDER_STORAGE_KEY,
  LEFT_SIDEBAR_STORAGE_KEY,
  RIGHT_PANEL_STORAGE_KEY,
] as const;

export const SERVER_BOUND_PROFILE_STORAGE_KEYS = [
  CONVERSATION_STORAGE_KEY,
  PROJECT_ORDER_STORAGE_KEY,
] as const;

export const LEGACY_MIGRATION_MARKER = 'legacy-storage-migrated-v1';
export const BOUND_SERVER_INSTANCE_MARKER = 'bound-server-instance-v1';

const STORAGE_WRITE_PROBE = 'storage-write-probe-v1';
const STORAGE_WRITE_PROBE_VALUE = 'chattree-storage-probe';

type ReadWriteStorage = Pick<Storage, 'getItem' | 'setItem'>;
type PreparedStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

export class ProfileStorageUnavailableError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = 'ProfileStorageUnavailableError';
  }
}

export function profileStorageKey(profileId: string, logicalKey: string): string {
  if (!profileId || !logicalKey) {
    throw new Error('Profile storage key requires non-empty values');
  }
  return `chattree.profile.${encodeURIComponent(profileId)}.${logicalKey}`;
}

function unavailable(cause: unknown): ProfileStorageUnavailableError {
  return new ProfileStorageUnavailableError(
    'Profile storage is unavailable',
    { cause },
  );
}

export function migrateLegacyProfileStorage(
  storage: ReadWriteStorage,
  profileId: string,
  logicalKeys: readonly string[],
): void {
  if (profileId !== 'local') return;
  try {
    const marker = profileStorageKey(profileId, LEGACY_MIGRATION_MARKER);
    if (storage.getItem(marker) === '1') return;
    for (const logicalKey of logicalKeys) {
      const scopedKey = profileStorageKey(profileId, logicalKey);
      const scopedValue = storage.getItem(scopedKey);
      const legacyValue = storage.getItem(logicalKey);
      if (scopedValue === null && legacyValue !== null) {
        storage.setItem(scopedKey, legacyValue);
      }
    }
    storage.setItem(marker, '1');
  } catch (cause) {
    if (cause instanceof ProfileStorageUnavailableError) throw cause;
    throw unavailable(cause);
  }
}

function verifyStorageWritable(
  storage: PreparedStorage,
  profileId: string,
): void {
  const probeKey = profileStorageKey(profileId, STORAGE_WRITE_PROBE);
  const previous = storage.getItem(probeKey);
  storage.setItem(probeKey, STORAGE_WRITE_PROBE_VALUE);
  storage.removeItem(probeKey);
  if (previous !== null && previous !== STORAGE_WRITE_PROBE_VALUE) {
    storage.setItem(probeKey, previous);
  }
}

export function prepareProfileStorageForServer(
  storage: PreparedStorage,
  profileId: string,
  serverInstanceId: string,
  serverBoundLogicalKeys: readonly string[],
): void {
  if (!serverInstanceId) {
    throw new Error('Server instance ID is required for Profile storage');
  }
  try {
    verifyStorageWritable(storage, profileId);
    const marker = profileStorageKey(profileId, BOUND_SERVER_INSTANCE_MARKER);
    const previousServerInstanceId = storage.getItem(marker);
    if (previousServerInstanceId === serverInstanceId) return;
    if (previousServerInstanceId !== null) {
      for (const logicalKey of new Set(serverBoundLogicalKeys)) {
        storage.removeItem(profileStorageKey(profileId, logicalKey));
      }
    }
    storage.setItem(marker, serverInstanceId);
  } catch (cause) {
    if (cause instanceof ProfileStorageUnavailableError) throw cause;
    throw unavailable(cause);
  }
}
