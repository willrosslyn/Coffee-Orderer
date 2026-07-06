"""
weather_historical_sync.py
Backfills historical weather data from Open-Meteo using the ERA5 reanalysis API.
Filters to trading hours (05:00-18:00) and aggregates to daily stats.

Run once via GitHub Actions workflow_dispatch.

Required environment variables:
  SUPABASE_URL         - Supabase project URL
  SUPABASE_SERVICE_KEY - Supabase service role key
  WEATHER_LAT          - Latitude e.g. 51.5133
  WEATHER_LON          - Longitude e.g. -0.0886
  START_DATE           - e.g. "2025-04-14"
  END_DATE             - e.g. "2026-07-06" (optional, defaults to yesterday)
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
START_DATE           = os.environ["START_DATE"]
END_DATE             = os.environ.get("END_DATE", "")

SUPABASE_HEADERS = {
    "apikey":        SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type":  "application/json",
}

TRADING_START = 5
TRADING_END   = 18
CHUNK_DAYS    = 90  # fetch 90 days at a time to avoid huge responses


def fetch_historical_chunk(start_date: str, end_date: str) -> list:
    """
    Fetch hourly historical weather from Open-Meteo ERA5 reanalysis API.
    Filters to trading hours and aggregates to daily stats.
    """
    url = "https://archive-api.open-meteo.com/v1/archive"
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

    resp = requests.get(url, params=params, timeout=60)
    if resp.status_code != 200:
        raise Exception(f"Open-Meteo archive error {resp.status_code}: {resp.text[:300]}")

    data   = resp.json()
    hourly = data.get("hourly", {})
    daily  = data.get("daily", {})

    times   = hourly.get("time", [])
    temps   = hourly.get("temperature_2m", [])
    precips = hourly.get("precipitation", [])
    clouds  = hourly.get("cloudcover", [])

    sunrise_dates = daily.get("time", [])
    sunrise_vals  = daily.get("sunrise", [])

    # Build sunrise lookup
    sunrise_map = {}
    for d, s in zip(sunrise_dates, sunrise_vals):
        if s:
            try:
                time_part = s.split("T")[-1]
                h, m      = time_part.split(":")[:2]
                sunrise_map[d] = round(int(h)/24 + int(m)/1440, 8)
            except Exception:
                sunrise_map[d] = 0.27

    # Aggregate hourly to daily (trading hours only)
    daily_temps  = defaultdict(list)
    daily_precip = defaultdict(float)
    daily_cloud  = defaultdict(list)

    for i, ts in enumerate(times):
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

    return rows


def upsert_weather(rows: list):
    if not rows:
        return 0
    start = rows[0]["date"]
    end   = rows[-1]["date"]

    requests.delete(
        f"{SUPABASE_URL}/rest/v1/weather?date=gte.{start}&date=lte.{end}",
        headers=SUPABASE_HEADERS
    )

    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/weather",
        headers={**SUPABASE_HEADERS, "Prefer": "return=minimal"},
        json=rows
    )
    if r.status_code not in (200, 201):
        raise Exception(f"Supabase insert failed: {r.status_code} {r.text}")
    return len(rows)


def main():
    start_dt = datetime.strptime(START_DATE, "%Y-%m-%d")
    if END_DATE:
        end_dt = datetime.strptime(END_DATE, "%Y-%m-%d")
    else:
        end_dt = datetime.utcnow() - timedelta(days=1)

    print(f"=== Historical weather sync started {datetime.utcnow().isoformat()} ===", flush=True)
    print(f"  Location: {LAT}, {LON}", flush=True)
    print(f"  Date range: {start_dt.date()} to {end_dt.date()}", flush=True)
    print(f"  Trading hours filter: {TRADING_START:02d}:00 - {TRADING_END:02d}:00", flush=True)

    try:
        total     = 0
        chunk_start = start_dt
        while chunk_start <= end_dt:
            chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS - 1), end_dt)
            start_str = chunk_start.strftime("%Y-%m-%d")
            end_str   = chunk_end.strftime("%Y-%m-%d")

            print(f"  Fetching {start_str} to {end_str}...", flush=True)
            rows  = fetch_historical_chunk(start_str, end_str)
            count = upsert_weather(rows)
            total += count
            print(f"    Upserted {count} rows", flush=True)

            chunk_start = chunk_end + timedelta(days=1)

        print(f"\n  Total: {total} rows upserted", flush=True)
        print(f"=== Complete {datetime.utcnow().isoformat()} ===", flush=True)

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
