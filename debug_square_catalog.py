"""
debug_square_catalog.py
Lists all suppliers (vendors) and their linked catalog items from Square.
Run this to find the exact supplier names and item IDs for RFM/RFB/RFF.
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


def get_vendors():
    """List all vendors/suppliers."""
    resp = requests.post(
        f"{SQUARE_BASE}/vendors/search",
        headers=SQUARE_HEADERS,
        json={},
        timeout=30
    )
    resp.raise_for_status()
    vendors = resp.json().get("vendors", [])
    print(f"\n=== VENDORS ({len(vendors)}) ===")
    for v in vendors:
        print(f"  ID: {v['id']} | Name: '{v.get('name', '')}' | Status: {v.get('status', '')}")
    return vendors


def get_catalog_items_with_vendor():
    """List catalog items that have vendor/supplier info."""
    cursor = None
    items_with_vendor = []

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
            name = item_data.get("name", "")
            # Check for vendor info in item variations
            for variation in item_data.get("variations", []):
                var_data  = variation.get("item_variation_data", {})
                vendor_infos = var_data.get("item_variation_vendor_infos", [])
                sku = var_data.get("sku", "")
                if vendor_infos or sku:
                    items_with_vendor.append({
                        "item_name":    name,
                        "var_name":     var_data.get("name", ""),
                        "var_id":       variation["id"],
                        "sku":          sku,
                        "vendor_infos": vendor_infos,
                    })

        cursor = data.get("cursor")
        if not cursor:
            break

    print(f"\n=== CATALOG ITEMS WITH VENDOR/SKU INFO ({len(items_with_vendor)}) ===")
    for item in items_with_vendor:
        vendor_ids = [v.get("item_variation_vendor_info_data", {}).get("vendor_id", "")
                      for v in item["vendor_infos"]]
        vendor_skus = [v.get("item_variation_vendor_info_data", {}).get("sku", "")
                       for v in item["vendor_infos"]]
        print(f"  Item: '{item['item_name']}' | Variation: '{item['var_name']}' "
              f"| SKU: '{item['sku']}' | Vendor IDs: {vendor_ids} | Vendor SKUs: {vendor_skus}")
        print(f"    Variation ID: {item['var_id']}")

    return items_with_vendor


def main():
    print("=== Square Vendor/Supplier Debug ===", flush=True)
    get_vendors()
    get_catalog_items_with_vendor()


if __name__ == "__main__":
    main()
