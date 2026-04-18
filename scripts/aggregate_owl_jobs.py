#!/usr/bin/env python3
"""Aggregate per-company Owl snapshots into a single flat list.

Reads all `scrapes/owl_snapshots/*.json` files, merges them into
`scrapes/owl_jobs.json` (a flat array of jobs), and embeds the same
array into `index.html` as the `OWL_DATA` constant so the dashboard
has the data without an extra network fetch.

First-seen tracking: if `scrapes/owl_jobs.json` already exists, the
`first_seen` timestamp for an existing job (matched by url, or by
company+title when url is missing) is preserved. Otherwise
`first_seen` is set to the snapshot's `scraped_at`.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS_DIR = REPO_ROOT / "scrapes" / "owl_snapshots"
OUTPUT_PATH = REPO_ROOT / "scrapes" / "owl_jobs.json"
INDEX_HTML = REPO_ROOT / "index.html"


def _job_key(job: dict) -> str:
    url = (job.get("url") or "").strip()
    if url:
        return f"url:{url}"
    return f"ct:{(job.get('company') or '').lower()}|{(job.get('title') or '').lower()}"


def load_existing_first_seen() -> dict:
    if not OUTPUT_PATH.exists():
        return {}
    try:
        existing = json.loads(OUTPUT_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return {_job_key(j): j.get("first_seen") for j in existing if j.get("first_seen")}


def aggregate() -> list:
    prior_first_seen = load_existing_first_seen()
    now_iso = datetime.now(timezone.utc).isoformat()

    jobs: list[dict] = []
    for path in sorted(SNAPSHOTS_DIR.glob("*.json")):
        try:
            snap = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"skip {path.name}: {e}")
            continue
        company = snap.get("company") or path.stem
        scraped_at = snap.get("scraped_at") or now_iso
        for j in snap.get("jobs", []):
            entry = {
                "company": company,
                "title": j.get("title") or "",
                "department": j.get("department"),
                "location": j.get("location"),
                "url": j.get("url"),
                "scraped_at": scraped_at,
            }
            key = _job_key({**entry, "company": company})
            entry["first_seen"] = prior_first_seen.get(key, scraped_at)
            jobs.append(entry)

    jobs.sort(key=lambda j: (j["company"].lower(), j["title"].lower()))
    return jobs


def write_output(jobs: list) -> None:
    OUTPUT_PATH.write_text(json.dumps(jobs, indent=2) + "\n")
    print(f"wrote {len(jobs)} jobs to {OUTPUT_PATH.relative_to(REPO_ROOT)}")


def update_index_html(jobs: list) -> None:
    if not INDEX_HTML.exists():
        print("index.html not found; skipping embed")
        return
    html = INDEX_HTML.read_text()
    payload = "const OWL_DATA = " + json.dumps(jobs) + ";"

    pattern = re.compile(r"^const OWL_DATA = .+$", re.MULTILINE)
    if pattern.search(html):
        new_html = pattern.sub(lambda _m: payload, html, count=1)
    else:
        # Insert right after PIPELINE_DATA line so it lives with the other embedded data.
        pipeline_pat = re.compile(r"^(const PIPELINE_DATA = .+)$", re.MULTILINE)
        m = pipeline_pat.search(html)
        if not m:
            print("could not locate PIPELINE_DATA; skipping embed")
            return
        new_html = html[: m.end()] + "\n" + payload + html[m.end() :]

    if new_html != html:
        INDEX_HTML.write_text(new_html)
        print(f"embedded OWL_DATA ({len(jobs)} jobs) into index.html")


def main() -> None:
    if not SNAPSHOTS_DIR.exists():
        print(f"no snapshots dir at {SNAPSHOTS_DIR}")
        return
    jobs = aggregate()
    write_output(jobs)
    update_index_html(jobs)


if __name__ == "__main__":
    main()
