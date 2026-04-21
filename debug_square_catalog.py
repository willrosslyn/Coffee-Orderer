"""
debug_square_catalog.py
Lists all item variation names from your Square catalog.
Run this once to find the exact names of your bar coffee supplier items.
"""

import os
import requests

SQUARE_ACCESS_TOKEN = os.environ["SQUARE_ACCESS_TOKEN"]

SQUARE_BASE = "https://connect.squareup.com/v2"
SQUARE_HEADERS = {
    "Authorization":  f"Bearer {SQUARE_ACCESS_TOKEN}",
    "Content-Type":   "application/json",
    "Square-Version": "2024-01-17",
}

def main():
    cursor = None
    all_items = []

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
            name = obj.get("item_variation_data", {}).get("name", "")
            sku  = obj.get("item_variation_data", {}).get("sku", "")
            all_items.append((name, sku, obj["id"]))
        cursor = data.get("cursor")
        if not cursor:
            break

    print(f"Found {len(all_items)} item variations:\n")
    # Print anything that looks like it could be coffee related
    keywords = ["rfm", "rfb", "rff", "rfd", "uses", "decaf", "coffee",
                "espresso", "blend", "filter", "retail", "1kg", "200g", "scoop"]
    print("=== POSSIBLE COFFEE ITEMS ===")
    for name, sku, obj_id in sorted(all_items):
        if any(k in name.lower() for k in keywords):
            print(f"  Name: '{name}' | SKU: '{sku}' | ID: {obj_id}")

    print("\n=== ALL ITEM VARIATIONS ===")
    for name, sku, obj_id in sorted(all_items):
        print(f"  Name: '{name}' | SKU: '{sku}'")

if __name__ == "__main__":
    main()
