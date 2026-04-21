"""
square_sales_sync.py
Pulls last week's daily coffee sales from Square and upserts into Supabase.

Bar coffee (RFM/RFB/RFF):
  - Pulled from Inventory API adjustments (supplier usage / SOLD state)

Bar coffee (RFD - decaf):
  - Pulled from Orders API — counts line items that have a "Decaf" modifier

Retail coffee (1kg, 200g bags etc):
  - Pulled from Orders API (direct item sales)

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

# ── SKU mappings — update left side to match exact Square item names ──────────

# Bar: matched via inventory adjustments (supplier usage items)
BAR_SKUS = {
    "USES RFM": "rfm",
    "USES RFB": "rfb",
    "USES RFF": "rff",
}

# Retail: matched via order line item names (direct sales)
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


def get_bar_catalog_ids() -> dict:
    """
    Returns {catalog_object_id: sales_bar_column} for BAR_SKUS.
    Looks up item variation IDs from the Square catalog.
    """
    cursor      = None
    catalog_map = {}
    while True:
        params = {"types": "ITEM_VARIATION"}
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
            name = obj.get("item_variation_data", {}).get("name", "").strip().upper()
            col  = BAR_SKUS.get(name)
            if col:
                catalog_map[obj["id"]] = col
        cursor = data.get("cursor")
        if not cursor:
            break

    print(f"  Found {len(catalog_map)}/{len(BAR_SKUS)} bar catalog item IDs", flush=True)
    return catalog_map


def get_bar_daily_sales(location_id: str, catalog_map: dict,
                         start_dt: datetime, end_dt: datetime) -> dict:
    """
    Pull SOLD inventory adjustments for RFM/RFB/RFF at a given location.
    Returns {date_str: {column: qty}}
    """
    if not catalog_map:
        return {}

    daily  = defaultdict(lambda: defaultdict(float))
    cursor = None

    while True:
        body = {
            "catalog_object_ids": list(catalog_map.keys()),
            "location_ids":       [location_id],
            "types":              ["ADJUSTMENT"],
            "states":             ["SOLD"],
            "updated_after":      start_dt.isoformat(),
            "updated_before":     end_dt.isoformat(),
        }
        if cursor:
            body["cursor"] = cursor

        resp = requests.post(
            f"{SQUARE_BASE}/inventory/changes/batch-retrieve",
            headers=SQUARE_HEADERS,
            json=body,
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()

        for change in data.get("changes", []):
            adj         = change.get("adjustment", {})
            occurred_at = adj.get("occurred_at", "")
            if not occurred_at:
                continue
            date_str   = occurred_at[:10]
            catalog_id = adj.get("catalog_object_id")
            col        = catalog_map.get(catalog_id)
            quantity   = float(adj.get("quantity", 0))
            if col and quantity > 0:
                daily[date_str][col] += quantity

        cursor = data.get("cursor")
        if not cursor:
            break

    return daily


def get_orders(location_id: str, start_dt: datetime, end_dt: datetime) -> list:
    """
    Fetch all completed orders for a location within a date range.
    Handles pagination automatically.
    """
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


def get_rfd_daily_sales(orders: list) -> dict:
    """
    Count line items that have a Decaf modifier as RFD units.
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
                qty = float(item.get("quantity", 1))
                daily[date_str]["rfd"] += qty

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


def merge_bar_daily(inv_daily: dict, rfd_daily: dict) -> dict:
    """Merge RFM/RFB/RFF from inventory with RFD from orders."""
    all_dates = set(inv_daily.keys()) | set(rfd_daily.keys())
    merged    = {}
    for date_str in all_dates:
        merged[date_str] = {**inv_daily.get(date_str, {}),
                            **rfd_daily.get(date_str, {})}
    return merged


# ── Supabase upserts ──────────────────────────────────────────────────────────

def upsert_bar(shop_id: str, daily: dict):
    """Upsert daily bar sales rows into sales_bar."""
    all_bar_cols = set(BAR_SKUS.values()) | {"rfd"}
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
        locations   = get_locations()
        catalog_map = get_bar_catalog_ids()

        if not locations:
            raise Exception("No matching Square locations found — check LOCATION_MAP names")

        for loc_id, shop_id in locations.items():
            print(f"\n  Processing {shop_id}...", flush=True)

            # Fetch all orders once — used for both RFD and retail
            orders = get_orders(loc_id, start_dt, end_dt)
            print(f"    Found {len(orders)} completed orders", flush=True)

            # Bar: RFM/RFB/RFF from inventory + RFD from decaf modifier
            inv_daily = get_bar_daily_sales(loc_id, catalog_map, start_dt, end_dt)
            rfd_daily = get_rfd_daily_sales(orders)
            bar_daily = merge_bar_daily(inv_daily, rfd_daily)
            print(f"    Bar days found: {len(bar_daily)}", flush=True)
            upsert_bar(shop_id, bar_daily)

            # Retail: direct item sales from orders
            retail_daily = get_retail_daily_sales(orders)
            print(f"    Retail days found: {len(retail_daily)}", flush=True)
            upsert_retail(shop_id, retail_daily)

        print(f"\n=== Complete {datetime.now(timezone.utc).isoformat()} ===", flush=True)

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
