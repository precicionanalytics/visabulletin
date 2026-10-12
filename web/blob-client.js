import { upload } from '@vercel/blob/client';

window.uploadPrivateBulletin = (pathname, file, password) => upload(pathname, file, {
  access: 'private',
  contentType: 'application/pdf',
  multipart: true,
  handleUploadUrl: '/api/blob-upload',
  clientPayload: JSON.stringify({ password }),
});