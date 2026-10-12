import os
import re


MAX_PDF_BYTES = 10 * 1024 * 1024
ARCHIVE_PREFIX = "bulletins/"
INCOMING_PATTERN = re.compile(r"incoming/[a-f0-9]{32}\.pdf")
ARCHIVE_PATTERN = re.compile(r"bulletins/visabulletin_[A-Za-z]+\d{4}\.pdf")


class ArchiveStorageError(Exception):
    pass


class BlobBulletinArchive:
    def __init__(self, client):
        self.client = client

    def entries(self):
        entries = []
        cursor = None
        while True:
            page = self.client.list_objects(prefix=ARCHIVE_PREFIX, limit=1000, cursor=cursor)
            for blob in page.blobs:
                if ARCHIVE_PATTERN.fullmatch(blob.pathname):
                    entries.append({
                        "filename": os.path.basename(blob.pathname),
                        "version": (blob.uploaded_at.isoformat(), blob.size),
                    })
            if not page.has_more:
                return entries
            if not page.cursor or page.cursor == cursor:
                raise ArchiveStorageError("Blob returned an invalid archive cursor.")
            cursor = page.cursor

    def read(self, pathname):
        if not (ARCHIVE_PATTERN.fullmatch(pathname) or INCOMING_PATTERN.fullmatch(pathname)):
            raise ArchiveStorageError("Invalid Blob archive path.")
        metadata = self.client.head(pathname)
        if metadata.size > MAX_PDF_BYTES:
            raise ArchiveStorageError("PDF exceeds the 10 MB archive limit.")
        result = self.client.get(pathname, access="private", timeout=30, use_cache=False)
        if result is None or result.status_code != 200:
            raise ArchiveStorageError("The uploaded PDF could not be read from Blob.")
        if len(result.content) > MAX_PDF_BYTES:
            raise ArchiveStorageError("PDF exceeds the 10 MB archive limit.")
        return result.content

    def write(self, filename, pdf_bytes):
        pathname = ARCHIVE_PREFIX + filename
        if not ARCHIVE_PATTERN.fullmatch(pathname) or len(pdf_bytes) > MAX_PDF_BYTES:
            raise ArchiveStorageError("Invalid bulletin archive file.")
        return self.client.put(
            pathname, pdf_bytes, access="private", content_type="application/pdf",
            overwrite=True, cache_control_max_age=60, multipart=True,
        )

    def discard(self, pathname):
        if not INCOMING_PATTERN.fullmatch(pathname):
            raise ArchiveStorageError("Invalid staging path.")
        self.client.delete(pathname)