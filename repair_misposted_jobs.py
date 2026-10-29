"""Find VMS jobs that were posted with wrong details and repost them from Nexus.

A job is a repair candidate when it is open on the VMS and its title is one of
--bad-titles (default: "Travel", the generic title wrongly-sourced jobs carry).
The job id is the only key used to match VMS and Nexus.

Per candidate:
  * On the Nexus OPEN search (and not LOCUM): confirmed. Reposted with the
    correct title, description and other details from Nexus. The VMS stores
    jobs by job id, so this overwrites the record in place - no delete, no
    duplicate, and submissions that reference the job keep working.
  * Not on the Nexus OPEN search: cannot be verified, so it is only reported.
    With --close-unverifiable it is closed, which deletes it from the VMS.

Dry run unless --apply is passed. Reposts are sent with notifications off.
Reposting refreshes the job's posted date, since the VMS stamps it on every write.

    python repair_misposted_jobs.py                  # dry run
    python repair_misposted_jobs.py --apply
    python repair_misposted_jobs.py --date 2026-10-28 --apply
"""

import argparse
import asyncio
import csv
from collections import Counter
from typing import Any, Dict, List

import httpx

from api_nexus_oauth_jobs import (
    DEFAULT_ADMIN_EMAIL,
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_VMS_BASE_URL,
    TIMEOUT,
    build_vms_payload,
    chunk_list,
    close_jobs_on_vms,
    is_locum_job,
    login_to_vms,
    post_jobs_to_vms_bulk_with_retries,
    utc_now_iso,
)
from backfill_missing_job_descriptions import (
    NO_DESCRIPTION_PLACEHOLDER,
    fetch_all_nexus_jobs,
)

REPORT_CSV = "repair_misposted_jobs_report.csv"
REPORT_FIELDS = ["job_id", "vms_title", "nexus_title", "description_missing", "result", "detail"]
POST_BATCH_SIZE = 25


async def run(args: argparse.Namespace) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    bad_titles = {t.strip().lower() for t in args.bad_titles}

    def record(job_id: str, vms_title: str, nexus_title: str, desc_missing: bool, result: str, detail: str = "") -> None:
        rows.append({
            "job_id": job_id, "vms_title": vms_title, "nexus_title": nexus_title,
            "description_missing": desc_missing, "result": result, "detail": detail,
        })
        print(f"[{result}] {job_id}" + (f" - {detail}" if detail else ""))

    async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT)) as client:
        token = await login_to_vms(client, args.vms_base_url, args.admin_email, args.admin_password)
        headers = {"Authorization": f"Bearer {token}"}
        base = args.vms_base_url.rstrip("/")

        response = await client.get(f"{base}/api/jobs", headers=headers)
        response.raise_for_status()
        vms_jobs = response.json().get("jobs", [])

        duplicate_ids = [i for i, n in Counter(str(j.get("id")) for j in vms_jobs).items() if n > 1]
        if duplicate_ids:
            print(f"WARNING: {len(duplicate_ids)} job id(s) already appear more than once on the VMS: {duplicate_ids[:10]}")

        candidates = [
            j for j in vms_jobs
            if (j.get("title") or "").strip().lower() in bad_titles
            and (not args.date or str(j.get("posted_date", ""))[:10] == args.date)
        ]
        print(f"VMS: {len(vms_jobs)} open job(s); {len(candidates)} candidate(s) with title in {sorted(bad_titles)}"
              + (f" posted on {args.date}" if args.date else "") + ".")
        if not candidates:
            return rows

        nexus_by_id = await fetch_all_nexus_jobs(client)
        print(f"Nexus: {len(nexus_by_id)} open job(s) fetched.")
        if not nexus_by_id:
            raise RuntimeError("Nexus returned no jobs; refusing to treat every candidate as unverifiable.")

        to_repost: List[Dict[str, str]] = []
        to_close: List[str] = []
        for vms_job in candidates:
            job_id = str(vms_job.get("id", ""))
            vms_title = vms_job.get("title") or ""
            desc_missing = not (vms_job.get("description") or "").strip() or \
                (vms_job.get("description") or "").strip() == NO_DESCRIPTION_PLACEHOLDER

            nexus_job = nexus_by_id.get(job_id)
            if nexus_job is None:
                if args.close_unverifiable:
                    to_close.append(job_id)
                    record(job_id, vms_title, "", desc_missing, "would_close" if not args.apply else "closing",
                           "not on Nexus OPEN search")
                else:
                    record(job_id, vms_title, "", desc_missing, "needs_attention",
                           "not on Nexus OPEN search; cannot verify (use --close-unverifiable to close)")
                continue
            if is_locum_job(nexus_job):
                record(job_id, vms_title, "", desc_missing, "needs_attention", "LOCUM job; the sync never posts these")
                continue

            payload = build_vms_payload(nexus_job)
            if payload["job_type"].strip().lower() in bad_titles:
                record(job_id, vms_title, payload["job_type"], desc_missing, "skipped",
                       "Nexus title is also generic; nothing to correct")
                continue
            to_repost.append(payload)
            record(job_id, vms_title, payload["job_type"], desc_missing,
                   "would_repost" if not args.apply else "reposting")

        if not args.apply:
            return rows

        by_id = {r["job_id"]: r for r in rows}
        for batch in chunk_list(to_repost, POST_BATCH_SIZE):
            try:
                result = await post_jobs_to_vms_bulk_with_retries(
                    client, args.vms_base_url, batch, token=token, send_notifications=False
                )
            except Exception as exc:
                for payload in batch:
                    by_id[payload["job_id"]].update(result="failed", detail=f"bulk repost failed: {exc}")
                continue
            stored = set(result.get("stored_job_ids", []))
            errors = {i.get("job_id"): i.get("error") for i in result.get("failed", [])}
            for payload in batch:
                row = by_id[payload["job_id"]]
                if payload["job_id"] in stored:
                    row["result"] = "reposted"
                else:
                    row.update(result="failed", detail=errors.get(payload["job_id"], "not confirmed stored"))

        if to_close:
            try:
                result = await close_jobs_on_vms(client, args.vms_base_url, to_close, token=token)
                closed = set(result.get("closed", [])) | set(result.get("already_closed", []))
                for job_id in to_close:
                    by_id[job_id].update(result="closed" if job_id in closed else "failed",
                                         detail="" if job_id in closed else "not found or not closed")
            except Exception as exc:
                for job_id in to_close:
                    by_id[job_id].update(result="failed", detail=f"close failed: {exc}")

        # Verify: each reposted job now has the Nexus title, and ids are still unique.
        response = await client.get(f"{base}/api/jobs", headers=headers)
        response.raise_for_status()
        after = response.json().get("jobs", [])
        title_after = {str(j.get("id")): j.get("title") for j in after}
        for payload in to_repost:
            row = by_id[payload["job_id"]]
            if row["result"] == "reposted" and title_after.get(payload["job_id"]) != payload["job_type"]:
                row.update(result="failed", detail="title on VMS still differs after repost (cache lag?)")
        new_dupes = [i for i, n in Counter(str(j.get("id")) for j in after).items() if n > 1 and i not in duplicate_ids]
        if new_dupes:
            print(f"WARNING: repost created duplicate id(s): {new_dupes}")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Make the changes. Default is a dry run.")
    parser.add_argument("--date", help="Only consider jobs posted on this date, YYYY-MM-DD (default: any date).")
    parser.add_argument("--bad-titles", nargs="+", default=["Travel"],
                        help="VMS titles that mark a job as wrongly posted (default: Travel).")
    parser.add_argument("--close-unverifiable", action="store_true",
                        help="Also close candidates that are not on the Nexus OPEN search.")
    parser.add_argument("--report-csv", default=REPORT_CSV)
    parser.add_argument("--vms-base-url", default=DEFAULT_VMS_BASE_URL)
    parser.add_argument("--admin-email", default=DEFAULT_ADMIN_EMAIL)
    parser.add_argument("--admin-password", default=DEFAULT_ADMIN_PASSWORD)
    args = parser.parse_args()

    print(f"Repair started at {utc_now_iso()} ({'APPLY' if args.apply else 'DRY RUN'})")
    rows = asyncio.run(run(args))
    with open(args.report_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    counts = Counter(r["result"] for r in rows)
    print("\n===== Repair report =====")
    for result, n in sorted(counts.items()):
        print(f"{result:18} {n}")
    print(f"Full report: {args.report_csv}")
    if not args.apply and rows:
        print("Dry run only. Review the list, then re-run with --apply.")


if __name__ == "__main__":
    main()
