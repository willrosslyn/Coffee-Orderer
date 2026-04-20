"""
weather_sync.py
Fetches weather from Visual Crossing Timeline API:
  - 7 days back through 28 days ahead (35 days total)
Replaces any existing Supabase weather rows for those dates.

Required environment variables:
  VISUAL_CROSSING_KEY  - your Visual Crossing API key
  SUPABASE_URL         - your Supabase project URL
  SUPABASE_SERVICE_KEY - your Supabase service role key
  WEATHER_LOCATION     - e.g. "London,UK" or "51.5074,-0.1278"
"""

import os
import sys
import requests
from datetime import datetime, timedelta

VISUAL_CROSSING_KEY  = os.environ["4WWZH8VR79RMDE5J9KLFWVL6T"]
SUPABASE_URL         = os.environ["https://wvpkztolfgvkagjylyuj.supabase.co"]
SUPABASE_SERVICE_KEY = os.environ["eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Ind2cGt6dG9sZmd2a2FnanlseXVqIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3NTAzMjcxOSwiZXhwIjoyMDkwNjA4NzE5fQ.G-bZGmXGoiW0UDzb6V7G7yvOj6Spw4qExxFZvQ5BFEs"]
LOCATION             = os.environ.get("London,UK", "London,UK")

SUPABASE_HEADERS = {
    "apikey":        SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type":  "application/json",
}


def parse_sunrise(sunrise_val) -> float:
    """
    Convert Visual Crossing sunrise to decimal fraction of day.
    Handles both "2026-04-13T06:09:19" and "06:09:19" formats.
    e.g. 06:09:19 -> 0.2565 (matches your existing weather table format)
    """
    if not sunrise_val:
        return 0.27
    try:
        # Strip date portion if present (e.g. "2026-04-13T06:09:19")
        time_part = sunrise_val.split("T")[-1]  # gives "06:09:19"
        h, m, s = time_part.split(":")
        return round(int(h)/24 + int(m)/1440 + int(s)/86400, 8)
    except Exception:
        return 0.27


def fetch_weather(start_date: str, end_date: str) -> list:
    """
    Fetch daily weather from Visual Crossing Timeline API.
    Column mapping:
      temp        <- temp        (daily average °C)
      rainfall    <- precip      (mm)
      cloud_cover <- cloudcover  (%)
      sunrise     <- sunrise     (decimal fraction of day)
    """
    url = (
        f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/"
        f"timeline/{LOCATION}/{start_date}/{end_date}"
        f"?unitGroup=metric"
        f"&elements=datetime,temp,precip,cloudcover,sunrise"
        f"&include=days"
        f"&key={VISUAL_CROSSING_KEY}"
        f"&contentType=json"
    )

    resp = requests.get(url, timeout=30)
    if resp.status_code != 200:
        raise Exception(f"Visual Crossing error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    rows = []
    for day in data.get("days", []):
        rows.append({
            "date":        day["datetime"],                          # "2026-04-13"
            "temp":        day.get("temp"),                         # e.g. 47.5
            "rainfall":    round(day.get("precip", 0) or 0, 4),    # e.g. 0.095
            "cloud_cover": day.get("cloudcover", 0) or 0,          # e.g. 52.6
            "sunrise":     parse_sunrise(day.get("sunrise")),       # e.g. 0.25647...
        })

    print(f"  Fetched {len(rows)} days from Visual Crossing", flush=True)
    return rows


def upsert_weather(rows: list):
    """Delete existing rows for the date range then insert fresh ones."""
    if not rows:
        return

    start = rows[0]["date"]
    end   = rows[-1]["date"]

    del_resp = requests.delete(
        f"{SUPABASE_URL}/rest/v1/weather?date=gte.{start}&date=lte.{end}",
        headers=SUPABASE_HEADERS
    )
    print(f"  Deleted existing rows ({start} to {end}): HTTP {del_resp.status_code}", flush=True)

    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/weather",
        headers={**SUPABASE_HEADERS, "Prefer": "return=minimal"},
        json=rows
    )
    if r.status_code not in (200, 201):
        raise Exception(f"Supabase insert failed: {r.status_code} {r.text}")

    print(f"  Inserted {len(rows)} weather rows", flush=True)


def main():
    today = datetime.utcnow().date()
    start_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    end_date   = (today + timedelta(days=28)).strftime("%Y-%m-%d")

    print(f"=== Weather sync started {datetime.utcnow().isoformat()} ===", flush=True)
    print(f"  Location:   {LOCATION}", flush=True)
    print(f"  Date range: {start_date} to {end_date} (35 days)", flush=True)

    try:
        rows = fetch_weather(start_date, end_date)
        upsert_weather(rows)
        print(f"=== Complete {datetime.utcnow().isoformat()} ===", flush=True)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
