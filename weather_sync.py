"""
weather_sync.py
Fetches hourly weather from Open-Meteo API, filters to 05:00-18:00 trading hours,
then aggregates into daily stats:
  - temp:        average temperature 05:00-18:00 (Fahrenheit)
  - rainfall:    sum of precipitation 05:00-18:00 (mm)
  - cloud_cover: average cloud cover 05:00-18:00 (%)
  - sunrise:     decimal fraction of day

Syncs 7 days back through 28 days ahead (35 days total).

No API key required.

Required environment variables:
  SUPABASE_URL         - Supabase project URL
  SUPABASE_SERVICE_KEY - Supabase service role key
  WEATHER_LAT          - Latitude e.g. 51.5133
  WEATHER_LON          - Longitude e.g. -0.0886
"""

import os
import sys
import requests
from datetime import datetime, timedelta
from collections import defaultdict

SUPABASE_URL         = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
LAT                  = os.environ.get("WEATHER_LAT", "51.5133")
LON                  = os.environ.get("WEATHER_LON", "-0.0886")

SUPABASE_HEADERS = {
    "apikey":        SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type":  "application/json",
}

TRADING_START = 5   # 05:00
TRADING_END   = 18  # up to but not including 18:00 (so 05:00-17:59)


def fetch_weather(start_date: str, end_date: str) -> list:
    """
    Fetch hourly weather from Open-Meteo, filter to trading hours,
    aggregate to daily stats.
    Also fetches daily sunrise separately.
    """
    url = "https://api.open-meteo.com/v1/forecast"

    # Fetch hourly data for trading hour aggregation
    params = {
        "latitude":           LAT,
        "longitude":          LON,
        "hourly":             "temperature_2m,precipitation,cloudcover",
        "daily":              "sunrise",
        "temperature_unit":   "fahrenheit",
        "precipitation_unit": "mm",
        "timezone":           "Europe/London",
        "start_date":         start_date,
        "end_date":           end_date,
    }

    resp = requests.get(url, params=params, timeout=30)
    if resp.status_code != 200:
        raise Exception(f"Open-Meteo error {resp.status_code}: {resp.text[:300]}")

    data   = resp.json()
    hourly = data.get("hourly", {})
    daily  = data.get("daily", {})

    times       = hourly.get("time", [])           # "2026-04-13T06:00"
    temps       = hourly.get("temperature_2m", [])
    precips     = hourly.get("precipitation", [])
    clouds      = hourly.get("cloudcover", [])

    sunrise_dates = daily.get("time", [])
    sunrise_vals  = daily.get("sunrise", [])

    # Build sunrise lookup {date_str: decimal_fraction}
    sunrise_map = {}
    for d, s in zip(sunrise_dates, sunrise_vals):
        if s:
            try:
                time_part = s.split("T")[-1]  # "06:09"
                h, m      = time_part.split(":")[:2]
                sunrise_map[d] = round(int(h)/24 + int(m)/1440, 8)
            except Exception:
                sunrise_map[d] = 0.27

    # Aggregate hourly data into daily buckets (trading hours only)
    daily_temps   = defaultdict(list)
    daily_precip  = defaultdict(float)
    daily_cloud   = defaultdict(list)

    for i, ts in enumerate(times):
        # ts = "2026-04-13T06:00"
        try:
            dt       = datetime.strptime(ts, "%Y-%m-%dT%H:%M")
            date_str = dt.strftime("%Y-%m-%d")
            hour     = dt.hour
        except Exception:
            continue

        if hour < TRADING_START or hour >= TRADING_END:
            continue

        if i < len(temps) and temps[i] is not None:
            daily_temps[date_str].append(temps[i])
        if i < len(precips) and precips[i] is not None:
            daily_precip[date_str] += precips[i]
        if i < len(clouds) and clouds[i] is not None:
            daily_cloud[date_str].append(clouds[i])

    # Build final rows
    all_dates = sorted(set(list(daily_temps.keys()) + list(sunrise_map.keys())))
    rows = []
    for date_str in all_dates:
        temp_vals  = daily_temps.get(date_str, [])
        cloud_vals = daily_cloud.get(date_str, [])

        rows.append({
            "date":        date_str,
            "temp":        round(sum(temp_vals) / len(temp_vals), 2) if temp_vals else None,
            "rainfall":    round(daily_precip.get(date_str, 0), 4),
            "cloud_cover": round(sum(cloud_vals) / len(cloud_vals), 2) if cloud_vals else 0,
            "sunrise":     sunrise_map.get(date_str, 0.27),
        })

    print(f"  Fetched {len(rows)} days from Open-Meteo (trading hours 05:00-18:00)", flush=True)
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
    today      = datetime.utcnow().date()
    start_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    end_date   = (today + timedelta(days=27)).strftime("%Y-%m-%d")

    print(f"=== Weather sync started {datetime.utcnow().isoformat()} ===", flush=True)
    print(f"  Location: {LAT}, {LON}", flush=True)
    print(f"  Date range: {start_date} to {end_date} (35 days)", flush=True)
    print(f"  Trading hours filter: {TRADING_START:02d}:00 - {TRADING_END:02d}:00", flush=True)

    try:
        rows = fetch_weather(start_date, end_date)
        upsert_weather(rows)
        print(f"=== Complete {datetime.utcnow().isoformat()} ===", flush=True)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
