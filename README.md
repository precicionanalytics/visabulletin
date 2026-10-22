# Visa Bulletin Tracker

Scrapes the [U.S. State Department Visa Bulletin](https://travel.state.gov/content/travel/en/legal/visa-law0/visa-bulletin.html) and generates:

- **`visa_bulletin_output.png`** — a comparison image showing Final Action Dates and Dates for Filing (Employment-Based EB1/EB2/EB3) with month-over-month movement
- **`visa_bulletin.csv`** — the same data in CSV format for further use (e.g. pasting into ChatGPT for custom image generation)

---

## Requirements

```bash
pip3 install requests beautifulsoup4 python-dateutil pillow
```

---

## Usage

### Default (no argument)

Automatically uses the **current calendar month** as Previous and **next calendar month** as Current.

> Running in June 2026 → Previous = June 2026, Current = July 2026

```bash
python3 visa_bulletin.py
```

---

### Specify a month

Pass any month name as an argument. That month becomes **Current**, and the month before it becomes **Previous**.

```bash
# Current = July 2026, Previous = June 2026
python3 visa_bulletin.py july

# Current = May 2026, Previous = April 2026
python3 visa_bulletin.py may

# Current = August 2026, Previous = July 2026
python3 visa_bulletin.py august
```

---

## Output

Both files are always generated together in the same run:

| File | Description |
|---|---|
| `visa_bulletin_output.png` | Dark-themed comparison image |
| `visa_bulletin.csv` | Raw data table with previous & current dates |

### Sample CSV output (`python3 visa_bulletin.py july`)

```
Table,Category,Country,June 2026,July 2026
Final Action Dates,EB1,ROW,Current,Current
Final Action Dates,EB1,China,01-Apr-2023,01-Jun-2023
Final Action Dates,EB1,India,15-Dec-2022,15-Oct-2022
Final Action Dates,EB2,ROW,Current,Current
Final Action Dates,EB2,China,01-Sep-2021,01-Sep-2021
Final Action Dates,EB2,India,01-Sep-2013,Unavailable
Final Action Dates,EB3,ROW,01-Jun-2024,01-Aug-2024
Final Action Dates,EB3,China,01-Aug-2021,22-Dec-2021
Final Action Dates,EB3,India,15-Dec-2013,01-Jan-2014
Dates for Filing,EB1,ROW,Current,Current
Dates for Filing,EB1,China,01-Dec-2023,01-Dec-2023
Dates for Filing,EB1,India,01-Dec-2023,01-Dec-2023
Dates for Filing,EB2,ROW,Current,Current
Dates for Filing,EB2,China,01-Jan-2022,01-Jan-2022
Dates for Filing,EB2,India,15-Jan-2015,15-Jan-2015
Dates for Filing,EB3,ROW,Current,Current
Dates for Filing,EB3,China,01-Jan-2022,01-Jan-2022
Dates for Filing,EB3,India,15-Jan-2015,15-Jan-2015
```

---

## Web Upload Limits

Vercel Functions reject request bodies above **4.5 MB**, including multipart form
data, before the request reaches Flask. Raising Flask's `MAX_CONTENT_LENGTH` does
not change that platform limit. When Blob is configured, Compare, Predict, and
Admin upload PDFs directly to **private Vercel Blob** using the official SDK's
multipart client upload, with a **10 MiB** file limit. Flask receives only the
uploaded pathname and original filename, not the file in the HTTP request.

### Vercel Setup

1. Connect your private Blob store to this project for Production and Preview.
2. Verify `BLOB_READ_WRITE_TOKEN` is available in the project's Environment Variables.
3. Add `UPLOAD_ADMIN_PASSWORD` as a secret environment variable with a strong,
	unique password. Never put either secret in Git or client-side configuration.
4. Deploy the changes. The configuration uses Python 3.13 for Flask and a Node
	function at `/api/blob-upload` for SDK upload authorization. The build runs
	`npm run build` to bundle the browser SDK into `public/static/blob-client.js`.
5. Choose a PDF from Admin, Predict, or Compare and enter the admin password in
	the upload dialog. Credentials are not stored in browser storage.

The file first lands under `incoming/`. Authenticated completion downloads it
server-side, checks its PDF signature, reads the month/year from its filename,
and stores its unchanged bytes under `bulletins/visabulletin_MonthYear.pdf`.
Upload does not parse PDF text or tables. Blob bulletins
override bundled PDFs of the same month. Comparison and prediction read private
Blob content server-side; no read-write token or private PDF is served publicly.
Same-month replacements use last-successful-write semantics.

PDFs can be uploaded even if they contain no supported bulletin tables, including
the 19-page October analysis PDF. Comparison and prediction parse files only when
those operations are requested; saving a file does not make its contents usable
for those operations. Include the month and year in upload filenames, such as
`Visa-Bulletin-October-2026.pdf`, so the monthly archive can label the file.
Month Comparison requires readable employment-based Final Action Dates and Dates
for Filing tables in **both** selected months. If a table is missing, comparison
returns an error naming the month and table instead of exporting blank previous
values or silently omitting filing dates. This also applies to website comparisons.
Replace an analysis-only PDF with a bulletin containing those tables before
comparing. On Vercel, replace the file through Admin if that month already exists
in Blob: adding a corrected PDF to Git does not replace the Blob override.

Uploads abandoned before completion or interrupted by storage failures can leave
staging files; remove old `incoming/` files through
the Blob dashboard. Published files remain intact if staging cleanup fails.

Without Blob, local Flask uploads still use `bulletins/`. On Vercel, Blob and an
admin password are required for uploads because its deployment filesystem is not
writable persistent storage. Bundled PDFs continue to work without either secret.

### Local Development And Tests

Use Python 3.13 and Node.js 22+. To test real Blob uploads locally, connect the
Development environment, pull secrets with `vercel env pull`, and run
`vercel dev` so both Flask and the Node authorization function are available.
Do not share the pulled environment file. Pure Flask development without Blob
can run with `python app.py` after building the browser bundle.

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
npm ci
npm run build
PYTHON=python npm test
python -m unittest discover -s tests -p 'test_*.py' -v
```

## Running Tests

```bash
python3 -m pytest test_visa_bulletin.py -v
```
