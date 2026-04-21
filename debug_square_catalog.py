"""
debug_square_catalog.py
Shows reporting category IDs on actual drink items.
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

# Category IDs we expect to find
TARGET_IDS = {
    "A2ZNWW447QACM3MC2WBSBZ76": "RFW/RFM",
    "YJS2ONRSUIV2XLDHY55RUOOX": "RFB",
    "JAETG4OASNYOFKXZ4ODWS7NW": "RFF",
}

def main():
    cursor = None
    matched   = []
    unmatched = {}

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
            rep_cat   = item_data.get("reporting_category", {})
            rep_id    = rep_cat.get("id", "")

            # Check all category fields
            categories      = item_data.get("categories", [])
            cat_ids         = [c.get("id", "") for c in categories]

            if rep_id in TARGET_IDS:
                matched.append(f"  MATCH reporting_category: '{name}' -> {TARGET_IDS[rep_id]}")
            
            for cat_id in cat_ids:
                if cat_id in TARGET_IDS:
                    matched.append(f"  MATCH categories[]: '{name}' -> {TARGET_IDS[cat_id]}")

            # For drink-sounding items show all their category info
            keywords = ["flat","white","americano","latte","cappuccino",
                        "cortado","espresso","long black","batch","filter"]
            if any(k in name.lower() for k in keywords):
                unmatched[name] = {
                    "reporting_category_id": rep_id,
                    "category_ids": cat_ids,
                }

        cursor = data.get("cursor")
        if not cursor:
            break

    print(f"\n=== ITEMS MATCHING TARGET CATEGORY IDs ===")
    if matched:
        for m in matched[:30]:
            print(m)
    else:
        print("  NONE FOUND")

    print(f"\n=== SAMPLE DRINK ITEMS - ALL CATEGORY INFO ===")
    for name, info in list(unmatched.items())[:20]:
        print(f"  '{name}'")
        print(f"    reporting_category_id: '{info['reporting_category_id']}'")
        print(f"    category_ids: {info['category_ids']}")

if __name__ == "__main__":
    main()
