const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

require.extensions['.ts'] = function loadTs(module, filename) {
  const source = fs.readFileSync(filename, 'utf8');
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      esModuleInterop: true,
      importsNotUsedAsValues: ts.ImportsNotUsedAsValues.Remove,
    },
    fileName: filename,
  }).outputText;
  module._compile(output, filename);
};

const profileStorageModule = path.join(
  __dirname,
  '../src/runtime/profileStorage.ts',
);

const SERVER_A = '11111111-1111-4111-8111-111111111111';
const SERVER_B = '22222222-2222-4222-8222-222222222222';

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  const operations = [];
  let failure = null;
  return {
    values,
    operations,
    storage: {
      getItem(key) {
        operations.push(['get', key]);
        failure?.('get', key);
        return values.has(key) ? values.get(key) : null;
      },
      setItem(key, value) {
        operations.push(['set', key, String(value)]);
        failure?.('set', key);
        values.set(key, String(value));
      },
      removeItem(key) {
        operations.push(['remove', key]);
        failure?.('remove', key);
        values.delete(key);
      },
    },
    failWith(callback) {
      failure = callback;
    },
    clearFailure() {
      failure = null;
    },
  };
}

function testCanonicalKeysAndCentralKeySets() {
  const {
    ALL_PROFILE_STORAGE_KEYS,
    SERVER_BOUND_PROFILE_STORAGE_KEYS,
    profileStorageKey,
  } = require(profileStorageModule);
  assert.equal(
    profileStorageKey('profile a', 'conversation-storage'),
    'chattree.profile.profile%20a.conversation-storage',
  );
  assert.notEqual(
    profileStorageKey('profile-a', 'conversation-storage'),
    profileStorageKey('profile-b', 'conversation-storage'),
  );
  assert.throws(() => profileStorageKey('', 'key'), /non-empty/);
  assert.throws(() => profileStorageKey('profile-a', ''), /non-empty/);
  assert.deepEqual(ALL_PROFILE_STORAGE_KEYS, [
    'conversation-storage',
    'chattree.manualProjectWorkspaces',
    'chattree.projectOrder',
    'chattree.leftSidebarWidth',
    'chattree.rightPanelWidth',
  ]);
  assert.deepEqual(SERVER_BOUND_PROFILE_STORAGE_KEYS, [
    'conversation-storage',
    'chattree.manualProjectWorkspaces',
    'chattree.projectOrder',
  ]);
}

function testOnlyLocalAdoptsLegacyOnceWithoutOverwritingScopedValues() {
  const {
    LEGACY_MIGRATION_MARKER,
    migrateLegacyProfileStorage,
    profileStorageKey,
  } = require(profileStorageModule);
  let nonLocalReads = 0;
  migrateLegacyProfileStorage({
    getItem() {
      nonLocalReads += 1;
      throw new Error('must not read');
    },
    setItem() {
      throw new Error('must not write');
    },
  }, 'profile-b', ['conversation-storage']);
  assert.equal(nonLocalReads, 0);

  const scopedConversation = profileStorageKey('local', 'conversation-storage');
  const scopedOrder = profileStorageKey('local', 'chattree.projectOrder');
  const marker = profileStorageKey('local', LEGACY_MIGRATION_MARKER);
  const harness = memoryStorage({
    'conversation-storage': 'legacy conversations',
    'chattree.projectOrder': 'legacy order',
    [scopedOrder]: 'existing scoped order',
  });
  migrateLegacyProfileStorage(
    harness.storage,
    'local',
    ['conversation-storage', 'chattree.projectOrder'],
  );
  assert.equal(harness.storage.getItem(scopedConversation), 'legacy conversations');
  assert.equal(harness.storage.getItem(scopedOrder), 'existing scoped order');
  assert.equal(harness.storage.getItem(marker), '1');
  const markerWrite = harness.operations.findLastIndex(
    ([kind, key]) => kind === 'set' && key === marker,
  );
  const scopedWrites = harness.operations
    .map((operation, index) => ({ operation, index }))
    .filter(({ operation: [kind, key] }) => kind === 'set' && key !== marker);
  assert.ok(scopedWrites.every(({ index }) => index < markerWrite));

  harness.storage.setItem('conversation-storage', 'changed legacy');
  migrateLegacyProfileStorage(
    harness.storage,
    'local',
    ['conversation-storage', 'chattree.projectOrder'],
  );
  assert.equal(harness.storage.getItem(scopedConversation), 'legacy conversations');
}

function testMigrationFailureLeavesMarkerUnsetAndRetryConverges() {
  const {
    LEGACY_MIGRATION_MARKER,
    ProfileStorageUnavailableError,
    migrateLegacyProfileStorage,
    profileStorageKey,
  } = require(profileStorageModule);
  const marker = profileStorageKey('local', LEGACY_MIGRATION_MARKER);
  const scoped = profileStorageKey('local', 'conversation-storage');
  const harness = memoryStorage({ 'conversation-storage': 'legacy' });
  let failOnce = true;
  harness.failWith((kind, key) => {
    if (failOnce && kind === 'set' && key === scoped) {
      failOnce = false;
      throw new DOMException('blocked');
    }
  });
  assert.throws(
    () => migrateLegacyProfileStorage(
      harness.storage,
      'local',
      ['conversation-storage'],
    ),
    ProfileStorageUnavailableError,
  );
  assert.equal(harness.values.has(marker), false);
  harness.clearFailure();
  migrateLegacyProfileStorage(
    harness.storage,
    'local',
    ['conversation-storage'],
  );
  assert.equal(harness.storage.getItem(scoped), 'legacy');
  assert.equal(harness.storage.getItem(marker), '1');
}

function testFirstBindSameBindAndExactRebindCleanup() {
  const {
    ALL_PROFILE_STORAGE_KEYS,
    BOUND_SERVER_INSTANCE_MARKER,
    SERVER_BOUND_PROFILE_STORAGE_KEYS,
    prepareProfileStorageForServer,
    profileStorageKey,
  } = require(profileStorageModule);
  const harness = memoryStorage({
    'conversation-storage': 'legacy remains',
    [profileStorageKey('profile-b', 'conversation-storage')]: 'other profile',
    [profileStorageKey('profile-a', 'unrelated')]: 'unrelated scoped value',
  });
  const marker = profileStorageKey('profile-a', BOUND_SERVER_INSTANCE_MARKER);

  prepareProfileStorageForServer(
    harness.storage,
    'profile-a',
    SERVER_A,
    SERVER_BOUND_PROFILE_STORAGE_KEYS,
  );
  assert.equal(harness.storage.getItem(marker), SERVER_A);
  for (const [index, logicalKey] of ALL_PROFILE_STORAGE_KEYS.entries()) {
    harness.storage.setItem(
      profileStorageKey('profile-a', logicalKey),
      `value-${index}`,
    );
  }

  const beforeSameBind = new Map(harness.values);
  prepareProfileStorageForServer(
    harness.storage,
    'profile-a',
    SERVER_A,
    SERVER_BOUND_PROFILE_STORAGE_KEYS,
  );
  for (const [key, value] of beforeSameBind) {
    if (!key.endsWith('.storage-write-probe-v1')) {
      assert.equal(harness.values.get(key), value);
    }
  }

  const operationStart = harness.operations.length;
  prepareProfileStorageForServer(
    harness.storage,
    'profile-a',
    SERVER_B,
    SERVER_BOUND_PROFILE_STORAGE_KEYS,
  );
  for (const logicalKey of SERVER_BOUND_PROFILE_STORAGE_KEYS) {
    assert.equal(
      harness.storage.getItem(profileStorageKey('profile-a', logicalKey)),
      null,
    );
  }
  for (const logicalKey of ALL_PROFILE_STORAGE_KEYS.slice(3)) {
    assert.notEqual(
      harness.storage.getItem(profileStorageKey('profile-a', logicalKey)),
      null,
      'layout preferences survive a Server rebind',
    );
  }
  assert.equal(harness.storage.getItem(marker), SERVER_B);
  assert.equal(harness.storage.getItem('conversation-storage'), 'legacy remains');
  assert.equal(
    harness.storage.getItem(profileStorageKey('profile-b', 'conversation-storage')),
    'other profile',
  );
  assert.equal(
    harness.storage.getItem(profileStorageKey('profile-a', 'unrelated')),
    'unrelated scoped value',
  );
  const rebindOperations = harness.operations.slice(operationStart);
  const markerWriteIndex = rebindOperations.findLastIndex(
    ([kind, key]) => kind === 'set' && key === marker,
  );
  const lastServerKeyRemoval = rebindOperations.findLastIndex(
    ([kind, key]) => kind === 'remove'
      && SERVER_BOUND_PROFILE_STORAGE_KEYS.some(
        (logicalKey) => key === profileStorageKey('profile-a', logicalKey),
      ),
  );
  assert.ok(markerWriteIndex > lastServerKeyRemoval);
}

function testStorageFailuresAreWrappedAndRebindIsRetryable() {
  const {
    BOUND_SERVER_INSTANCE_MARKER,
    ProfileStorageUnavailableError,
    SERVER_BOUND_PROFILE_STORAGE_KEYS,
    prepareProfileStorageForServer,
    profileStorageKey,
  } = require(profileStorageModule);

  for (const method of ['get', 'set', 'remove']) {
    const harness = memoryStorage();
    harness.failWith((kind) => {
      if (kind === method) throw new DOMException(`${method} blocked`);
    });
    assert.throws(
      () => prepareProfileStorageForServer(
        harness.storage,
        'profile-a',
        SERVER_A,
        SERVER_BOUND_PROFILE_STORAGE_KEYS,
      ),
      (error) => error instanceof ProfileStorageUnavailableError
        && error.cause instanceof DOMException,
    );
  }

  const marker = profileStorageKey('profile-a', BOUND_SERVER_INSTANCE_MARKER);
  const failingKey = profileStorageKey('profile-a', 'chattree.projectOrder');
  const harness = memoryStorage({
    [marker]: SERVER_A,
    [failingKey]: 'server-a-data',
  });
  harness.failWith((kind, key) => {
    if (kind === 'remove' && key === failingKey) {
      throw new DOMException('remove blocked');
    }
  });
  assert.throws(
    () => prepareProfileStorageForServer(
      harness.storage,
      'profile-a',
      SERVER_B,
      SERVER_BOUND_PROFILE_STORAGE_KEYS,
    ),
    ProfileStorageUnavailableError,
  );
  assert.equal(harness.values.get(marker), SERVER_A);
  harness.clearFailure();
  prepareProfileStorageForServer(
    harness.storage,
    'profile-a',
    SERVER_B,
    SERVER_BOUND_PROFILE_STORAGE_KEYS,
  );
  assert.equal(harness.values.get(marker), SERVER_B);
  assert.equal(harness.values.has(failingKey), false);
}

function main() {
  testCanonicalKeysAndCentralKeySets();
  testOnlyLocalAdoptsLegacyOnceWithoutOverwritingScopedValues();
  testMigrationFailureLeavesMarkerUnsetAndRetryConverges();
  testFirstBindSameBindAndExactRebindCleanup();
  testStorageFailuresAreWrappedAndRebindIsRetryable();
  console.log('profile storage tests passed');
}

main();
