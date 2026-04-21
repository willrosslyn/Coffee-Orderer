"""
debug_square_catalog.py
Debugs Square setup to find how supplier/coffee usage is tracked.
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


def get_vendors():
    """List all vendors/suppliers."""
    resp = requests.post(
        f"{SQUARE_BASE}/vendors/search",
        headers=SQUARE_HEADERS,
        json={"filter": {}, "sort": {"field": "NAME", "order": "ASC"}},
        timeout=30
    )
    print(f"Vendors API status: {resp.status_code}", flush=True)
    if resp.status_code == 200:
        vendors = resp.json().get("vendors", [])
        print(f"\n=== VENDORS ({len(vendors)}) ===")
        for v in vendors:
            print(f"  ID: {v['id']} | Name: '{v.get('name', '')}' | Status: {v.get('status', '')}")
        return vendors
    else:
        print(f"  Vendors API error: {resp.text[:200]}")
        return []


def get_locations():
    resp = requests.get(f"{SQUARE_BASE}/locations", headers=SQUARE_HEADERS)
    resp.raise_for_status()
    locations = []
    for loc in resp.json().get("locations", []):
        locations.append((loc["id"], loc.get("name", "")))
        print(f"  Location: '{loc.get('name','')}' ID: {loc['id']}")
    return locations


def check_inventory_adjustments(location_id, location_name):
    """Check what inventory adjustment types exist for this location last week."""
    today    = datetime.now(timezone.utc)
    week_ago = today - timedelta(days=7)

    resp = requests.post(
        f"{SQUARE_BASE}/inventory/changes/batch-retrieve",
        headers=SQUARE_HEADERS,
        json={
            "location_ids":  [location_id],
            "types":         ["ADJUSTMENT"],
            "updated_after": week_ago.isoformat(),
            "updated_before": today.isoformat(),
        },
        timeout=30
    )
    print(f"\n  Inventory adjustments for {location_name}: HTTP {resp.status_code}")
    if resp.status_code == 200:
        changes = resp.json().get("changes", [])
        print(f"  Total adjustments: {len(changes)}")
        # Show unique states and a sample
        states = set()
        samples = []
        for c in changes[:5]:
            adj = c.get("adjustment", {})
            states.add(adj.get("to_state", ""))
            samples.append({
                "to_state":   adj.get("to_state"),
                "from_state": adj.get("from_state"),
                "quantity":   adj.get("quantity"),
                "catalog_id": adj.get("catalog_object_id"),
                "occurred":   adj.get("occurred_at", "")[:10],
            })
        print(f"  States seen: {states}")
        print(f"  Sample adjustments:")
        for s in samples:
            print(f"    {s}")
    else:
        print(f"  Error: {resp.text[:200]}")


def check_catalog_with_reporting():
    """List items including reporting category which may show supplier grouping."""
    cursor = None
    print(f"\n=== CATALOG ITEMS (checking for supplier/component links) ===")
    count = 0
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
            name      = item_data.get("name", "")
            # Print everything that might relate to coffee/supplier
            keywords = ["rfm","rfb","rff","rfd","uses","supplier","coffee",
                        "espresso","blend","filter","decaf","flat","latte",
                        "cappuccino","cortado","drip","batch"]
            if any(k in name.lower() for k in keywords):
                count += 1
                cats = [c.get("name","") for c in item_data.get("categories", [])]
                print(f"  '{name}' | Categories: {cats}")
                for var in item_data.get("variations", []):
                    vd  = var.get("item_variation_data", {})
                    sku = vd.get("sku", "")
                    vname = vd.get("name", "")
                    vendor_infos = vd.get("item_variation_vendor_infos", [])
                    if sku or vendor_infos:
                        print(f"    Variation: '{vname}' SKU:'{sku}' Vendors:{vendor_infos}")

        cursor = data.get("cursor")
        if not cursor:
            break
    print(f"  Total coffee-related items: {count}")


def main():
    print("=== Square Supplier Debug ===\n", flush=True)

    print("--- Locations ---")
    locations = get_locations()

    print("\n--- Vendors ---")
    get_vendors()

    print("\n--- Inventory Adjustments (first location) ---")
    if locations:
        check_inventory_adjustments(locations[0][0], locations[0][1])

    check_catalog_with_reporting()


if __name__ == "__main__":
    main()
