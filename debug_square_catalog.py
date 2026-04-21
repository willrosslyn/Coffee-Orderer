"""
debug_square_catalog.py
Maps all item variations to their SKUs to understand supplier grouping.
"""

import os
import requests
from datetime import datetime, timedelta, timezone

SQUARE_ACCESS_TOKEN = os.environ["SQUARE_ACCESS_TOKEN"]

SQUARE_BASE = "https://connect.squareup.com/v2"
SQUARE_HEADERS = {
    "Authorization":  f"Bearer {SQUARE_ACCESS_TOKEN}",
    "Content-Type":   "application/json",
    "Square-Version": "2024-01-17",
}


def get_locations():
    resp = requests.get(f"{SQUARE_BASE}/locations", headers=SQUARE_HEADERS)
    resp.raise_for_status()
    locations = []
    for loc in resp.json().get("locations", []):
        locations.append((loc["id"], loc.get("name", "")))
    return locations


def get_all_variation_skus():
    """Build a map of {variation_id: sku} for all catalog items."""
    cursor   = None
    sku_map  = {}
    all_vars = []

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
            var_data = obj.get("item_variation_data", {})
            name     = var_data.get("name", "")
            sku      = var_data.get("sku", "")
            obj_id   = obj["id"]
            sku_map[obj_id] = sku
            all_vars.append((name, sku, obj_id))

        cursor = data.get("cursor")
        if not cursor:
            break

    # Print variations grouped by SKU
    from collections import defaultdict
    by_sku = defaultdict(list)
    for name, sku, obj_id in all_vars:
        by_sku[sku].append(name)

    print(f"\n=== VARIATIONS BY SKU ===")
    for sku in sorted(by_sku.keys()):
        if sku:  # only show ones with a SKU set
            names = sorted(set(by_sku[sku]))
            print(f"  SKU '{sku}': {names}")

    print(f"\n=== ALL SKUS FOUND: {sorted(s for s in by_sku.keys() if s)} ===")
    return sku_map


def sample_orders(location_id, location_name, sku_map):
    """Pull a sample of recent orders and show what SKUs appear."""
    today    = datetime.now(timezone.utc)
    week_ago = today - timedelta(days=7)

    resp = requests.post(
        f"{SQUARE_BASE}/orders/search",
        headers=SQUARE_HEADERS,
        json={
            "location_ids": [location_id],
            "query": {
                "filter": {
                    "state_filter": {"states": ["COMPLETED"]},
                    "date_time_filter": {
                        "closed_at": {
                            "start_at": week_ago.isoformat(),
                            "end_at":   today.isoformat(),
                        }
                    }
                }
            },
            "limit": 10,
        },
        timeout=30
    )
    resp.raise_for_status()
    orders = resp.json().get("orders", [])
    print(f"\n=== SAMPLE ORDERS for {location_name} (last 7 days, first 10) ===")
    print(f"  Total returned: {len(orders)}")

    from collections import defaultdict
    sku_totals = defaultdict(float)

    for order in orders:
        for item in order.get("line_items", []):
            name       = item.get("name", "")
            qty        = float(item.get("quantity", 0))
            cat_obj_id = item.get("catalog_object_id", "")
            sku        = sku_map.get(cat_obj_id, "NO_SKU")
            modifiers  = [m.get("name","") for m in item.get("modifiers", [])]
            print(f"    Item: '{name}' qty:{qty} SKU:'{sku}' mods:{modifiers}")
            if sku and sku != "NO_SKU":
                sku_totals[sku] += qty

    print(f"\n  SKU totals from sample orders: {dict(sku_totals)}")


def main():
    print("=== Square SKU/Supplier Debug ===\n", flush=True)
    locations = get_locations()
    print(f"Locations: {[(n, i) for i,n in locations]}")

    sku_map = get_all_variation_skus()

    if locations:
        # Use first location as sample
        sample_orders(locations[0][0], locations[0][1], sku_map)


if __name__ == "__main__":
    main()
