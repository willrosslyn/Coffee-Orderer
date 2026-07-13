"""
weather_sync.py
Fetches weather from Open-Meteo using two API calls:
  1. Archive API  — last 7 days (ERA5 reanalysis, accurate)
  2. Forecast API — next 16 days (high-resolution forecast)
  3. Ensemble API — days 17-28 (GFS ensemble, lower resolution but 35-day range)

Filters to trading hours 05:00-18:00 and aggregates to daily stats.
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

TRADING_START = 5
TRADING_END   = 18


def parse_sunrise(sunrise_str: str) -> float:
    """Convert "2026-04-13T06:09" to decimal fraction of day."""
    try:
        time_part = sunrise_str.split("T")[-1]
        h, m      = time_part.split(":")[:2]
        return round(int(h)/24 + int(m)/1440, 8)
    except Exception:
        return 0.27


def aggregate_hourly(times, temps, precips, clouds) -> tuple:
    """
    Filter hourly data to trading hours and aggregate to daily.
    Returns (daily_temps, daily_precip, daily_cloud) dicts keyed by date string.
    """
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

    return daily_temps, daily_precip, daily_cloud


def fetch_archive(start_date: str, end_date: str) -> dict:
    """Fetch past weather from ERA5 archive API. Returns {date: row}."""
    resp = requests.get(
        "https://archive-api.open-meteo.com/v1/archive",
        params={
            "latitude":           LAT,
            "longitude":          LON,
            "hourly":             "temperature_2m,precipitation,cloudcover",
            "daily":              "sunrise",
            "temperature_unit":   "fahrenheit",
            "precipitation_unit": "mm",
            "timezone":           "Europe/London",
            "start_date":         start_date,
            "end_date":           end_date,
        },
        timeout=30
    )
    if resp.status_code != 200:
        raise Exception(f"Archive API error {resp.status_code}: {resp.text[:200]}")

    data   = resp.json()
    hourly = data.get("hourly", {})
    daily  = data.get("daily", {})

    sunrise_map = {d: parse_sunrise(s) for d, s in
                   zip(daily.get("time", []), daily.get("sunrise", [])) if s}

    dt, dp, dc = aggregate_hourly(
        hourly.get("time", []),
        hourly.get("temperature_2m", []),
        hourly.get("precipitation", []),
        hourly.get("cloudcover", []),
    )

    rows = {}
    for date_str in sorted(set(list(dt.keys()) + list(sunrise_map.keys()))):
        tv = dt.get(date_str, [])
        cv = dc.get(date_str, [])
        rows[date_str] = {
            "date":        date_str,
            "temp":        round(sum(tv)/len(tv), 2) if tv else None,
            "rainfall":    round(dp.get(date_str, 0), 4),
            "cloud_cover": round(sum(cv)/len(cv), 2) if cv else 0,
            "sunrise":     sunrise_map.get(date_str, 0.27),
        }
    print(f"  Archive API: {len(rows)} days ({start_date} to {end_date})", flush=True)
    return rows


def fetch_forecast(start_date: str, end_date: str) -> dict:
    """Fetch near-future forecast (up to 16 days). Returns {date: row}."""
    resp = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude":           LAT,
            "longitude":          LON,
            "hourly":             "temperature_2m,precipitation,cloudcover",
            "daily":              "sunrise",
            "temperature_unit":   "fahrenheit",
            "precipitation_unit": "mm",
            "timezone":           "Europe/London",
            "start_date":         start_date,
            "end_date":           end_date,
        },
        timeout=30
    )
    if resp.status_code != 200:
        raise Exception(f"Forecast API error {resp.status_code}: {resp.text[:200]}")

    data   = resp.json()
    hourly = data.get("hourly", {})
    daily  = data.get("daily", {})

    sunrise_map = {d: parse_sunrise(s) for d, s in
                   zip(daily.get("time", []), daily.get("sunrise", [])) if s}

    dt, dp, dc = aggregate_hourly(
        hourly.get("time", []),
        hourly.get("temperature_2m", []),
        hourly.get("precipitation", []),
        hourly.get("cloudcover", []),
    )

    rows = {}
    for date_str in sorted(set(list(dt.keys()) + list(sunrise_map.keys()))):
        tv = dt.get(date_str, [])
        cv = dc.get(date_str, [])
        rows[date_str] = {
            "date":        date_str,
            "temp":        round(sum(tv)/len(tv), 2) if tv else None,
            "rainfall":    round(dp.get(date_str, 0), 4),
            "cloud_cover": round(sum(cv)/len(cv), 2) if cv else 0,
            "sunrise":     sunrise_map.get(date_str, 0.27),
        }
    print(f"  Forecast API: {len(rows)} days ({start_date} to {end_date})", flush=True)
    return rows


def fetch_ensemble(start_date: str, end_date: str) -> dict:
    """Fetch extended forecast using GFS ensemble (up to 35 days). Returns {date: row}."""
    resp = requests.get(
        "https://ensemble-api.open-meteo.com/v1/ensemble",
        params={
            "latitude":           LAT,
            "longitude":          LON,
            "hourly":             "temperature_2m,precipitation,cloudcover",
            "models":             "gfs_seamless",
            "temperature_unit":   "fahrenheit",
            "precipitation_unit": "mm",
            "timezone":           "Europe/London",
            "start_date":         start_date,
            "end_date":           end_date,
        },
        timeout=30
    )
    if resp.status_code != 200:
        raise Exception(f"Ensemble API error {resp.status_code}: {resp.text[:200]}")

    data   = resp.json()
    hourly = data.get("hourly", {})

    # Ensemble returns member columns like temperature_2m_member01 etc.
    # Average across all members
    times   = hourly.get("time", [])
    temp_keys   = [k for k in hourly if k.startswith("temperature_2m")]
    precip_keys = [k for k in hourly if k.startswith("precipitation")]
    cloud_keys  = [k for k in hourly if k.startswith("cloudcover")]

    def avg_members(keys, i):
        vals = [hourly[k][i] for k in keys if i < len(hourly[k]) and hourly[k][i] is not None]
        return sum(vals)/len(vals) if vals else None

    temps   = [avg_members(temp_keys, i) for i in range(len(times))]
    precips = [avg_members(precip_keys, i) for i in range(len(times))]
    clouds  = [avg_members(cloud_keys, i) for i in range(len(times))]

    dt, dp, dc = aggregate_hourly(times, temps, precips, clouds)

    # Estimate sunrise from lat/lon (simple approximation for London)
    rows = {}
    for date_str in sorted(dt.keys()):
        tv = dt.get(date_str, [])
        cv = dc.get(date_str, [])
        rows[date_str] = {
            "date":        date_str,
            "temp":        round(sum(tv)/len(tv), 2) if tv else None,
            "rainfall":    round(dp.get(date_str, 0), 4),
            "cloud_cover": round(sum(cv)/len(cv), 2) if cv else 0,
            "sunrise":     0.27,  # approximate — will be overwritten by forecast API where available
        }
    print(f"  Ensemble API: {len(rows)} days ({start_date} to {end_date})", flush=True)
    return rows


def upsert_weather(rows: list):
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
    past_start = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    past_end   = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    fc_start   = today.strftime("%Y-%m-%d")
    fc_end     = (today + timedelta(days=15)).strftime("%Y-%m-%d")
    ens_start  = (today + timedelta(days=16)).strftime("%Y-%m-%d")
    ens_end    = (today + timedelta(days=35)).strftime("%Y-%m-%d")

    print(f"=== Weather sync started {datetime.utcnow().isoformat()} ===", flush=True)
    print(f"  Location: {LAT}, {LON}", flush=True)
    print(f"  Trading hours filter: {TRADING_START:02d}:00 - {TRADING_END:02d}:00", flush=True)

    try:
        all_rows = {}

        # 1. Past 7 days from archive
        archive_rows = fetch_archive(past_start, past_end)
        all_rows.update(archive_rows)

        # 2. Next 16 days from forecast API
        forecast_rows = fetch_forecast(fc_start, fc_end)
        all_rows.update(forecast_rows)

        # 3. Days 17-28 from ensemble API
        ensemble_rows = fetch_ensemble(ens_start, ens_end)
        # Only add ensemble rows that aren't already covered by forecast
        for date_str, row in ensemble_rows.items():
            if date_str not in all_rows:
                all_rows[date_str] = row

        # Sort and convert to list
        rows = [all_rows[d] for d in sorted(all_rows.keys())]
        print(f"\n  Total: {len(rows)} days ({rows[0]['date']} to {rows[-1]['date']})", flush=True)

        upsert_weather(rows)
        print(f"=== Complete {datetime.utcnow().isoformat()} ===", flush=True)

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
