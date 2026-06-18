"""
Flask web interface for the Visa Bulletin Tracker.

Endpoints:
  GET  /healthy  — health check
  GET  /         — form to select month
  POST /generate — scrapes bulletin and returns CSV in a copyable textarea
"""

import sys
from datetime import datetime
from dateutil import parser as dparser
from dateutil.relativedelta import relativedelta

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template_string, request, jsonify

app = Flask(__name__)

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
        cols = rows[i + 1].find_all(["td", "th"])
        vals = [c.get_text(strip=True) for c in cols]
        data[cat] = {
            "ROW":   parse_date(vals[1]),
            "China": parse_date(vals[2]),
            "India": parse_date(vals[3]),
        }
    return data


def _build_comparison(old_dict: dict, new_dict: dict) -> dict:
    result = {}
    for cat in new_dict:
        result[cat] = {}
        for country in ("India", "China", "ROW"):
            old = old_dict[cat][country]
            new = new_dict[cat][country]
            result[cat][country] = {
                "old":  old,
                "new":  new,
                "diff": date_diff(old, new),
            }
    return result


def generate_csv(month_input: str) -> tuple[str, str, str, str]:
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
            return "", "", "", f"Could not parse month '{month_input}'. Try e.g. 'july'."
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
        return "", prev_label, curr_label, f"Network error: {exc}"

    prev_final, prev_filing = _get_eb_tables(prev_html)
    curr_final, curr_filing = _get_eb_tables(curr_html)

    if not curr_final:
        return "", prev_label, curr_label, (
            f"Could not find employment-based table for {curr_label}. "
            "The bulletin may not be published yet."
        )

    final   = _build_comparison(_table_to_dict(prev_final),  _table_to_dict(curr_final))
    filing  = _build_comparison(_table_to_dict(prev_filing), _table_to_dict(curr_filing)) \
              if prev_filing and curr_filing else {}

    lines = [f"Table,Category,Country,{prev_label},{curr_label}"]
    for tbl_label, data_dict in [
        ("Final Action Dates", final),
        ("Dates for Filing",   filing),
    ]:
        if not data_dict:
            continue
        for cat_key, eb_label in [("1st", "EB1"), ("2nd", "EB2"), ("3rd", "EB3")]:
            for country in ("ROW", "China", "India"):
                item = data_dict[cat_key][country]
                lines.append(
                    f"{tbl_label},{eb_label},{country},"
                    f"{to_display(item['old'])},{to_display(item['new'])}"
                )

    return "\n".join(lines), prev_label, curr_label, ""


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

          // Build and show the ChatGPT prompt
          const prompt =
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
    body        = request.get_json(silent=True) or {}
    month_input = (body.get("month") or "").strip()

    csv_text, prev_label, curr_label, error = generate_csv(month_input)

    if error:
        return jsonify({"error": error}), 400

    return jsonify({
        "csv":        csv_text,
        "prev_label": prev_label,
        "curr_label": curr_label,
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, port=5050)
