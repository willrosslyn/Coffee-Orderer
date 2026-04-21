"""
square_historical_sync.py
Pulls historical Square sales from a given start date through yesterday
and upserts into Supabase sales_bar and sales_retail.

Run manually via GitHub Actions workflow_dispatch with inputs:
  start_date: e.g. "2026-04-14"
  end_date:   e.g. "2026-04-21" (optional, defaults to yesterday)

Required environment variables:
  SQUARE_ACCESS_TOKEN  - Square Production Access Token
  SUPABASE_URL         - Supabase project URL
  SUPABASE_SERVICE_KEY - Supabase service role key
  START_DATE           - e.g. "2026-04-14"
  END_DATE             - e.g. "2026-04-21" (optional)
"""

import os
import sys
import requests
from datetime import datetime, timedelta, timezone, date
from collections import defaultdict

SQUARE_ACCESS_TOKEN  = os.environ["SQUARE_ACCESS_TOKEN"]
SUPABASE_URL         = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
START_DATE           = os.environ["START_DATE"]
END_DATE             = os.environ.get("END_DATE", "")

SQUARE_BASE = "https://connect.squareup.com/v2"
SQUARE_HEADERS = {
    "Authorization":  f"Bearer {SQUARE_ACCESS_TOKEN}",
    "Content-Type":   "application/json",
    "Square-Version": "2024-01-17",
}

SUPABASE_HEADERS = {
    "apikey":        SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type":  "application/json",
}

DECAF_MODIFIER_NAME = "Decaf"

LOCATION_MAP = {
    "London Wall":              "LDW",
    "Queen Victoria":           "QVS",
    "Cannon Street":            "CAS",
    "Tower 42":                 "T42",
    "Royal Exchange":           "TRE",
    "Leadenhall Market":        "LEM",
    "Ludgate Circus":           "LUC",
    "Fenchurch Street":         "FSS",
    "Liverpool Street Station": "LSS",
}

DRINK_TO_BAR = {
    "americano":            "rfb",
    "£1 americano":         "rfb",
    "batch brew":           "rff",
    "£1 batch brew":        "rff",
    "cappuccino":           "rfm",
    "£1 cappuccino":        "rfm",
    "cortado":              "rfm",
    "£1 cortado":           "rfm",
    "flat white":           "rfm",
    "£1 flat white":        "rfm",
    "*flat white":          "rfm",
    "iced flat white":      "rfm",
    "£1 iced flat white":   "rfm",
    "iced latte":           "rfm",
    "£1 iced latte":        "rfm",
    "iced mocha":           "rfm",
    "£1 iced mocha":        "rfm",
    "latte":                "rfm",
    "£1 latte":             "rfm",
    "*latte":               "rfm",
    "macchiato":            "rfm",
    "£1 macchiato":         "rfm",
    "mocha":                "rfm",
    "£1 mocha":             "rfm",
    "piccolo":              "rfm",
    "£1 piccolo":           "rfm",
    "flat white (take away)":    "rfm",
    "cappuccino (take away)":    "rfm",
    "latte (take away)":         "rfm",
}

RETAIL_SKUS = {
    "Roasted for Milk 1kg":   "rfm_1kg",
    "Roasted for Milk 200g":  "rfm_200g",
    "Roasted for Black 1kg":  "rfb_1kg",
    "Roasted for Black 200g": "rfb_200g",
    "Roasted for Filter 1kg": "rff_1kg",
    "Roasted for Filter 200g": "rff_200g",
    "FT Scoop":               "ft_scoop",
}


def get_locations() -> dict:
    resp = requests.get(f"{SQUARE_BASE}/locations", headers=SQUARE_HEADERS)
    resp.raise_for_status()
    locations = {}
    for loc in resp.json().get("locations", []):
        name    = loc.get("name", "")
        loc_id  = loc["id"]
        shop_id = LOCATION_MAP.get(name)
        if shop_id:
            locations[loc_id] = shop_id
            print(f"  Mapped: {name} -> {shop_id}", flush=True)
        else:
            print(f"  Skipped: {name}", flush=True)
    return locations


def get_orders(location_id: str, start_dt: datetime, end_dt: datetime) -> list:
    """Fetch all completed orders in chunks to avoid timeouts on large date ranges."""
    all_orders = []
    # Process week by week to avoid huge responses
    chunk_start = start_dt
    while chunk_start < end_dt:
        chunk_end = min(chunk_start + timedelta(days=7), end_dt)
        cursor    = None
        while True:
            body = {
                "location_ids": [location_id],
                "query": {
                    "filter": {
                        "state_filter": {"states": ["COMPLETED"]},
                        "date_time_filter": {
                            "closed_at": {
                                "start_at": chunk_start.isoformat(),
                                "end_at":   chunk_end.isoformat(),
                            }
                        }
                    },
                    "sort": {"sort_field": "CLOSED_AT", "sort_order": "ASC"}
                },
                "limit": 500,
            }
            if cursor:
                body["cursor"] = cursor
            resp = requests.post(
                f"{SQUARE_BASE}/orders/search",
                headers=SQUARE_HEADERS,
                json=body,
                timeout=60
            )
            resp.raise_for_status()
            data = resp.json()
            all_orders.extend(data.get("orders", []))
            cursor = data.get("cursor")
            if not cursor:
                break
        chunk_start = chunk_end

    return all_orders


def get_bar_daily_sales(orders: list) -> dict:
    daily = defaultdict(lambda: defaultdict(float))
    for order in orders:
        closed_at = order.get("closed_at", "")
        if not closed_at:
            continue
        date_str = closed_at[:10]
        for item in order.get("line_items", []):
            name = item.get("name", "").strip().lower()
            col  = DRINK_TO_BAR.get(name)
            if col:
                daily[date_str][col] += float(item.get("quantity", 0))
    return daily


def get_rfd_daily_sales(orders: list) -> dict:
    daily = defaultdict(lambda: defaultdict(float))
    for order in orders:
        closed_at = order.get("closed_at", "")
        if not closed_at:
            continue
        date_str = closed_at[:10]
        for item in order.get("line_items", []):
            modifiers = item.get("modifiers", [])
            is_decaf  = any(
                m.get("name", "").strip().lower() == DECAF_MODIFIER_NAME.lower()
                for m in modifiers
            )
            if is_decaf:
                daily[date_str]["rfd"] += float(item.get("quantity", 1))
    return daily


def get_retail_daily_sales(orders: list) -> dict:
    daily = defaultdict(lambda: defaultdict(float))
    for order in orders:
        closed_at = order.get("closed_at", "")
        if not closed_at:
            continue
        date_str = closed_at[:10]
        for item in order.get("line_items", []):
            name = item.get("name", "").strip()
            col  = RETAIL_SKUS.get(name)
            if not col:
                catalog_name = item.get("catalog_item_name", "").strip()
                col = RETAIL_SKUS.get(catalog_name)
            if col:
                daily[date_str][col] += float(item.get("quantity", 0))
    return daily


def merge_bar_daily(bar_daily: dict, rfd_daily: dict) -> dict:
    all_dates = set(bar_daily.keys()) | set(rfd_daily.keys())
    merged    = {}
    for date_str in all_dates:
        merged[date_str] = {**bar_daily.get(date_str, {}),
                            **rfd_daily.get(date_str, {})}
    return merged


def upsert_bar(shop_id: str, daily: dict):
    all_bar_cols = {"rfm", "rfb", "rff", "rfd"}
    records      = []
    for date_str, cols in sorted(daily.items()):
        row = {c: round(cols.get(c, 0), 2) for c in all_bar_cols}
        if any(v > 0 for v in row.values()):
            records.append({"shop_id": shop_id, "week": date_str, **row})

    if not records:
        print(f"    No bar sales for {shop_id}", flush=True)
        return

    # Upsert in batches of 200
    for i in range(0, len(records), 200):
        batch = records[i:i+200]
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/sales_bar?on_conflict=shop_id,week",
            headers={**SUPABASE_HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"},
            json=batch
        )
        if r.status_code not in (200, 201):
            raise Exception(f"sales_bar upsert failed for {shop_id}: {r.status_code} {r.text}")
    print(f"    Upserted {len(records)} bar rows for {shop_id}", flush=True)


def upsert_retail(shop_id: str, daily: dict):
    retail_cols = set(RETAIL_SKUS.values())
    records     = []
    for date_str, cols in sorted(daily.items()):
        row = {c: round(cols.get(c, 0), 2) for c in retail_cols}
        if any(v > 0 for v in row.values()):
            records.append({"shop_id": shop_id, "week": date_str, **row})

    if not records:
        print(f"    No retail sales for {shop_id}", flush=True)
        return

    for i in range(0, len(records), 200):
        batch = records[i:i+200]
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/sales_retail?on_conflict=shop_id,week",
            headers={**SUPABASE_HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"},
            json=batch
        )
        if r.status_code not in (200, 201):
            raise Exception(f"sales_retail upsert failed for {shop_id}: {r.status_code} {r.text}")
    print(f"    Upserted {len(records)} retail rows for {shop_id}", flush=True)


def main():
    # Parse dates
    start_dt = datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if END_DATE:
        end_dt = datetime.strptime(END_DATE, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc)
    else:
        yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
        end_dt    = datetime(yesterday.year, yesterday.month, yesterday.day,
                             23, 59, 59, tzinfo=timezone.utc)

    print(f"=== Square historical sync started {datetime.now(timezone.utc).isoformat()} ===", flush=True)
    print(f"  Syncing: {start_dt.date()} to {end_dt.date()}", flush=True)

    try:
        locations = get_locations()
        if not locations:
            raise Exception("No matching Square locations found")

        for loc_id, shop_id in locations.items():
            print(f"\n  Processing {shop_id}...", flush=True)
            orders = get_orders(loc_id, start_dt, end_dt)
            print(f"    Found {len(orders)} completed orders", flush=True)

            bar_daily = get_bar_daily_sales(orders)
            rfd_daily = get_rfd_daily_sales(orders)
            merged    = merge_bar_daily(bar_daily, rfd_daily)
            upsert_bar(shop_id, merged)

            retail_daily = get_retail_daily_sales(orders)
            upsert_retail(shop_id, retail_daily)

        print(f"\n=== Complete {datetime.now(timezone.utc).isoformat()} ===", flush=True)

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
