"""
forecast_trigger.py
Runs every Monday at 06:00 UTC after Square and weather syncs.
Creates a forecast job for every shop and triggers the GitHub Actions
forecast model for each one, then waits for all to complete.

Required environment variables:
  SUPABASE_URL         - Supabase project URL
  SUPABASE_SERVICE_KEY - Supabase service role key
  GITHUB_TOKEN_PAT     - GitHub Personal Access Token
  GITHUB_REPO          - e.g. "yourusername/Coffee-Orderer"
"""

import os
import sys
import time
import requests
from datetime import datetime, timezone

SUPABASE_URL         = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
GITHUB_TOKEN         = os.environ["GITHUB_TOKEN_PAT"]
GITHUB_REPO          = os.environ["GITHUB_REPO"]

SUPABASE_HEADERS = {
    "apikey":        SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type":  "application/json",
}

GITHUB_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept":        "application/vnd.github+json",
    "Content-Type":  "application/json",
}

# Shop ID -> has_saturday flag
SHOPS = {
    "LDW": False,
    "QVS": True,
    "CAS": False,
    "T42": False,
    "TRE": False,
    "LEM": False,
    "LUC": True,
    "FSS": False,
    "LSS": False,
}


def create_job(shop_id: str) -> str:
    """Create a forecast job in Supabase and return the job ID."""
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/forecast_jobs",
        headers={**SUPABASE_HEADERS, "Prefer": "return=representation"},
        json=[{"shop_id": shop_id, "status": "pending"}]
    )
    if r.status_code not in (200, 201):
        raise Exception(f"Failed to create job for {shop_id}: {r.status_code} {r.text}")
    data = r.json()
    job_id = data[0]["id"] if isinstance(data, list) else data["id"]
    print(f"  Created job {job_id} for {shop_id}", flush=True)
    return job_id


def trigger_forecast(shop_id: str, job_id: str, has_saturday: bool):
    """Trigger GitHub Actions forecast workflow for a shop."""
    r = requests.post(
        f"https://api.github.com/repos/{GITHUB_REPO}/dispatches",
        headers=GITHUB_HEADERS,
        json={
            "event_type": "run-forecast",
            "client_payload": {
                "shop_id":      shop_id,
                "job_id":       job_id,
                "has_saturday": "true" if has_saturday else "false",
            }
        }
    )
    if r.status_code != 204:
        raise Exception(f"Failed to trigger forecast for {shop_id}: {r.status_code} {r.text}")
    print(f"  Triggered forecast for {shop_id}", flush=True)


def poll_job(job_id: str, timeout_secs: int = 300) -> str:
    """Poll until job is done or error. Returns final status."""
    start = time.time()
    while time.time() - start < timeout_secs:
        time.sleep(5)
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/forecast_jobs?id=eq.{job_id}&select=status,message",
            headers=SUPABASE_HEADERS
        )
        if r.status_code == 200:
            data = r.json()
            if data:
                status = data[0].get("status", "")
                if status in ("done", "error"):
                    return status
    return "timeout"


def main():
    print(f"=== Forecast trigger started {datetime.now(timezone.utc).isoformat()} ===", flush=True)
    print(f"  Triggering forecasts for {len(SHOPS)} shops", flush=True)

    jobs = {}  # shop_id -> job_id

    # Create jobs and trigger forecasts for all shops
    for shop_id, has_saturday in SHOPS.items():
        try:
            job_id = create_job(shop_id)
            trigger_forecast(shop_id, job_id, has_saturday)
            jobs[shop_id] = job_id
            time.sleep(2)  # Stagger triggers slightly
        except Exception as e:
            print(f"  ERROR triggering {shop_id}: {e}", flush=True)

    if not jobs:
        print("ERROR: No jobs triggered", file=sys.stderr)
        sys.exit(1)

    # Poll all jobs until complete
    print(f"\n  Polling {len(jobs)} jobs (timeout 5 min each)...", flush=True)
    results = {}
    for shop_id, job_id in jobs.items():
        print(f"  Waiting for {shop_id}...", flush=True)
        status = poll_job(job_id)
        results[shop_id] = status
        print(f"  {shop_id}: {status}", flush=True)

    # Summary
    done    = [s for s, r in results.items() if r == "done"]
    errors  = [s for s, r in results.items() if r == "error"]
    timeouts = [s for s, r in results.items() if r == "timeout"]

    print(f"\n=== Complete {datetime.now(timezone.utc).isoformat()} ===", flush=True)
    print(f"  Done:     {done}", flush=True)
    print(f"  Errors:   {errors}", flush=True)
    print(f"  Timeouts: {timeouts}", flush=True)

    if errors or timeouts:
        sys.exit(1)


if __name__ == "__main__":
    main()
