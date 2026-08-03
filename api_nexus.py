import argparse
import json
import os
from typing import Any, Dict, List, Optional

import pandas as pd
import requests


BASE_URL = "https://api-nexus.laboredge.com/api/leap-service/v1/unsecured"
ORGANIZATION_ID = 491
OFFERING_ID = "ADVANCE_PRACTICE"
LIST_URL = f"{BASE_URL}/jobboard/organization/{ORGANIZATION_ID}?offeringId={OFFERING_ID}"
DETAIL_URL_TEMPLATE = (
    f"{BASE_URL}/jobboard/organization/{ORGANIZATION_ID}/job/{{job_id}}"
    f"?offeringId={OFFERING_ID}"
)

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://nexus-leap.laboredge.com",
    "Referer": "https://nexus-leap.laboredge.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0"
    ),
}
# python submit_candidate_client.py --email virinchi@radixsol.com --password virinchi@321 --csv candidate_submission_template.csv --base-url https://radixsolvms.com/
PAGE_SIZE = 50
TIMEOUT = 30
OUTPUT_CSV = "jobs.csv"


def build_payload(start: int) -> Dict[str, Any]:
    return {
        "professionIds": [],
        "countryId": 370,
        "specialtyIds": None,
        "stateCodes": None,
        "jobTypeIds": ["LOCAL", "LOCUM", "PERM", "TRAVEL"],
        "startDate": None,
        "assignmentDuration": None,
        "weeklyPayRange": None,
        "filterByType": None,
        "compactAll": None,
        "featured": None,
        "hotJob": None,
        "openJobFilter": None,
        "pagingSortingDetails": {
            "start": start,
            "maxRowsToFetch": PAGE_SIZE,
            "sortField": "clientName",
            "sortOrder": -1,
        },
        "exclusive": False,
    }


def fetch_job_summaries() -> List[Dict[str, Any]]:
    all_jobs: List[Dict[str, Any]] = []
    start = 0

    while True:
        response = requests.post(
            LIST_URL,
            headers=HEADERS,
            json=build_payload(start),
            timeout=TIMEOUT,
        )
        response.raise_for_status()

        data = response.json()
        records = data.get("records", [])
        if not records:
            break

        all_jobs.extend(records)
        print(f"Fetched {len(records)} summary records (Total: {len(all_jobs)})")

        total_count = data.get("count", len(all_jobs))
        if len(all_jobs) >= total_count:
            break

        start += PAGE_SIZE

    return all_jobs


def fetch_job_detail(job_id: Any) -> Dict[str, Any]:
    response = requests.get(
        DETAIL_URL_TEMPLATE.format(job_id=job_id),
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def flatten_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return json.dumps(value, ensure_ascii=False)


def flatten_record(
    data: Dict[str, Any],
    parent_key: str = "",
    sep: str = ".",
) -> Dict[str, Any]:
    items: Dict[str, Any] = {}

    for key, value in data.items():
        new_key = f"{parent_key}{sep}{key}" if parent_key else key
        if isinstance(value, dict):
            items.update(flatten_record(value, new_key, sep=sep))
        else:
            items[new_key] = flatten_value(value)

    return items


def merge_job_data(summary_job: Dict[str, Any], detail_job: Dict[str, Any]) -> Dict[str, Any]:
    merged = {}
    merged.update(flatten_record(summary_job, parent_key="summary"))
    merged.update(flatten_record(detail_job, parent_key="detail"))
    return merged


def first_non_empty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
            continue
        if isinstance(value, (int, float, bool)):
            return str(value)
    return ""


def coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def build_description_summary(summary_job: Dict[str, Any], detail_job: Dict[str, Any]) -> str:
    profession = first_non_empty(detail_job.get("profession"), summary_job.get("profession"))
    specialty = first_non_empty(detail_job.get("specialty"), summary_job.get("specialty"))
    client = first_non_empty(detail_job.get("clientName"), summary_job.get("clientName"))
    city = first_non_empty(detail_job.get("city"), summary_job.get("city"))
    state = first_non_empty(detail_job.get("state"), summary_job.get("state"))
    start_date = first_non_empty(detail_job.get("startDate"), summary_job.get("startDate"))
    end_date = first_non_empty(detail_job.get("endDate"), summary_job.get("endDate"))
    shift = first_non_empty(
        (detail_job.get("jobShiftDetails") or {}).get("shift_1_name")
        if isinstance(detail_job.get("jobShiftDetails"), dict)
        else None,
        summary_job.get("shiftName"),
        summary_job.get("shift"),
    )
    bill_rate = first_non_empty(detail_job.get("billRate"), summary_job.get("billRate"))

    lead_parts = [part for part in [profession, specialty] if part]
    lead = " - ".join(lead_parts) if lead_parts else first_non_empty(detail_job.get("jobType"), summary_job.get("jobType"))

    details = []
    if client:
        details.append(f"Client: {client}")
    if city or state:
        details.append(f"Location: {', '.join(part for part in [city, state] if part)}")
    if start_date or end_date:
        details.append(f"Dates: {' to '.join(part for part in [start_date, end_date] if part)}")
    if shift:
        details.append(f"Shift: {shift}")
    if bill_rate:
        details.append(f"Bill rate: {bill_rate}")

    if lead and details:
        return f"{lead}. {'; '.join(details)}."
    if details:
        return "; ".join(details) + "."
    if lead:
        return lead + "."
    return ""


def extract_description(summary_job: Dict[str, Any], detail_job: Dict[str, Any]) -> str:
    description = first_non_empty(
        detail_job.get("jobDescription"),
        detail_job.get("description"),
        detail_job.get("jobdescription"),
        summary_job.get("jobDescription"),
        summary_job.get("description"),
    )
    if description:
        return description

    summary = build_description_summary(summary_job, detail_job)
    if summary:
        return summary

    return "No description provided by Nexus."


def build_vms_payload(summary_job: Dict[str, Any], detail_job: Dict[str, Any]) -> Dict[str, str]:
    job_id = first_non_empty(
        detail_job.get("jobId"),
        detail_job.get("jobCode"),
        summary_job.get("jobId"),
        summary_job.get("jobCode"),
        summary_job.get("id"),
    )
    if not job_id:
        raise ValueError("Missing job identifier in Nexus payload")

    city = first_non_empty(
        detail_job.get("city"),
        detail_job.get("locationCity"),
        summary_job.get("city"),
        summary_job.get("locationCity"),
    )
    state = first_non_empty(
        detail_job.get("state"),
        detail_job.get("stateCode"),
        detail_job.get("locationState"),
        summary_job.get("state"),
        summary_job.get("stateCode"),
        summary_job.get("locationState"),
    )

    payload = {
        "job_id": job_id,
        "job_code": job_id,
        "job_type": first_non_empty(
            detail_job.get("jobTitle"),
            detail_job.get("jobType"),
            summary_job.get("jobTitle"),
            summary_job.get("jobType"),
            f"Nexus Job {job_id}",
        ),
        "status": first_non_empty(
            detail_job.get("status"),
            summary_job.get("status"),
            "Active",
        ),
        "profession": first_non_empty(
            detail_job.get("profession"),
            detail_job.get("professionName"),
            summary_job.get("profession"),
            summary_job.get("professionName"),
            "Not specified",
        ),
        "specialty": first_non_empty(
            detail_job.get("specialty"),
            detail_job.get("specialtyName"),
            summary_job.get("specialty"),
            summary_job.get("specialtyName"),
            "Not specified",
        ),
        "city": city,
        "state": state,
        "jobdescription": extract_description(summary_job, detail_job),
        "billrate": first_non_empty(
            detail_job.get("billRate"),
            detail_job.get("payRate"),
            detail_job.get("weeklyPayRange"),
            summary_job.get("billRate"),
            summary_job.get("payRate"),
            "",
        ),
        "client": first_non_empty(
            detail_job.get("clientName"),
            detail_job.get("facilityName"),
            summary_job.get("clientName"),
            summary_job.get("facilityName"),
            "",
        ),
    }

    return {key: coerce_text(value) for key, value in payload.items()}


def login_to_vms(session: requests.Session, base_url: str, email: str, password: str) -> str:
    response = session.post(
        f"{base_url.rstrip('/')}/api/auth/login",
        json={"email": email, "password": password},
        timeout=TIMEOUT,
    )
    response.raise_for_status()

    data = response.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError("VMS login succeeded but no access token was returned")
    return token


def post_job_to_vms(
    session: requests.Session,
    base_url: str,
    job_payload: Dict[str, str],
    token: Optional[str] = None,
) -> Dict[str, Any]:
    endpoint = "/api/admin/jobs" if token else "/api/nexus/jobs"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = session.post(
        f"{base_url.rstrip('/')}{endpoint}",
        headers=headers,
        json=job_payload,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def sync_jobs_to_vms(
    rows: List[Dict[str, Any]],
    vms_base_url: str,
    admin_email: str = "",
    admin_password: str = "",
) -> Dict[str, Any]:
    session = requests.Session()
    token: Optional[str] = None

    if admin_email and admin_password:
        token = login_to_vms(session, vms_base_url, admin_email, admin_password)
        print(f"Authenticated to VMS as {admin_email}")
    else:
        print("Admin credentials not provided. Using /api/nexus/jobs without login.")

    created = 0
    failed: List[Dict[str, str]] = []

    for index, row in enumerate(rows, start=1):
        summary_job = row["summary"]
        detail_job = row["detail"]
        job_id = first_non_empty(
            detail_job.get("jobId"),
            summary_job.get("jobId"),
            summary_job.get("id"),
        )

        try:
            payload = build_vms_payload(summary_job, detail_job)
            post_job_to_vms(session, vms_base_url, payload, token=token)
            created += 1
            print(f"Posted {index}/{len(rows)} to VMS for job {payload['job_id']}")
        except Exception as exc:
            failed.append({"job_id": job_id or "unknown", "error": str(exc)})
            print(f"Failed to post job {job_id or 'unknown'} to VMS: {exc}")

    return {
        "posted": created,
        "failed": failed,
        "total": len(rows),
    }


def fetch_detailed_jobs() -> List[Dict[str, Any]]:
    summary_jobs = fetch_job_summaries()
    print(f"\nTotal summary records fetched: {len(summary_jobs)}")

    rows: List[Dict[str, Any]] = []
    failed_job_ids: List[Any] = []

    for index, summary_job in enumerate(summary_jobs, start=1):
        job_id = summary_job.get("jobId")
        if not job_id:
            print(f"Skipping summary row {index}: missing jobId")
            continue

        try:
            detail_job = fetch_job_detail(job_id)
        except requests.RequestException as exc:
            failed_job_ids.append(job_id)
            print(f"Failed to fetch detail for job {job_id}: {exc}")
            continue

        rows.append(
            {
                "summary": summary_job,
                "detail": detail_job,
                "merged": merge_job_data(summary_job, detail_job),
            }
        )
        print(f"Fetched detail {index}/{len(summary_jobs)} for job {job_id}")

    if failed_job_ids:
        print(f"Failed detail fetches: {len(failed_job_ids)} job(s): {failed_job_ids}")

    return rows


def save_csv(rows: List[Dict[str, Any]], output_csv: str) -> None:
    df = pd.DataFrame([row["merged"] for row in rows])
    df.to_csv(output_csv, index=False, encoding="utf-8-sig")
    print(f"\nSaved {len(df)} detailed job records to {output_csv}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch jobs from Nexus and sync them into the VMS backend."
    )
    parser.add_argument("--vms-base-url", default=DEFAULT_VMS_BASE_URL)
    parser.add_argument("--admin-email", default=DEFAULT_ADMIN_EMAIL)
    parser.add_argument("--admin-password", default=DEFAULT_ADMIN_PASSWORD)
    parser.add_argument("--output-csv", default=OUTPUT_CSV)
    parser.add_argument(
        "--skip-csv",
        action="store_true",
        help="Do not write the flattened jobs CSV export.",
    )
    parser.add_argument(
        "--fetch-only",
        action="store_true",
        help="Fetch jobs from Nexus but do not push them into the VMS backend.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rows = fetch_detailed_jobs()

    if not args.skip_csv:
        save_csv(rows, args.output_csv)

    if args.fetch_only:
        print("Fetch completed. Skipped VMS sync because --fetch-only was provided.")
        return

    result = sync_jobs_to_vms(
        rows=rows,
        vms_base_url=args.vms_base_url,
        admin_email=args.admin_email,
        admin_password=args.admin_password,
    )

    print(
        f"\nVMS sync finished. Posted {result['posted']} of {result['total']} jobs."
    )
    if result["failed"]:
        print("Failed posts:")
        for item in result["failed"]:
            print(f"  - {item['job_id']}: {item['error']}")


if __name__ == "__main__":
    main()
