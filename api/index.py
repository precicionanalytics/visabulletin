"""
Flask web interface for the Visa Bulletin Tracker.

Endpoints:
  GET  /healthy  — health check
  GET  /         — form to select month
  POST /generate — scrapes bulletin and returns CSV in a copyable textarea
"""

import io
import os
import re
import sys
from datetime import datetime
from dateutil import parser as dparser
from dateutil.relativedelta import relativedelta


import pdfplumber
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template_string, request, jsonify

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024

# ---------------------------------------------------------------------------
# Core scraping helpers (shared with visa_bulletin.py logic)
# ---------------------------------------------------------------------------

BASE_URL = (
    "https://travel.state.gov/content/travel/en/legal/visa-law0/"
    "visa-bulletin/{year}/visa-bulletin-for-{month}-{year}.html"
)


def build_url(dt: datetime) -> str:
    return BASE_URL.format(year=dt.year, month=dt.strftime("%B").lower())


def parse_date(value: str):
    value = value.strip()
    if value in ("C", "Current"):
        return "Current"
    if value == "U":
        return "Unavailable"
    try:
        return dparser.parse(value)
    except Exception:
        return value


def to_display(val) -> str:
    if val == "Current":
        return "Current"
    if val == "Unavailable":
        return "Unavailable"
    if isinstance(val, datetime):
        return val.strftime("%d-%b-%Y")
    return str(val)


def date_diff(old, new) -> int:
    if isinstance(old, datetime) and isinstance(new, datetime):
        return (new - old).days
    return 0


def _movement_text(old, new, diff: int) -> str:
  if isinstance(old, datetime) and isinstance(new, datetime):
    if diff > 0:
      return "Forward"
    if diff < 0:
      return "Retrogression"
    return "No Change"
  return "N/A"


def _change_days_text(old, new, diff: int) -> str:
  if isinstance(old, datetime) and isinstance(new, datetime):
    return str(diff)
  return ""


def _build_csv_text(prev_label: str, curr_label: str, final: dict, filing: dict) -> str:
  lines = [f"Table,Category,Country,{prev_label},{curr_label},ChangeDays,Movement"]
  for tbl_label, data_dict in [
    ("Final Action Dates", final),
    ("Dates for Filing", filing),
  ]:
    if not data_dict:
      continue
    for cat_key, eb_label in [("1st", "EB1"), ("2nd", "EB2"), ("3rd", "EB3")]:
      for country in ("ROW", "China", "India"):
        item = data_dict[cat_key][country]
        old = item["old"]
        new = item["new"]
        diff = item["diff"]
        lines.append(
          f"{tbl_label},{eb_label},{country},"
          f"{to_display(old)},{to_display(new)},"
          f"{_change_days_text(old, new, diff)},"
          f"{_movement_text(old, new, diff)}"
        )
  return "\n".join(lines)


def _build_movement_summary(final: dict, filing: dict) -> list[str]:
  points = []
  for tbl_label, data_dict in [("Final Action", final), ("Filing", filing)]:
    if not data_dict:
      continue
    for cat_key, eb_label in [("1st", "EB1"), ("2nd", "EB2"), ("3rd", "EB3")]:
      for country in ("India", "China", "ROW"):
        item = data_dict[cat_key][country]
        old = item["old"]
        new = item["new"]
        diff = item["diff"]
        if not isinstance(old, datetime) or not isinstance(new, datetime):
          continue
        if diff != 0:
          direction = "forward" if diff > 0 else "retrogressed"
          points.append(
            f"- {tbl_label} {eb_label} {country} {direction} by {abs(diff)} days"
          )

  if not points:
    return ["- Most categories are unchanged or marked Current/Unavailable."]

  return points[:6]


def _build_image_prompt(prev_label: str, curr_label: str, csv_text: str, final: dict, filing: dict) -> str:
  summary = "\n".join(_build_movement_summary(final, filing))
  return (
    "Create a high-quality Facebook infographic in the same visual style as my sample image for page name \"U.S. Immigration Hub\".\n"
    f"Title: VISA BULLETIN - {curr_label.upper()}\n"
    "Subtitle: EMPLOYMENT-BASED PREFERENCES (EB-1, EB-2 & EB-3)\n\n"
    f"Comparison period: {prev_label} -> {curr_label}\n"
    "Build two side-by-side sections: FINAL ACTION DATES and DATES FOR FILING.\n"
    "For each table, include columns: Category, Country, Previous Month, Current Month, Change.\n"
    "Use arrows and color coding: green up arrow for forward movement, red down arrow for retrogression, neutral for no change.\n"
    "Show change in days where both values are real dates.\n"
    "Include a Movement Summary box with concise bullet points.\n"
    "Keep branding prominent with U.S. Immigration Hub text and a professional immigration-news look.\n\n"
    "Movement Summary Notes:\n"
    f"{summary}\n\n"
    "Use this exact data:\n"
    f"{csv_text}"
  )


def _get_eb_tables(html: str):
    """Return (final_action_table, dates_for_filing_table) BeautifulSoup objects."""
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    eb = [
        t for t in tables
        if t.find_all("tr") and "Employment" in t.find_all("tr")[0].get_text()
    ]
    final   = eb[0] if len(eb) > 0 else None
    filing  = eb[1] if len(eb) > 1 else None
    return final, filing


def _table_to_dict(table) -> dict:
    if table is None:
        return {}
    rows = table.find_all("tr")
    data = {}
    for i, cat in enumerate(["1st", "2nd", "3rd"]):
        if len(rows) <= i + 1:
            data[cat] = {"ROW": "", "China": "", "India": ""}
            continue
        cols = rows[i + 1].find_all(["td", "th"])
        vals = [c.get_text(strip=True) for c in cols]
        data[cat] = {
            "ROW":   parse_date(vals[1]) if len(vals) > 1 else "",
            "China": parse_date(vals[2]) if len(vals) > 2 else "",
            "India": parse_date(vals[3]) if len(vals) > 3 else "",
        }
    return data


def _normalize_pdf_cell(value):
    if value is None:
        return ""
    return str(value).strip()


def _rows_from_pdf_bytes(pdf_bytes: bytes):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        tables = []
        for page in pdf.pages:
            for table in page.extract_tables():
                if not table:
                    continue
                normalized = [
                    [_normalize_pdf_cell(cell) for cell in row]
                    for row in table
                    if any(_normalize_pdf_cell(cell) for cell in row)
                ]
                if normalized:
                    tables.append(normalized)
        return tables


def _find_eb_tables_from_pdf(pdf_bytes: bytes):
    tables = _rows_from_pdf_bytes(pdf_bytes)
    eb_tables = []
    for table in tables:
        flat_text = " ".join(cell.lower() for row in table for cell in row if cell)
        if "china" in flat_text and "india" in flat_text and any(word in flat_text for word in ["1st", "2nd", "3rd"]):
            eb_tables.append(table)
    if len(eb_tables) >= 2:
        return eb_tables[0], eb_tables[1]
    if len(eb_tables) == 1:
        return eb_tables[0], None
    return None, None


def _parse_pdf_label(pdf_bytes: bytes):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    month_match = re.search(r"visa bulletin(?: for)?\s+([A-Za-z]+)\s+(\d{4})", text, re.IGNORECASE)
    if month_match:
        month_name = month_match.group(1)
        year = month_match.group(2)
        try:
            month_dt = dparser.parse(f"{month_name} {year}")
            return month_dt.strftime("%B %Y")
        except Exception:
            return f"{month_name} {year}"
    return None


def _parse_label_from_filename(filename: str):
    if not filename:
        return None
    month_match = re.search(r"([A-Za-z]+)[-_ ]+(\d{4})", filename)
    if month_match:
        month_name, year = month_match.group(1), month_match.group(2)
        try:
            month_dt = dparser.parse(f"{month_name} {year}")
            return month_dt.strftime("%B %Y")
        except Exception:
            return f"{month_name} {year}"
    month_only = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)", filename, re.IGNORECASE)
    if month_only:
        try:
            month_dt = dparser.parse(month_only.group(1))
            return month_dt.strftime("%B %Y")
        except Exception:
            return month_only.group(1).title()
    return None


def _table_rows_to_dict(table_rows) -> dict:
    if not table_rows:
        return {}
    data = {}
    for row in table_rows:
        if not row:
            continue
        first = row[0].strip().lower()
        if first in ("1st", "2nd", "3rd"):
            values = [cell.strip() for cell in row[1:]]
            if len(values) >= 3:
                data[first] = {
                    "ROW":   parse_date(values[0]),
                    "China": parse_date(values[1]),
                    "India": parse_date(values[2]),
                }
    return data


def generate_csv_from_pdf(previous_pdf: bytes, current_pdf: bytes, previous_filename: str = None, current_filename: str = None) -> tuple[str, str, str, str, str]:
    prev_label = (
        _parse_pdf_label(previous_pdf)
        or _parse_label_from_filename(previous_filename)
        or "Previous Month"
    )
    curr_label = (
        _parse_pdf_label(current_pdf)
        or _parse_label_from_filename(current_filename)
        or "Current Month"
    )

    prev_final, prev_filing = _find_eb_tables_from_pdf(previous_pdf)
    curr_final, curr_filing = _find_eb_tables_from_pdf(current_pdf)

    if not curr_final:
      return "", prev_label, curr_label, (
            "Could not find the employment-based table in the current PDF. "
            "Please make sure the PDF contains the Visa Bulletin employment-based tables."
      ), ""

    final = _build_comparison(_table_rows_to_dict(prev_final), _table_rows_to_dict(curr_final))
    filing = _build_comparison(_table_rows_to_dict(prev_filing), _table_rows_to_dict(curr_filing)) if prev_filing and curr_filing else {}
    csv_text = _build_csv_text(prev_label, curr_label, final, filing)
    prompt_text = _build_image_prompt(prev_label, curr_label, csv_text, final, filing)
    return csv_text, prev_label, curr_label, "", prompt_text


def _build_comparison(old_dict: dict, new_dict: dict) -> dict:
    result = {}
    for cat in ("1st", "2nd", "3rd"):
        result[cat] = {}
        for country in ("India", "China", "ROW"):
            old = old_dict.get(cat, {}).get(country, "")
            new = new_dict.get(cat, {}).get(country, "")
            result[cat][country] = {
                "old":  old,
                "new":  new,
                "diff": date_diff(old, new),
            }
    return result


def generate_csv(month_input: str) -> tuple[str, str, str, str, str]:
    """
    Scrape and build CSV text.
    Returns (csv_text, prev_label, curr_label, error_message).
    error_message is empty string on success.
    """
    # Resolve months
    if month_input:
        try:
            current_dt = dparser.parse(month_input + " 2026").replace(day=1)
        except Exception:
          return "", "", "", f"Could not parse month '{month_input}'. Try e.g. 'july'.", ""
    else:
        today = datetime.today().replace(day=1)
        current_dt = today + relativedelta(months=1)

    previous_dt = current_dt - relativedelta(months=1)
    prev_label  = previous_dt.strftime("%B %Y")
    curr_label  = current_dt.strftime("%B %Y")

    # Fetch pages
    try:
        prev_html = requests.get(build_url(previous_dt), timeout=15).text
        curr_html = requests.get(build_url(current_dt),  timeout=15).text
    except requests.RequestException as exc:
      return "", prev_label, curr_label, f"Network error: {exc}", ""

    prev_final, prev_filing = _get_eb_tables(prev_html)
    curr_final, curr_filing = _get_eb_tables(curr_html)

    if not curr_final:
      return "", prev_label, curr_label, (
            f"Could not find employment-based table for {curr_label}. "
            "The bulletin may not be published yet."
      ), ""

    final   = _build_comparison(_table_to_dict(prev_final),  _table_to_dict(curr_final))
    filing  = _build_comparison(_table_to_dict(prev_filing), _table_to_dict(curr_filing)) \
              if prev_filing and curr_filing else {}
    csv_text = _build_csv_text(prev_label, curr_label, final, filing)
    prompt_text = _build_image_prompt(prev_label, curr_label, csv_text, final, filing)
    return csv_text, prev_label, curr_label, "", prompt_text


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Visa Bulletin Tracker</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0b1f3a;
      color: #e0e0e0;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 40px 16px;
    }
    h1 { color: #ffd700; margin-bottom: 4px; font-size: 1.8rem; }
    p.sub { color: #888; margin-top: 0; margin-bottom: 32px; font-size: 0.9rem; }

    .card {
      background: #132840;
      border: 1px solid #1e3a5f;
      border-radius: 10px;
      padding: 28px 32px;
      width: 100%;
      max-width: 680px;
    }

    label { font-size: 0.9rem; color: #aaa; display: block; margin-bottom: 6px; }

    .row { display: flex; gap: 12px; align-items: flex-end; flex-wrap: wrap; }

    .file-row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
    .file-input { display: none; }
    .file-picker-btn {
      padding: 10px 14px;
      background: #f5f5f5;
      color: #111;
      border: 1px solid #999;
      border-radius: 6px;
      font-size: 0.95rem;
      cursor: pointer;
      white-space: nowrap;
    }
    .file-picker-btn:hover { background: #e9e9e9; }
    .file-name {
      min-width: 180px;
      color: #d6d6d6;
      font-size: 0.95rem;
    }

    input[type=text] {
      flex: 1;
      min-width: 160px;
      padding: 10px 14px;
      background: #0b1f3a;
      border: 1px solid #2a4a6a;
      border-radius: 6px;
      color: #fff;
      font-size: 1rem;
    }
    input[type=text]::placeholder { color: #555; }
    input[type=text]:focus { outline: none; border-color: #ffd700; }

    button {
      padding: 10px 22px;
      background: #ffd700;
      color: #0b1f3a;
      border: none;
      border-radius: 6px;
      font-size: 1rem;
      font-weight: 700;
      cursor: pointer;
      white-space: nowrap;
    }
    button:hover { background: #ffe44d; }
    button:disabled { background: #555; color: #888; cursor: not-allowed; }

    .spinner { display: none; margin-left: 10px; color: #ffd700; font-size: 0.9rem; }

    .error {
      margin-top: 16px;
      padding: 10px 14px;
      background: #3a1515;
      border: 1px solid #a03030;
      border-radius: 6px;
      color: #ff8888;
      font-size: 0.9rem;
      display: none;
    }

    .result-section { margin-top: 24px; display: none; }
    .result-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
    }
    .result-header span { color: #aaa; font-size: 0.85rem; }
    .copy-btn {
      padding: 6px 14px;
      font-size: 0.82rem;
      font-weight: 600;
      background: #1e3a5f;
      color: #ffd700;
      border: 1px solid #2a5080;
      border-radius: 6px;
      cursor: pointer;
    }
    .copy-btn:hover { background: #254a78; }

    textarea {
      width: 100%;
      height: 320px;
      background: #0b1f3a;
      border: 1px solid #2a4a6a;
      border-radius: 6px;
      color: #c8e6c9;
      font-family: "Menlo", "Courier New", monospace;
      font-size: 0.82rem;
      padding: 12px;
      resize: vertical;
    }
    textarea:focus { outline: none; border-color: #ffd700; }

    .hint { margin-top: 10px; color: #555; font-size: 0.78rem; }

    .prompt-section { margin-top: 28px; display: none; }
    .prompt-section .section-title {
      font-size: 0.85rem;
      color: #ffd700;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin-bottom: 10px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .prompt-ta {
      width: 100%;
      height: 200px;
      background: #0b1f3a;
      border: 1px solid #2a4a6a;
      border-radius: 6px;
      color: #ffe082;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 0.85rem;
      line-height: 1.6;
      padding: 12px;
      resize: vertical;
    }
    .prompt-ta:focus { outline: none; border-color: #ffd700; }
  </style>
</head>
<body>
  <h1>Visa Bulletin Tracker</h1>
  <p class="sub">Employment-Based Final Action &amp; Filing Dates — month-over-month comparison</p>

  <div class="card">
    <label for="month">Month (leave blank to auto-detect)</label>
    <div class="row">
      <input type="text" id="month" placeholder="e.g. july, august, may …">
      <button id="generateBtn" onclick="generate()">Generate CSV</button>
      <span class="spinner" id="spinner">⏳ Loading…</span>
    </div>
    <p class="hint">Compares the entered month against the previous month. Blank = uses today's month as previous, next month as current.</p>
    <hr />
    <label>Upload Visa Bulletin PDF files (order matters)</label>
    <div class="file-row">
      <input class="file-input" type="file" id="previousPdf" accept="application/pdf" title="Previous Month File">
      <button type="button" class="file-picker-btn" onclick="pickFile('previousPdf')">Choose Previous Month File</button>
      <span class="file-name" id="previousPdfName">No file chosen</span>

      <input class="file-input" type="file" id="currentPdf" accept="application/pdf" title="Current Month File">
      <button type="button" class="file-picker-btn" onclick="pickFile('currentPdf')">Choose Current Month File</button>
      <span class="file-name" id="currentPdfName">No file chosen</span>
    </div>
    <div class="row" style="margin-top: 12px;">
      <button id="uploadBtn" onclick="uploadPdfs()">Upload</button>
    </div>
    <p class="hint">Select Previous Month File in the first picker and Current Month File in the second picker.</p>

    <div class="error" id="errorBox"></div>

    <div class="result-section" id="resultSection">
      <div class="result-header">
        <span id="resultLabel"></span>
        <button class="copy-btn" onclick="copyCSV()">Copy CSV</button>
      </div>
      <textarea id="csvOutput" readonly></textarea>
    </div>

    <div class="prompt-section" id="promptSection">
      <div class="section-title">
        <span>Copy Prompt</span>
        <button class="copy-btn" onclick="copyPrompt()">Copy Prompt</button>
      </div>
      <textarea class="prompt-ta" id="promptOutput" readonly></textarea>
    </div>
  </div>

  <script>
    async function generate() {
      const month = document.getElementById('month').value.trim();
      const btn   = document.getElementById('generateBtn');
      const spinner = document.getElementById('spinner');
      const errorBox = document.getElementById('errorBox');
      const resultSection = document.getElementById('resultSection');

      btn.disabled = true;
      spinner.style.display = 'inline';
      errorBox.style.display = 'none';
      resultSection.style.display = 'none';

      try {
        const resp = await fetch('/generate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ month })
        });
        const data = await resp.json();

        if (data.error) {
          errorBox.textContent = data.error;
          errorBox.style.display = 'block';
        } else {
          document.getElementById('csvOutput').value = data.csv;
          document.getElementById('resultLabel').textContent =
            data.prev_label + '  →  ' + data.curr_label;
          resultSection.style.display = 'block';

          const prompt = data.prompt ||
            `Generate fb page image for my Facebook page name "U.S. Immigration Hub" include my page name in image for Visa bulletin ${data.curr_label} vs ${data.prev_label} comparison. Refer attached image to follow the same theme and pattern as image attached\n\nHere is the ${data.curr_label} vs ${data.prev_label} data:\n${data.csv}`;
          document.getElementById('promptOutput').value = prompt;
          document.getElementById('promptSection').style.display = 'block';
        }
      } catch (err) {
        errorBox.textContent = 'Request failed: ' + err.message;
        errorBox.style.display = 'block';
      } finally {
        btn.disabled = false;
        spinner.style.display = 'none';
      }
    }

    async function uploadPdfs() {
      const prev = document.getElementById('previousPdf').files[0];
      const curr = document.getElementById('currentPdf').files[0];
      const btn = document.getElementById('uploadBtn');
      const spinner = document.getElementById('spinner');
      const errorBox = document.getElementById('errorBox');
      const resultSection = document.getElementById('resultSection');

      if (!prev || !curr) {
        errorBox.textContent = 'Please select both PDF files.';
        errorBox.style.display = 'block';
        return;
      }

      btn.disabled = true;
      spinner.style.display = 'inline';
      errorBox.style.display = 'none';
      resultSection.style.display = 'none';

      const formData = new FormData();
      formData.append('previous_pdf', prev);
      formData.append('current_pdf', curr);

      try {
        const resp = await fetch('/generate', {
          method: 'POST',
          body: formData,
        });
        const data = await resp.json();

        if (data.error) {
          errorBox.textContent = data.error;
          errorBox.style.display = 'block';
        } else {
          document.getElementById('csvOutput').value = data.csv;
          document.getElementById('resultLabel').textContent =
            data.prev_label + '  →  ' + data.curr_label;
          resultSection.style.display = 'block';

          const prompt = data.prompt ||
            `Generate fb page image for my Facebook page name "U.S. Immigration Hub" include my page name in image for Visa bulletin ${data.curr_label} vs ${data.prev_label} comparison. Refer attached image to follow the same theme and pattern as image attached\n\nHere is the ${data.curr_label} vs ${data.prev_label} data:\n${data.csv}`;
          document.getElementById('promptOutput').value = prompt;
          document.getElementById('promptSection').style.display = 'block';
        }
      } catch (err) {
        errorBox.textContent = 'Request failed: ' + err.message;
        errorBox.style.display = 'block';
      } finally {
        btn.disabled = false;
        spinner.style.display = 'none';
      }
    }

    function pickFile(inputId) {
      document.getElementById(inputId).click();
    }

    function setFileName(inputId, labelId) {
      const input = document.getElementById(inputId);
      const label = document.getElementById(labelId);
      const file = input.files && input.files[0];
      label.textContent = file ? file.name : 'No file chosen';
    }

    function copyCSV() {
      const ta = document.getElementById('csvOutput');
      ta.select();
      navigator.clipboard.writeText(ta.value).then(() => {
        const btn = document.querySelector('.copy-btn');
        btn.textContent = 'Copied!';
        setTimeout(() => btn.textContent = 'Copy CSV', 1800);
      });
    }

    function copyPrompt() {
      const ta = document.getElementById('promptOutput');
      ta.select();
      navigator.clipboard.writeText(ta.value).then(() => {
        const btns = document.querySelectorAll('.copy-btn');
        const btn = btns[btns.length - 1];
        btn.textContent = 'Copied!';
        setTimeout(() => btn.textContent = 'Copy Prompt', 1800);
      });
    }

    // Allow Enter key in the input to trigger generate
    document.addEventListener('DOMContentLoaded', () => {
      document.getElementById('month').addEventListener('keydown', e => {
        if (e.key === 'Enter') generate();
      });
      document.getElementById('previousPdf').addEventListener('change', () => {
        setFileName('previousPdf', 'previousPdfName');
      });
      document.getElementById('currentPdf').addEventListener('change', () => {
        setFileName('currentPdf', 'currentPdfName');
      });
    });
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/healthy")
def healthy():
    return jsonify({"status": "ok", "service": "visa-bulletin-tracker", "timestamp": datetime.utcnow().isoformat() + "Z"})


@app.get("/")
def index():
    return render_template_string(TEMPLATE)


@app.post("/generate")
def generate():
    # Check for file upload (multipart form data)
    if request.files:
        previous_file = request.files.get("previous_pdf")
        current_file = request.files.get("current_pdf")
        if not previous_file or not current_file:
            return jsonify({"error": "Please upload both previous and current Visa Bulletin PDFs."}), 400

        try:
            previous_pdf = previous_file.read()
            current_pdf = current_file.read()
            csv_text, prev_label, curr_label, error, prompt_text = generate_csv_from_pdf(
                previous_pdf,
                current_pdf,
                previous_filename=previous_file.filename,
                current_filename=current_file.filename,
            )
        except Exception as exc:
            return jsonify({"error": f"PDF parsing failed: {str(exc)}"}), 400
    else:
        # Handle JSON requests for month-based generation
        body = request.get_json(silent=True) or {}
        month_input = (body.get("month") or "").strip()
        csv_text, prev_label, curr_label, error, prompt_text = generate_csv(month_input)

    if error:
        return jsonify({"error": error}), 400

    return jsonify({
        "csv":        csv_text,
        "prev_label": prev_label,
        "curr_label": curr_label,
      "prompt":     prompt_text,
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, port=5050)
