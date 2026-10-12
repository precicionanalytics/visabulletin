import { timingSafeEqual } from 'node:crypto';
import { handleUpload } from '@vercel/blob/client';

const MAX_PDF_BYTES = 10 * 1024 * 1024;
const TOKEN_LIFETIME_MS = 15 * 60 * 1000;
const UPLOAD_PATH = /^incoming\/[a-f0-9]{32}\.pdf$/;

function passwordMatches(supplied, expected) {
  if (typeof supplied !== 'string' || !expected) return false;
  const suppliedBytes = Buffer.from(supplied);
  const expectedBytes = Buffer.from(expected);
  return suppliedBytes.length === expectedBytes.length && timingSafeEqual(suppliedBytes, expectedBytes);
}

export default async function handler(request, response) {
  response.setHeader('Cache-Control', 'no-store');
  if (request.method !== 'POST') {
    response.setHeader('Allow', 'POST');
    return response.status(405).json({ error: 'Use POST for upload authorization.' });
  }
  if (!process.env.BLOB_READ_WRITE_TOKEN || !process.env.UPLOAD_ADMIN_PASSWORD) {
    return response.status(503).json({ error: 'Blob uploads are not configured.' });
  }
  const body = request.body;
  if (!body || typeof body !== 'object') {
    return response.status(400).json({ error: 'Invalid upload request.' });
  }
  if (body.type === 'blob.generate-client-token') {
    let credentials;
    try {
      credentials = JSON.parse(body.payload?.clientPayload || '{}');
    } catch {
      return response.status(400).json({ error: 'Invalid upload credentials.' });
    }
    if (!passwordMatches(credentials.password, process.env.UPLOAD_ADMIN_PASSWORD)) {
      return response.status(401).json({ error: 'Incorrect admin password.' });
    }
    if (!UPLOAD_PATH.test(body.payload?.pathname || '')) {
      return response.status(400).json({ error: 'Invalid upload destination.' });
    }
  } else if (body.type !== 'blob.upload-completed') {
    return response.status(400).json({ error: 'Invalid upload request type.' });
  }
  try {
    const result = await handleUpload({
      request,
      body,
      token: process.env.BLOB_READ_WRITE_TOKEN,
      onBeforeGenerateToken: async pathname => {
        if (!UPLOAD_PATH.test(pathname)) throw new Error('Invalid upload destination.');
        return {
          allowedContentTypes: ['application/pdf'],
          maximumSizeInBytes: MAX_PDF_BYTES,
          addRandomSuffix: false,
          allowOverwrite: false,
          tokenPayload: '',
          validUntil: Date.now() + TOKEN_LIFETIME_MS,
        };
      },
      onUploadCompleted: async () => undefined,
    });
    return response.status(200).json(result);
  } catch {
    return response.status(400).json({ error: 'Could not authorize or verify the Blob upload.' });
  }
}