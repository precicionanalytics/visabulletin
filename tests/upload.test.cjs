const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { test } = require('node:test');

const html = execFileSync(process.env.PYTHON || 'python3', [
  '-c',
  'from api.index import app; print(app.test_client().get("/").get_data(as_text=True))',
], { encoding: 'utf8', env: { ...process.env, VERCEL: '1' } });
const source = html.match(/async function uploadBulletinFile\(file\) \{[\s\S]*?\n    \}/)[0];
const localConfig = { blob_enabled: false, auth_required: false, max_bytes: 4_436_000 };
const createUploader = (fetchImplementation, config = localConfig, blobUpload, passwordPrompt = async () => 'test-password') =>
  new Function('fetch', 'window', 'requestUploadPassword', `return (${source});`)(
    (url, options) => url === '/bulletins/upload-config'
      ? Promise.resolve(Response.json(config)) : fetchImplementation(url, options),
    { uploadPrivateBulletin: blobUpload }, passwordPrompt,
  );
const smallPdf = new File(['%PDF-test'], 'October2026.pdf', { type: 'application/pdf' });

test('oversized Vercel upload is rejected before sending the request', async () => {
  let requested = false;
  const upload = createUploader(async () => { requested = true; });
  const largePdf = new File([Buffer.alloc(4_500_000)], 'October2026.pdf');
  await assert.rejects(upload(largePdf), /upload limit/);
  assert.equal(requested, false);
});

test('platform plain-text 413 reports the upload limit instead of a JSON parsing error', async () => {
  const upload = createUploader(async () => new Response('FUNCTION_PAYLOAD_TOO_LARGE', {
    status: 413,
    headers: { 'content-type': 'text/plain' },
  }));
  await assert.rejects(upload(smallPdf), /above 4\.5 MB/);
});

test('non-JSON server failure reports its HTTP status', async () => {
  const upload = createUploader(async () => new Response('<html>Server error</html>', {
    status: 500,
    headers: { 'content-type': 'text/html' },
  }));
  await assert.rejects(upload(smallPdf), /Upload failed \(HTTP 500\)/);
});

test('JSON validation errors retain the server error message', async () => {
  const upload = createUploader(async () => Response.json({ error: 'Invalid PDF' }, { status: 400 }));
  await assert.rejects(upload(smallPdf), /Invalid PDF/);
});

test('unsuccessful JSON responses without an error field are not treated as success', async () => {
  const upload = createUploader(async () => Response.json({}, { status: 500 }));
  await assert.rejects(upload(smallPdf), /Upload failed \(HTTP 500\)/);
});

test('small PDF is sent as multipart data and the archive result is returned', async () => {
  const result = { label: 'October 2026', filename: 'visabulletin_October2026.pdf', bulletins: [] };
  const upload = createUploader(async (url, options) => {
    assert.equal(url, '/bulletins/upload');
    assert.equal(options.method, 'POST');
    assert.equal(options.body.get('bulletin_pdf'), smallPdf);
    return Response.json(result);
  });
  assert.deepEqual(await upload(smallPdf), result);
});

test('6.5 MB PDF uploads through Blob and only metadata is posted to Flask', async () => {
  const largePdf = new File([Buffer.alloc(6_500_000)], 'October2026.pdf', { type: 'application/pdf' });
  const result = { filename: 'visabulletin_October2026.pdf', bulletins: [] };
  let uploadedFile;
  const upload = createUploader(async (url, options) => {
    assert.equal(url, '/bulletins/complete');
    assert.equal(options.headers.Authorization, 'Bearer test-password');
    assert.equal(options.headers['Content-Type'], 'application/json');
    assert.ok(options.body.length < 300);
    assert.equal(JSON.parse(options.body).filename, largePdf.name);
    return Response.json(result);
  }, { blob_enabled: true, auth_required: true, max_bytes: 10 * 1024 * 1024 }, async (pathname, file, password) => {
    assert.match(pathname, /^incoming\/[a-f0-9]{32}\.pdf$/);
    assert.equal(password, 'test-password');
    uploadedFile = file;
    return { pathname };
  });
  assert.deepEqual(await upload(largePdf), result);
  assert.equal(uploadedFile, largePdf);
});

test('failed Blob upload does not publish a bulletin', async () => {
  let completed = false;
  const upload = createUploader(async () => { completed = true; },
    { blob_enabled: true, auth_required: true, max_bytes: 10 * 1024 * 1024 },
    async () => { throw new Error('Blob upload failed'); });
  await assert.rejects(upload(smallPdf), /Blob upload failed/);
  assert.equal(completed, false);
});

test('cancelled admin prompt does not upload a PDF', async () => {
  let requested = false;
  const upload = createUploader(async () => { requested = true; },
    { ...localConfig, auth_required: true }, undefined,
    async () => { throw new Error('Upload cancelled.'); });
  await assert.rejects(upload(smallPdf), /Upload cancelled/);
  assert.equal(requested, false);
});