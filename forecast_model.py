"""
Coffee Ordering System v5 - Multi-shop RF Forecast Model
4 bar models (RFM/RFB/RFF/RFD) + 4 retail models (RFM/RFB/RFF/FTScoop)
Bar data is daily. Retail data is weekly (expanded to daily).
Triggered via GitHub Actions repository_dispatch with shop_id + job_id payload.
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
    "2026-04-03","2026-04-06","2026-05-04","2026-05-25","2026-08-31",
    "2027-04-02","2027-04-05","2027-05-03","2027-05-31","2027-08-30",
])

FEATURE_COLS = ["dow","temp","rainfall","sunrise","is_holiday","cloud_cover"]


def sb_get(table, params):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}",
                     headers=HEADERS, params=params)
    r.raise_for_status()
    return r.json()


def sb_upsert(table, records):
    del_resp = requests.delete(
        f"{SUPABASE_URL}/rest/v1/{table}?shop_id=eq.{SHOP_ID}",
        headers=HEADERS
    )
    print(f"Delete {table}: {del_resp.status_code}")

    for i in range(0, len(records), 200):
        batch = records[i:i+200]
        print(f"Inserting batch {i//200}, first record: {batch[0]}")
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers={**HEADERS, "Prefer": "return=minimal"},
            json=batch
        )
        print(f"Response: {r.status_code} {r.text[:300]}")
        if r.status_code not in (200, 201):
            raise Exception(f"Upsert {table} failed: {r.status_code} {r.text}")


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
    # Rename week to date — bar data is already daily rows
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
        return pd.DataFrame(columns=["week","rfm_1kg","rfm_200g","rfb_1kg",
                                      "rfb_200g","rff_1kg","rff_200g","ft_scoop"])
    df["week"] = pd.to_datetime(df["week"])
    for c in ["rfm_1kg","rfm_200g","rfb_1kg","rfb_200g",
              "rff_1kg","rff_200g","ft_scoop"]:
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
        raise Exception("No weather data found - please populate the weather table")
    df["date"] = pd.to_datetime(df["date"])
    for c in ["temp","rainfall","cloud_cover","sunrise"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def bar_to_daily(bar_df, weather_df):
    """Bar data is already daily — just merge weather and add features."""
    daily = bar_df.copy()
    daily = daily.merge(weather_df, on="date", how="left")

    for c in ["temp","rainfall","cloud_cover","sunrise"]:
        daily[c] = daily[c].ffill().fillna(weather_df[c].median())

    daily["dow"]        = daily["date"].dt.dayofweek
    daily["day_index"]  = range(len(daily))
    daily["is_holiday"] = daily["date"].isin(BANK_HOLIDAYS).astype(int)
    return daily


def weekly_to_daily(weekly_df, value_cols, weather_df):
    """Retail data is weekly — expand to daily rows."""
    records = []
    for _, row in weekly_df.iterrows():
        week_start = row["week"]
        for d in range(5):
            day = week_start + timedelta(days=d)
            rec = {"date": day}
            for c in value_cols:
                rec[c] = row[c] / 5.0
            records.append(rec)

    daily = pd.DataFrame(records)
    daily["date"] = pd.to_datetime(daily["date"])
    daily = daily.merge(weather_df, on="date", how="left")

    for c in ["temp","rainfall","cloud_cover","sunrise"]:
        daily[c] = daily[c].ffill().fillna(weather_df[c].median())

    daily["dow"]        = daily["date"].dt.dayofweek
    daily["day_index"]  = range(len(daily))
    daily["is_holiday"] = daily["date"].isin(BANK_HOLIDAYS).astype(int)
    return daily


def build_future(weather_df, last_index, last_date, n_days=90):
    future_dates = []
    d = last_date + timedelta(days=1)
    while len(future_dates) < n_days:
        if d.weekday() < 5:
            future_dates.append(d)
        d += timedelta(days=1)

    future = pd.DataFrame({"date": pd.to_datetime(future_dates)})
    future = future.merge(weather_df, on="date", how="left")
    for c in ["temp","rainfall","cloud_cover","sunrise"]:
        future[c] = future[c].ffill().fillna(weather_df[c].median())

    future["dow"]        = future["date"].dt.dayofweek
    future["day_index"]  = range(last_index + 1, last_index + 1 + len(future))
    future["is_holiday"] = future["date"].isin(BANK_HOLIDAYS).astype(int)
    return future


def train_and_forecast(daily, future, target_col):
    training = daily[daily[target_col].notna()].copy()
    print(f"  {target_col}: {len(training)} training rows", end="")

    t = training["day_index"].values
    y = training[target_col].values
    coef     = np.polyfit(t, y, deg=2)
    trend_fn = np.poly1d(coef)

    training = training.copy()
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
    print(f", uplift={uplift:.3f}" + (f", OOB R2={oob:.3f}" if oob else ""))

    future = future.copy()
    future["trend"]    = trend_fn(future["day_index"].values)
    future["residual"] = model.predict(future[FEATURE_COLS])
    future["forecast"] = (future["trend"] + future["residual"]) * uplift
    future["forecast"] = future["forecast"].clip(lower=0).round(2)
    future.loc[future["is_holiday"] == 1, "forecast"] = 0

    return future[["date","forecast"]].set_index("date")["forecast"]


def run_bar_models(bar_sales, weather):
    print(f"\n-- Bar models (shop={SHOP_ID}) --")
    daily  = bar_to_daily(bar_sales, weather)
    future = build_future(weather, daily["day_index"].max(), daily["date"].max())

    results = {ct: train_and_forecast(daily, future, ct)
               for ct in ["rfm","rfb","rff","rfd"]}

    records = []
    for d in results["rfm"].index:
        records.append({
            "shop_id":       SHOP_ID,
            "week":          d.strftime("%Y-%m-%d"),
            "forecast_type": "bar",
            "rfm":  float(results["rfm"].get(d, 0)),
            "rfb":  float(results["rfb"].get(d, 0)),
            "rff":  float(results["rff"].get(d, 0)),
            "rfd":  float(results["rfd"].get(d, 0)),
            "updated_at": datetime.utcnow().isoformat(),
        })
    return records


def run_retail_models(retail_sales, weather):
    print(f"\n-- Retail models (shop={SHOP_ID}) --")

    if retail_sales.empty or len(retail_sales) < 5:
        print("  Not enough retail data - skipping retail forecasts")
        return []

    rs = retail_sales.copy()
    rs["rfm_total"] = rs["rfm_1kg"] + rs["rfm_200g"]
    rs["rfb_total"] = rs["rfb_1kg"] + rs["rfb_200g"]
    rs["rff_total"] = rs["rff_1kg"] + rs["rff_200g"]
    rs["fts_total"] = rs["ft_scoop"]

    total_cols = ["rfm_total","rfb_total","rff_total","fts_total"]
    daily  = weekly_to_daily(rs, total_cols, weather)
    future = build_future(weather, daily["day_index"].max(), daily["date"].max())

    results = {ct: train_and_forecast(daily, future, ct) for ct in total_cols}

    def size_ratio(a, b):
        total = retail_sales[a].sum() + retail_sales[b].sum()
        return retail_sales[a].sum() / total if total > 0 else 0.5

    rfm_r = size_ratio("rfm_1kg", "rfm_200g")
    rfb_r = size_ratio("rfb_1kg", "rfb_200g")
    rff_r = size_ratio("rff_1kg", "rff_200g")

    records = []
    for d in results["rfm_total"].index:
        rfm = float(results["rfm_total"].get(d, 0))
        rfb = float(results["rfb_total"].get(d, 0))
        rff = float(results["rff_total"].get(d, 0))
        fts = float(results["fts_total"].get(d, 0))
        records.append({
            "shop_id":       SHOP_ID,
            "week":          d.strftime("%Y-%m-%d"),
            "forecast_type": "retail",
            "rfm_1kg":  round(rfm * rfm_r,       2),
            "rfm_200g": round(rfm * (1 - rfm_r), 2),
            "rfb_1kg":  round(rfb * rfb_r,       2),
            "rfb_200g": round(rfb * (1 - rfb_r), 2),
            "rff_1kg":  round(rff * rff_r,       2),
            "rff_200g": round(rff * (1 - rff_r), 2),
            "ft_scoop": round(fts,               2),
            "updated_at": datetime.utcnow().isoformat(),
        })
    return records


def main():
    print(f"=== Forecast job={JOB_ID} shop={SHOP_ID} started {datetime.utcnow().isoformat()} ===")
    update_job("running")

    try:
        print("Fetching data...")
        bar_sales    = fetch_bar_sales()
        retail_sales = fetch_retail_sales()
        weather      = fetch_weather()
        print(f"  Bar: {len(bar_sales)} days | Retail: {len(retail_sales)} weeks | Weather: {len(weather)} days")

        bar_recs    = run_bar_models(bar_sales, weather)
        retail_recs = run_retail_models(retail_sales, weather)

        print(f"\nUpserting {len(bar_recs)} bar + {len(retail_recs)} retail forecast rows...")
        if bar_recs:
            sb_upsert("forecasts", bar_recs)
        if retail_recs:
            sb_upsert("forecasts", retail_recs)

        update_job("done")
        print(f"=== Complete {datetime.utcnow().isoformat()} ===")

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        update_job("error", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
