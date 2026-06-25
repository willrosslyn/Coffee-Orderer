"""
Coffee Ordering System - Multi-shop RF Forecast Model
Bar: Random Forest with weather/seasonal features
Retail: Rolling 8-week day-of-week average (simpler, more reliable for sparse data)
"""

import os, sys, requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from sklearn.ensemble import RandomForestRegressor

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SHOP_ID      = os.environ["SHOP_ID"]
JOB_ID       = os.environ.get("JOB_ID", "manual")

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
    "2027-01-01","2027-04-02","2027-04-05","2027-05-03","2027-05-31",
    "2027-08-30","2027-12-27","2027-12-28",
])

SATURDAY_SHOPS = {"QVS", "LUC","LSS"}

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
ROLLING_WEEKS = 8


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
    daily["day_index"] = range(len(daily))
    return daily


def build_future(weather_df, last_index, last_date, n_days=90, include_saturday=False):
    future_dates = []
    d = last_date + timedelta(days=1)
    max_dow = 5 if include_saturday else 4
    while len(future_dates) < n_days:
        if d.weekday() <= max_dow:
            future_dates.append(d)
        d += timedelta(days=1)

    future = pd.DataFrame({"date": pd.to_datetime(future_dates)})
    future = future.merge(weather_df, on="date", how="left")
    for c in ["temp","rainfall","cloud_cover","sunrise"]:
        future[c] = future[c].ffill().fillna(weather_df[c].median())
    future = add_features(future)
    future["day_index"] = range(last_index + 1, last_index + 1 + len(future))
    return future


def train_and_forecast_bar(daily, future, target_col):
    """Random Forest model for bar coffee."""
    training = daily[
        (daily[target_col].notna()) &
        (daily["is_holiday"] == 0)
    ].copy()

    print(f"  {target_col}: {len(training)} training rows (excl. bank holidays)", end="", flush=True)

    t = training["day_index"].values
    y = training[target_col].values
    coef     = np.polyfit(t, y, deg=2)
    trend_fn = np.poly1d(coef)

    training["trend"]    = trend_fn(t)
    training["residual"] = y - training["trend"]

    model = RandomForestRegressor(
        n_estimators=500, max_depth=10, min_samples_leaf=5,
        random_state=42, oob_score=True, bootstrap=True
    )
    model.fit(training[FEATURE_COLS], training["residual"])

    fitted           = training["trend"] + model.predict(training[FEATURE_COLS])
    recent           = training.tail(20).copy()
    recent["fitted"] = fitted[-20:]
    ratio            = recent[target_col] / recent["fitted"].replace(0, np.nan)
    uplift           = ratio.median()
    if np.isnan(uplift) or uplift <= 0:
        uplift = 1.0

    oob = getattr(model, "oob_score_", None)
    print(f", uplift={uplift:.3f}" + (f", OOB R2={oob:.3f}" if oob else ""), flush=True)

    future = future.copy()
    future["trend"]    = trend_fn(future["day_index"].values)
    future["residual"] = model.predict(future[FEATURE_COLS])
    future["forecast"] = (future["trend"] + future["residual"]) * uplift
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

        include_saturday = SHOP_ID in SATURDAY_SHOPS
        print(f"  Saturday trading: {include_saturday}", flush=True)

        # ── Bar models (Random Forest) ────────────────────────
        print("\n-- Bar models --", flush=True)
        bar_daily  = to_daily(bar_sales, weather)
        bar_future = build_future(
            weather,
            bar_daily["day_index"].max(),
            bar_daily["date"].max(),
            include_saturday=include_saturday
        )

        bar_results = {}
        for ct in ["rfm","rfb","rff","rfd"]:
            bar_results[ct] = train_and_forecast_bar(bar_daily, bar_future, ct)

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
