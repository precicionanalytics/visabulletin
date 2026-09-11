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


# ---------------------------------------------------------------------------
# Category definitions shared across Employment-Based and Family-Sponsored
# comparisons (both use the same Final Action / Dates for Filing structure).
# ---------------------------------------------------------------------------

EB_CATEGORY_MAP = [("1st", "EB1"), ("2nd", "EB2"), ("3rd", "EB3")]
EB_COUNTRIES = ("ROW", "China", "India")

FAMILY_CATEGORY_MAP = [("f1", "F1"), ("f2a", "F2A"), ("f2b", "F2B"), ("f3", "F3"), ("f4", "F4")]
FAMILY_COUNTRIES = ("ROW", "China", "India", "Mexico", "Philippines")


def _build_csv_text(
  prev_label: str, curr_label: str, final: dict, filing: dict,
  category_map=EB_CATEGORY_MAP, countries=EB_COUNTRIES,
) -> str:
  lines = [f"Table,Category,Country,{prev_label},{curr_label},ChangeDays,Movement"]
  for tbl_label, data_dict in [
    ("Final Action Dates", final),
    ("Dates for Filing", filing),
  ]:
    if not data_dict:
      continue
    for cat_key, label in category_map:
      for country in countries:
        item = data_dict[cat_key][country]
        old = item["old"]
        new = item["new"]
        diff = item["diff"]
        lines.append(
          f"{tbl_label},{label},{country},"
          f"{to_display(old)},{to_display(new)},"
          f"{_change_days_text(old, new, diff)},"
          f"{_movement_text(old, new, diff)}"
        )
  return "\n".join(lines)


def _build_movement_summary(
  final: dict, filing: dict, category_map=EB_CATEGORY_MAP, countries=("India", "China", "ROW"),
) -> list[str]:
  points = []
  for tbl_label, data_dict in [("Final Action", final), ("Filing", filing)]:
    if not data_dict:
      continue
    for cat_key, label in category_map:
      for country in countries:
        item = data_dict[cat_key][country]
        old = item["old"]
        new = item["new"]
        diff = item["diff"]
        if not isinstance(old, datetime) or not isinstance(new, datetime):
          continue
        if diff != 0:
          direction = "forward" if diff > 0 else "retrogressed"
          points.append(
            f"- {tbl_label} {label} {country} {direction} by {abs(diff)} days"
          )

  if not points:
    return ["- Most categories are unchanged or marked Current/Unavailable."]

  return points[:6]


def _build_image_prompt(
  prev_label: str, curr_label: str, csv_text: str, final: dict, filing: dict,
  category_map=EB_CATEGORY_MAP, countries=("India", "China", "ROW"),
  subtitle: str = "EMPLOYMENT-BASED PREFERENCES (EB-1, EB-2 & EB-3)",
) -> str:
  summary = "\n".join(_build_movement_summary(final, filing, category_map, countries))
  return (
    "Create a high-quality Facebook infographic in the same visual style as my sample image for page name \"U.S. Immigration Hub\".\n"
    f"Title: VISA BULLETIN - {curr_label.upper()}\n"
    f"Subtitle: {subtitle}\n\n"
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


def _get_category_tables(html: str, header_keyword: str):
    """Return (final_action_table, dates_for_filing_table) BeautifulSoup objects
    whose header row contains header_keyword (e.g. 'Employment', 'Family')."""
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    matched = [
        t for t in tables
        if t.find_all("tr") and header_keyword in t.find_all("tr")[0].get_text()
    ]
    final   = matched[0] if len(matched) > 0 else None
    filing  = matched[1] if len(matched) > 1 else None
    return final, filing


def _get_eb_tables(html: str):
    return _get_category_tables(html, "Employment")


def _get_family_tables(html: str):
    return _get_category_tables(html, "Family")


def _table_to_dict(table, category_keys=("1st", "2nd", "3rd"), countries=EB_COUNTRIES) -> dict:
    if table is None:
        return {}
    rows = table.find_all("tr")
    data = {}
    for i, cat in enumerate(category_keys):
        if len(rows) <= i + 1:
            data[cat] = {country: "" for country in countries}
            continue
        cols = rows[i + 1].find_all(["td", "th"])
        vals = [c.get_text(strip=True) for c in cols]
        data[cat] = {
            country: parse_date(vals[idx + 1]) if len(vals) > idx + 1 else ""
            for idx, country in enumerate(countries)
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


def _find_tables_from_pdf(pdf_bytes: bytes, marker_words):
    tables = _rows_from_pdf_bytes(pdf_bytes)
    matched = []
    for table in tables:
        flat_text = " ".join(cell.lower() for row in table for cell in row if cell)
        if "china" in flat_text and "india" in flat_text and any(word in flat_text for word in marker_words):
            matched.append(table)
    if len(matched) >= 2:
        return matched[0], matched[1]
    if len(matched) == 1:
        return matched[0], None
    return None, None


def _normalize_eb_category_key(raw: str):
    """Map a raw EB row label (which may carry a multi-line parenthetical
    suffix, e.g. '5th Set Aside:\\nRural (20%)\\n(including NR, RR)') to a
    canonical category key."""
    text = re.sub(r"\s+", " ", raw.replace("\n", " ")).strip().lower()
    if text in ("1st", "2nd", "3rd", "4th"):
        return text
    if text.startswith("other workers"):
        return "other workers"
    if text.startswith("certain religious workers"):
        return "certain religious workers"
    if text.startswith("5th unreserved"):
        return "5th unreserved"
    if text.startswith("5th set aside: rural") or text.startswith("5th set aside rural"):
        return "5th set aside rural"
    if text.startswith("5th set aside: high") or text.startswith("5th set aside high"):
        return "5th set aside high unemployment"
    if text.startswith("5th set aside: infrastructure") or text.startswith("5th set aside infrastructure"):
        return "5th set aside infrastructure"
    return None


EB_SUB_CATEGORY_MAP = [
    ("other workers", "EB3 Other Workers"),
    ("4th", "EB4"),
    ("certain religious workers", "Certain Religious Workers"),
    ("5th unreserved", "EB5 Unreserved"),
    ("5th set aside rural", "EB5 Set-Aside Rural"),
    ("5th set aside high unemployment", "EB5 Set-Aside High Unemployment"),
    ("5th set aside infrastructure", "EB5 Set-Aside Infrastructure"),
]
EB_SUB_CATEGORY_KEYS = [k for k, _ in EB_SUB_CATEGORY_MAP]
EB_FULL_CATEGORY_KEYS = ["1st", "2nd", "3rd"] + EB_SUB_CATEGORY_KEYS


def _is_eb_table_start(table) -> bool:
    flat_text = " ".join(cell.lower() for row in table for cell in row if cell)
    return "china" in flat_text and "india" in flat_text and any(w in flat_text for w in ("1st", "2nd", "3rd"))


def _is_eb_row(row) -> bool:
    return bool(row) and _normalize_eb_category_key(row[0]) is not None


def _find_eb_tables_from_pdf(pdf_bytes: bytes):
    """Return (final_action_rows, dates_for_filing_rows) for the Employment-Based
    tables, merging continuation rows (e.g. the EB-5 set-asides) that pdfplumber
    splits into a separate table across a page break."""
    tables = _rows_from_pdf_bytes(pdf_bytes)
    merged = []
    i = 0
    while i < len(tables) and len(merged) < 2:
        if not _is_eb_table_start(tables[i]):
            i += 1
            continue
        rows = list(tables[i][1:])
        j = i + 1
        while j < len(tables):
            if _is_eb_table_start(tables[j]):
                break
            # Skip intervening non-data tables (e.g. page-number labels) rather
            # than stopping, since continuation rows may appear further ahead.
            rows.extend(row for row in tables[j] if _is_eb_row(row))
            j += 1
        merged.append(rows)
        i = j
    if len(merged) >= 2:
        return merged[0], merged[1]
    if len(merged) == 1:
        return merged[0], None
    return None, None


def _find_family_tables_from_pdf(pdf_bytes: bytes):
    return _find_tables_from_pdf(pdf_bytes, ["f1", "f2a", "f2b"])


DV_REGION_PREFIXES = ("AFRICA", "ASIA", "EUROPE", "NORTH AMERICA", "OCEANIA", "SOUTH AMERICA")


def _is_dv_region_row(first_cell: str) -> bool:
    normalized = first_cell.replace("\n", " ").strip().upper()
    return any(normalized.startswith(prefix) for prefix in DV_REGION_PREFIXES)


def _find_dv_table_from_pdf(pdf_bytes: bytes):
    """Return the rows (region, allocation) for the current bulletin's Diversity
    Visa table, merging the continuation table when the row set is split across
    a page break. Non-data tables (e.g. page headers) between the two halves are
    skipped; a second, complete DV table (a look-ahead preview) stops the merge."""
    tables = _rows_from_pdf_bytes(pdf_bytes)
    for idx, table in enumerate(tables):
        header = table[0] if table else []
        header_text = " ".join(cell.lower() for cell in header if cell)
        if "region" in header_text and "dv" in header_text:
            rows = [row for row in table[1:] if row and _is_dv_region_row(row[0])]
            j = idx + 1
            while len(rows) < len(DV_REGION_PREFIXES) and j < len(tables):
                nxt = tables[j]
                nxt_header = nxt[0] if nxt else []
                nxt_header_text = " ".join(cell.lower() for cell in nxt_header if cell)
                if "region" in nxt_header_text and "dv" in nxt_header_text:
                    break  # a new full DV table (look-ahead preview) - stop merging
                rows.extend(row for row in nxt if row and _is_dv_region_row(row[0]))
                j += 1
            return rows
    return None


def _dv_rows_to_dict(rows) -> dict:
    data = {}
    for row in rows or []:
        if not row:
            continue
        label = row[0].replace("\n", " ").strip()
        value = row[1].replace("\n", " ").strip() if len(row) > 1 else ""
        data[label] = value
    return data


def _build_dv_comparison(old_dict: dict, new_dict: dict) -> dict:
    result = {}
    labels = list(new_dict.keys()) if new_dict else list(old_dict.keys())
    for label in labels:
        result[label] = {"old": old_dict.get(label, ""), "new": new_dict.get(label, "")}
    return result


def _csv_field(value: str) -> str:
    return f'"{value}"' if "," in value else value


def _build_dv_csv_text(prev_label: str, curr_label: str, dv_data: dict) -> str:
    lines = [f"Category,Region,{prev_label},{curr_label},Status"]
    for region, item in dv_data.items():
        old_val = item["old"]
        new_val = item["new"]
        status = "New" if not old_val else ("Unchanged" if old_val == new_val else "Changed")
        lines.append(
            f"Diversity Visa,{_csv_field(region)},"
            f"{_csv_field(old_val or 'N/A')},{_csv_field(new_val or 'N/A')},{status}"
        )
    return "\n".join(lines)


def _build_dv_image_prompt(prev_label: str, curr_label: str, csv_text: str) -> str:
    return (
        "Create a high-quality Facebook infographic in the same visual style as my sample image for page name \"U.S. Immigration Hub\".\n"
        f"Title: VISA BULLETIN - {curr_label.upper()}\n"
        "Subtitle: DIVERSITY VISA (DV) LOTTERY REGIONAL ALLOCATIONS\n\n"
        f"Comparison period: {prev_label} -> {curr_label}\n"
        "Build one table with columns: Region, Previous Month Allocation, Current Month Allocation, Status.\n"
        "Use color coding: green for regions with an increased allocation, red for a decreased allocation, neutral for unchanged, and a highlight for regions now marked Current.\n"
        "Keep branding prominent with U.S. Immigration Hub text and a professional immigration-news look.\n\n"
        "Use this exact data:\n"
        f"{csv_text}"
    )


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


MONTH_NAME_PATTERN = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
)


def _parse_label_from_filename(filename: str):
    if not filename:
        return None
    # Matches both "visabulletin_April2026.pdf" and "visa-bulletin-April-2026.pdf".
    month_match = re.search(rf"({MONTH_NAME_PATTERN})[-_ ]*(\d{{4}})", filename, re.IGNORECASE)
    if month_match:
        month_name, year = month_match.group(1), month_match.group(2)
        try:
            month_dt = dparser.parse(f"{month_name} {year}")
            return month_dt.strftime("%B %Y")
        except Exception:
            return f"{month_name} {year}"
    month_only = re.search(rf"({MONTH_NAME_PATTERN})", filename, re.IGNORECASE)
    if month_only:
        try:
            month_dt = dparser.parse(month_only.group(1))
            return month_dt.strftime("%B %Y")
        except Exception:
            return month_only.group(1).title()
    return None


def _table_rows_to_dict(
  table_rows, category_keys=("1st", "2nd", "3rd"), countries=EB_COUNTRIES, key_normalizer=None,
) -> dict:
    if not table_rows:
        return {}
    data = {}
    for row in table_rows:
        if not row:
            continue
        key = key_normalizer(row[0]) if key_normalizer else row[0].replace("\n", " ").strip().lower()
        if key in category_keys:
            values = [cell.strip() for cell in row[1:]]
            if len(values) >= len(countries):
                data[key] = {
                    country: parse_date(values[idx])
                    for idx, country in enumerate(countries)
                }
    return data


def generate_csv_from_pdf(previous_pdf: bytes, current_pdf: bytes, previous_filename: str = None, current_filename: str = None) -> dict:
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
        return _error_result(prev_label, curr_label, (
            "Could not find the employment-based table in the current PDF. "
            "Please make sure the PDF contains the Visa Bulletin employment-based tables."
        ))

    to_eb_dict = lambda raw: _table_rows_to_dict(
        raw, EB_FULL_CATEGORY_KEYS, EB_COUNTRIES, key_normalizer=_normalize_eb_category_key,
    )
    eb_final, eb_filing, eb_sub_final, eb_sub_filing = _build_eb_full_comparisons(
        prev_final, curr_final, prev_filing, curr_filing, to_eb_dict,
    )

    family_keys = [k for k, _ in FAMILY_CATEGORY_MAP]
    prev_fam_final, prev_fam_filing = _find_family_tables_from_pdf(previous_pdf)
    curr_fam_final, curr_fam_filing = _find_family_tables_from_pdf(current_pdf)
    family_final = _build_comparison(
        _table_rows_to_dict(prev_fam_final, family_keys, FAMILY_COUNTRIES),
        _table_rows_to_dict(curr_fam_final, family_keys, FAMILY_COUNTRIES),
        family_keys, FAMILY_COUNTRIES,
    ) if curr_fam_final else {}
    family_filing = _build_comparison(
        _table_rows_to_dict(prev_fam_filing, family_keys, FAMILY_COUNTRIES),
        _table_rows_to_dict(curr_fam_filing, family_keys, FAMILY_COUNTRIES),
        family_keys, FAMILY_COUNTRIES,
    ) if prev_fam_filing and curr_fam_filing else {}

    prev_dv = _dv_rows_to_dict(_find_dv_table_from_pdf(previous_pdf))
    curr_dv = _dv_rows_to_dict(_find_dv_table_from_pdf(current_pdf))
    dv_data = _build_dv_comparison(prev_dv, curr_dv) if curr_dv else {}

    return _build_result(
        prev_label, curr_label,
        eb_final, eb_filing, eb_sub_final, eb_sub_filing,
        family_final, family_filing, dv_data,
    )


def _parse_bulletin_dt(pdf_bytes: bytes, filename: str):
    label = _parse_pdf_label(pdf_bytes) or _parse_label_from_filename(filename)
    if not label:
        return None
    try:
        return dparser.parse(label).replace(day=1)
    except Exception:
        return None


def generate_csv_from_pdf_auto_order(bytes_a: bytes, name_a: str, bytes_b: bytes, name_b: str) -> dict:
    """Compare two bulletin PDFs, auto-detecting which is earlier (previous)
    and which is later (current) from each PDF's own content/filename rather
    than trusting the order they were supplied in."""
    dt_a = _parse_bulletin_dt(bytes_a, name_a)
    dt_b = _parse_bulletin_dt(bytes_b, name_b)

    if dt_a is not None and dt_b is not None and dt_a > dt_b:
        bytes_a, name_a, bytes_b, name_b = bytes_b, name_b, bytes_a, name_a

    return generate_csv_from_pdf(bytes_a, bytes_b, name_a, name_b)


def _build_comparison(
  old_dict: dict, new_dict: dict, category_keys=("1st", "2nd", "3rd"), countries=("India", "China", "ROW"),
) -> dict:
    result = {}
    for cat in category_keys:
        result[cat] = {}
        for country in countries:
            old = old_dict.get(cat, {}).get(country, "")
            new = new_dict.get(cat, {}).get(country, "")
            result[cat][country] = {
                "old":  old,
                "new":  new,
                "diff": date_diff(old, new),
            }
    return result


def _build_eb_full_comparisons(prev_final_raw, curr_final_raw, prev_filing_raw, curr_filing_raw, to_dict_fn):
    """Build EB1-3 and EB sub-category (4th, Other Workers, Certain Religious
    Workers, EB-5 Unreserved + set-asides) comparisons from the same raw tables.
    to_dict_fn converts a raw table (PDF rows or an HTML table) into a dict."""
    curr_full = to_dict_fn(curr_final_raw)
    prev_full = to_dict_fn(prev_final_raw)
    eb_final = _build_comparison(prev_full, curr_full, ("1st", "2nd", "3rd"), EB_COUNTRIES)
    eb_sub_final = _build_comparison(prev_full, curr_full, EB_SUB_CATEGORY_KEYS, EB_COUNTRIES)

    if prev_filing_raw and curr_filing_raw:
        curr_filing_full = to_dict_fn(curr_filing_raw)
        prev_filing_full = to_dict_fn(prev_filing_raw)
        eb_filing = _build_comparison(prev_filing_full, curr_filing_full, ("1st", "2nd", "3rd"), EB_COUNTRIES)
        eb_sub_filing = _build_comparison(prev_filing_full, curr_filing_full, EB_SUB_CATEGORY_KEYS, EB_COUNTRIES)
    else:
        eb_filing, eb_sub_filing = {}, {}

    return eb_final, eb_filing, eb_sub_final, eb_sub_filing


def _error_result(prev_label: str, curr_label: str, error: str) -> dict:
    return {
        "csv": "", "prompt": "",
        "eb_sub_csv": "", "eb_sub_prompt": "",
        "family_csv": "", "family_prompt": "",
        "dv_csv": "", "dv_prompt": "",
        "prev_label": prev_label, "curr_label": curr_label,
        "error": error,
    }


def _build_result(
  prev_label: str, curr_label: str,
  eb_final: dict, eb_filing: dict,
  eb_sub_final: dict, eb_sub_filing: dict,
  family_final: dict, family_filing: dict,
  dv_data: dict,
) -> dict:
    eb_csv = _build_csv_text(prev_label, curr_label, eb_final, eb_filing)
    eb_prompt = _build_image_prompt(prev_label, curr_label, eb_csv, eb_final, eb_filing)

    eb_sub_csv = _build_csv_text(
        prev_label, curr_label, eb_sub_final, eb_sub_filing,
        category_map=EB_SUB_CATEGORY_MAP, countries=EB_COUNTRIES,
    )
    eb_sub_prompt = _build_image_prompt(
        prev_label, curr_label, eb_sub_csv, eb_sub_final, eb_sub_filing,
        category_map=EB_SUB_CATEGORY_MAP, countries=EB_COUNTRIES,
        subtitle="EMPLOYMENT-BASED: EB-4, OTHER WORKERS, RELIGIOUS WORKERS & EB-5",
    )

    family_csv = _build_csv_text(
        prev_label, curr_label, family_final, family_filing,
        category_map=FAMILY_CATEGORY_MAP, countries=FAMILY_COUNTRIES,
    )
    family_prompt = _build_image_prompt(
        prev_label, curr_label, family_csv, family_final, family_filing,
        category_map=FAMILY_CATEGORY_MAP, countries=FAMILY_COUNTRIES,
        subtitle="FAMILY-SPONSORED PREFERENCES (F1, F2A, F2B, F3 & F4)",
    )

    dv_csv = _build_dv_csv_text(prev_label, curr_label, dv_data)
    dv_prompt = _build_dv_image_prompt(prev_label, curr_label, dv_csv)

    return {
        "csv": eb_csv, "prompt": eb_prompt,
        "eb_sub_csv": eb_sub_csv, "eb_sub_prompt": eb_sub_prompt,
        "family_csv": family_csv, "family_prompt": family_prompt,
        "dv_csv": dv_csv, "dv_prompt": dv_prompt,
        "prev_label": prev_label, "curr_label": curr_label,
        "error": "",
    }


def generate_csv(month_input: str) -> dict:
    """
    Scrape and build CSV text and prompts for Employment-Based, Family-Sponsored,
    and Diversity Visa categories.
    """
    # Resolve months
    if month_input:
        try:
            current_dt = dparser.parse(month_input + " 2026").replace(day=1)
        except Exception:
          return _error_result("", "", f"Could not parse month '{month_input}'. Try e.g. 'july'.")
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
      return _error_result(prev_label, curr_label, f"Network error: {exc}")

    prev_final, prev_filing = _get_eb_tables(prev_html)
    curr_final, curr_filing = _get_eb_tables(curr_html)

    if not curr_final:
      return _error_result(prev_label, curr_label, (
            f"Could not find employment-based table for {curr_label}. "
            "The bulletin may not be published yet."
      ))

    eb_final, eb_filing, eb_sub_final, eb_sub_filing = _build_eb_full_comparisons(
        prev_final, curr_final, prev_filing, curr_filing,
        lambda raw: _table_to_dict(raw, EB_FULL_CATEGORY_KEYS, EB_COUNTRIES),
    )

    family_keys = [k for k, _ in FAMILY_CATEGORY_MAP]
    prev_fam_final, prev_fam_filing = _get_family_tables(prev_html)
    curr_fam_final, curr_fam_filing = _get_family_tables(curr_html)
    family_final = _build_comparison(
        _table_to_dict(prev_fam_final, family_keys, FAMILY_COUNTRIES),
        _table_to_dict(curr_fam_final, family_keys, FAMILY_COUNTRIES),
        family_keys, FAMILY_COUNTRIES,
    ) if curr_fam_final else {}
    family_filing = _build_comparison(
        _table_to_dict(prev_fam_filing, family_keys, FAMILY_COUNTRIES),
        _table_to_dict(curr_fam_filing, family_keys, FAMILY_COUNTRIES),
        family_keys, FAMILY_COUNTRIES,
    ) if prev_fam_filing and curr_fam_filing else {}

    # Diversity Visa allocations are not published on the HTML bulletin pages
    # in a consistently table-parseable form, so DV comparison is PDF-only.
    dv_data = {}

    return _build_result(
        prev_label, curr_label,
        eb_final, eb_filing, eb_sub_final, eb_sub_filing,
        family_final, family_filing, dv_data,
    )


# ---------------------------------------------------------------------------
# Future-month projection (trend analysis over the bulletins/ archive)
# ---------------------------------------------------------------------------

BULLETINS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bulletins")

DEFAULT_LOOKBACK_MONTHS = 6

# Parsed-snapshot cache keyed by (path, mtime, size) so repeated projections do
# not re-run pdfplumber over the whole archive.
_SNAPSHOT_CACHE: dict = {}


def _snapshot_from_pdf(pdf_bytes: bytes) -> dict:
    """Extract every comparable table from one bulletin PDF into plain dicts."""
    eb_final_raw, eb_filing_raw = _find_eb_tables_from_pdf(pdf_bytes)
    fam_final_raw, fam_filing_raw = _find_family_tables_from_pdf(pdf_bytes)
    family_keys = [k for k, _ in FAMILY_CATEGORY_MAP]

    to_eb_dict = lambda raw: _table_rows_to_dict(
        raw, EB_FULL_CATEGORY_KEYS, EB_COUNTRIES, key_normalizer=_normalize_eb_category_key,
    )
    return {
        "eb_final": to_eb_dict(eb_final_raw),
        "eb_filing": to_eb_dict(eb_filing_raw),
        "family_final": _table_rows_to_dict(fam_final_raw, family_keys, FAMILY_COUNTRIES),
        "family_filing": _table_rows_to_dict(fam_filing_raw, family_keys, FAMILY_COUNTRIES),
        "dv": _dv_rows_to_dict(_find_dv_table_from_pdf(pdf_bytes)),
    }


def _load_bulletin_history() -> list:
    """Return [(month_datetime, label, snapshot), ...] sorted oldest to newest."""
    if not os.path.isdir(BULLETINS_DIR):
        return []

    history = []
    for filename in sorted(os.listdir(BULLETINS_DIR)):
        if not filename.lower().endswith(".pdf"):
            continue
        path = os.path.join(BULLETINS_DIR, filename)
        label = _parse_label_from_filename(filename)
        if not label:
            continue
        try:
            month_dt = dparser.parse(label).replace(day=1)
        except Exception:
            continue

        stat = os.stat(path)
        cache_key = (path, stat.st_mtime, stat.st_size)
        snapshot = _SNAPSHOT_CACHE.get(cache_key)
        if snapshot is None:
            with open(path, "rb") as handle:
                snapshot = _snapshot_from_pdf(handle.read())
            _SNAPSHOT_CACHE[cache_key] = snapshot

        history.append((month_dt, month_dt.strftime("%B %Y"), snapshot))

    history.sort(key=lambda item: item[0])
    return history


def _list_bulletin_files() -> list:
    """Lightweight archive listing (filename + label only, no PDF parsing) for
    the UI's archive status display."""
    if not os.path.isdir(BULLETINS_DIR):
        return []
    entries = []
    for filename in sorted(os.listdir(BULLETINS_DIR)):
        if not filename.lower().endswith(".pdf"):
            continue
        label = _parse_label_from_filename(filename)
        if not label:
            continue
        try:
            month_dt = dparser.parse(label).replace(day=1)
        except Exception:
            continue
        entries.append((month_dt, label, filename))
    entries.sort(key=lambda item: item[0])
    return [{"label": label, "filename": filename} for _, label, filename in entries]


class BulletinError(Exception):
    pass


class BulletinUploadError(BulletinError):
    pass


class BulletinNotFoundError(BulletinError):
    pass


def save_bulletin_pdf(pdf_bytes: bytes, filename_hint: str = None) -> dict:
    """Validate and persist an uploaded bulletin PDF into bulletins/ so future
    projections can trend it. The destination filename is derived entirely
    from the parsed bulletin month/year (never from the client-supplied name),
    which keeps the write confined to BULLETINS_DIR."""
    if not pdf_bytes:
        raise BulletinUploadError("The uploaded file is empty.")
    if not pdf_bytes.lstrip().startswith(b"%PDF"):
        raise BulletinUploadError("The uploaded file does not look like a valid PDF.")

    label = _parse_pdf_label(pdf_bytes) or _parse_label_from_filename(filename_hint)
    if not label:
        raise BulletinUploadError(
            "Could not determine the bulletin's month and year from the PDF or its filename."
        )
    try:
        month_dt = dparser.parse(label).replace(day=1)
    except Exception:
        raise BulletinUploadError(f"Could not parse a valid month/year from '{label}'.")

    filename = f"visabulletin_{month_dt.strftime('%B%Y')}.pdf"
    path = os.path.join(BULLETINS_DIR, filename)
    os.makedirs(BULLETINS_DIR, exist_ok=True)

    replaced = os.path.exists(path)
    with open(path, "wb") as handle:
        handle.write(pdf_bytes)

    return {
        "label": month_dt.strftime("%B %Y"),
        "filename": filename,
        "replaced": replaced,
        "bulletins": _list_bulletin_files(),
    }


def _read_bulletin_file(filename: str) -> bytes:
    """Read a bulletin PDF from the archive by filename, validated against the
    current archive listing so client input can never escape BULLETINS_DIR."""
    valid_names = {entry["filename"] for entry in _list_bulletin_files()}
    if filename not in valid_names:
        raise BulletinNotFoundError(f"'{filename}' was not found in the bulletins archive.")
    with open(os.path.join(BULLETINS_DIR, filename), "rb") as handle:
        return handle.read()


def generate_csv_from_archive_selection(filename_a: str, filename_b: str) -> dict:
    """Compare two bulletins already stored in the archive, auto-detecting
    which is earlier (previous) and which is later (current) by parsed date -
    the dropdown order the user picked them in doesn't matter."""
    bytes_a = _read_bulletin_file(filename_a)
    bytes_b = _read_bulletin_file(filename_b)
    return generate_csv_from_pdf_auto_order(bytes_a, filename_a, bytes_b, filename_b)


def _seasonal_delta_days(history_by_month: dict, snapshot_key: str, cat: str, country: str, latest_dt: datetime, next_dt: datetime):
    """Return the actual day movement for this category/country during last
    year's same-season transition (e.g. last year's Sep -> Oct), if both
    bulletins are in the archive. Used instead of a within-year average when
    the projection crosses a fiscal-year boundary, since Oct 1 resets annual
    per-country limits and the recent trend does not carry over."""
    prev_latest = history_by_month.get((latest_dt.year - 1, latest_dt.month))
    prev_next = history_by_month.get((next_dt.year - 1, next_dt.month))
    if not prev_latest or not prev_next:
        return None
    old = prev_latest.get(snapshot_key, {}).get(cat, {}).get(country, "")
    new = prev_next.get(snapshot_key, {}).get(cat, {}).get(country, "")
    if isinstance(old, datetime) and isinstance(new, datetime):
        return (new - old).days
    return None


def _downgrade_confidence(confidence: str) -> str:
    return {"High": "Medium", "Medium": "Low", "Low": "Low"}.get(confidence, "Low")


def _project_series(values: list, seasonal_delta=None, crossing_fy: bool = False) -> dict:
    """Project the next month's value from an ordered list of parsed cut-off
    dates. Returns latest/projected display values plus trend metadata.

    When crossing_fy is True, prefer seasonal_delta (last year's actual
    same-season movement) over the recent within-year average, since Oct 1
    resets annual per-country limits and can produce an atypical jump."""
    latest = values[-1] if values else ""
    if latest == "Current":
        return {"latest": "Current", "projected": "Current", "avg_days": "", "trend": "Current", "confidence": "High", "note": ""}

    dates = [v for v in values if isinstance(v, datetime)]
    if not dates:
        return {
            "latest": to_display(latest), "projected": to_display(latest),
            "avg_days": "", "trend": "Insufficient data", "confidence": "Low", "note": "",
        }

    if crossing_fy and seasonal_delta is not None:
        projected = dates[-1] + relativedelta(days=seasonal_delta)
        trend = "Forward" if seasonal_delta > 0 else ("Retrogression" if seasonal_delta < 0 else "No Change")
        return {
            "latest": to_display(dates[-1]),
            "projected": to_display(projected),
            "avg_days": str(seasonal_delta),
            "trend": trend,
            "confidence": "Medium",
            "note": "New fiscal year: based on last year's Sep-to-Oct move, not the recent monthly average.",
        }

    if len(dates) < 2:
        return {
            "latest": to_display(latest), "projected": to_display(latest),
            "avg_days": "", "trend": "Insufficient data", "confidence": "Low",
            "note": "New fiscal year: no prior-year data to base a reset estimate on." if crossing_fy else "",
        }

    deltas = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
    avg_days = sum(deltas) / len(deltas)
    projected = dates[-1] + relativedelta(days=int(round(avg_days)))

    if avg_days > 0.5:
        trend = "Forward"
    elif avg_days < -0.5:
        trend = "Retrogression"
    else:
        trend = "No Change"

    forward = sum(1 for d in deltas if d > 0)
    backward = sum(1 for d in deltas if d < 0)
    dominant = max(forward, backward, len(deltas) - forward - backward)
    consistency = dominant / len(deltas)
    if len(deltas) >= 4 and consistency >= 0.75:
        confidence = "High"
    elif len(deltas) >= 2 and consistency >= 0.5:
        confidence = "Medium"
    else:
        confidence = "Low"

    note = ""
    if crossing_fy:
        confidence = _downgrade_confidence(confidence)
        note = "New fiscal year: annual per-country limits reset Oct 1, so the actual move may differ from the recent trend."

    return {
        "latest": to_display(dates[-1]),
        "projected": to_display(projected),
        "avg_days": str(int(round(avg_days))),
        "trend": trend,
        "confidence": confidence,
        "note": note,
    }


def _project_category_table(
  history: list, snapshot_key: str, category_keys, countries,
  history_by_month: dict = None, crossing_fy: bool = False, latest_dt: datetime = None, next_dt: datetime = None,
) -> dict:
    """Build {category_key: {country: projection}} for one bulletin table."""
    result = {}
    for cat in category_keys:
        result[cat] = {}
        for country in countries:
            series = []
            for _, _, snapshot in history:
                value = snapshot.get(snapshot_key, {}).get(cat, {}).get(country, "")
                if value != "":
                    series.append(value)
            seasonal_delta = (
                _seasonal_delta_days(history_by_month, snapshot_key, cat, country, latest_dt, next_dt)
                if crossing_fy and history_by_month else None
            )
            result[cat][country] = _project_series(series, seasonal_delta, crossing_fy)
    return result


def _build_projection_csv(
  latest_label: str, next_label: str, final: dict, filing: dict, category_map, countries,
) -> str:
    lines = [
        f"Table,Category,Country,{latest_label} (Actual),"
        f"AvgMonthlyMovementDays,{next_label} (Projected),Trend,Confidence,Note"
    ]
    for tbl_label, data_dict in [("Final Action Dates", final), ("Dates for Filing", filing)]:
        if not data_dict:
            continue
        for cat_key, label in category_map:
            for country in countries:
                item = data_dict.get(cat_key, {}).get(country)
                if not item:
                    continue
                lines.append(
                    f"{tbl_label},{label},{country},{item['latest']},"
                    f"{item['avg_days']},{item['projected']},{item['trend']},{item['confidence']},"
                    f"{_csv_field(item.get('note', ''))}"
                )
    return "\n".join(lines)


def _build_projection_prompt(
  latest_label: str, next_label: str, csv_text: str, subtitle: str, months_used: int, crossing_fy: bool = False,
) -> str:
    fy_note = (
        "Note: this projection crosses into a new fiscal year (Oct 1) when annual per-country limits reset, "
        "so some categories use last year's actual Sep-to-Oct move instead of the recent trend.\n"
        if crossing_fy else ""
    )
    return (
        "Create a high-quality Facebook infographic in the same visual style as my sample image for page name \"U.S. Immigration Hub\".\n"
        f"Title: VISA BULLETIN FORECAST - {next_label.upper()}\n"
        f"Subtitle: {subtitle}\n\n"
        f"This is a PROJECTION based on the trend across the last {months_used} published bulletins "
        f"(latest actual bulletin: {latest_label}).\n"
        f"{fy_note}"
        "Build two side-by-side sections: FINAL ACTION DATES and DATES FOR FILING.\n"
        "For each table, include columns: Category, Country, Latest Actual Date, Avg Monthly Movement, Projected Date, Confidence.\n"
        "Use arrows and color coding: green up arrow for projected forward movement, red down arrow for projected retrogression, neutral for no change.\n"
        "Show a clear disclaimer banner: \"Projection only - not an official Department of State forecast.\"\n"
        "Keep branding prominent with U.S. Immigration Hub text and a professional immigration-news look.\n\n"
        "Use this exact data:\n"
        f"{csv_text}"
    )


def _fiscal_year(dt: datetime) -> int:
    """US federal fiscal year: October starts the next fiscal year."""
    return dt.year + 1 if dt.month >= 10 else dt.year


def _project_dv(history: list, history_by_month: dict = None, crossing_fy: bool = False, next_dt: datetime = None) -> dict:
    """Project the next month's DV allocation per region using the numeric part
    of each allocation string. Allocations reset each October, so only months in
    the latest bulletin's fiscal year are trended. When the projection target
    itself crosses into a new fiscal year, extrapolating that within-year growth
    would be wrong (DV restarts low each October) - use last year's actual
    value for the target month instead, if the archive has it."""
    latest_fy = _fiscal_year(history[-1][0])
    history = [entry for entry in history if _fiscal_year(entry[0]) == latest_fy]

    result = {}
    regions = []
    for _, _, snapshot in history:
        for region in snapshot.get("dv", {}):
            if region not in regions:
                regions.append(region)

    for region in regions:
        numbers = []
        latest_raw = ""
        for _, _, snapshot in history:
            raw = snapshot.get("dv", {}).get(region, "")
            if not raw:
                continue
            latest_raw = raw
            match = re.search(r"([\d,]+)", raw)
            if match:
                numbers.append(int(match.group(1).replace(",", "")))

        if crossing_fy and history_by_month and next_dt is not None:
            prior_year_next = history_by_month.get((next_dt.year - 1, next_dt.month))
            prior_raw = prior_year_next.get("dv", {}).get(region, "") if prior_year_next else ""
            if prior_raw:
                result[region] = {
                    "latest": latest_raw or "N/A",
                    "projected": prior_raw,
                    "avg_change": "",
                    "trend": "Reset (new fiscal year)",
                    "confidence": "Medium",
                    "note": "New fiscal year: projected from last year's same month, since DV allocations restart each Oct 1.",
                }
                continue

        if len(numbers) < 2:
            result[region] = {
                "latest": latest_raw or "N/A", "projected": latest_raw or "N/A",
                "avg_change": "", "trend": "Insufficient data", "confidence": "Low",
                "note": "New fiscal year: no prior-year data to base a reset estimate on." if crossing_fy else "",
            }
            continue

        if not re.search(r"\d", latest_raw):
            # e.g. a region already marked "Current" - nothing numeric to project.
            result[region] = {
                "latest": latest_raw, "projected": latest_raw,
                "avg_change": "", "trend": "Flat", "confidence": "High", "note": "",
            }
            continue

        deltas = [numbers[i] - numbers[i - 1] for i in range(1, len(numbers))]
        avg_change = sum(deltas) / len(deltas)
        projected_value = max(0, int(round(numbers[-1] + avg_change)))
        prefix = re.sub(r"[\d,]+.*$", "", latest_raw).strip()
        confidence = "High" if len(deltas) >= 4 else "Medium"
        note = ""
        if crossing_fy:
            confidence = _downgrade_confidence(confidence)
            note = "New fiscal year: no prior-year figure found for this region; extrapolated within-year growth instead, which likely overstates the actual reset."
        result[region] = {
            "latest": latest_raw,
            "projected": f"{prefix} {projected_value:,}".strip(),
            "avg_change": str(int(round(avg_change))),
            "trend": "Increasing" if avg_change > 0 else ("Decreasing" if avg_change < 0 else "Flat"),
            "confidence": confidence,
            "note": note,
        }
    return result


def _build_dv_projection_csv(latest_label: str, next_label: str, dv_data: dict) -> str:
    lines = [
        f"Category,Region,{latest_label} (Actual),AvgMonthlyChange,"
        f"{next_label} (Projected),Trend,Confidence,Note"
    ]
    for region, item in dv_data.items():
        lines.append(
            f"Diversity Visa,{_csv_field(region)},{_csv_field(item['latest'])},"
            f"{item['avg_change']},{_csv_field(item['projected'])},{item['trend']},{item['confidence']},"
            f"{_csv_field(item.get('note', ''))}"
        )
    return "\n".join(lines)


def _build_dv_projection_prompt(latest_label: str, next_label: str, csv_text: str, months_used: int, crossing_fy: bool = False) -> str:
    fy_note = (
        "Note: DV allocations reset every Oct 1 (new fiscal year), so where last year's same-month figure was "
        "available it was used directly instead of extrapolating this year's growth.\n"
        if crossing_fy else ""
    )
    return (
        "Create a high-quality Facebook infographic in the same visual style as my sample image for page name \"U.S. Immigration Hub\".\n"
        f"Title: VISA BULLETIN FORECAST - {next_label.upper()}\n"
        "Subtitle: DIVERSITY VISA (DV) LOTTERY REGIONAL ALLOCATIONS - PROJECTED\n\n"
        f"This is a PROJECTION based on the trend across the last {months_used} published bulletins "
        f"(latest actual bulletin: {latest_label}).\n"
        f"{fy_note}"
        "Build one table with columns: Region, Latest Actual Allocation, Avg Monthly Change, Projected Allocation, Confidence.\n"
        "Use color coding: green for regions projected to increase, red for a projected decrease, neutral for flat.\n"
        "Show a clear disclaimer banner: \"Projection only - not an official Department of State forecast.\"\n"
        "Keep branding prominent with U.S. Immigration Hub text and a professional immigration-news look.\n\n"
        "Use this exact data:\n"
        f"{csv_text}"
    )


def _projection_error(error: str) -> dict:
    return {
        "csv": "", "prompt": "",
        "eb_sub_csv": "", "eb_sub_prompt": "",
        "family_csv": "", "family_prompt": "",
        "dv_csv": "", "dv_prompt": "",
        "latest_label": "", "next_label": "", "months_used": 0,
        "months_trended": [], "months_available": [], "crosses_fiscal_year": False,
        "error": error,
    }


def generate_projection(lookback_months: int = DEFAULT_LOOKBACK_MONTHS) -> dict:
    """Project the next (unpublished) month for every category from the trend
    across the bulletin PDFs stored in bulletins/."""
    full_history = _load_bulletin_history()
    if len(full_history) < 2:
        return _projection_error(
            "Need at least two bulletin PDFs in the bulletins/ folder to project a future month."
        )

    lookback_months = max(2, min(lookback_months, len(full_history)))
    history = full_history[-lookback_months:]

    latest_dt, latest_label, _ = history[-1]
    next_dt = latest_dt + relativedelta(months=1)
    next_label = next_dt.strftime("%B %Y")
    months_used = len(history)

    # Oct 1 starts a new fiscal year: annual per-country limits reset and DV
    # allocations restart low, so a plain within-year average can mislead.
    # Use the full (unrestricted) archive to look up last year's actual
    # same-season transition as a better basis for the projection.
    crossing_fy = _fiscal_year(next_dt) != _fiscal_year(latest_dt)
    history_by_month = {(dt.year, dt.month): snapshot for dt, _, snapshot in full_history}

    fy_kwargs = dict(history_by_month=history_by_month, crossing_fy=crossing_fy, latest_dt=latest_dt, next_dt=next_dt)

    eb_final = _project_category_table(history, "eb_final", ("1st", "2nd", "3rd"), EB_COUNTRIES, **fy_kwargs)
    eb_filing = _project_category_table(history, "eb_filing", ("1st", "2nd", "3rd"), EB_COUNTRIES, **fy_kwargs)
    eb_sub_final = _project_category_table(history, "eb_final", EB_SUB_CATEGORY_KEYS, EB_COUNTRIES, **fy_kwargs)
    eb_sub_filing = _project_category_table(history, "eb_filing", EB_SUB_CATEGORY_KEYS, EB_COUNTRIES, **fy_kwargs)

    family_keys = [k for k, _ in FAMILY_CATEGORY_MAP]
    family_final = _project_category_table(history, "family_final", family_keys, FAMILY_COUNTRIES, **fy_kwargs)
    family_filing = _project_category_table(history, "family_filing", family_keys, FAMILY_COUNTRIES, **fy_kwargs)

    dv_data = _project_dv(history, history_by_month=history_by_month, crossing_fy=crossing_fy, next_dt=next_dt)

    eb_csv = _build_projection_csv(latest_label, next_label, eb_final, eb_filing, EB_CATEGORY_MAP, EB_COUNTRIES)
    eb_sub_csv = _build_projection_csv(latest_label, next_label, eb_sub_final, eb_sub_filing, EB_SUB_CATEGORY_MAP, EB_COUNTRIES)
    family_csv = _build_projection_csv(latest_label, next_label, family_final, family_filing, FAMILY_CATEGORY_MAP, FAMILY_COUNTRIES)
    dv_csv = _build_dv_projection_csv(latest_label, next_label, dv_data)

    return {
        "csv": eb_csv,
        "prompt": _build_projection_prompt(
            latest_label, next_label, eb_csv,
            "EMPLOYMENT-BASED PREFERENCES (EB-1, EB-2 & EB-3)", months_used, crossing_fy,
        ),
        "eb_sub_csv": eb_sub_csv,
        "eb_sub_prompt": _build_projection_prompt(
            latest_label, next_label, eb_sub_csv,
            "EMPLOYMENT-BASED: EB-4, OTHER WORKERS, RELIGIOUS WORKERS & EB-5", months_used, crossing_fy,
        ),
        "family_csv": family_csv,
        "family_prompt": _build_projection_prompt(
            latest_label, next_label, family_csv,
            "FAMILY-SPONSORED PREFERENCES (F1, F2A, F2B, F3 & F4)", months_used, crossing_fy,
        ),
        "dv_csv": dv_csv,
        "dv_prompt": _build_dv_projection_prompt(latest_label, next_label, dv_csv, months_used, crossing_fy),
        "latest_label": latest_label,
        "next_label": next_label,
        "months_used": months_used,
        "months_trended": [label for _, label, _ in history],
        "months_available": [label for _, label, _ in full_history],
        "crosses_fiscal_year": crossing_fy,
        "error": "",
    }


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

    #homeView { display: flex; flex-direction: column; align-items: center; }
    .nav-cards { display: flex; gap: 20px; flex-wrap: wrap; justify-content: center; margin-top: 16px; }
    .nav-card {
      background: #132840;
      border: 1px solid #1e3a5f;
      border-radius: 10px;
      padding: 28px 24px;
      width: 240px;
      text-align: left;
      cursor: pointer;
      color: #e0e0e0;
      white-space: normal;
      font-weight: 400;
    }
    .nav-card:hover { border-color: #ffd700; background: #17304f; }
    .nav-card-icon {
      width: 44px;
      height: 44px;
      display: flex;
      align-items: center;
      justify-content: center;
      background: rgba(255, 215, 0, 0.12);
      border-radius: 10px;
      color: #ffd700;
      margin-bottom: 14px;
    }
    .nav-card-icon svg { width: 24px; height: 24px; }
    .nav-card-title { color: #ffd700; font-size: 1.05rem; font-weight: 700; margin-bottom: 6px; }
    .nav-card-desc { font-size: 0.82rem; color: #aaa; line-height: 1.4; font-weight: 400; }

    .view { width: 100%; max-width: 680px; display: flex; flex-direction: column; align-items: center; }
    .view-header { display: flex; align-items: flex-start; gap: 14px; margin-bottom: 16px; width: 100%; }
    .back-btn { background: transparent; color: #ffd700; border: 1px solid #2a5080; padding: 8px 14px; font-size: 0.85rem; font-weight: 600; }
    .back-btn:hover { background: #17304f; }
    .view-header h1 { font-size: 1.5rem; margin: 0 0 2px; }
    .view-header p.sub { margin: 0; }

    select {
      flex: 1;
      min-width: 200px;
      padding: 10px 14px;
      background: #0b1f3a;
      border: 1px solid #2a4a6a;
      border-radius: 6px;
      color: #fff;
      font-size: 0.95rem;
    }
    select:focus { outline: none; border-color: #ffd700; }

    .bulletins-table { width: 100%; border-collapse: collapse; margin-top: 12px; }
    .bulletins-table th, .bulletins-table td {
      text-align: left; padding: 8px 10px; border-bottom: 1px solid #1e3a5f; font-size: 0.85rem;
    }
    .bulletins-table th { color: #8ee6a0; font-size: 0.78rem; text-transform: uppercase; letter-spacing: .04em; }
  </style>
</head>
<body>
  <div id="homeView" style="display:flex;">
    <h1>Visa Bulletin Tracker</h1>
    <p class="sub">Choose what you'd like to do</p>
    <div class="nav-cards">
      <button class="nav-card" onclick="navigateTo('compare')">
        <div class="nav-card-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"></line><line x1="12" y1="20" x2="12" y2="4"></line><line x1="6" y1="20" x2="6" y2="14"></line></svg>
        </div>
        <div class="nav-card-title">Month Comparison</div>
        <div class="nav-card-desc">Compare two bulletins, current vs previous. Pick from the archive or upload — we auto-detect which is earlier.</div>
      </button>
      <button class="nav-card" onclick="navigateTo('predict')">
        <div class="nav-card-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"></polyline><polyline points="17 6 23 6 23 12"></polyline></svg>
        </div>
        <div class="nav-card-title">Future Prediction</div>
        <div class="nav-card-desc">Forecast the next unpublished month by trending the bulletin archive.</div>
      </button>
      <button class="nav-card" onclick="navigateTo('admin')">
        <div class="nav-card-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="17 8 12 3 7 8"></polyline><line x1="12" y1="3" x2="12" y2="15"></line></svg>
        </div>
        <div class="nav-card-title">Admin / Uploads</div>
        <div class="nav-card-desc">Upload bulletin PDFs and see everything currently stored in the archive.</div>
      </button>
    </div>
  </div>

  <div id="compareView" class="view" style="display:none;">
    <div class="view-header">
      <button class="back-btn" onclick="navigateTo('home')">← Back</button>
      <div>
        <h1>Month Comparison</h1>
        <p class="sub">Employment-Based, Family-Sponsored &amp; Diversity Visa — pick any two bulletins</p>
      </div>
    </div>

    <div class="card">
      <label>Bulletin 1</label>
      <div class="file-row">
        <select id="bulletinASelect" onchange="onBulletinSelectChange('A')"></select>
      </div>
      <input class="file-input" type="file" id="bulletinAUpload" accept="application/pdf">

      <label style="margin-top:16px;">Bulletin 2</label>
      <div class="file-row">
        <select id="bulletinBSelect" onchange="onBulletinSelectChange('B')"></select>
      </div>
      <input class="file-input" type="file" id="bulletinBUpload" accept="application/pdf">

      <div class="row" style="margin-top: 16px;">
        <button id="compareBtn" onclick="runComparison()">Compare</button>
        <span class="spinner" id="compareSpinner">⏳ Loading…</span>
      </div>
      <p class="hint">We automatically detect which bulletin is earlier (previous) and which is later (current) — selection order doesn't matter.</p>

      <details style="margin-top: 16px;">
        <summary style="cursor:pointer; color:#8ee6a0; font-size:0.9rem;">Advanced: fetch by month name (scrapes travel.state.gov)</summary>
        <div class="row" style="margin-top: 12px;">
          <input type="text" id="month" placeholder="e.g. july, august, may …">
          <button id="generateBtn" onclick="generate()">Generate CSV</button>
          <span class="spinner" id="spinner">⏳ Loading…</span>
        </div>
        <p class="hint">Compares the entered month against the previous month. Blank = uses today's month as previous, next month as current.</p>
      </details>

      <div class="error" id="errorBox"></div>

      <template id="categorySectionTemplate">
        <div class="result-section">
          <div class="result-header">
            <span class="result-label"></span>
            <button class="copy-btn" data-copy="csv">Copy CSV</button>
          </div>
          <textarea class="csv-output" readonly></textarea>
        </div>
        <div class="prompt-section">
          <div class="section-title">
            <span class="section-title-label">Copy Prompt</span>
            <button class="copy-btn" data-copy="prompt">Copy Prompt</button>
          </div>
          <textarea class="prompt-ta prompt-output" readonly></textarea>
        </div>
      </template>


    <div id="ebSection" data-title="Employment-Based (EB1, EB2 &amp; EB3)"></div>
    <div id="ebSubSection" data-title="Employment-Based: EB-4, Other Workers, Religious Workers &amp; EB-5"></div>
    <div id="familySection" data-title="Family-Sponsored (F1, F2A, F2B, F3 &amp; F4)"></div>
    <div id="dvSection" data-title="Diversity Visa (DV)"></div>
    </div>
  </div>

  <div id="predictView" class="view" style="display:none;">
    <div class="view-header">
      <button class="back-btn" onclick="navigateTo('home')">← Back</button>
      <div>
        <h1>Future Prediction</h1>
        <p class="sub">Forecast the next unpublished month by trending the bulletin archive</p>
      </div>
    </div>

    <div class="card">
    <p class="hint" style="margin-top:0;">
      Forecasts the next (unpublished) month for every category by trending the bulletin PDFs
      stored in the <code>bulletins/</code> folder. Projection only — not an official forecast.
    </p>

    <label>Add a bulletin PDF to the archive</label>
    <div class="file-row">
      <input class="file-input" type="file" id="predictUploadFile" accept="application/pdf" title="Bulletin PDF">
      <button type="button" class="file-picker-btn" onclick="pickFile('predictUploadFile')">Choose Bulletin PDF</button>
      <span class="file-name" id="predictUploadFileName">No file chosen</span>
      <button id="predictUploadBtn" onclick="handleWidgetUpload('predictUploadFile', 'predictUploadFileName', 'predictUploadErrorBox')">Save to Archive</button>
      <span class="spinner" id="predictUploadSpinner">⏳ Saving…</span>
    </div>
    <p class="hint">The month/year is read from the PDF itself, so it's saved with the right filename automatically.</p>
    <div class="error" id="predictUploadErrorBox"></div>
    <p class="hint" id="archiveStatus"></p>

    <hr />
    <div class="row">
      <div style="flex:1; min-width:160px;">
        <label for="lookback">Months of history to trend</label>
        <input type="text" id="lookback" value="6" placeholder="e.g. 6">
      </div>
      <button id="projectBtn" onclick="projectFuture()">Generate Projection</button>
      <span class="spinner" id="projectSpinner">⏳ Loading…</span>
    </div>
    <p class="hint" id="projectMeta"></p>
    <div class="error" id="fyBanner" style="background:#3a2d15; border-color:#a07a30; color:#ffcf7a; display:none;">
      ⚠️ This projection crosses into a new fiscal year (Oct 1) — annual per-country limits reset and DV
      allocations restart low, so some categories below use last year's actual Sep→Oct move instead of the
      recent monthly average. Check each row's Note column and treat these projections as lower confidence.
    </div>

    <div class="error" id="projectErrorBox"></div>

    <div id="projEbSection"></div>
    <div id="projEbSubSection"></div>
    <div id="projFamilySection"></div>
    <div id="projDvSection"></div>
    </div>
  </div>

  <div id="adminView" class="view" style="display:none;">
    <div class="view-header">
      <button class="back-btn" onclick="navigateTo('home')">← Back</button>
      <div>
        <h1>Admin / Uploads</h1>
        <p class="sub">Upload bulletin PDFs and manage the archive</p>
      </div>
    </div>

    <div class="card">
      <label>Upload a bulletin PDF</label>
      <div class="file-row">
        <input class="file-input" type="file" id="adminUploadFile" accept="application/pdf" title="Bulletin PDF">
        <button type="button" class="file-picker-btn" onclick="pickFile('adminUploadFile')">Choose Bulletin PDF</button>
        <span class="file-name" id="adminUploadFileName">No file chosen</span>
        <button id="adminUploadBtn" onclick="handleWidgetUpload('adminUploadFile', 'adminUploadFileName', 'adminUploadErrorBox')">Save to Archive</button>
        <span class="spinner" id="adminUploadSpinner">⏳ Saving…</span>
      </div>
      <p class="hint">The month/year is read from the PDF itself, so it's saved with the right filename automatically.</p>
      <div class="error" id="adminUploadErrorBox"></div>

      <h2 style="color:#8ee6a0; font-size:1rem; margin-top:24px; margin-bottom:0;">Archived Bulletins</h2>
      <table class="bulletins-table">
        <thead><tr><th>Month</th><th>Filename</th></tr></thead>
        <tbody id="bulletinsTableBody"></tbody>
      </table>
      <p class="hint" id="adminEmptyHint" style="display:none;">No bulletins uploaded yet.</p>
    </div>
  </div>

  <script>
    const CATEGORIES = [
      { key: 'eb', title: 'Employment-Based (EB1, EB2 & EB3)', csvField: 'csv', promptField: 'prompt' },
      { key: 'ebSub', title: 'Employment-Based: EB-4, Other Workers, Religious Workers & EB-5', csvField: 'eb_sub_csv', promptField: 'eb_sub_prompt' },
      { key: 'family', title: 'Family-Sponsored (F1, F2A, F2B, F3 & F4)', csvField: 'family_csv', promptField: 'family_prompt' },
      { key: 'dv', title: 'Diversity Visa (DV)', csvField: 'dv_csv', promptField: 'dv_prompt' },
    ];

    const PROJECTION_CATEGORIES = CATEGORIES.map(cat => ({
      ...cat,
      key: 'proj' + cat.key.charAt(0).toUpperCase() + cat.key.slice(1),
      title: 'Projected — ' + cat.title,
    }));

    function buildCategorySections(categories) {
      const tpl = document.getElementById('categorySectionTemplate');
      categories.forEach(cat => {
        const container = document.getElementById(cat.key + 'Section');
        const heading = document.createElement('h3');
        heading.textContent = cat.title;
        heading.style.cssText = 'color:#ffd700; font-size:1rem; margin: 24px 0 8px;';
        container.appendChild(heading);
        const clone = tpl.content.cloneNode(true);
        container.appendChild(clone);
        container.querySelector('.section-title-label').textContent = `Copy Prompt - ${cat.title}`;
        container.querySelectorAll('.result-section, .prompt-section').forEach(el => {
          el.style.display = 'none';
        });
      });
    }

    function renderCategory(cat, data, labelText) {
      const container = document.getElementById(cat.key + 'Section');
      const resultSection = container.querySelector('.result-section');
      const promptSection = container.querySelector('.prompt-section');
      const csv = data[cat.csvField] || '';
      const prompt = data[cat.promptField] || '';

      container.querySelector('.result-label').textContent = labelText;
      container.querySelector('.csv-output').value = csv;
      container.querySelector('.prompt-output').value = prompt;

      resultSection.style.display = 'block';
      promptSection.style.display = 'block';
    }

    function renderAllCategories(data) {
      const labelText = data.prev_label + '  →  ' + data.curr_label;
      CATEGORIES.forEach(cat => renderCategory(cat, data, labelText));
    }

    function renderProjection(data) {
      const labelText = data.latest_label + ' (actual)  →  ' + data.next_label + ' (projected)';
      PROJECTION_CATEGORIES.forEach(cat => renderCategory(cat, data, labelText));
      document.getElementById('projectMeta').textContent =
        `Trended over ${data.months_used} bulletin(s): ${data.months_trended.join(', ')}`
        + ` — ${data.months_available.length} bulletin(s) available in bulletins/.`;
      document.getElementById('fyBanner').style.display = data.crosses_fiscal_year ? 'block' : 'none';
    }

    function hideCategories(categories) {
      categories.forEach(cat => {
        document
          .getElementById(cat.key + 'Section')
          .querySelectorAll('.result-section, .prompt-section')
          .forEach(el => { el.style.display = 'none'; });
      });
    }

    function hideAllCategories() {
      hideCategories(CATEGORIES);
    }

    async function generate() {
      const month = document.getElementById('month').value.trim();
      const btn   = document.getElementById('generateBtn');
      const spinner = document.getElementById('spinner');
      const errorBox = document.getElementById('errorBox');

      btn.disabled = true;
      spinner.style.display = 'inline';
      errorBox.style.display = 'none';
      hideAllCategories();

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
          renderAllCategories(data);
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

    async function projectFuture() {
      const btn = document.getElementById('projectBtn');
      const spinner = document.getElementById('projectSpinner');
      const errorBox = document.getElementById('projectErrorBox');
      const lookback = parseInt(document.getElementById('lookback').value, 10) || 6;

      btn.disabled = true;
      spinner.style.display = 'inline';
      errorBox.style.display = 'none';
      document.getElementById('projectMeta').textContent = '';
      document.getElementById('fyBanner').style.display = 'none';
      hideCategories(PROJECTION_CATEGORIES);

      try {
        const resp = await fetch('/project', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ lookback })
        });
        const data = await resp.json();

        if (data.error) {
          errorBox.textContent = data.error;
          errorBox.style.display = 'block';
        } else {
          renderProjection(data);
        }
      } catch (err) {
        errorBox.textContent = 'Request failed: ' + err.message;
        errorBox.style.display = 'block';
      } finally {
        btn.disabled = false;
        spinner.style.display = 'none';
      }
    }

    function setFileName(inputId, labelId) {
      const input = document.getElementById(inputId);
      const label = document.getElementById(labelId);
      const file = input.files && input.files[0];
      label.textContent = file ? file.name : 'No file chosen';
    }

    // ---- Archive helpers shared by the Compare / Predict / Admin views ----

    async function uploadBulletinFile(file) {
      const formData = new FormData();
      formData.append('bulletin_pdf', file);
      const resp = await fetch('/bulletins/upload', { method: 'POST', body: formData });
      const data = await resp.json();
      if (data.error) throw new Error(data.error);
      return data;
    }

    function renderArchiveStatus(bulletins) {
      const el = document.getElementById('archiveStatus');
      if (!el) return;
      const months = bulletins.map(b => b.label);
      el.textContent = months.length
        ? `Archive has ${months.length} bulletin(s): ${months.join(', ')}`
        : 'Archive is empty — upload a bulletin PDF to get started.';
    }

    function renderBulletinsTable(bulletins) {
      const tbody = document.getElementById('bulletinsTableBody');
      const emptyHint = document.getElementById('adminEmptyHint');
      if (!tbody) return;
      tbody.innerHTML = '';
      if (!bulletins.length) {
        emptyHint.style.display = 'block';
        return;
      }
      emptyHint.style.display = 'none';
      bulletins.forEach(b => {
        const tr = document.createElement('tr');
        const tdLabel = document.createElement('td');
        tdLabel.textContent = b.label;
        const tdFile = document.createElement('td');
        tdFile.textContent = b.filename;
        tr.appendChild(tdLabel);
        tr.appendChild(tdFile);
        tbody.appendChild(tr);
      });
    }

    function populateBulletinSelect(selectEl, bulletins, preferredValue) {
      selectEl.innerHTML = '';
      const placeholder = document.createElement('option');
      placeholder.value = '';
      placeholder.textContent = bulletins.length ? 'Select a bulletin…' : 'No bulletins yet — upload one below';
      placeholder.disabled = true;
      selectEl.appendChild(placeholder);

      bulletins.forEach(b => {
        const opt = document.createElement('option');
        opt.value = b.filename;
        opt.textContent = b.label;
        selectEl.appendChild(opt);
      });

      const uploadOpt = document.createElement('option');
      uploadOpt.value = '__upload__';
      uploadOpt.textContent = '+ Upload a new bulletin PDF…';
      selectEl.appendChild(uploadOpt);

      const hasPreferred = preferredValue && bulletins.some(b => b.filename === preferredValue);
      selectEl.value = hasPreferred ? preferredValue : '';
      selectEl.dataset.lastValid = selectEl.value;
    }

    function syncArchiveEverywhere(bulletins) {
      ['A', 'B'].forEach(slot => {
        const select = document.getElementById('bulletin' + slot + 'Select');
        if (select) populateBulletinSelect(select, bulletins, select.dataset.lastValid);
      });
      renderArchiveStatus(bulletins);
      renderBulletinsTable(bulletins);
    }

    async function fetchArchive() {
      try {
        const resp = await fetch('/bulletins');
        const data = await resp.json();
        const bulletins = data.bulletins || [];
        const selectA = document.getElementById('bulletinASelect');
        const selectB = document.getElementById('bulletinBSelect');

        let preferredA = selectA.dataset.lastValid;
        let preferredB = selectB.dataset.lastValid;
        if (!selectA.dataset.initialized) {
          // Default to the two most recently archived bulletins — the usual
          // "new bulletin just arrived" comparison.
          selectA.dataset.initialized = '1';
          if (bulletins.length >= 2) {
            preferredA = bulletins[bulletins.length - 2].filename;
            preferredB = bulletins[bulletins.length - 1].filename;
          } else if (bulletins.length === 1) {
            preferredA = bulletins[0].filename;
          }
        }

        populateBulletinSelect(selectA, bulletins, preferredA);
        populateBulletinSelect(selectB, bulletins, preferredB);
        renderArchiveStatus(bulletins);
        renderBulletinsTable(bulletins);
      } catch (err) {
        // Non-fatal: leave existing UI state if this fails.
      }
    }

    function onBulletinSelectChange(slot) {
      const select = document.getElementById('bulletin' + slot + 'Select');
      if (select.value === '__upload__') {
        const revertTo = select.dataset.lastValid || '';
        document.getElementById('bulletin' + slot + 'Upload').click();
        select.value = revertTo;
      } else {
        select.dataset.lastValid = select.value;
      }
    }

    async function handleInlineUpload(slot) {
      const fileInput = document.getElementById('bulletin' + slot + 'Upload');
      const select = document.getElementById('bulletin' + slot + 'Select');
      const errorBox = document.getElementById('errorBox');
      const file = fileInput.files[0];
      if (!file) return;

      try {
        const data = await uploadBulletinFile(file);
        syncArchiveEverywhere(data.bulletins);
        select.value = data.filename;
        select.dataset.lastValid = data.filename;
      } catch (err) {
        errorBox.textContent = err.message;
        errorBox.style.display = 'block';
      } finally {
        fileInput.value = '';
      }
    }

    async function handleWidgetUpload(fileInputId, fileNameLabelId, errorBoxId) {
      const prefix = fileInputId.replace(/File$/, '');
      const fileInput = document.getElementById(fileInputId);
      const btn = document.getElementById(prefix + 'Btn');
      const spinner = document.getElementById(prefix + 'Spinner');
      const errorBox = document.getElementById(errorBoxId);
      const file = fileInput.files[0];

      if (!file) {
        errorBox.textContent = 'Please choose a bulletin PDF first.';
        errorBox.style.display = 'block';
        return;
      }

      btn.disabled = true;
      spinner.style.display = 'inline';
      errorBox.style.display = 'none';

      try {
        const data = await uploadBulletinFile(file);
        fileInput.value = '';
        setFileName(fileInputId, fileNameLabelId);
        syncArchiveEverywhere(data.bulletins);
      } catch (err) {
        errorBox.textContent = err.message;
        errorBox.style.display = 'block';
      } finally {
        btn.disabled = false;
        spinner.style.display = 'none';
      }
    }

    async function runComparison() {
      const a = document.getElementById('bulletinASelect').value;
      const b = document.getElementById('bulletinBSelect').value;
      const btn = document.getElementById('compareBtn');
      const spinner = document.getElementById('compareSpinner');
      const errorBox = document.getElementById('errorBox');

      if (!a || !b) {
        errorBox.textContent = 'Please select two bulletins to compare.';
        errorBox.style.display = 'block';
        return;
      }
      if (a === b) {
        errorBox.textContent = 'Please select two different bulletins.';
        errorBox.style.display = 'block';
        return;
      }

      btn.disabled = true;
      spinner.style.display = 'inline';
      errorBox.style.display = 'none';
      hideAllCategories();

      try {
        const resp = await fetch('/generate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ bulletin_a: a, bulletin_b: b })
        });
        const data = await resp.json();

        if (data.error) {
          errorBox.textContent = data.error;
          errorBox.style.display = 'block';
        } else {
          renderAllCategories(data);
        }
      } catch (err) {
        errorBox.textContent = 'Request failed: ' + err.message;
        errorBox.style.display = 'block';
      } finally {
        btn.disabled = false;
        spinner.style.display = 'none';
      }
    }

    // ---- Home / section navigation ----

    const VIEWS = ['home', 'compare', 'predict', 'admin'];

    function showView(view) {
      if (!VIEWS.includes(view)) view = 'home';
      VIEWS.forEach(v => {
        document.getElementById(v + 'View').style.display = (v === view) ? 'flex' : 'none';
      });
      window.scrollTo(0, 0);
      if (view === 'admin') fetchArchive();
    }

    function navigateTo(view) {
      location.hash = view === 'home' ? '' : view;
      showView(view);
    }

    window.addEventListener('hashchange', () => {
      showView(location.hash.replace('#', '') || 'home');
    });

    document.addEventListener('click', e => {
      const btn = e.target.closest('.copy-btn');
      if (!btn) return;
      const section = btn.closest('.result-section, .prompt-section');
      const ta = section.querySelector('textarea');
      ta.select();
      navigator.clipboard.writeText(ta.value).then(() => {
        const original = btn.textContent;
        btn.textContent = 'Copied!';
        setTimeout(() => btn.textContent = original, 1800);
      });
    });

    document.addEventListener('DOMContentLoaded', () => {
      buildCategorySections(CATEGORIES);
      buildCategorySections(PROJECTION_CATEGORIES);

      document.getElementById('month').addEventListener('keydown', e => {
        if (e.key === 'Enter') generate();
      });
      document.getElementById('lookback').addEventListener('keydown', e => {
        if (e.key === 'Enter') projectFuture();
      });
      document.getElementById('bulletinAUpload').addEventListener('change', () => handleInlineUpload('A'));
      document.getElementById('bulletinBUpload').addEventListener('change', () => handleInlineUpload('B'));
      document.getElementById('predictUploadFile').addEventListener('change', () => {
        setFileName('predictUploadFile', 'predictUploadFileName');
      });
      document.getElementById('adminUploadFile').addEventListener('change', () => {
        setFileName('adminUploadFile', 'adminUploadFileName');
      });

      fetchArchive();
      showView(location.hash.replace('#', '') || 'home');
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
            result = generate_csv_from_pdf_auto_order(
                previous_file.read(), previous_file.filename,
                current_file.read(), current_file.filename,
            )
        except Exception as exc:
            return jsonify({"error": f"PDF parsing failed: {str(exc)}"}), 400
    else:
        body = request.get_json(silent=True) or {}
        bulletin_a = (body.get("bulletin_a") or "").strip()
        bulletin_b = (body.get("bulletin_b") or "").strip()

        if bulletin_a and bulletin_b:
            try:
                result = generate_csv_from_archive_selection(bulletin_a, bulletin_b)
            except BulletinError as exc:
                return jsonify({"error": str(exc)}), 400
        else:
            # Fall back to scraping the official site for a given month name.
            month_input = (body.get("month") or "").strip()
            result = generate_csv(month_input)

    if result["error"]:
        return jsonify({"error": result["error"]}), 400

    return jsonify(result)


@app.post("/project")
def project():
    body = request.get_json(silent=True) or {}
    try:
        lookback = int(body.get("lookback") or DEFAULT_LOOKBACK_MONTHS)
    except (TypeError, ValueError):
        lookback = DEFAULT_LOOKBACK_MONTHS

    result = generate_projection(lookback)
    if result["error"]:
        return jsonify({"error": result["error"]}), 400

    return jsonify(result)


@app.get("/bulletins")
def list_bulletins():
    return jsonify({"bulletins": _list_bulletin_files()})


@app.post("/bulletins/upload")
def upload_bulletin():
    uploaded = request.files.get("bulletin_pdf")
    if not uploaded:
        return jsonify({"error": "Please choose a Visa Bulletin PDF to upload."}), 400

    try:
        result = save_bulletin_pdf(uploaded.read(), filename_hint=uploaded.filename)
    except BulletinUploadError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"Upload failed: {str(exc)}"}), 400

    return jsonify(result)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, port=5050)
