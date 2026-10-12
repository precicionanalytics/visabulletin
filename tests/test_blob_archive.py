import io
import os
import unittest
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from api import index
from bulletin_storage import BlobBulletinArchive, MAX_PDF_BYTES


class MemoryBlobClient:
    def __init__(self):
        self.files = {}
        self.read_access = []

    def list_objects(self, prefix, limit, cursor):
        items = [SimpleNamespace(pathname=path, size=len(data), uploaded_at=datetime(2026, 10, 1))
                 for path, data in self.files.items() if path.startswith(prefix)]
        return SimpleNamespace(blobs=items, cursor=None, has_more=False)

    def head(self, pathname):
        return SimpleNamespace(size=len(self.files[pathname]))

    def get(self, pathname, access, timeout, use_cache):
        self.read_access.append((access, use_cache))
        return SimpleNamespace(status_code=200, content=self.files[pathname])

    def put(self, pathname, data, **options):
        assert options["access"] == "private"
        self.files[pathname] = data

    def delete(self, pathname):
        self.files.pop(pathname, None)


class BlobArchiveTests(unittest.TestCase):
    def setUp(self):
        self.blobs = MemoryBlobClient()
        self.archive = BlobBulletinArchive(self.blobs)
        self.env = patch.dict(os.environ, {
            "BLOB_READ_WRITE_TOKEN": "test-store-token", "UPLOAD_ADMIN_PASSWORD": "test-password",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

        @contextmanager
        def archive_context():
            yield self.archive

        self.context = patch.object(index, "_blob_archive", archive_context)
        self.context.start()
        self.addCleanup(self.context.stop)
        self.client = index.app.test_client()
        self.auth = {"Authorization": "Bearer test-password"}

    def test_completion_without_password_is_rejected(self):
        response = self.client.post("/bulletins/complete", json={})
        self.assertEqual(response.status_code, 401)

    def test_legacy_upload_cannot_bypass_password(self):
        response = self.client.post("/bulletins/upload", data={"bulletin_pdf": (io.BytesIO(b"%PDF"), "October2026.pdf")})
        self.assertEqual(response.status_code, 401)

    def test_completion_rejects_external_url_before_reading(self):
        response = self.client.post("/bulletins/complete", headers=self.auth,
                                    json={"pathname": "https://example.com/test.pdf", "filename": "October2026.pdf"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.blobs.read_access, [])

    def test_configuration_does_not_disclose_credentials(self):
        response = self.client.get("/bulletins/upload-config")
        self.assertEqual(response.json["max_bytes"], MAX_PDF_BYTES)
        self.assertNotIn("test-store-token", response.get_data(as_text=True))
        self.assertNotIn("test-password", response.get_data(as_text=True))

    def test_browser_bundle_is_served(self):
        response = self.client.get("/static/blob-client.js")
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)

    def test_oversized_staging_pdf_is_rejected_before_download(self):
        pathname = "incoming/" + "a" * 32 + ".pdf"
        self.blobs.files[pathname] = b"x" * (MAX_PDF_BYTES + 1)
        response = self.client.post("/bulletins/complete", headers=self.auth,
                                    json={"pathname": pathname, "filename": "October2026.pdf"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.blobs.read_access, [])

    def test_listing_merges_blob_and_bundled_bulletins(self):
        self.blobs.files["bulletins/visabulletin_January2027.pdf"] = b"test"
        filenames = {entry["filename"] for entry in self.client.get("/bulletins").json["bulletins"]}
        self.assertIn("visabulletin_January2027.pdf", filenames)
        self.assertGreater(len(filenames), 1)

    def test_staging_files_are_not_listed(self):
        self.blobs.files["incoming/" + "a" * 32 + ".pdf"] = b"test"
        self.assertFalse(any(entry["filename"].startswith("incoming")
                             for entry in self.client.get("/bulletins").json["bulletins"]))

    def test_blob_version_is_used_instead_of_bundled_pdf(self):
        filename = index._local_bulletin_files()[0]["filename"]
        self.blobs.files["bulletins/" + filename] = b"blob version"
        self.assertEqual(index._read_bulletin_file(filename), b"blob version")
        self.assertEqual(self.blobs.read_access, [("private", False)])

    def test_invalid_pdf_is_not_published_and_staging_is_removed(self):
        pathname = "incoming/" + "a" * 32 + ".pdf"
        self.blobs.files[pathname] = b"not a PDF"
        response = self.client.post("/bulletins/complete", headers=self.auth,
                                    json={"pathname": pathname, "filename": "October2026.pdf"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.blobs.files, {})

    def test_valid_bulletin_is_published_without_local_write(self):
        filename = index._local_bulletin_files()[0]["filename"]
        with open(os.path.join(index.BULLETINS_DIR, filename), "rb") as handle:
            data = handle.read()
        pathname = "incoming/" + "a" * 32 + ".pdf"
        self.blobs.files[pathname] = data
        with patch("builtins.open", side_effect=AssertionError("must not write to the deployment filesystem")):
            response = self.client.post("/bulletins/complete", headers=self.auth,
                                        json={"pathname": pathname, "filename": filename})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.blobs.files["bulletins/" + response.json["filename"]], data)
        self.assertNotIn(pathname, self.blobs.files)

    def test_comparison_reads_blob_pdf(self):
        filenames = [entry["filename"] for entry in index._local_bulletin_files()[:2]]
        with open(os.path.join(index.BULLETINS_DIR, filenames[1]), "rb") as handle:
            self.blobs.files["bulletins/" + filenames[1]] = handle.read()
        response = self.client.post("/generate", json={"bulletin_a": filenames[0], "bulletin_b": filenames[1]})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIn("Final Action Dates", response.json["csv"])
        self.assertEqual(self.blobs.read_access, [("private", False)])

    def test_prediction_reads_blob_history(self):
        filename = index._local_bulletin_files()[-1]["filename"]
        with open(os.path.join(index.BULLETINS_DIR, filename), "rb") as handle:
            self.blobs.files["bulletins/" + filename] = handle.read()
        with patch.dict(index._SNAPSHOT_CACHE, clear=True):
            response = self.client.post("/project", json={"lookback": 2})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIn("private", [access for access, _ in self.blobs.read_access])


if __name__ == "__main__":
    unittest.main()