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

const { parseFrontendRoute, buildFrontendRoute } = require('../src/runtime/profileRoute.ts');
const {
  buildProfileApiBase,
  getFrontendBootstrap,
  initializeFrontendBootstrap,
  resolveFrontendBootstrap,
} = require('../src/runtime/frontendBootstrap.ts');

function testExactProfileRoutes() {
  assert.deepEqual(parseFrontendRoute('/s/profile-a'), {
    kind: 'profile', profileId: 'profile-a',
  });
  assert.deepEqual(parseFrontendRoute('/s/profile-a/c/conv-1'), {
    kind: 'conversation', profileId: 'profile-a', conversationId: 'conv-1',
  });
  assert.deepEqual(parseFrontendRoute('/s/profile-a/c/conv-1/n/node-2'), {
    kind: 'node', profileId: 'profile-a', conversationId: 'conv-1', nodeId: 'node-2',
  });
  assert.deepEqual(parseFrontendRoute('/s/profile-a/r/run-1'), {
    kind: 'run', profileId: 'profile-a', runId: 'run-1',
  });
  assert.equal(
    buildFrontendRoute({ kind: 'run', profileId: 'profile a', runId: 'run/1' }),
    '/s/profile%20a/r/run%2F1',
  );
  assert.equal(buildProfileApiBase('profile a'), '/p/profile%20a/api/v1');
  assert.deepEqual(
    parseFrontendRoute(buildFrontendRoute({
      kind: 'run', profileId: 'profile a', runId: 'run/1',
    })),
    { kind: 'run', profileId: 'profile a', runId: 'run/1' },
  );
}

function testBrowserBootstrapDerivesCanonicalLauncherProxy() {
  assert.deepEqual(resolveFrontendBootstrap({
    injected: undefined,
    href: 'http://127.0.0.1:5173/s/profile-a',
  }), {
    profileId: 'profile-a',
    apiBase: '/p/profile-a/api/v1',
  });
  assert.deepEqual(resolveFrontendBootstrap({
    injected: undefined,
    href: 'https://launcher.example:9443/s/profile%20a',
  }), {
    profileId: 'profile a',
    apiBase: '/p/profile%20a/api/v1',
  });
  assert.deepEqual(resolveFrontendBootstrap({
    injected: undefined,
    href: 'HTTP://LAUNCHER.EXAMPLE:80/s/profile-a',
  }), {
    profileId: 'profile-a',
    apiBase: '/p/profile-a/api/v1',
  });

  assert.throws(() => resolveFrontendBootstrap({
    injected: { profileId: 'profile-b', apiBase: '/p/profile-b/api/v1' },
    href: 'http://127.0.0.1:5173/s/profile-a',
  }), /does not match bootstrap Profile/);

  assert.throws(() => resolveFrontendBootstrap({
    injected: undefined,
    href: 'http://127.0.0.1:5173/',
  }), /Profile route is required/);

  for (const href of ['file:///s/profile-a', 'javascript:alert(1)', 'ftp://host/s/profile-a']) {
    assert.throws(() => resolveFrontendBootstrap({
      injected: undefined, href,
    }), /Launcher HTTP origin is required/);
  }
}

function testTrustedInjectedBootstrap() {
  assert.deepEqual(resolveFrontendBootstrap({
    injected: {
      profileId: 'profile-a',
      apiBase: 'https://launcher.example:9443/p/profile-a/api/v1',
    },
    href: 'http://127.0.0.1:5173/s/profile-a',
  }), {
    profileId: 'profile-a',
    apiBase: 'https://launcher.example:9443/p/profile-a/api/v1',
  });
  assert.deepEqual(resolveFrontendBootstrap({
    injected: {
      profileId: 'profile-a',
      apiBase: 'HTTPS://LAUNCHER.EXAMPLE:443/p/profile-a/api/v1',
    },
    href: 'http://127.0.0.1:5173/s/profile-a',
  }), {
    profileId: 'profile-a',
    apiBase: 'https://launcher.example/p/profile-a/api/v1',
  });

  for (const injected of [
    null,
    {},
    { profileId: 'profile-a' },
    { profileId: 'profile-a', apiBase: '/p/profile-a/api/v1', extra: true },
    { profileId: 'profile-a', apiBase: '//evil.example/p/profile-a/api/v1' },
    { profileId: 'profile-a', apiBase: '/p/profile-a/api/v1/' },
    { profileId: 'profile-a', apiBase: 'ws://launcher.example/p/profile-a/api/v1' },
    { profileId: 'profile-a', apiBase: 'https:launcher.example/p/profile-a/api/v1' },
    { profileId: 'profile-a', apiBase: 'https://user:pass@launcher.example/p/profile-a/api/v1' },
    { profileId: 'profile-a', apiBase: 'https://launcher.example/p/profile-a/api/v1/' },
    { profileId: 'profile-a', apiBase: 'https://launcher.example/p/profile-a/api/v1?' },
    { profileId: 'profile-a', apiBase: 'https://launcher.example/p/profile-a/api/v1#' },
    { profileId: 'profile-a', apiBase: 'https://launcher.example/p/profile-a/api/v1?x=1' },
    { profileId: 'profile-a', apiBase: 'https://launcher.example/p/profile-a/api/v1#x' },
    { profileId: 'profile-a', apiBase: 'https://launcher.example/p/profile-a/./api/v1' },
    { profileId: 'profile-a', apiBase: 'https://launcher.example/p/profile-a/temp/../api/v1' },
    { profileId: 'profile-a', apiBase: 'https://launcher.example/p/profile-a/%2e/api/v1' },
    { profileId: 'profile-a', apiBase: 'https://launcher.example/p/profile-a/temp/%2e%2e/api/v1' },
    { profileId: 'profile-a', apiBase: 'http://launcher.example\\p\\profile-a\\api\\v1' },
  ]) {
    assert.throws(() => resolveFrontendBootstrap({
      injected,
      href: 'http://127.0.0.1:5173/s/profile-a',
    }));
  }
}

function testRoutesFailClosed() {
  for (const pathname of [
    '/s/profile-a/', '/s//profile-a', '/s/profile-a//c/conv-1',
    '/s/%', '/s/%E0%A4%A', '/s/profile%00a', '/s/profile\u0000a', '/s/%70rofile-a',
  ]) {
    assert.throws(() => parseFrontendRoute(pathname));
  }

  for (const href of [
    'http://127.0.0.1:5173/s/profile-a?',
    'http://127.0.0.1:5173/s/profile-a#',
    'http://127.0.0.1:5173/s/profile-a?x=1',
    'http://127.0.0.1:5173/s/profile-a#x',
  ]) {
    assert.throws(() => resolveFrontendBootstrap({ injected: undefined, href }),
      /query or hash/);
  }

  for (const href of [
    'http://127.0.0.1:5173/s/./profile-a',
    'http://127.0.0.1:5173/s/%2e/profile-a',
    'http://127.0.0.1:5173/s/temp/../profile-a',
    'http://127.0.0.1:5173/s/temp/%2e%2e/profile-a',
    'http://127.0.0.1:5173/s\\profile-a',
    'http:127.0.0.1/s/profile-a',
  ]) {
    assert.throws(() => resolveFrontendBootstrap({ injected: undefined, href }),
      /canonical/);
  }
}

function testDotSegmentsCannotBeIdentifiers() {
  for (const value of ['.', '..']) {
    assert.throws(() => buildProfileApiBase(value), /invalid/);
    assert.throws(() => buildFrontendRoute({ kind: 'profile', profileId: value }), /invalid/);
    assert.throws(() => buildFrontendRoute({
      kind: 'conversation', profileId: 'profile-a', conversationId: value,
    }), /invalid/);
    assert.throws(() => buildFrontendRoute({
      kind: 'node', profileId: 'profile-a', conversationId: 'conv-1', nodeId: value,
    }), /invalid/);
    assert.throws(() => buildFrontendRoute({
      kind: 'run', profileId: 'profile-a', runId: value,
    }), /invalid/);
  }

  for (const pathname of [
    '/s/.', '/s/..', '/s/%2e', '/s/%2E%2E',
    '/s/profile-a/c/.', '/s/profile-a/c/..',
    '/s/profile-a/c/conv-1/n/.', '/s/profile-a/r/..',
  ]) {
    assert.throws(() => parseFrontendRoute(pathname), /invalid/);
  }
}

function testInitializationIsImmutable() {
  const originalWindow = globalThis.window;
  assert.throws(() => getFrontendBootstrap(), /has not been initialized/);

  try {
    globalThis.window = {
      location: { href: 'http://127.0.0.1:5173/s/profile-a' },
    };
    const initialized = initializeFrontendBootstrap();
    assert.deepEqual(initialized, {
      profileId: 'profile-a',
      apiBase: '/p/profile-a/api/v1',
    });
    assert.equal(Object.isFrozen(initialized), true);
    assert.deepEqual(getFrontendBootstrap(), initialized);

    globalThis.window = {
      location: { href: 'http://127.0.0.1:5173/s/profile-b' },
    };
    assert.throws(() => initializeFrontendBootstrap(), /cannot change inside one page instance/);
    assert.deepEqual(getFrontendBootstrap(), initialized);
  } finally {
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  }
}

function testMainDefersApplicationImportsUntilBootstrap() {
  const mainPath = path.join(__dirname, '../src/main.tsx');
  const mainSource = fs.readFileSync(mainPath, 'utf8');
  const sourceFile = ts.createSourceFile(mainPath, mainSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const imports = sourceFile.statements
    .filter(ts.isImportDeclaration)
    .map((statement) => statement.moduleSpecifier.text);

  assert.deepEqual(imports, [
    'react',
    'react-dom/client',
    './index.css',
    './runtime/frontendBootstrap',
  ]);
  const initializeIndex = mainSource.indexOf('initializeFrontendBootstrap()');
  const appImportIndex = mainSource.indexOf("import('./App')");
  assert.ok(initializeIndex >= 0, 'main.tsx must initialize the bootstrap');
  assert.ok(appImportIndex > initializeIndex, 'App must load only after bootstrap initialization');

  const appPath = path.join(__dirname, '../src/App.tsx');
  const appSource = fs.readFileSync(appPath, 'utf8');
  const appFile = ts.createSourceFile(appPath, appSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const appImports = appFile.statements
    .filter(ts.isImportDeclaration)
    .map((statement) => statement.moduleSpecifier.text);
  assert.equal(
    appImports.some((specifier) => /(?:pages|store|services|perf|components)\//.test(specifier)),
    false,
    'App binding shell must not statically import the business realm',
  );
  assert.match(appSource, /lazy\(\(\) => import\(['"]\.\/runtime\/ServerSessionApp['"]\)\)/);
  assert.match(appSource, /if \(!state\.context\)/);
  assert.match(appSource, /<ServerSessionApp binding=\{state\.context\} connected=\{state\.status === ['"]ready['"]\}/);
  assert.doesNotMatch(appSource, /<ServerSessionApp[^>]*\bkey=/);
}

testExactProfileRoutes();
testBrowserBootstrapDerivesCanonicalLauncherProxy();
testTrustedInjectedBootstrap();
testRoutesFailClosed();
testDotSegmentsCannotBeIdentifiers();
testInitializationIsImmutable();
testMainDefersApplicationImportsUntilBootstrap();
console.log('frontend bootstrap tests passed');
