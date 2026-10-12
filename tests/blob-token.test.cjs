const assert = require('node:assert/strict');
const { test } = require('node:test');

async function callHandler(body, method = 'POST') {
  const { default: handler } = await import('../api/blob-upload.js');
  const response = {
    code: 200,
    headers: {},
    setHeader(name, value) { this.headers[name] = value; },
    status(code) { this.code = code; return this; },
    json(data) { this.data = data; return this; },
  };
  await handler({ method, body, headers: { host: 'localhost' } }, response);
  return response;
}

process.env.UPLOAD_ADMIN_PASSWORD = 'test-admin-password';
process.env.BLOB_READ_WRITE_TOKEN = 'vercel_blob_rw_teststore_testsecret';
process.env.VERCEL_PROJECT_PRODUCTION_URL = 'example.vercel.app';
process.env.VERCEL_BLOB_CALLBACK_URL = 'http://localhost:5051/api/blob-upload';

const tokenBody = (password, pathname = `incoming/${'a'.repeat(32)}.pdf`) => ({
  type: 'blob.generate-client-token',
  payload: { pathname, multipart: true, clientPayload: JSON.stringify({ password }) },
});

test('unauthenticated callers cannot receive a Blob upload token', async () => {
  const response = await callHandler(tokenBody('wrong-password'));
  assert.equal(response.code, 401);
  assert.equal(response.data.clientToken, undefined);
});

test('authorized callers cannot issue tokens for archive overwrites', async () => {
  const response = await callHandler(tokenBody('test-admin-password', 'bulletins/visabulletin_October2026.pdf'));
  assert.equal(response.code, 400);
});

test('malformed credential JSON returns a client error', async () => {
  const body = tokenBody('test-admin-password');
  body.payload.clientPayload = '{';
  assert.equal((await callHandler(body)).code, 400);
});

test('GET does not authorize an upload', async () => {
  assert.equal((await callHandler({}, 'GET')).code, 405);
});

test('official SDK generates a scoped multipart token for an authorized upload', async () => {
  const response = await callHandler(tokenBody('test-admin-password'));
  assert.equal(response.code, 200);
  assert.equal(response.data.type, 'blob.generate-client-token');
  assert.match(response.data.clientToken, /^vercel_blob_client_/);
  assert.equal(JSON.stringify(response.data).includes('test-admin-password'), false);
  const { getPayloadFromClientToken } = await import('@vercel/blob/client');
  const payload = getPayloadFromClientToken(response.data.clientToken);
  assert.equal(payload.maximumSizeInBytes, 10 * 1024 * 1024);
  assert.deepEqual(payload.allowedContentTypes, ['application/pdf']);
  assert.equal(payload.pathname, `incoming/${'a'.repeat(32)}.pdf`);
  assert.equal(payload.allowOverwrite, false);
  assert.equal(JSON.stringify(payload).includes('test-admin-password'), false);
});

test('unsigned completion callbacks cannot be trusted', async () => {
  const response = await callHandler({ type: 'blob.upload-completed', payload: {} });
  assert.equal(response.code, 400);
});

test('missing admin configuration disables token issuance', async () => {
  delete process.env.UPLOAD_ADMIN_PASSWORD;
  try {
    assert.equal((await callHandler(tokenBody('test-admin-password'))).code, 503);
  } finally {
    process.env.UPLOAD_ADMIN_PASSWORD = 'test-admin-password';
  }
});