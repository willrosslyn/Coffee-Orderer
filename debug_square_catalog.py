"""
debug_square_catalog.py
Finds custom attributes on catalog items to locate supplier tags.
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


def get_custom_attribute_definitions():
    """List all custom attribute definitions for catalog objects."""
    print("\n=== CUSTOM ATTRIBUTE DEFINITIONS ===")
    cursor = None
    while True:
        params = {"resource_type": "ITEM"}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(
            f"{SQUARE_BASE}/catalog/custom-attribute-definitions",
            headers=SQUARE_HEADERS,
            params=params,
            timeout=30
        )
        print(f"  Status: {resp.status_code}")
        if resp.status_code != 200:
            print(f"  Error: {resp.text[:300]}")
            break
        data = resp.json()
        defs = data.get("custom_attribute_definitions", [])
        for d in defs:
            print(f"  Key: '{d.get('key')}' | Name: '{d.get('name')}' | Type: '{d.get('type')}' | ID: {d.get('id')}")
        cursor = data.get("cursor")
        if not cursor:
            break


def get_item_with_custom_attrs(item_name_search="Flat White"):
    """Fetch a specific item and show all its custom attributes."""
    print(f"\n=== SEARCHING FOR ITEM: '{item_name_search}' ===")
    resp = requests.post(
        f"{SQUARE_BASE}/catalog/search",
        headers=SQUARE_HEADERS,
        json={
            "text_filter": {"keyword": item_name_search},
            "object_types": ["ITEM"],
            "include_related_objects": True,
        },
        timeout=30
    )
    resp.raise_for_status()
    data = resp.json()
    items = data.get("objects", [])
    print(f"  Found {len(items)} items")
    for obj in items[:3]:
        item_data = obj.get("item_data", {})
        name      = item_data.get("name", "")
        custom    = obj.get("custom_attribute_values", {})
        cats      = [c.get("name","") for c in item_data.get("categories", [])]
        reporting = item_data.get("reporting_category", {})
        print(f"\n  Item: '{name}'")
        print(f"    Categories: {cats}")
        print(f"    Reporting category: {reporting}")
        print(f"    Custom attributes: {custom}")
        for var in item_data.get("variations", [])[:2]:
            vd     = var.get("item_variation_data", {})
            vcustom = var.get("custom_attribute_values", {})
            print(f"    Variation '{vd.get('name','')}' SKU:'{vd.get('sku','')}' custom:{vcustom}")


def get_reporting_categories():
    """List all reporting categories."""
    print("\n=== REPORTING CATEGORIES ===")
    cursor = None
    while True:
        params = {"types": "CATEGORY"}
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
            cat = obj.get("category_data", {})
            name     = cat.get("name", "")
            cat_type = cat.get("category_type", "")
            print(f"  ID: {obj['id']} | Name: '{name}' | Type: '{cat_type}'")
        cursor = data.get("cursor")
        if not cursor:
            break


def main():
    print("=== Square Custom Attribute / Supplier Debug ===\n", flush=True)
    get_custom_attribute_definitions()
    get_reporting_categories()
    get_item_with_custom_attrs("Flat White")
    get_item_with_custom_attrs("Americano")


if __name__ == "__main__":
    main()
