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
  <p class="sub">Employment-Based, Family-Sponsored &amp; Diversity Visa — month-over-month comparison</p>

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

  <script>
    const CATEGORIES = [
      { key: 'eb', title: 'Employment-Based (EB1, EB2 & EB3)', csvField: 'csv', promptField: 'prompt' },
      { key: 'ebSub', title: 'Employment-Based: EB-4, Other Workers, Religious Workers & EB-5', csvField: 'eb_sub_csv', promptField: 'eb_sub_prompt' },
      { key: 'family', title: 'Family-Sponsored (F1, F2A, F2B, F3 & F4)', csvField: 'family_csv', promptField: 'family_prompt' },
      { key: 'dv', title: 'Diversity Visa (DV)', csvField: 'dv_csv', promptField: 'dv_prompt' },
    ];

    function buildCategorySections() {
      const tpl = document.getElementById('categorySectionTemplate');
      CATEGORIES.forEach(cat => {
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

    function renderCategory(cat, data) {
      const container = document.getElementById(cat.key + 'Section');
      const resultSection = container.querySelector('.result-section');
      const promptSection = container.querySelector('.prompt-section');
      const csv = data[cat.csvField] || '';
      const prompt = data[cat.promptField] || '';

      container.querySelector('.result-label').textContent =
        data.prev_label + '  →  ' + data.curr_label;
      container.querySelector('.csv-output').value = csv;
      container.querySelector('.prompt-output').value = prompt;

      resultSection.style.display = 'block';
      promptSection.style.display = 'block';
    }

    function renderAllCategories(data) {
      CATEGORIES.forEach(cat => renderCategory(cat, data));
    }

    function hideAllCategories() {
      document.querySelectorAll('.result-section, .prompt-section').forEach(el => {
        el.style.display = 'none';
      });
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

    async function uploadPdfs() {
      const prev = document.getElementById('previousPdf').files[0];
      const curr = document.getElementById('currentPdf').files[0];
      const btn = document.getElementById('uploadBtn');
      const spinner = document.getElementById('spinner');
      const errorBox = document.getElementById('errorBox');

      if (!prev || !curr) {
        errorBox.textContent = 'Please select both PDF files.';
        errorBox.style.display = 'block';
        return;
      }

      btn.disabled = true;
      spinner.style.display = 'inline';
      errorBox.style.display = 'none';
      hideAllCategories();

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

    function setFileName(inputId, labelId) {
      const input = document.getElementById(inputId);
      const label = document.getElementById(labelId);
      const file = input.files && input.files[0];
      label.textContent = file ? file.name : 'No file chosen';
    }

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

    // Allow Enter key in the input to trigger generate
    document.addEventListener('DOMContentLoaded', () => {
      buildCategorySections();
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
            result = generate_csv_from_pdf(
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
        result = generate_csv(month_input)

    if result["error"]:
        return jsonify({"error": result["error"]}), 400

    return jsonify(result)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, port=5050)
