"""
square_sales_sync.py
Pulls last week's daily coffee sales from Square and upserts into Supabase.

Bar coffee (RFM/RFB/RFF):
  - Pulled from Orders API using reporting category IDs
  - Each line item's variation is mapped to its parent item's reporting_category_id
  - RFW 200g category = rfm, RFB 200g = rfb, RFF 200g = rff

Bar coffee (RFD - decaf):
  - Pulled from Orders API — counts line items that have a "Decaf" modifier

Retail coffee (1kg, 200g bags etc):
  - Pulled from Orders API by item name (direct sales)

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
DRY_RUN = True

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

# Name of the decaf modifier in Square
DECAF_MODIFIER_NAME = "Decaf"

# ── Reporting category ID -> sales_bar column ─────────────────────────────────
# These are the REGULAR_CATEGORY IDs from Square that map to bar coffee suppliers
BAR_CATEGORY_MAP = {
    "A2ZNWW447QACM3MC2WBSBZ76": "rfm",   # RFW 200g  (= RFM)
    "YJS2ONRSUIV2XLDHY55RUOOX": "rfb",   # RFB 200g
    "JAETG4OASNYOFKXZ4ODWS7NW": "rff",   # RFF 200g
}

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

# ── Retail SKU mappings — update left side to match exact Square item names ───
RETAIL_SKUS = {
    "RFM 1KG":  "rfm_1kg",
    "RFM 200G": "rfm_200g",
    "RFB 1KG":  "rfb_1kg",
    "RFB 200G": "rfb_200g",
    "RFF 1KG":  "rff_1kg",
    "RFF 200G": "rff_200g",
    "FT SCOOP": "ft_scoop",
}


# ── Square helpers ────────────────────────────────────────────────────────────

def get_locations() -> dict:
    """Returns {location_id: shop_id} for all mapped locations."""
    resp = requests.get(f"{SQUARE_BASE}/locations", headers=SQUARE_HEADERS)
    resp.raise_for_status()
    locations = {}
    for loc in resp.json().get("locations", []):
        name    = loc.get("name", "")
        loc_id  = loc["id"]
        shop_id = LOCATION_MAP.get(name)
        if shop_id:
            locations[loc_id] = shop_id
            print(f"  Mapped: {name} ({loc_id}) -> {shop_id}", flush=True)
        else:
            print(f"  Skipped unmapped location: {name}", flush=True)
    return locations


def build_variation_category_map() -> dict:
    """
    Scans all catalog items and returns:
    {variation_id: bar_column}
    for any variation whose parent item has a reporting category in BAR_CATEGORY_MAP.
    """
    cursor  = None
    var_map = {}

    while True:
        params = {"types": "ITEM"}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(
            f"{SQUARE_BASE}/catalog/list",
            headers=SQUARE_HEADERS,
            params=params,
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()

        for obj in data.get("objects", []):
            item_data = obj.get("item_data", {})
            rep_cat   = item_data.get("reporting_category", {})
            rep_id    = rep_cat.get("id", "")
            col       = BAR_CATEGORY_MAP.get(rep_id)
            if col:
                for var in item_data.get("variations", []):
                    var_map[var["id"]] = col

        cursor = data.get("cursor")
        if not cursor:
            break

    matched = len(var_map)
    print(f"  Built variation->category map: {matched} bar variations found", flush=True)
    if matched == 0:
        print("  WARNING: No bar variations found — check BAR_CATEGORY_MAP IDs", flush=True)
    return var_map


def get_orders(location_id: str, start_dt: datetime, end_dt: datetime) -> list:
    """Fetch all completed orders for a location within a date range."""
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


def get_bar_daily_sales(orders: list, var_map: dict) -> dict:
    """
    Aggregate bar coffee sales from orders using reporting category map.
    Returns {date_str: {column: qty}}
    """
    daily = defaultdict(lambda: defaultdict(float))

    for order in orders:
        closed_at = order.get("closed_at", "")
        if not closed_at:
            continue
        date_str = closed_at[:10]

        for item in order.get("line_items", []):
            cat_obj_id = item.get("catalog_object_id", "")
            col        = var_map.get(cat_obj_id)
            if col:
                qty = float(item.get("quantity", 0))
                daily[date_str][col] += qty

    return daily


def get_rfd_daily_sales(orders: list) -> dict:
    """
    Count line items with a Decaf modifier as RFD units.
    Returns {date_str: {"rfd": count}}
    """
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
    """
    Pull retail SKU quantities from completed order line items.
    Returns {date_str: {column: qty}}
    """
    daily = defaultdict(lambda: defaultdict(float))

    for order in orders:
        closed_at = order.get("closed_at", "")
        if not closed_at:
            continue
        date_str = closed_at[:10]

        for item in order.get("line_items", []):
            name = item.get("name", "").strip().upper()
            col  = RETAIL_SKUS.get(name)
            if col:
                daily[date_str][col] += float(item.get("quantity", 0))

    return daily


def merge_bar_daily(bar_daily: dict, rfd_daily: dict) -> dict:
    """Merge RFM/RFB/RFF from category map with RFD from decaf modifier."""
    all_dates = set(bar_daily.keys()) | set(rfd_daily.keys())
    merged    = {}
    for date_str in all_dates:
        merged[date_str] = {**bar_daily.get(date_str, {}),
                            **rfd_daily.get(date_str, {})}
    return merged


# ── Supabase upserts ──────────────────────────────────────────────────────────

def upsert_bar(shop_id: str, daily: dict):
    """Upsert daily bar sales rows into sales_bar."""
    all_bar_cols = set(BAR_CATEGORY_MAP.values()) | {"rfd"}
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
    """Upsert daily retail sales rows into sales_retail."""
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
    last_fri = last_mon + timedelta(days=4)

    start_dt = datetime(last_mon.year, last_mon.month, last_mon.day,
                        0, 0, 0, tzinfo=timezone.utc)
    end_dt   = datetime(last_fri.year, last_fri.month, last_fri.day,
                        23, 59, 59, tzinfo=timezone.utc)

    print(f"=== Square sales sync started {datetime.now(timezone.utc).isoformat()} ===", flush=True)
    print(f"  Mode: {'DRY RUN - no data will be written' if DRY_RUN else 'LIVE - writing to Supabase'}", flush=True)
    print(f"  Syncing: {last_mon} to {last_fri}", flush=True)

    try:
        locations = get_locations()
        if not locations:
            raise Exception("No matching Square locations found — check LOCATION_MAP names")

        # Build variation->bar column map once for all locations
        var_map = build_variation_category_map()

        for loc_id, shop_id in locations.items():
            print(f"\n  Processing {shop_id}...", flush=True)

            orders = get_orders(loc_id, start_dt, end_dt)
            print(f"    Found {len(orders)} completed orders", flush=True)

            # Bar: RFM/RFB/RFF from reporting categories + RFD from decaf modifier
            bar_daily = get_bar_daily_sales(orders, var_map)
            rfd_daily = get_rfd_daily_sales(orders)
            merged    = merge_bar_daily(bar_daily, rfd_daily)
            print(f"    Bar days found: {len(merged)}", flush=True)
            upsert_bar(shop_id, merged)

            # Retail: direct item sales
            retail_daily = get_retail_daily_sales(orders)
            print(f"    Retail days found: {len(retail_daily)}", flush=True)
            upsert_retail(shop_id, retail_daily)

        print(f"\n=== Complete {datetime.now(timezone.utc).isoformat()} ===", flush=True)

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
