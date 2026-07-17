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

const ownershipModule = path.join(
  __dirname,
  '../src/runtime/profileRendererOwnership.ts',
);

function loadFreshOwnership() {
  delete require.cache[require.resolve(ownershipModule)];
  return require(ownershipModule);
}

function sharedLockManager() {
  const held = new Set();
  const calls = [];
  return {
    calls,
    manager: {
      async request(name, options, callback) {
        calls.push({ name, options });
        if (held.has(name)) {
          await callback(null);
          return;
        }
        held.add(name);
        await callback({ name });
      },
    },
  };
}

async function testClaimIsExclusiveAcrossRendererRealms() {
  const shared = sharedLockManager();
  const firstRealm = loadFreshOwnership();
  const first = firstRealm.acquireProfileRendererOwnership(
    'profile a',
    shared.manager,
  );
  assert.equal(
    first,
    firstRealm.acquireProfileRendererOwnership('profile a', shared.manager),
  );
  await first;
  assert.deepEqual(shared.calls[0], {
    name: 'chattree-profile-renderer:profile%20a',
    options: { mode: 'exclusive', ifAvailable: true },
  });

  const secondRealm = loadFreshOwnership();
  await assert.rejects(
    secondRealm.acquireProfileRendererOwnership('profile a', shared.manager),
    /already open in another tab/,
  );
}

async function testFailuresAreStableAndFailClosed() {
  const unavailable = loadFreshOwnership();
  await assert.rejects(
    unavailable.acquireProfileRendererOwnership('profile-a', {}),
    /ownership is unavailable/,
  );
  await assert.rejects(
    unavailable.acquireProfileRendererOwnership('profile-a', {
      request() {
        throw new Error('must reuse the first failed claim');
      },
    }),
    /ownership is unavailable/,
  );

  const changing = loadFreshOwnership();
  const shared = sharedLockManager();
  await changing.acquireProfileRendererOwnership('profile-a', shared.manager);
  await assert.rejects(
    changing.acquireProfileRendererOwnership('profile-b', shared.manager),
    /cannot change inside one page/,
  );
}

async function main() {
  await testClaimIsExclusiveAcrossRendererRealms();
  await testFailuresAreStableAndFailClosed();
  console.log('profile renderer ownership tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
