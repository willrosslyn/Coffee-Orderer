"""
square_sales_sync.py
Pulls last week's daily coffee sales from Square and upserts into Supabase.

Bar coffee (RFM/RFB/RFF):
  - Matched by item name from orders

Bar coffee (RFD - decaf):
  - Counted from orders where line item has a "Decaf" modifier

Retail coffee (1kg, 200g bags etc):
  - Matched by item name from orders (direct sales)

Runs every Monday via GitHub Actions — syncs Mon-Fri of the previous week.

Required environment variables:
  SQUARE_ACCESS_TOKEN  - Square Production Access Token
  SUPABASE_URL         - Supabase project URL
  SUPABASE_SERVICE_KEY - Supabase service role key
"""

import os
import sys
import requests
from datetime import datetime, timedelta, timezone
from collections import defaultdict

SQUARE_ACCESS_TOKEN  = os.environ["SQUARE_ACCESS_TOKEN"]
SUPABASE_URL         = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

# ── Set to False when you are happy the data looks correct ────────────────────
DRY_RUN = False

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

# ── Location mapping ──────────────────────────────────────────────────────────
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

# ── Drink name -> bar coffee column ───────────────────────────────────────────
# Matched on item name (case-insensitive). Variation name is ignored.
DRINK_TO_BAR = {
    # RFB
    "americano":        "rfb",
    "£1 americano":     "rfb",
    # RFF
    "batch brew":       "rff",
    "£1 batch brew":    "rff",
    # RFM
    "cappuccino":       "rfm",
    "£1 cappuccino":    "rfm",
    "cortado":          "rfm",
    "£1 cortado":       "rfm",
    "flat white":       "rfm",
    "£1 flat white":    "rfm",
    "*flat white":      "rfm",
    "iced flat white":  "rfm",
    "£1 iced flat white": "rfm",
    "iced latte":       "rfm",
    "£1 iced latte":    "rfm",
    "iced mocha":       "rfm",
    "£1 iced mocha":    "rfm",
    "latte":            "rfm",
    "£1 latte":         "rfm",
    "*latte":           "rfm",
    "macchiato":        "rfm",
    "£1 macchiato":     "rfm",
    "mocha":            "rfm",
    "£1 mocha":         "rfm",
    "piccolo":          "rfm",
    "£1 piccolo":       "rfm",
    "flat white (take away)": "rfm",
    "cappuccino (take away)": "rfm",
    "latte (take away)":      "rfm",
}

# ── Retail SKU mappings ───────────────────────────────────────────────────────
# Update left side to match exact Square item names
RETAIL_SKUS = {
    "Roasted for Milk 1kg":  "rfm_1kg",
    "Roasted for Milk 200g": "rfm_200g",
    "Roasted for Black 1kg":  "rfb_1kg",
    "Roasted for Black 200g": "rfb_200g",
    "Roasted for Filter 1kg":  "rff_1kg",
    "Roasted for Filter 200g": "rff_200g",
    "FT Scoop": "ft_scoop",
}


# ── Square helpers ────────────────────────────────────────────────────────────

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
    orders = []
    cursor = None
    while True:
        body = {
            "location_ids": [location_id],
            "query": {
                "filter": {
                    "state_filter": {"states": ["COMPLETED"]},
                    "date_time_filter": {
                        "closed_at": {
                            "start_at": start_dt.isoformat(),
                            "end_at":   end_dt.isoformat(),
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
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        orders.extend(data.get("orders", []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return orders


def get_bar_daily_sales(orders: list) -> dict:
    """Aggregate RFM/RFB/RFF from drink name mapping."""
    daily = defaultdict(lambda: defaultdict(float))
    unmatched = set()

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
            else:
                # Track unmatched names so we can spot gaps
                unmatched.add(item.get("name", "").strip())

    if unmatched and DRY_RUN:
        # Only show non-retail unmatched items
        retail_names = {k.lower() for k in RETAIL_SKUS.keys()}
        unknown = {n for n in unmatched
                   if n.lower() not in retail_names
                   and n.strip() != ""}
        if unknown:
            print(f"    Unmatched item names (not in DRINK_TO_BAR or RETAIL_SKUS):", flush=True)
            for n in sorted(unknown)[:30]:
                print(f"      '{n}'", flush=True)

    return daily


def get_rfd_daily_sales(orders: list) -> dict:
    """Count line items with Decaf modifier as RFD."""
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
            # Square sometimes returns variation name in 'name' field
            # Check both name and item name fields
            name     = item.get("name", "").strip()
            col      = RETAIL_SKUS.get(name)
            if not col:
                # Try the catalog_item_name field if present
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


# ── Supabase upserts ──────────────────────────────────────────────────────────

def upsert_bar(shop_id: str, daily: dict):
    all_bar_cols = {"rfm", "rfb", "rff", "rfd"}
    records      = []
    for date_str, cols in sorted(daily.items()):
        row = {c: round(cols.get(c, 0), 2) for c in all_bar_cols}
        if any(v > 0 for v in row.values()):
            records.append({"shop_id": shop_id, "week": date_str, **row})

    if not records:
        print(f"    No bar sales to upsert for {shop_id}", flush=True)
        return

    if DRY_RUN:
        print(f"    DRY RUN - would upsert {len(records)} bar rows for {shop_id}:", flush=True)
        for rec in records:
            print(f"      {rec}", flush=True)
        return

    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/sales_bar?on_conflict=shop_id,week",
        headers={**SUPABASE_HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=records
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
        print(f"    No retail sales to upsert for {shop_id}", flush=True)
        return

    if DRY_RUN:
        print(f"    DRY RUN - would upsert {len(records)} retail rows for {shop_id}:", flush=True)
        for rec in records:
            print(f"      {rec}", flush=True)
        return

    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/sales_retail?on_conflict=shop_id,week",
        headers={**SUPABASE_HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=records
    )
    if r.status_code not in (200, 201):
        raise Exception(f"sales_retail upsert failed for {shop_id}: {r.status_code} {r.text}")
    print(f"    Upserted {len(records)} retail rows for {shop_id}", flush=True)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today    = datetime.now(timezone.utc).date()
    last_mon = today - timedelta(days=today.weekday() + 7)
    last_sat = last_mon + timedelta(days=5)

    start_dt = datetime(last_mon.year, last_mon.month, last_mon.day,
                        0, 0, 0, tzinfo=timezone.utc)
    end_dt   = datetime(last_sat.year, last_sat.month, last_sat.day,
                        23, 59, 59, tzinfo=timezone.utc)

    print(f"=== Square sales sync started {datetime.now(timezone.utc).isoformat()} ===", flush=True)
    print(f"  Mode: {'DRY RUN - no data will be written' if DRY_RUN else 'LIVE - writing to Supabase'}", flush=True)
    print(f"  Syncing: {last_mon} to {last_sat}", flush=True)

    try:
        locations = get_locations()
        if not locations:
            raise Exception("No matching Square locations found — check LOCATION_MAP names")

        for loc_id, shop_id in locations.items():
            print(f"\n  Processing {shop_id}...", flush=True)

            orders = get_orders(loc_id, start_dt, end_dt)
            print(f"    Found {len(orders)} completed orders", flush=True)

            bar_daily = get_bar_daily_sales(orders)
            rfd_daily = get_rfd_daily_sales(orders)
            merged    = merge_bar_daily(bar_daily, rfd_daily)
            print(f"    Bar days found: {len(merged)}", flush=True)
            upsert_bar(shop_id, merged)

            retail_daily = get_retail_daily_sales(orders)
            print(f"    Retail days found: {len(retail_daily)}", flush=True)
            upsert_retail(shop_id, retail_daily)

        print(f"\n=== Complete {datetime.now(timezone.utc).isoformat()} ===", flush=True)

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
