"""
Coffee Ordering System - Multi-shop RF Forecast Model
Bar: Random Forest on deviations from a rolling 4-week level, with weather/seasonal features
Retail: Rolling 8-week day-of-week average (simpler, more reliable for sparse data)
"""

import os, sys, requests
import pandas as pd
from datetime import datetime, timedelta
from sklearn.ensemble import RandomForestRegressor

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SHOP_ID      = os.environ["SHOP_ID"]
JOB_ID       = os.environ.get("JOB_ID", "manual")
HAS_SATURDAY = os.environ.get("HAS_SATURDAY", "false").strip().lower() == "true"

HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
}

BANK_HOLIDAYS = pd.to_datetime([
    # 2025
    "2025-04-18","2025-04-21","2025-05-05","2025-05-26","2025-08-25",
    "2025-12-25","2025-12-26",
    # 2026
    "2026-01-01","2026-04-03","2026-04-06","2026-05-04","2026-05-25",
    "2026-08-31","2026-12-25","2026-12-28",
    # 2027
    "2027-01-01","2027-03-26","2027-03-29","2027-05-03","2027-05-31",
    "2027-08-30","2027-12-27","2027-12-28",
    # 2028
    "2028-01-03","2028-04-14","2028-04-17","2028-05-01","2028-05-29",
    "2028-08-28","2028-12-25","2028-12-26",
])

# Every shop shuts from 24 December to 4 January inclusive, the same rule as
# Lynda's isShopClosedDate, so those days forecast as zero like a bank holiday.
# Without this they forecast as normal trading days, which inflated orders placed
# from the end of November.
CHRISTMAS_CLOSURES = pd.DatetimeIndex([])
for _year in range(2025, 2029):
    CHRISTMAS_CLOSURES = CHRISTMAS_CLOSURES.union(
        pd.date_range(f"{_year}-12-24", f"{_year + 1}-01-04")
    )
BANK_HOLIDAYS = BANK_HOLIDAYS.union(CHRISTMAS_CLOSURES)

FEATURE_COLS = [
    "dow",
    "week_of_year",
    "month",
    "is_december",
    "temp",
    "rainfall",
    "sunrise",
    "cloud_cover",
    "is_holiday",
]

RETAIL_COLS = ["rfm_1kg","rfm_200g","rfb_1kg","rfb_200g","rff_1kg","rff_200g","ft_scoop"]
ROLLING_WEEKS = 8   # retail: day-of-week average window
LEVEL_WEEKS   = 4   # bar: trailing window for the rolling level
HORIZON_DAYS  = 35  # calendar days ahead of today; matches the weather sync's forecast range


def sb_get(table, params):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=HEADERS, params=params
    )
    r.raise_for_status()
    return r.json()


def sb_upsert(records):
    requests.delete(
        f"{SUPABASE_URL}/rest/v1/forecasts?shop_id=eq.{SHOP_ID}",
        headers=HEADERS
    )
    for i in range(0, len(records), 200):
        batch = records[i:i+200]
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/forecasts",
            headers={**HEADERS, "Prefer": "return=minimal"},
            json=batch
        )
        if r.status_code not in (200, 201):
            raise Exception(f"Insert failed: {r.status_code} {r.text}")
    print(f"Inserted {len(records)} forecast rows", flush=True)


def update_job(status, message=""):
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/forecast_jobs?id=eq.{JOB_ID}",
        headers=HEADERS,
        json={"status": status, "message": message,
              "completed_at": datetime.utcnow().isoformat()}
    )


def fetch_bar_sales():
    rows = sb_get("sales_bar", {
        "select":  "week,rfm,rfb,rff,rfd",
        "shop_id": f"eq.{SHOP_ID}",
        "order":   "week.asc",
        "limit":   "2000"
    })
    df = pd.DataFrame(rows)
    if df.empty:
        raise Exception(f"No bar sales found for shop_id={SHOP_ID}")
    df = df.rename(columns={"week": "date"})
    df["date"] = pd.to_datetime(df["date"])
    for c in ["rfm","rfb","rff","rfd"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df


def fetch_retail_sales():
    rows = sb_get("sales_retail", {
        "select":  "week,rfm_1kg,rfm_200g,rfb_1kg,rfb_200g,rff_1kg,rff_200g,ft_scoop",
        "shop_id": f"eq.{SHOP_ID}",
        "order":   "week.asc",
        "limit":   "2000"
    })
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["date"] + RETAIL_COLS)
    df = df.rename(columns={"week": "date"})
    df["date"] = pd.to_datetime(df["date"])
    for c in RETAIL_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df


def fetch_weather():
    rows = sb_get("weather", {
        "select": "date,temp,rainfall,cloud_cover,sunrise",
        "order":  "date.asc",
        "limit":  "2000"
    })
    df = pd.DataFrame(rows)
    if df.empty:
        raise Exception("No weather data found")
    df["date"] = pd.to_datetime(df["date"])
    for c in ["temp","rainfall","cloud_cover","sunrise"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def add_features(df):
    df = df.copy()
    df["dow"]          = df["date"].dt.dayofweek
    df["week_of_year"] = df["date"].dt.isocalendar().week.astype(int)
    df["month"]        = df["date"].dt.month
    df["is_december"]  = (df["date"].dt.month == 12).astype(int)
    df["is_holiday"]   = df["date"].isin(BANK_HOLIDAYS).astype(int)
    return df


def to_daily(df, weather_df):
    daily = df.copy()
    daily = daily.merge(weather_df, on="date", how="left")
    for c in ["temp","rainfall","cloud_cover","sunrise"]:
        daily[c] = daily[c].ffill().fillna(weather_df[c].median())
    daily = add_features(daily)
    return daily


def build_future(weather_df, last_date, end_date, include_saturday=False):
    """Trading days from the day after the last sales row through end_date inclusive."""
    future_dates = []
    d = last_date + timedelta(days=1)
    max_dow = 5 if include_saturday else 4
    while d <= end_date:
        if d.weekday() <= max_dow:
            future_dates.append(d)
        d += timedelta(days=1)
    if not future_dates:
        raise Exception(f"No future trading days between {last_date.date()} and {end_date.date()}")

    future = pd.DataFrame({"date": pd.to_datetime(future_dates)})
    future = future.merge(weather_df, on="date", how="left")
    for c in ["temp","rainfall","cloud_cover","sunrise"]:
        future[c] = future[c].ffill().fillna(weather_df[c].median())
    future = add_features(future)
    return future


def rolling_level(daily, target_col, window, min_periods):
    """
    Trailing mean of the last `window` trading days, bank holidays excluded.
    Window is a whole number of trading weeks so it stays day-of-week balanced.
    """
    s = daily[target_col].where(daily["is_holiday"] == 0)
    return s.rolling(window, min_periods=min_periods).mean()


def train_and_forecast_bar(daily, future, target_col, trading_days_per_week):
    """
    Random Forest model for bar coffee.
    The rolling level carries the trend; the RF learns day-of-week / weather /
    seasonal deviations from that level. Future rows are held at the current
    level, so the forecast can't run away the way an extrapolated trend can.
    """
    window = LEVEL_WEEKS * trading_days_per_week
    level  = rolling_level(daily, target_col, window, min_periods=trading_days_per_week)

    daily = daily.copy()
    daily["level"] = level.shift(1)   # level known at the start of each day
    training = daily[
        (daily[target_col].notna()) &
        (daily["level"].notna()) &
        (daily["is_holiday"] == 0)
    ].copy()
    if training.empty:
        raise Exception(f"{target_col}: not enough history to build a level "
                        f"(need > {trading_days_per_week} trading days)")
    training["residual"] = training[target_col] - training["level"]

    print(f"  {target_col}: {len(training)} training rows (excl. bank holidays)", end="", flush=True)

    features = FEATURE_COLS + ["level"]
    model = RandomForestRegressor(
        n_estimators=500, max_depth=10, min_samples_leaf=5,
        random_state=42, oob_score=True, bootstrap=True
    )
    model.fit(training[features], training["residual"])

    current_level = level.iloc[-1]
    oob = getattr(model, "oob_score_", None)
    print(f", level={current_level:.1f}/day" + (f", OOB R2={oob:.3f}" if oob else ""), flush=True)

    future = future.copy()
    future["level"]    = current_level
    future["forecast"] = current_level + model.predict(future[features])
    future["forecast"] = future["forecast"].clip(lower=0).round(2)
    future.loc[future["is_holiday"] == 1, "forecast"] = 0

    return future[["date","forecast"]].set_index("date")["forecast"]


def forecast_retail_rolling(retail_df, future_dates, include_saturday=False):
    """
    Rolling 8-week day-of-week average for retail.
    For each future date, returns the average sales for that day of week
    over the last ROLLING_WEEKS weeks, excluding bank holidays.
    """
    if retail_df.empty or len(retail_df) < 7:
        print(f"  Retail: insufficient data, using zeros", flush=True)
        result = {}
        for col in RETAIL_COLS:
            result[col] = pd.Series(0.0, index=future_dates)
        return result

    # Use most recent ROLLING_WEEKS weeks only
    cutoff = retail_df["date"].max() - timedelta(weeks=ROLLING_WEEKS)
    recent = retail_df[
        (retail_df["date"] >= cutoff) &
        (~retail_df["date"].isin(BANK_HOLIDAYS))
    ].copy()
    recent["dow"] = recent["date"].dt.dayofweek

    result = {}
    for col in RETAIL_COLS:
        dow_avg = recent.groupby("dow")[col].mean()
        forecasts = []
        for d in future_dates:
            dow = d.dayofweek
            if d in BANK_HOLIDAYS:
                forecasts.append(0.0)
            else:
                avg = dow_avg.get(dow, 0.0)
                forecasts.append(round(float(avg), 2))
        result[col] = pd.Series(forecasts, index=future_dates)

    # Print summary
    for col in RETAIL_COLS:
        avg = result[col].mean()
        print(f"  {col}: {ROLLING_WEEKS}wk rolling avg={avg:.2f}/day", flush=True)

    return result


def main():
    print(f"=== Forecast job={JOB_ID} shop={SHOP_ID} started {datetime.utcnow().isoformat()} ===", flush=True)
    update_job("running")

    try:
        print("Fetching data...", flush=True)
        bar_sales    = fetch_bar_sales()
        retail_sales = fetch_retail_sales()
        weather      = fetch_weather()
        print(f"  Bar: {len(bar_sales)} days | Retail: {len(retail_sales)} days | Weather: {len(weather)} days", flush=True)

        include_saturday = HAS_SATURDAY
        trading_days_per_week = 6 if include_saturday else 5
        print(f"  Saturday trading: {include_saturday}", flush=True)

        # ── Bar models (Random Forest) ────────────────────────
        print("\n-- Bar models --", flush=True)
        bar_daily   = to_daily(bar_sales, weather)
        horizon_end = pd.Timestamp(datetime.utcnow().date()) + timedelta(days=HORIZON_DAYS)
        bar_future  = build_future(
            weather,
            bar_daily["date"].max(),
            horizon_end,
            include_saturday=include_saturday
        )
        print(f"  Horizon: {len(bar_future)} trading days, "
              f"{bar_future['date'].min().date()} to {bar_future['date'].max().date()}", flush=True)

        bar_results = {}
        for ct in ["rfm","rfb","rff","rfd"]:
            bar_results[ct] = train_and_forecast_bar(bar_daily, bar_future, ct, trading_days_per_week)

        # ── Retail models (Rolling average) ──────────────────
        print("\n-- Retail models (rolling 8-week DOW average) --", flush=True)
        future_dates   = bar_future["date"].values
        future_dates   = pd.to_datetime(future_dates)
        has_retail     = not retail_sales.empty and len(retail_sales) >= 7
        retail_results = forecast_retail_rolling(
            retail_sales if has_retail else pd.DataFrame(columns=["date"] + RETAIL_COLS),
            future_dates,
            include_saturday=include_saturday
        )

        # ── Combine into one row per date ─────────────────────
        all_dates = bar_results["rfm"].index
        records = []
        for d in all_dates:
            rec = {
                "shop_id":    SHOP_ID,
                "week":       d.strftime("%Y-%m-%d"),
                "rfm":        float(bar_results["rfm"].get(d, 0)),
                "rfb":        float(bar_results["rfb"].get(d, 0)),
                "rff":        float(bar_results["rff"].get(d, 0)),
                "rfd":        float(bar_results["rfd"].get(d, 0)),
                "rfm_1kg":    float(retail_results["rfm_1kg"].get(d, 0)),
                "rfm_200g":   float(retail_results["rfm_200g"].get(d, 0)),
                "rfb_1kg":    float(retail_results["rfb_1kg"].get(d, 0)),
                "rfb_200g":   float(retail_results["rfb_200g"].get(d, 0)),
                "rff_1kg":    float(retail_results["rff_1kg"].get(d, 0)),
                "rff_200g":   float(retail_results["rff_200g"].get(d, 0)),
                "ft_scoop":   float(retail_results["ft_scoop"].get(d, 0)),
                "updated_at": datetime.utcnow().isoformat(),
            }
            records.append(rec)

        print(f"\nUpserting {len(records)} combined forecast rows...", flush=True)
        sb_upsert(records)

        update_job("done")
        print(f"=== Complete {datetime.utcnow().isoformat()} ===", flush=True)

    except Exception as e:
        print(f"ERROR: {e}", flush=True)
        update_job("error", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
