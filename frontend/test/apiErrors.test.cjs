const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const axios = require('axios');

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

const errorsModule = path.join(__dirname, '../src/api/errors.ts');
const {
  ChatTreeApiError,
  getApiErrorMessage,
  normalizeApiError,
  parseModernErrorEnvelope,
} = require(errorsModule);

function axiosError({ status, data, headers = {}, code }) {
  const error = new Error('request failed');
  error.isAxiosError = true;
  error.code = code;
  if (status !== undefined) error.response = { status, data, headers };
  return error;
}

function testModernEnvelope() {
  const source = axiosError({
    status: 409,
    data: {
      error: {
        code: 'active_runs_present',
        message: 'blocked',
        retryable: true,
        request_id: 'req_1',
        details: { active_run_ids: ['run_1'] },
      },
    },
  });
  const modern = normalizeApiError(source);

  assert.ok(modern instanceof ChatTreeApiError);
  assert.equal(modern.status, 409);
  assert.equal(modern.code, 'active_runs_present');
  assert.equal(modern.message, 'blocked');
  assert.equal(modern.retryable, true);
  assert.equal(modern.requestId, 'req_1');
  assert.deepEqual(modern.details.active_run_ids, ['run_1']);
  assert.equal(modern.cause, source);
  assert.equal(getApiErrorMessage(modern, 'fallback'), 'blocked');

  assert.deepEqual(parseModernErrorEnvelope(409, source.response.data), {
    status: 409,
    code: 'active_runs_present',
    message: 'blocked',
    retryable: true,
    requestId: 'req_1',
    details: { active_run_ids: ['run_1'] },
  });
}

function testMalformedModernEnvelopesAreRejected() {
  const malformedBodies = [
    { error: { message: 'missing code', retryable: false, request_id: 'req_1' } },
    { error: { code: 'bad', message: 7, retryable: false, request_id: 'req_1' } },
    { error: { code: 'bad', message: 'bad', retryable: 'no', request_id: 'req_1' } },
    { error: { code: 'bad', message: 'bad', retryable: false, request_id: 'not valid!' } },
    {
      error: {
        code: 'bad',
        message: 'bad',
        retryable: false,
        request_id: 'req_1',
        details: [],
      },
    },
  ];

  for (const data of malformedBodies) {
    assert.equal(parseModernErrorEnvelope(400, data), null);
    const normalized = normalizeApiError(axiosError({ status: 400, data }));
    assert.equal(normalized.code, 'unexpected_response');
    assert.equal(normalized.status, 400);
  }
  assert.equal(parseModernErrorEnvelope(200, {
    error: {
      code: 'not_an_error_response',
      message: 'invalid status',
      retryable: false,
      request_id: 'req_status',
    },
  }), null);
}

function testLegacyActiveRunConflict() {
  const legacy = normalizeApiError(axiosError({
    status: 409,
    data: {
      detail: {
        message: 'blocked',
        active_run_ids: ['run_old'],
      },
    },
    headers: { 'x-request-id': 'req_old' },
  }));

  assert.equal(legacy.code, 'active_runs_present');
  assert.equal(legacy.message, 'blocked');
  assert.equal(legacy.requestId, 'req_old');
  assert.deepEqual(legacy.details.active_run_ids, ['run_old']);
}

function testLegacyCodeIsNeverDerivedFromMessageText() {
  const legacy = normalizeApiError(axiosError({
    status: 409,
    data: {
      detail: {
        message: 'active_runs_present 该分支仍有运行中的任务',
        active_run_ids: [],
      },
    },
  }));

  assert.equal(legacy.code, 'http_error');
}

function testLegacyStringDetailAndHeader() {
  const legacy = normalizeApiError(axiosError({
    status: 404,
    data: { detail: 'not found' },
    headers: { 'X-Request-ID': 'req_legacy_404' },
  }));

  assert.equal(legacy.code, 'http_error');
  assert.equal(legacy.message, 'not found');
  assert.equal(legacy.requestId, 'req_legacy_404');
}

function testLegacyServerErrorIsSanitized() {
  const legacy = normalizeApiError(axiosError({
    status: 500,
    data: { detail: 'provider-secret' },
    headers: { 'x-request-id': 'req_500' },
  }));

  assert.equal(legacy.status, 500);
  assert.equal(legacy.code, 'internal_error');
  assert.equal(legacy.retryable, false);
  assert.equal(legacy.requestId, 'req_500');
  assert.equal(legacy.message.includes('provider-secret'), false);
}

function testTimeoutNetworkAndMalformedResponses() {
  const timeout = normalizeApiError(axiosError({ code: 'ECONNABORTED' }));
  assert.equal(timeout.code, 'request_timeout');
  assert.equal(timeout.retryable, true);

  const network = normalizeApiError(axiosError({}));
  assert.equal(network.code, 'network_error');
  assert.equal(network.retryable, true);

  const malformed = normalizeApiError(axiosError({
    status: 418,
    data: { unsupported: true },
  }));
  assert.equal(malformed.code, 'unexpected_response');
  assert.equal(malformed.status, 418);

  const malformedServerError = normalizeApiError(axiosError({
    status: 500,
    data: { unsupported: true },
  }));
  assert.equal(malformedServerError.code, 'unexpected_response');
  assert.equal(malformedServerError.status, 500);
}

function testCancellationPreservesIdentity() {
  const cancelled = new axios.CanceledError('cancelled');

  assert.equal(normalizeApiError(cancelled), cancelled);
}

function main() {
  testModernEnvelope();
  testMalformedModernEnvelopesAreRejected();
  testLegacyActiveRunConflict();
  testLegacyCodeIsNeverDerivedFromMessageText();
  testLegacyStringDetailAndHeader();
  testLegacyServerErrorIsSanitized();
  testTimeoutNetworkAndMalformedResponses();
  testCancellationPreservesIdentity();
  console.log('api error tests passed');
}

main();
