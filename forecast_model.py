"""
Coffee Ordering System - Multi-shop RF Forecast Model
Improvements:
- Bank holiday days excluded from training
- Added week_of_year, month, is_december, is_summer features
- Saturday auto-detection from historical data
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

FEATURE_COLS = [
    "dow",
    "week_of_year",
    "month",
    "is_december",
    "is_summer",
    "temp",
    "rainfall",
    "sunrise",
    "cloud_cover",
    "is_holiday",
]


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
        return pd.DataFrame(columns=["date","rfm_1kg","rfm_200g","rfb_1kg",
                                      "rfb_200g","rff_1kg","rff_200g","ft_scoop"])
    df = df.rename(columns={"week": "date"})
    df["date"] = pd.to_datetime(df["date"])
    for c in ["rfm_1kg","rfm_200g","rfb_1kg","rfb_200g","rff_1kg","rff_200g","ft_scoop"]:
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


def detect_saturday_trading(bar_sales):
    sat     = bar_sales[bar_sales["date"].dt.dayofweek == 5]
    has_sat = (sat["rfm"] > 0).any() if not sat.empty else False
    print(f"  Saturday trading detected: {has_sat}", flush=True)
    return has_sat


def add_features(df):
    """Add all model features to a dataframe that has a 'date' column."""
    df = df.copy()
    df["dow"]          = df["date"].dt.dayofweek
    df["week_of_year"] = df["date"].dt.isocalendar().week.astype(int)
    df["month"]        = df["date"].dt.month
    df["is_december"]  = (df["date"].dt.month == 12).astype(int)
    df["is_summer"]    = (df["date"].dt.month.isin([7, 8])).astype(int)
    df["is_holiday"]   = df["date"].isin(BANK_HOLIDAYS).astype(int)
    return df


def to_daily(df, weather_df):
    """Merge daily sales data with weather and add model features."""
    daily = df.copy()
    daily = daily.merge(weather_df, on="date", how="left")
    for c in ["temp","rainfall","cloud_cover","sunrise"]:
        daily[c] = daily[c].ffill().fillna(weather_df[c].median())
    daily = add_features(daily)
    daily["day_index"] = range(len(daily))
    return daily


def build_future(weather_df, last_index, last_date, n_days=90, include_saturday=False):
    """Build future feature rows for forecasting."""
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


def train_and_forecast(daily, future, target_col):
    # Exclude bank holiday days from training
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


def main():
    print(f"=== Forecast job={JOB_ID} shop={SHOP_ID} started {datetime.utcnow().isoformat()} ===", flush=True)
    update_job("running")

    try:
        print("Fetching data...", flush=True)
        bar_sales    = fetch_bar_sales()
        retail_sales = fetch_retail_sales()
        weather      = fetch_weather()
        print(f"  Bar: {len(bar_sales)} days | Retail: {len(retail_sales)} days | Weather: {len(weather)} days", flush=True)

        include_saturday = detect_saturday_trading(bar_sales)

        # ── Bar models ────────────────────────────────────────
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
            bar_results[ct] = train_and_forecast(bar_daily, bar_future, ct)

        # ── Retail models ─────────────────────────────────────
        retail_results = {}
        has_retail = not retail_sales.empty and len(retail_sales) >= 5

        if has_retail:
            print("\n-- Retail models --", flush=True)
            rs = retail_sales.copy()
            rs["rfm_total"] = rs["rfm_1kg"] + rs["rfm_200g"]
            rs["rfb_total"] = rs["rfb_1kg"] + rs["rfb_200g"]
            rs["rff_total"] = rs["rff_1kg"] + rs["rff_200g"]
            rs["fts_total"] = rs["ft_scoop"]

            total_cols = ["rfm_total","rfb_total","rff_total","fts_total"]
            ret_daily  = to_daily(rs, weather)
            ret_future = build_future(
                weather,
                ret_daily["day_index"].max(),
                ret_daily["date"].max(),
                include_saturday=include_saturday
            )

            for ct in total_cols:
                retail_results[ct] = train_and_forecast(ret_daily, ret_future, ct)

            def size_ratio(a, b):
                total = retail_sales[a].sum() + retail_sales[b].sum()
                return retail_sales[a].sum() / total if total > 0 else 0.5

            rfm_r = size_ratio("rfm_1kg", "rfm_200g")
            rfb_r = size_ratio("rfb_1kg", "rfb_200g")
            rff_r = size_ratio("rff_1kg", "rff_200g")
        else:
            print("\n-- Retail: not enough data, skipping --", flush=True)

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
                "updated_at": datetime.utcnow().isoformat(),
            }
            if has_retail:
                rfm = float(retail_results["rfm_total"].get(d, 0))
                rfb = float(retail_results["rfb_total"].get(d, 0))
                rff = float(retail_results["rff_total"].get(d, 0))
                fts = float(retail_results["fts_total"].get(d, 0))
                rec["rfm_1kg"]  = round(rfm * rfm_r,       2)
                rec["rfm_200g"] = round(rfm * (1 - rfm_r), 2)
                rec["rfb_1kg"]  = round(rfb * rfb_r,       2)
                rec["rfb_200g"] = round(rfb * (1 - rfb_r), 2)
                rec["rff_1kg"]  = round(rff * rff_r,       2)
                rec["rff_200g"] = round(rff * (1 - rff_r), 2)
                rec["ft_scoop"] = round(fts,               2)
            else:
                rec["rfm_1kg"]  = None
                rec["rfm_200g"] = None
                rec["rfb_1kg"]  = None
                rec["rfb_200g"] = None
                rec["rff_1kg"]  = None
                rec["rff_200g"] = None
                rec["ft_scoop"] = None
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
