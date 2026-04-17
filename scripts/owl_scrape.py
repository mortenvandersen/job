#!/usr/bin/env python3
"""
Owl portfolio companies — job scraper.

Initial run: scrapes all companies, saves JSON snapshots as baseline.
Weekly run: scrapes and diffs against snapshots, reports new postings.

Usage:
    pip install firecrawl-py python-dotenv
    export FIRECRAWL_API_KEY=fc-...   # or put in .env

    python scripts/owl_scrape.py                     # scrape all
    python scripts/owl_scrape.py --company Newsela   # single company
    python scripts/owl_scrape.py --diff              # weekly: new jobs only
"""

import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from firecrawl import FirecrawlApp

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = REPO_ROOT / "Owl Companies - Owl Full List (1).csv"
SNAPSHOTS_DIR = REPO_ROOT / "scrapes" / "owl_snapshots"

JOB_SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "department": {"type": "string"},
                    "location": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": ["title"],
            },
        }
    },
    "required": ["jobs"],
}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def scrape_company(app: FirecrawlApp, company: str, url: str) -> dict:
    result = app.scrape_url(
        url,
        params={
            "formats": ["extract"],
            "extract": {"schema": JOB_SCHEMA},
        },
    )
    jobs = (result.get("extract") or {}).get("jobs") or []
    return {
        "company": company,
        "url": url,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "job_count": len(jobs),
        "jobs": jobs,
    }


def load_companies(filter_name: str | None = None) -> list[dict]:
    companies = []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            company = row.get("Company", "").strip()
            url = row.get("Careers Page URL", "").strip()
            if company and url and url.startswith("http"):
                companies.append({"company": company, "url": url})
    if filter_name:
        companies = [c for c in companies if filter_name.lower() in c["company"].lower()]
    return companies


def diff_jobs(old_jobs: list[dict], new_jobs: list[dict]) -> list[dict]:
    old_titles = {j.get("title", "").strip().lower() for j in old_jobs}
    return [j for j in new_jobs if j.get("title", "").strip().lower() not in old_titles]


def run_scrape(companies: list[dict], diff_mode: bool) -> None:
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        print("Error: FIRECRAWL_API_KEY not set")
        sys.exit(1)

    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    app = FirecrawlApp(api_key=api_key)

    print(f"{'Diff check' if diff_mode else 'Initial scrape'} — {len(companies)} companies\n")

    errors = []
    all_new_jobs = []

    for i, entry in enumerate(companies, 1):
        company = entry["company"]
        url = entry["url"]
        snapshot_path = SNAPSHOTS_DIR / f"{slug(company)}.json"

        print(f"[{i}/{len(companies)}] {company}")
        try:
            snapshot = scrape_company(app, company, url)

            if diff_mode and snapshot_path.exists():
                previous = json.loads(snapshot_path.read_text())
                new_jobs = diff_jobs(previous.get("jobs", []), snapshot["jobs"])
                if new_jobs:
                    print(f"  -> {len(new_jobs)} NEW job(s):")
                    for j in new_jobs:
                        loc = f" [{j['location']}]" if j.get("location") else ""
                        print(f"     • {j['title']}{loc}")
                        all_new_jobs.append({**j, "company": company, "careers_url": url})
                else:
                    print(f"  -> no new jobs ({snapshot['job_count']} total)")
            else:
                print(f"  -> {snapshot['job_count']} jobs found")

            snapshot_path.write_text(json.dumps(snapshot, indent=2))

        except Exception as e:
            print(f"  -> ERROR: {e}")
            errors.append({"company": company, "url": url, "error": str(e)})

        if i < len(companies):
            time.sleep(1)

    print(f"\n{'—' * 50}")
    if diff_mode:
        print(f"{len(all_new_jobs)} new posting(s) found across {len(companies) - len(errors)} companies.")
        if all_new_jobs:
            out_path = REPO_ROOT / "scrapes" / "owl_new_jobs.json"
            out_path.write_text(json.dumps(all_new_jobs, indent=2))
            print(f"Saved to {out_path.relative_to(REPO_ROOT)}")
    else:
        print(f"Baseline snapshots saved to scrapes/owl_snapshots/")

    if errors:
        print(f"\n{len(errors)} failed:")
        for e in errors:
            print(f"  {e['company']}: {e['error']}")


def main() -> None:
    diff_mode = "--diff" in sys.argv
    filter_name = None
    if "--company" in sys.argv:
        idx = sys.argv.index("--company")
        filter_name = sys.argv[idx + 1]

    companies = load_companies(filter_name)
    if not companies:
        print("No companies found.")
        sys.exit(1)

    run_scrape(companies, diff_mode)


if __name__ == "__main__":
    main()
