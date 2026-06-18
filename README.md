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

## Running Tests

```bash
python3 -m pytest test_visa_bulletin.py -v
```
