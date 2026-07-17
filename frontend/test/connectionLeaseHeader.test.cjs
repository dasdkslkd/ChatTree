const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const { AxiosHeaders } = require('axios');

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

const headerModule = path.join(__dirname, '../src/api/connectionLeaseHeader.ts');
const identityModule = path.join(__dirname, '../src/runtime/connectionIdentity.ts');
const LEASE_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const LEASE_B = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';

function testAcceptsOneLogicalCanonicalValue() {
  const headers = require(headerModule);
  assert.equal(headers.CONNECTION_LEASE_HEADER, 'X-ChatTree-Connection-Lease-ID');
  assert.equal(headers.readConnectionLeaseHeader({
    'x-chattree-connection-lease-id': LEASE_A,
  }), LEASE_A);
  assert.equal(headers.readConnectionLeaseHeader(new AxiosHeaders({
    'X-ChatTree-Connection-Lease-ID': LEASE_A,
  })), LEASE_A);
  assert.equal(headers.readConnectionLeaseHeader(new Headers({
    'X-ChatTree-Connection-Lease-ID': LEASE_A,
  })), LEASE_A);
  assert.equal(headers.readConnectionLeaseHeader({
    get(name) {
      return name.toLowerCase() === 'x-chattree-connection-lease-id'
        ? LEASE_A
        : null;
    },
  }), LEASE_A);
}

function testRejectsMissingAmbiguousOrSpoofedValues() {
  const { readConnectionLeaseHeader } = require(headerModule);
  for (const value of [
    null,
    undefined,
    {},
    { 'x-chattree-connection-lease-id': [LEASE_A] },
    { 'x-chattree-connection-lease-id': [LEASE_A, LEASE_A] },
    { 'x-chattree-connection-lease-id': `${LEASE_A}, ${LEASE_A}` },
    { 'x-chattree-connection-lease-id': `${LEASE_A} ` },
    { 'x-chattree-connection-lease-id': LEASE_A.toUpperCase() },
    { 'x-chattree-connection-lease-id': 'not-a-uuid' },
    {
      'x-chattree-connection-lease-id': LEASE_A,
      'X-ChatTree-Connection-Lease-ID': LEASE_A,
    },
    {
      entries() {
        return [
          ['x-chattree-connection-lease-id', LEASE_A],
          ['X-ChatTree-Connection-Lease-ID', LEASE_B],
        ][Symbol.iterator]();
      },
      get() { return LEASE_A; },
    },
  ]) {
    assert.equal(readConnectionLeaseHeader(value), null);
  }
}

function testMatchingRequirementFailsClosed() {
  const headers = require(headerModule);
  const { BoundServerLeaseChangedError } = require(identityModule);
  assert.equal(headers.requireMatchingConnectionLeaseHeader({
    'x-chattree-connection-lease-id': LEASE_A,
  }, LEASE_A), LEASE_A);
  for (const [bag, expected] of [
    [{}, LEASE_A],
    [{ 'x-chattree-connection-lease-id': LEASE_B }, LEASE_A],
    [{ 'x-chattree-connection-lease-id': LEASE_A }, 'invalid'],
  ]) {
    assert.throws(
      () => headers.requireMatchingConnectionLeaseHeader(bag, expected),
      BoundServerLeaseChangedError,
    );
  }
}

function main() {
  testAcceptsOneLogicalCanonicalValue();
  testRejectsMissingAmbiguousOrSpoofedValues();
  testMatchingRequirementFailsClosed();
  console.log('connection lease header tests passed');
}

main();
