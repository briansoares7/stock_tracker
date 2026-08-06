#!/usr/bin/env python3
"""
fetch_deals.py
==============
Pulls publicly disclosed BULK DEALS, BLOCK DEALS, and SHORT-SELL data from
NSE (and, best-effort, BSE), normalizes them into one schema, and writes a
JSON file that the dashboard artifact can load.

IMPORTANT CONTEXT (read before relying on this)
-------------------------------------------------
1. There is NO public, real-time feed of any individual's full trading
   activity in India. What regulators require to be disclosed are:
     - Bulk deals  : single client trades >= 0.5% of a company's shares
                      in a day (disclosed same day, exchange-published)
     - Block deals : large pre-arranged trades in the separate block window
                      (disclosed same day)
     - Short sells  in high volume (disclosed same day)
     - Shareholding pattern changes >1%/5%/10% and SAST/PIT disclosures
       (disclosed within 2 trading days) -- NOT covered by this script,
       would need a separate filings-scraper.
   This script covers the first three, which are the closest thing to
   "real-time" public disclosure of big trades.

2. NSE actively changes anti-bot measures on nseindia.com. This script
   uses the same approach NSE's own website uses (visit homepage first to
   get session cookies, then call the JSON API with browser-like headers).
   If NSE changes something, this may need small header/endpoint tweaks --
   check the response status/text if you get empty results.

3. BSE's endpoint is included but marked best-effort: BSE's undocumented
   API occasionally changes parameter names. Test it before relying on it.

Usage
-----
    pip install requests --break-system-packages
    python fetch_deals.py --days 5 --out deals.json

Then schedule it (cron / Windows Task Scheduler) to refresh, e.g. every
15-30 minutes during market hours (9:15am - 3:30pm IST, Mon-Fri):

    */15 9-15 * * 1-5  cd /path/to/script && python fetch_deals.py --days 1 --out deals.json

To feed the dashboard, either:
  (a) open deals.json and paste its contents into the dashboard's
      "Paste JSON" box, or
  (b) push deals.json to a public GitHub Gist/repo and paste the raw URL
      into the dashboard's "Fetch from URL" box so it auto-refreshes.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta

import requests

NSE_HOME = "https://www.nseindia.com"
NSE_LARGEDEAL_API = "https://www.nseindia.com/api/snapshot-capital-market-largedeal"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/report-detail/display-bulk-and-block-deals",
}

# bandtype -> our category name
NSE_BANDS = {
    "bulk_deals": "bulk",
    "block_deals": "block",
    "short_deals": "short",
}


def get_nse_session() -> requests.Session:
    """NSE requires cookies from a normal page visit before the API works."""
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get(NSE_HOME, timeout=10)  # sets cookies
    time.sleep(1)
    return s


def _find_record_list(data):
    """NSE's response shape for this endpoint isn't stable/documented, so
    search for the actual list of deal-record dicts rather than assuming a
    fixed key name. Returns (list_of_dicts, path_used) or (None, None)."""
    if isinstance(data, list) and (not data or isinstance(data[0], dict)):
        return data, "<root>"
    if isinstance(data, dict):
        # common key names seen across NSE endpoints over time
        for key in ("data", "Data", "largeDeal", "largedeal", "bulk_deals",
                    "block_deals", "short_deals", "value"):
            v = data.get(key)
            if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                return v, key
        # fall back: scan all values for the first list-of-dicts
        for key, v in data.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v, key
    return None, None


def fetch_nse_band(session: requests.Session, bandtype: str) -> list:
    """Fetch one band (bulk_deals / block_deals / short_deals) for the current day."""
    params = {"bandtype": bandtype, "view": "mode"}
    resp = session.get(NSE_LARGEDEAL_API, params=params, timeout=10)
    if resp.status_code != 200:
        print(f"  [NSE] {bandtype}: HTTP {resp.status_code} - skipping", file=sys.stderr)
        return []
    try:
        data = resp.json()
    except ValueError:
        print(f"  [NSE] {bandtype}: non-JSON response - NSE may have changed the API", file=sys.stderr)
        print(f"  [NSE] {bandtype}: first 300 chars: {resp.text[:300]!r}", file=sys.stderr)
        return []

    records, used_key = _find_record_list(data)
    if records is None:
        shape = list(data.keys()) if isinstance(data, dict) else type(data).__name__
        print(f"  [NSE] {bandtype}: couldn't find a record list in the response. "
              f"Top-level shape: {shape}. Response snippet: {str(data)[:300]!r}", file=sys.stderr)
        return []
    if used_key != "<root>":
        print(f"  [NSE] {bandtype}: records found under key '{used_key}'", file=sys.stderr)
    return records


def normalize_nse_row(row: dict, category: str) -> dict:
    """Map NSE's raw field names onto our unified schema.
    NSE's field names have shifted over time; we try a few known variants.
    """
    if not isinstance(row, dict):
        # Defensive: shouldn't happen now that _find_record_list validates
        # shape, but never crash the whole run over one bad record.
        return {"date": "", "exchange": "NSE", "category": category, "symbol": "",
                "company": "", "client": "", "side": "", "quantity": 0, "price": 0, "value": 0}

    def pick(*keys, default=""):
        for k in keys:
            if k in row and row[k] not in (None, ""):
                return row[k]
        return default

    qty = pick("qty", "QTY", "TTL_TRD_QNTY", default=0)
    price = pick("watp", "WATP", "TRD_PR", default=0)
    try:
        qty = float(str(qty).replace(",", ""))
        price = float(str(price).replace(",", ""))
    except ValueError:
        qty, price = 0.0, 0.0

    side_raw = str(pick("buySell", "BUY_SELL", default="")).upper()
    side = "BUY" if "B" in side_raw[:1] else ("SELL" if "S" in side_raw[:1] else side_raw)

    return {
        "date": pick("date", "DT", "BD_DT_DATE", default=""),
        "exchange": "NSE",
        "category": category,
        "symbol": pick("symbol", "SYMBOL", default=""),
        "company": pick("name", "SEC_NAME", default=""),
        "client": pick("clientName", "CLIENT_NAME", default=""),
        "side": side,
        "quantity": qty,
        "price": price,
        "value": round(qty * price, 2),
    }


def fetch_bse_band(category: str, from_date: str, to_date: str) -> list:
    """Best-effort BSE fetch. BSE's API params are undocumented and may need
    adjustment -- verify results before trusting this in production."""
    url = f"https://api.bseindia.com/BseIndiaAPI/api/{'Block' if category == 'block' else 'Bulk'}DealData/w"
    params = {"category": category, "scripcode": "", "segment": "", "Fdate": from_date, "Todate": to_date}
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("Table", []) if isinstance(data, dict) else data
    except Exception as e:  # noqa: BLE001
        print(f"  [BSE] {category}: fetch failed ({e}) - skipping", file=sys.stderr)
        return []


def normalize_bse_row(row: dict, category: str) -> dict:
    if not isinstance(row, dict):
        return {"date": "", "exchange": "BSE", "category": category, "symbol": "",
                "company": "", "client": "", "side": "", "quantity": 0, "price": 0, "value": 0}

    def pick(*keys, default=""):
        for k in keys:
            if k in row and row[k] not in (None, ""):
                return row[k]
        return default

    qty = pick("TTL_QTY", "Quantity", default=0)
    price = pick("Price", "Rate", default=0)
    try:
        qty = float(str(qty).replace(",", ""))
        price = float(str(price).replace(",", ""))
    except ValueError:
        qty, price = 0.0, 0.0

    side_raw = str(pick("Deal_type", "BuySell", default="")).upper()
    side = "BUY" if side_raw.startswith("B") else ("SELL" if side_raw.startswith("S") else side_raw)

    return {
        "date": pick("Deal_date", "Date", default=""),
        "exchange": "BSE",
        "category": category,
        "symbol": pick("scrip_code", "ScripCode", default=""),
        "company": pick("ScripName", "Scrip_Name", default=""),
        "client": pick("ClientName", "Client_Name", default=""),
        "side": side,
        "quantity": qty,
        "price": price,
        "value": round(qty * price, 2),
    }


def main():
    ap = argparse.ArgumentParser(description="Fetch NSE/BSE bulk, block and short-deal disclosures")
    ap.add_argument("--days", type=int, default=1, help="how many days back to include in filenames/log (NSE endpoint returns current day snapshot)")
    ap.add_argument("--out", default="deals.json", help="output JSON path")
    ap.add_argument("--include-bse", action="store_true", help="also attempt BSE (best-effort, verify before trusting)")
    args = ap.parse_args()

    all_rows = []

    print("Connecting to NSE...", file=sys.stderr)
    try:
        session = get_nse_session()
        for bandtype, category in NSE_BANDS.items():
            print(f"  fetching NSE {bandtype}...", file=sys.stderr)
            raw = fetch_nse_band(session, bandtype)
            all_rows.extend(normalize_nse_row(r, category) for r in raw)
    except requests.RequestException as e:
        print(f"NSE fetch failed entirely: {e}", file=sys.stderr)

    if args.include_bse:
        today = datetime.now().strftime("%Y%m%d")
        from_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y%m%d")
        for category in ("bulk", "block"):
            print(f"  fetching BSE {category}...", file=sys.stderr)
            raw = fetch_bse_band(category, from_date, today)
            all_rows.extend(normalize_bse_row(r, category) for r in raw)

    # drop rows we couldn't parse meaningfully
    all_rows = [r for r in all_rows if r["symbol"] and r["quantity"] > 0]

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(all_rows),
        "deals": all_rows,
    }

    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {len(all_rows)} deals to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
