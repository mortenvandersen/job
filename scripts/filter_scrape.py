#!/usr/bin/env python3
"""
Layer 0 — Rule-based job scrape filter.
Zero LLM tokens. Reads a CSV from Apify scrape + filter_config.json,
applies keyword/rule filters, outputs:
  - scrapes/filtered.json  (jobs that passed — ready for Layer 1 triage)
  - scrapes/rejected.json  (jobs that failed — with rejection reasons for audit)
  - Updates pipeline.json and job_posts/inbox/ for passed jobs

Usage:
  python scripts/filter_scrape.py scrapes/latest.csv
  python scripts/filter_scrape.py scrapes/latest.csv --dry-run
"""

import csv
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "scripts" / "filter_config.json"
PIPELINE_PATH = REPO_ROOT / "pipeline.json"
INBOX_DIR = REPO_ROOT / "job_posts" / "inbox"
FILTERED_PATH = REPO_ROOT / "scrapes" / "filtered.json"
REJECTED_PATH = REPO_ROOT / "scrapes" / "rejected.json"


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_pipeline():
    if PIPELINE_PATH.exists():
        with open(PIPELINE_PATH) as f:
            return json.load(f)
    return {"last_updated": str(date.today()), "roles": []}


def save_pipeline(pipeline):
    with open(PIPELINE_PATH, "w") as f:
        json.dump(pipeline, f, indent=2, ensure_ascii=False)


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def parse_salary(salary_str):
    """Extract numeric salary values from a string. Returns (min, max) or (None, None)."""
    if not salary_str:
        return None, None
    # Find all dollar amounts (handle $200K, $200,000, $200000 formats)
    amounts = []
    for m in re.finditer(r"\$?([\d,]+(?:\.\d+)?)\s*[kK]?", salary_str):
        val = float(m.group(1).replace(",", ""))
        # Check if 'K' or 'k' follows
        end = m.end()
        if end < len(salary_str) and salary_str[end - 1] in "kK":
            val *= 1000
        elif val < 1000:
            val *= 1000  # Assume values like 200 mean 200K
        amounts.append(val)
    if not amounts:
        return None, None
    return min(amounts), max(amounts)


def check_title_allowlist(title, config):
    """Returns True if title matches at least one allowlist term."""
    title_lower = title.lower()
    for term in config["title_allowlist"]:
        if term in title_lower:
            return True
    return False


def apply_filters(row, config):
    """
    Apply all Layer 0 filters to a single row.
    Returns (passed: bool, reasons: list[str]).
    reasons is empty if passed, otherwise contains rejection reasons.
    """
    reasons = []
    title = (row.get("title") or "").strip()
    title_lower = title.lower()
    company = (row.get("companyName") or "").strip()
    description = (row.get("descriptionText") or "").strip()
    desc_lower = description.lower()
    location = (row.get("location") or "").strip()
    loc_lower = location.lower()
    salary = (row.get("salary") or "").strip()
    salary_info_0 = (row.get("salaryInfo/0") or "").strip()
    salary_info_1 = (row.get("salaryInfo/1") or "").strip()
    seniority = (row.get("seniorityLevel") or "").strip()
    sen_lower = seniority.lower()
    industries = (row.get("industries") or "").strip()
    ind_lower = industries.lower()
    company_desc = (row.get("companyDescription") or "").strip()
    comp_desc_lower = company_desc.lower()

    # --- 1. Title blocklist ---
    for term in config["title_blocklist"]:
        if term in title_lower:
            reasons.append(f"title_blocklist: '{term}' found in title")
            break

    # --- 2. Title allowlist (must match at least one) ---
    if not check_title_allowlist(title, config):
        # Also check job function field
        job_function = (row.get("jobFunction") or "").lower()
        if not any(term in job_function for term in config["title_allowlist"]):
            reasons.append("title_allowlist: no target role keyword found in title or jobFunction")

    # --- 3. Industry blocklist ---
    for term in config["industry_blocklist"]:
        if term in ind_lower or term in comp_desc_lower:
            reasons.append(f"industry_blocklist: '{term}' found")
            break

    # --- 4. Description blocklist (quota/commission) ---
    for term in config["description_blocklist"]:
        if term in desc_lower:
            reasons.append(f"description_blocklist: '{term}' found — likely sales/commission role")
            break

    # --- 5. Part-time / fractional ---
    for term in config["part_time_keywords"]:
        if term in title_lower or term in desc_lower[:500]:
            reasons.append(f"part_time: '{term}' found")
            break

    # --- 6. Location filter ---
    is_remote = any(kw in loc_lower or kw in title_lower for kw in config["remote_keywords"])
    if not is_remote:
        # Not remote — must be in an allowed hybrid location (SF Bay Area)
        is_allowed_location = any(loc in loc_lower for loc in config["location_allowlist_hybrid"])
        if not is_allowed_location and location:
            reasons.append(f"location_not_allowed: '{location}' is not remote and not in SF Bay Area")

    # --- 7. Seniority filter ---
    if sen_lower:
        for term in config["seniority_blocklist"]:
            if term in sen_lower:
                reasons.append(f"seniority_blocklist: '{term}' found in seniority level")
                break

    # --- 8. Salary floor ---
    combined_salary = " ".join(filter(None, [salary, salary_info_0, salary_info_1]))
    sal_min, sal_max = parse_salary(combined_salary)
    if sal_max is not None and sal_max < config["salary_floor_usd"]:
        reasons.append(f"salary_below_floor: max ${sal_max:,.0f} < ${config['salary_floor_usd']:,} floor")

    # --- 9. European hours ---
    for term in config["european_hours_keywords"]:
        if term in desc_lower or term in loc_lower:
            reasons.append(f"european_hours: '{term}' found")
            break

    return len(reasons) == 0, reasons


def create_inbox_file(row, slug, today):
    """Create a markdown file in job_posts/inbox/."""
    company = (row.get("companyName") or "").strip()
    title = (row.get("title") or "").strip()
    location = (row.get("location") or "").strip()
    salary = (row.get("salary") or "").strip()
    source_url = (row.get("link") or "").strip()
    description = (row.get("descriptionText") or "").strip()

    content = f"""---
company: {company}
role: {title}
status: inbox
date_saved: {today}
source_url: {source_url}
location: {location}
salary: {salary}
---

# {company} — {title}

## Job Description

{description}
"""
    filepath = INBOX_DIR / f"{slug}.md"
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w") as f:
        f.write(content)
    return str(filepath.relative_to(REPO_ROOT))


def create_pipeline_entry(row, slug, today):
    """Create a pipeline.json entry for a passed job."""
    description = (row.get("descriptionText") or "").strip()
    description_flat = " ".join(description.split())
    jd_snippet = description_flat[:600].rsplit(" ", 1)[0] + "…" if len(description_flat) > 600 else description_flat
    return {
        "id": slug,
        "company": (row.get("companyName") or "").strip(),
        "role": (row.get("title") or "").strip(),
        "location": (row.get("location") or "").strip(),
        "comp_range": (row.get("salary") or "").strip(),
        "date_saved": today,
        "date_assessed": None,
        "source_url": (row.get("link") or "").strip(),
        "status": "inbox",
        "role_fit_score": None,
        "candidate_fit_score": None,
        "recommendation": None,
        "file": f"job_posts/inbox/{slug}.md",
        "summary_note": "",
        "jd_snippet": jd_snippet,
        "gaps": [],
        "strongest_angles": [],
        "status_notes": "",
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/filter_scrape.py <csv_path> [--dry-run]")
        sys.exit(1)

    csv_path = Path(sys.argv[1])
    dry_run = "--dry-run" in sys.argv

    if not csv_path.exists():
        print(f"Error: CSV file not found: {csv_path}")
        sys.exit(1)

    config = load_config()
    pipeline = load_pipeline()
    existing_ids = {r["id"] for r in pipeline["roles"]}
    today = str(date.today())

    # Read CSV
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    print(f"Loaded {len(rows)} jobs from {csv_path}")

    passed = []
    rejected = []

    for row in rows:
        company = (row.get("companyName") or "").strip()
        title = (row.get("title") or "").strip()

        if not company or not title:
            rejected.append({
                "company": company,
                "title": title,
                "posted_at": (row.get("postedAt") or "")[:10],
                "reasons": ["missing_data: no company or title"],
            })
            continue

        ok, reasons = apply_filters(row, config)

        slug = f"{slugify(company)}-{slugify(title)}"

        if ok:
            # Skip if already in pipeline
            if slug in existing_ids:
                print(f"  SKIP (exists): {company} — {title}")
                continue

            passed.append({
                "company": company,
                "title": title,
                "slug": slug,
                "location": (row.get("location") or "").strip(),
                "salary": (row.get("salary") or "").strip(),
                "link": (row.get("link") or "").strip(),
                "seniority": (row.get("seniorityLevel") or "").strip(),
                "industries": (row.get("industries") or "").strip(),
                "_row": row,
            })
        else:
            rejected.append({
                "company": company,
                "title": title,
                "posted_at": (row.get("postedAt") or "")[:10],
                "reasons": reasons,
            })

    # Summary
    print(f"\n{'='*60}")
    print(f"RESULTS: {len(passed)} passed | {len(rejected)} rejected | {len(rows)} total")
    print(f"{'='*60}")

    if passed:
        print(f"\nPASSED ({len(passed)}):")
        for p in passed:
            print(f"  + {p['company']} — {p['title']}")

    if rejected:
        print(f"\nREJECTED ({len(rejected)}):")
        for r in rejected:
            print(f"  - {r['company']} — {r['title']}")
            for reason in r["reasons"]:
                print(f"      {reason}")

    # Save results
    passed_output = [{k: v for k, v in p.items() if k != "_row"} for p in passed]

    FILTERED_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(FILTERED_PATH, "w") as f:
        json.dump(passed_output, f, indent=2, ensure_ascii=False)
    with open(REJECTED_PATH, "w") as f:
        json.dump(rejected, f, indent=2, ensure_ascii=False)

    print(f"\nSaved: {FILTERED_PATH}")
    print(f"Saved: {REJECTED_PATH}")

    if dry_run:
        print("\n--dry-run: skipping pipeline and inbox file updates.")
        return

    # Create inbox files and update pipeline
    new_entries = []
    for p in passed:
        row = p["_row"]
        slug = p["slug"]
        filepath = create_inbox_file(row, slug, today)
        entry = create_pipeline_entry(row, slug, today)
        new_entries.append(entry)
        print(f"  Created: {filepath}")

    if new_entries:
        pipeline["roles"] = new_entries + pipeline["roles"]
        pipeline["last_updated"] = today
        save_pipeline(pipeline)
        print(f"\nUpdated pipeline.json with {len(new_entries)} new inbox entries.")
    else:
        print("\nNo new entries to add to pipeline.")


if __name__ == "__main__":
    main()
