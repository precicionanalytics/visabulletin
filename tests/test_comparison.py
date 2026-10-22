import csv
import io
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from api import index


class ComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.october = Path(index.BULLETINS_DIR, "visabulletin_October2026.pdf").read_bytes()
        cls.november = Path(index.BULLETINS_DIR, "visabulletin_November2026.pdf").read_bytes()
        with io.BytesIO() as output:
            with Image.new("RGB", (8, 8), "white") as image:
                image.save(output, format="PDF")
            cls.no_tables = output.getvalue()

    def setUp(self):
        self.client = index.app.test_client()

    def compare_pdfs(self, previous, current):
        response = self.client.post("/generate", data={
            "previous_pdf": (io.BytesIO(previous), "October2026.pdf"),
            "current_pdf": (io.BytesIO(current), "November2026.pdf"),
        })
        self.addCleanup(response.request.close)
        self.addCleanup(response.close)
        return response

    def test_october_to_november_archive_has_both_tables_and_real_movements(self):
        with patch.dict(os.environ, {"BLOB_READ_WRITE_TOKEN": ""}):
            response = self.client.post("/generate", json={
                "bulletin_a": "visabulletin_November2026.pdf",
                "bulletin_b": "visabulletin_October2026.pdf",
            })
        self.assertEqual(response.status_code, 200, response.json)
        rows = list(csv.DictReader(io.StringIO(response.json["csv"])))
        self.assertEqual(len(rows), 18)
        self.assertTrue(all(row["October 2026"] and row["November 2026"] for row in rows))
        movements = {
            (row["Table"], row["Category"], row["Country"]): row["ChangeDays"]
            for row in rows
        }
        self.assertEqual(movements[("Final Action Dates", "EB1", "China")], "31")
        self.assertEqual(movements[("Final Action Dates", "EB2", "ROW")], "90")
        self.assertEqual(movements[("Final Action Dates", "EB3", "India")], "21")
        self.assertEqual(movements[("Dates for Filing", "EB2", "India")], "0")
        self.assertIn("Final Action EB2 ROW forward by 90 days", response.json["prompt"])

    def test_previous_pdf_without_tables_is_rejected_instead_of_exporting_blanks(self):
        response = self.compare_pdfs(self.no_tables, self.november)
        self.assertEqual(response.status_code, 400)
        self.assertIn("October 2026", response.json["error"])
        self.assertIn("Final Action Dates", response.json["error"])
        self.assertNotIn("csv", response.json)

    def test_current_pdf_without_tables_is_rejected(self):
        response = self.compare_pdfs(self.october, self.no_tables)
        self.assertEqual(response.status_code, 400)
        self.assertIn("November 2026", response.json["error"])

    def test_missing_filing_table_is_reported_for_either_month(self):
        final, filing = index._find_eb_tables_from_pdf(self.october)
        for missing_month, tables in [
            ("October 2026", [(final, None), (final, filing)]),
            ("November 2026", [(final, filing), (final, None)]),
        ]:
            with self.subTest(month=missing_month):
                with patch.object(index, "_find_eb_tables_from_pdf", side_effect=tables):
                    response = self.compare_pdfs(self.october, self.november)
                self.assertEqual(response.status_code, 400)
                self.assertIn(missing_month, response.json["error"])
                self.assertIn("Dates for Filing", response.json["error"])

    def test_website_without_previous_month_table_is_rejected(self):
        table = (
            "<table><tr><th>Employment</th><th>China</th><th>India</th></tr>"
            "<tr><td>1st</td><td>C</td><td>C</td><td>C</td></tr></table>"
        )
        with patch.object(index.requests, "get", side_effect=[
            SimpleNamespace(text="<html>No bulletin tables</html>"),
            SimpleNamespace(text=table + table),
        ]):
            response = self.client.post("/generate", json={"month": "november"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("October 2026", response.json["error"])


if __name__ == "__main__":
    unittest.main()
