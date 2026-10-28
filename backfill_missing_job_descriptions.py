"""Backfill missing job descriptions on the VMS from Nexus "Additional Information".

Finds VMS jobs posted on a given date whose description is empty (or the
"No description provided by Nexus." placeholder), looks the same job up on
Nexus by its job id, and fills the description from the job's own description,
its "Job Description" section, or its Need Specifics / Unit Specifics notes.

It is a dry run unless --apply is passed. Only the description is written (via
PUT /api/admin/jobs/{id}/description), and only when the stored description is
still missing, so posted dates, statuses and notifications are untouched and
real descriptions are never overwritten.

Examples:
    python backfill_missing_job_descriptions.py                    # dry run, today
    python backfill_missing_job_descriptions.py --date 2026-10-27  # dry run, another day
    python backfill_missing_job_descriptions.py --apply            # write the changes
"""

import argparse
import asyncio
import csv
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import httpx

from api_nexus_oauth_jobs import (
    DEFAULT_ADMIN_EMAIL,
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_VMS_BASE_URL,
    PAGE_SIZE,
    TIMEOUT,
    build_vms_payload,
    clean_text_block,
    extract_description_from_additional_information,
    extract_specifics_from_additional_information,
    fetch_jobs_page,
    fetch_oauth_token,
    get_job_id,
    login_to_vms,
    utc_now_iso,
)

NO_DESCRIPTION_PLACEHOLDER = "No description provided by Nexus."
REPORT_CSV = "backfill_missing_descriptions_report.csv"
REPORT_FIELDS = ["job_id", "title", "posted_date", "result", "source", "detail"]


def is_missing_description(value: Any) -> bool:
    text = (value or "").strip()
    return not text or text == NO_DESCRIPTION_PLACEHOLDER


def recover_description(nexus_job: Dict[str, Any]) -> Optional[tuple]:
    """Return (description, source) from a Nexus job, or None if nothing usable.

    Deliberately stricter than the sync's last-resort fallback: the whole
    internal-notes blob is never used, only recognisable description content.
    """
    own = clean_text_block(nexus_job.get("description"))
    if own:
        return own, "description"
    additional = nexus_job.get("additionalInformation")
    section = extract_description_from_additional_information(additional)
    if section:
        return section, "additionalInformation:job description"
    specifics = extract_specifics_from_additional_information(additional)
    if specifics:
        return specifics, "additionalInformation:specifics"
    return None


async def fetch_all_nexus_jobs(client: httpx.AsyncClient) -> Dict[str, Dict[str, Any]]:
    token = (await fetch_oauth_token(client))["access_token"]
    by_id: Dict[str, Dict[str, Any]] = {}
    start = 0
    while True:
        records, total = await fetch_jobs_page(client, token, start)
        if not records:
            break
        for job in records:
            job_id = get_job_id(job)
            if job_id:
                by_id[job_id] = job
        if total and start + len(records) >= total:
            break
        start += PAGE_SIZE
    return by_id


async def run(args: argparse.Namespace) -> List[Dict[str, Any]]:
    target_date = args.date
    rows: List[Dict[str, Any]] = []

    def record(job_id: str, title: str, posted: str, result: str, source: str = "", detail: str = "") -> None:
        rows.append({
            "job_id": job_id, "title": title, "posted_date": posted,
            "result": result, "source": source, "detail": detail,
        })
        print(f"[{result}] {job_id} {('- ' + detail) if detail else ''}".rstrip())

    async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT)) as client:
        vms_token = await login_to_vms(client, args.vms_base_url, args.admin_email, args.admin_password)
        headers = {"Authorization": f"Bearer {vms_token}"}
        base = args.vms_base_url.rstrip("/")

        response = await client.get(f"{base}/api/jobs", headers=headers)
        response.raise_for_status()
        vms_jobs = response.json().get("jobs", [])
        todays = [j for j in vms_jobs if str(j.get("posted_date", ""))[:10] == target_date]
        candidates = [j for j in todays if is_missing_description(j.get("description"))]
        print(
            f"VMS: {len(vms_jobs)} open job(s), {len(todays)} posted on {target_date}, "
            f"{len(candidates)} with a missing description."
        )
        if not candidates:
            return rows

        nexus_by_id = await fetch_all_nexus_jobs(client)
        print(f"Nexus: {len(nexus_by_id)} open job(s) fetched.")

        for vms_job in candidates:
            job_id = str(vms_job.get("id", ""))
            title = vms_job.get("title") or ""
            posted = str(vms_job.get("posted_date", ""))

            # The list endpoint only carries a preview; confirm against the full job.
            detail_resp = await client.get(f"{base}/api/jobs/{job_id}", headers=headers)
            if detail_resp.status_code == 200 and not is_missing_description(detail_resp.json().get("description")):
                record(job_id, title, posted, "skipped_has_description")
                continue

            nexus_job = nexus_by_id.get(job_id)
            if nexus_job is None:
                record(job_id, title, posted, "needs_attention", detail="not in the Nexus OPEN search")
                continue
            # Stable-id match plus a title cross-check before touching anything.
            expected_title = build_vms_payload(nexus_job)["job_type"]
            if expected_title.strip() != title.strip():
                record(job_id, title, posted, "needs_attention",
                       detail=f"identity mismatch: VMS title {title!r} vs Nexus {expected_title!r}")
                continue

            recovered = recover_description(nexus_job)
            if recovered is None:
                record(job_id, title, posted, "needs_attention", detail="no usable description on Nexus")
                continue
            description, source = recovered

            if not args.apply:
                preview = description.replace("\n", " ")[:120]
                record(job_id, title, posted, "would_update", source, preview)
                continue

            put = await client.put(
                f"{base}/api/admin/jobs/{job_id}/description",
                headers=headers,
                json={"description": description},
            )
            if put.status_code == 200:
                record(job_id, title, posted, put.json().get("result", "updated"), source)
            else:
                record(job_id, title, posted, "failed", source, f"HTTP {put.status_code}: {put.text[:200]}")
    return rows


def write_report(rows: List[Dict[str, Any]], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def print_summary(rows: List[Dict[str, Any]], apply: bool, target_date: str, path: str) -> None:
    def count(*results: str) -> int:
        return sum(1 for r in rows if r["result"] in results)

    recovered = count("updated", "would_update")
    print("\n===== Backfill report =====")
    print(f"Mode:                    {'APPLY' if apply else 'DRY RUN (nothing written)'}")
    print(f"Date:                    {target_date}")
    print(f"Jobs checked:            {len(rows)}")
    print(f"Descriptions recovered:  {recovered}" + ("" if apply else " (proposed)"))
    print(f"Already had one:         {count('skipped_has_description', 'already_has_description')}")
    print(f"Failed:                  {count('failed')}")
    needs = [r for r in rows if r["result"] == "needs_attention"]
    print(f"Still need attention:    {len(needs)}")
    for r in needs:
        print(f"  - {r['job_id']}: {r['detail']}")
    print(f"Full report: {path}")
    if not apply and recovered:
        print("Review the proposals above, then re-run with --apply.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", default=date.today().isoformat(),
                        help="Posted date to repair, YYYY-MM-DD (default: today).")
    parser.add_argument("--apply", action="store_true",
                        help="Write the descriptions. Without it, only a dry run is performed.")
    parser.add_argument("--report-csv", default=REPORT_CSV)
    parser.add_argument("--vms-base-url", default=DEFAULT_VMS_BASE_URL)
    parser.add_argument("--admin-email", default=DEFAULT_ADMIN_EMAIL)
    parser.add_argument("--admin-password", default=DEFAULT_ADMIN_PASSWORD)
    args = parser.parse_args()
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        parser.error("--date must be YYYY-MM-DD")
    return args


def main() -> None:
    args = parse_args()
    print(f"Backfill started at {utc_now_iso()} ({'APPLY' if args.apply else 'dry run'}, date {args.date})")
    rows = asyncio.run(run(args))
    write_report(rows, args.report_csv)
    print_summary(rows, args.apply, args.date, args.report_csv)


if __name__ == "__main__":
    main()
