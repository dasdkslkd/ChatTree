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
    },
  }).outputText;
  module._compile(output, filename);
};

const routePath = require.resolve('../src/runtime/profileRoute.ts');
const contextPath = require.resolve('../src/runtime/profileContext.ts');

function freshContext() {
  delete require.cache[routePath];
  delete require.cache[contextPath];
  return require(contextPath);
}

function testRouteIsTheOnlyProfileBootstrapSource() {
  const runtime = freshContext();
  const context = runtime.initializeProfileContext(
    'http://127.0.0.1:18100/s/profile%20one',
  );

  assert.deepEqual(context, {
    profileId: 'profile one',
    apiBase: '/p/profile%20one/api/v1',
  });
  assert.equal(Object.isFrozen(context), true);
  assert.equal(runtime.getProfileContext(), context);
  assert.equal(
    runtime.initializeProfileContext('http://127.0.0.1:18100/s/profile%20one'),
    context,
  );
}

function testOnePageCannotChangeProfile() {
  const runtime = freshContext();
  runtime.initializeProfileContext('http://127.0.0.1:18100/s/first');
  assert.throws(
    () => runtime.initializeProfileContext('http://127.0.0.1:18100/s/second'),
    /cannot change/i,
  );
}

function testOnlyCanonicalProfileRoutesAreAccepted() {
  const { readFrontendRouteLocation } = require(routePath);
  for (const href of [
    'http://127.0.0.1:18100/',
    'http://127.0.0.1:18100/s/local/c/conversation',
    'http://127.0.0.1:18100/s/local/r/run-1',
    'http://127.0.0.1:18100/s/local?x=1',
    'file:///s/local',
    'http://127.0.0.1:18100/s/%6cocal',
  ]) {
    assert.throws(() => readFrontendRouteLocation({ href }), href);
  }
}

testRouteIsTheOnlyProfileBootstrapSource();
testOnePageCannotChangeProfile();
testOnlyCanonicalProfileRoutesAreAccepted();
console.log('profileContext tests passed');
