import argparse
import asyncio
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pandas as pd


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
DEFAULT_DETAIL_CONCURRENCY = 10
DEFAULT_POST_CONCURRENCY = 5
DEFAULT_NEXUS_POLL_SECONDS = 300
DEFAULT_CSV_POLL_SECONDS = 10
CSV_JOB_ID_COLUMN = "job_id"
CSV_STATUS_COLUMN = "posting_status"
CSV_POSTED_AT_COLUMN = "posted_at"
CSV_STARTED_AT_COLUMN = "posting_started_at"
CSV_ERROR_COLUMN = "post_error"
STATUS_PENDING = "pending"
STATUS_POSTING = "posting"
STATUS_POSTED = "posted"
STATUS_FAILED = "failed"
# DEFAULT_VMS_BASE_URL = os.getenv("VMS_BASE_URL", "https://radixsolvms.com/")
# DEFAULT_ADMIN_EMAIL = os.getenv("VMS_ADMIN_EMAIL", "Admin@radixsol.com")
# DEFAULT_ADMIN_PASSWORD = os.getenv("VMS_ADMIN_PASSWORD", "Admin123")
DEFAULT_VMS_BASE_URL = "https://radixsolvms.com/"
DEFAULT_ADMIN_EMAIL = "Admin@radixsol.com"
DEFAULT_ADMIN_PASSWORD = "Admin@123"

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


async def fetch_summary_page(
    client: httpx.AsyncClient,
    start: int,
) -> Tuple[List[Dict[str, Any]], int]:
    response = await client.post(
        LIST_URL,
        headers=HEADERS,
        json=build_payload(start),
    )
    response.raise_for_status()

    data = response.json()
    return data.get("records", []), data.get("count", 0)


async def fetch_job_detail(client: httpx.AsyncClient, job_id: Any) -> Dict[str, Any]:
    response = await client.get(
        DETAIL_URL_TEMPLATE.format(job_id=job_id),
        headers=HEADERS,
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


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_csv_value(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    return str(value).strip()


def unflatten_record(flat_row: Dict[str, Any], prefix: str, sep: str = ".") -> Dict[str, Any]:
    record: Dict[str, Any] = {}
    prefix_with_sep = f"{prefix}{sep}"

    for key, value in flat_row.items():
        if not key.startswith(prefix_with_sep):
            continue

        parts = key[len(prefix_with_sep):].split(sep)
        current = record
        for part in parts[:-1]:
            nested = current.setdefault(part, {})
            if not isinstance(nested, dict):
                nested = {}
                current[part] = nested
            current = nested
        current[parts[-1]] = normalize_csv_value(value)

    return record


def row_from_flat_csv(flat_row: Dict[str, Any]) -> Dict[str, Any]:
    summary = unflatten_record(flat_row, "summary")
    detail = unflatten_record(flat_row, "detail")

    if not summary and not detail:
        detail = {
            "jobId": flat_row.get("job_id", ""),
            "jobCode": flat_row.get("job_code", ""),
            "jobTitle": flat_row.get("job_type", ""),
            "status": flat_row.get("status", ""),
            "profession": flat_row.get("profession", ""),
            "specialty": flat_row.get("specialty", ""),
            "city": flat_row.get("city", ""),
            "state": flat_row.get("state", ""),
            "jobDescription": flat_row.get("jobdescription", ""),
            "billRate": flat_row.get("billrate", ""),
            "clientName": flat_row.get("client", ""),
        }

    return {
        "summary": summary,
        "detail": detail,
        "merged": dict(flat_row),
    }


def get_row_job_id(row: Dict[str, Any]) -> str:
    if "summary" in row and "detail" in row:
        summary_job = row["summary"]
        detail_job = row["detail"]
        return first_non_empty(
            detail_job.get("jobId"),
            detail_job.get("jobCode"),
            summary_job.get("jobId"),
            summary_job.get("jobCode"),
            summary_job.get("id"),
        )

    flat_row = row.get("merged", row)
    return first_non_empty(
        flat_row.get(CSV_JOB_ID_COLUMN),
        flat_row.get("detail.jobId"),
        flat_row.get("detail.jobCode"),
        flat_row.get("summary.jobId"),
        flat_row.get("summary.jobCode"),
        flat_row.get("summary.id"),
    )


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


def strip_html_text(value: Any) -> str:
    text = coerce_text(value)
    if not text:
        return ""

    if not re.search(r"<[a-zA-Z][^>]*>", text):
        return html.unescape(text).strip()

    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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
        return strip_html_text(description)

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


class CsvJobStore:
    def __init__(self, output_csv: str) -> None:
        self.path = Path(output_csv)
        self.lock = asyncio.Lock()

    def _read_df(self) -> pd.DataFrame:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return pd.DataFrame()
        return pd.read_csv(self.path, dtype=str, keep_default_na=False)

    def _write_df(self, df: pd.DataFrame) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(self.path, index=False, encoding="utf-8-sig")

    def _prepare_df(self, df: pd.DataFrame) -> pd.DataFrame:
        for column in [
            CSV_JOB_ID_COLUMN,
            CSV_STATUS_COLUMN,
            CSV_POSTED_AT_COLUMN,
            CSV_STARTED_AT_COLUMN,
            CSV_ERROR_COLUMN,
        ]:
            if column not in df.columns:
                df[column] = ""

        if not df.empty:
            missing_job_id = df[CSV_JOB_ID_COLUMN].map(normalize_csv_value) == ""
            if missing_job_id.any():
                df.loc[missing_job_id, CSV_JOB_ID_COLUMN] = df[missing_job_id].apply(
                    lambda item: get_row_job_id(item.to_dict()),
                    axis=1,
                )
            missing_status = df[CSV_STATUS_COLUMN].map(normalize_csv_value) == ""
            df.loc[missing_status, CSV_STATUS_COLUMN] = STATUS_PENDING

        return df

    async def has_job(self, job_id: str) -> bool:
        async with self.lock:
            df = self._prepare_df(self._read_df())
            if df.empty:
                return False
            return (df[CSV_JOB_ID_COLUMN].map(normalize_csv_value) == job_id).any()

    async def upsert_extracted_row(self, row: Dict[str, Any]) -> str:
        job_id = get_row_job_id(row)
        if not job_id:
            raise ValueError("Cannot persist Nexus row without a job identifier")

        merged = dict(row["merged"])
        merged[CSV_JOB_ID_COLUMN] = job_id

        async with self.lock:
            df = self._prepare_df(self._read_df())
            matches = []
            if not df.empty:
                matches = df.index[
                    df[CSV_JOB_ID_COLUMN].map(normalize_csv_value) == job_id
                ].tolist()

            if matches:
                index = matches[0]
                status = normalize_csv_value(df.at[index, CSV_STATUS_COLUMN]) or STATUS_PENDING
                for key, value in merged.items():
                    if key in [
                        CSV_STATUS_COLUMN,
                        CSV_POSTED_AT_COLUMN,
                        CSV_STARTED_AT_COLUMN,
                        CSV_ERROR_COLUMN,
                    ]:
                        continue
                    if key not in df.columns:
                        df[key] = ""
                    df.at[index, key] = flatten_value(value)
            else:
                status = STATUS_PENDING
                for key in merged:
                    if key not in df.columns:
                        df[key] = ""
                new_row = {column: "" for column in df.columns}
                for key, value in merged.items():
                    new_row[key] = flatten_value(value)
                new_row[CSV_STATUS_COLUMN] = STATUS_PENDING
                df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)

            self._write_df(df)
            return status

    async def ready_rows(self) -> List[Dict[str, Any]]:
        async with self.lock:
            df = self._prepare_df(self._read_df())
            if df.empty:
                self._write_df(df)
                return []

            ready = df[
                ~df[CSV_STATUS_COLUMN]
                .map(normalize_csv_value)
                .isin([STATUS_POSTED, STATUS_POSTING])
            ]
            if len(ready) != len(df):
                self._write_df(df)
            return [item.to_dict() for _, item in ready.iterrows()]

    async def claim_for_posting(self, job_id: str) -> bool:
        async with self.lock:
            df = self._prepare_df(self._read_df())
            if df.empty:
                self._write_df(df)
                return False

            matches = df.index[
                df[CSV_JOB_ID_COLUMN].map(normalize_csv_value) == job_id
            ].tolist()
            if not matches:
                self._write_df(df)
                return False

            index = matches[0]
            status = normalize_csv_value(df.at[index, CSV_STATUS_COLUMN])
            if status in [STATUS_POSTED, STATUS_POSTING]:
                self._write_df(df)
                return False

            df.at[index, CSV_STATUS_COLUMN] = STATUS_POSTING
            df.at[index, CSV_STARTED_AT_COLUMN] = utc_now_iso()
            df.at[index, CSV_ERROR_COLUMN] = ""
            self._write_df(df)
            return True

    async def posting_status(self, job_id: str) -> str:
        async with self.lock:
            df = self._prepare_df(self._read_df())
            if df.empty:
                return ""

            matches = df.index[
                df[CSV_JOB_ID_COLUMN].map(normalize_csv_value) == job_id
            ].tolist()
            if not matches:
                return ""
            return normalize_csv_value(df.at[matches[0], CSV_STATUS_COLUMN])

    async def mark_posted(self, job_id: str) -> None:
        async with self.lock:
            df = self._prepare_df(self._read_df())
            matches = df.index[
                df[CSV_JOB_ID_COLUMN].map(normalize_csv_value) == job_id
            ].tolist()
            if matches:
                index = matches[0]
                df.at[index, CSV_STATUS_COLUMN] = STATUS_POSTED
                df.at[index, CSV_POSTED_AT_COLUMN] = utc_now_iso()
                df.at[index, CSV_ERROR_COLUMN] = ""
            self._write_df(df)

    async def mark_failed(self, job_id: str, error: str) -> None:
        async with self.lock:
            df = self._prepare_df(self._read_df())
            matches = df.index[
                df[CSV_JOB_ID_COLUMN].map(normalize_csv_value) == job_id
            ].tolist()
            if matches:
                index = matches[0]
                df.at[index, CSV_STATUS_COLUMN] = STATUS_FAILED
                df.at[index, CSV_ERROR_COLUMN] = error
            self._write_df(df)


async def login_to_vms(
    client: httpx.AsyncClient,
    base_url: str,
    email: str,
    password: str,
) -> str:
    response = await client.post(
        f"{base_url.rstrip('/')}/api/auth/login",
        json={"email": email, "password": password},
    )
    response.raise_for_status()

    data = response.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError("VMS login succeeded but no access token was returned")
    return token


async def post_job_to_vms(
    client: httpx.AsyncClient,
    base_url: str,
    job_payload: Dict[str, str],
    token: Optional[str] = None,
) -> Dict[str, Any]:
    endpoint = "/api/admin/jobs" if token else "/api/nexus/jobs"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = await client.post(
        f"{base_url.rstrip('/')}{endpoint}",
        headers=headers,
        json=job_payload,
    )
    response.raise_for_status()
    return response.json()


async def produce_summary_jobs(
    client: httpx.AsyncClient,
    summary_queue: "asyncio.Queue[Optional[Tuple[int, Dict[str, Any]]]]",
    detail_worker_count: int,
) -> int:
    total_fetched = 0
    total_count: Optional[int] = None
    start = 0

    try:
        while True:
            records, reported_count = await fetch_summary_page(client, start)
            if not records:
                break

            if reported_count:
                total_count = reported_count

            for summary_job in records:
                total_fetched += 1
                await summary_queue.put((total_fetched, summary_job))

            print(f"Fetched {len(records)} summary records (Total: {total_fetched})")

            if total_count is not None and total_fetched >= total_count:
                break

            start += PAGE_SIZE
    finally:
        for _ in range(detail_worker_count):
            await summary_queue.put(None)

    return total_fetched


async def detail_worker(
    worker_id: int,
    client: httpx.AsyncClient,
    summary_queue: "asyncio.Queue[Optional[Tuple[int, Dict[str, Any]]]]",
    post_queue: Optional["asyncio.Queue[Optional[Dict[str, Any]]]"],
    csv_store: CsvJobStore,
    rows: List[Dict[str, Any]],
    failed_job_ids: List[Any],
) -> None:
    while True:
        item = await summary_queue.get()
        try:
            if item is None:
                return

            index, summary_job = item
            job_id = first_non_empty(
                summary_job.get("jobId"),
                summary_job.get("jobCode"),
                summary_job.get("id"),
            )
            if not job_id:
                print(f"Skipping summary row {index}: missing jobId")
                continue

            try:
                detail_job = await fetch_job_detail(client, job_id)
            except httpx.HTTPError as exc:
                failed_job_ids.append(job_id)
                print(f"Failed to fetch detail for job {job_id}: {exc}")
                continue

            row = {
                "summary": summary_job,
                "detail": detail_job,
                "merged": merge_job_data(summary_job, detail_job),
            }
            rows.append(row)
            print(f"Fetched detail for job {job_id} on detail worker {worker_id}")
            await csv_store.upsert_extracted_row(row)

            if post_queue is not None:
                if await csv_store.claim_for_posting(first_non_empty(job_id)):
                    await post_queue.put(row)
                else:
                    print(f"Skipped VMS queue for job {job_id}; CSV status is already posted/posting")
        finally:
            summary_queue.task_done()


async def post_worker(
    worker_id: int,
    client: httpx.AsyncClient,
    post_queue: "asyncio.Queue[Optional[Dict[str, Any]]]",
    vms_base_url: str,
    token: Optional[str],
    csv_store: CsvJobStore,
    result: Dict[str, Any],
) -> None:
    while True:
        row = await post_queue.get()
        try:
            if row is None:
                return

            summary_job = row["summary"]
            detail_job = row["detail"]
            job_id = get_row_job_id(row)
            if not job_id:
                result["failed"].append({"job_id": "unknown", "error": "missing job_id"})
                print("Failed to post job unknown to VMS: missing job_id")
                continue

            status = await csv_store.posting_status(job_id)
            if status != STATUS_POSTING:
                print(f"Skipped VMS post for job {job_id}; CSV status is {status or 'missing'}")
                continue

            result["total"] += 1
            try:
                payload = build_vms_payload(summary_job, detail_job)
                await post_job_to_vms(client, vms_base_url, payload, token=token)
                await csv_store.mark_posted(job_id)
                result["posted"] += 1
                print(f"Posted to VMS for job {payload['job_id']} on post worker {worker_id}")
            except (httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                result["failed"].append({"job_id": job_id, "error": str(exc)})
                print(
                    f"Post result uncertain for job {job_id}; leaving CSV status as posting: {exc}"
                )
            except Exception as exc:
                await csv_store.mark_failed(job_id, str(exc))
                result["failed"].append({"job_id": job_id or "unknown", "error": str(exc)})
                print(f"Failed to post job {job_id or 'unknown'} to VMS: {exc}")
        finally:
            post_queue.task_done()


async def fetch_and_optionally_sync_jobs(
    vms_base_url: str,
    admin_email: str = "",
    admin_password: str = "",
    output_csv: str = OUTPUT_CSV,
    fetch_only: bool = False,
    detail_concurrency: int = DEFAULT_DETAIL_CONCURRENCY,
    post_concurrency: int = DEFAULT_POST_CONCURRENCY,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    failed_job_ids: List[Any] = []
    sync_result: Dict[str, Any] = {"posted": 0, "failed": [], "total": 0}

    detail_concurrency = max(1, detail_concurrency)
    post_concurrency = max(1, post_concurrency)

    summary_queue: "asyncio.Queue[Optional[Tuple[int, Dict[str, Any]]]]" = asyncio.Queue(
        maxsize=detail_concurrency * 4
    )
    post_queue: Optional["asyncio.Queue[Optional[Dict[str, Any]]]"] = None
    if not fetch_only:
        post_queue = asyncio.Queue()
    csv_store = CsvJobStore(output_csv)

    timeout = httpx.Timeout(TIMEOUT)
    limits = httpx.Limits(
        max_connections=detail_concurrency + post_concurrency + 4,
        max_keepalive_connections=detail_concurrency + post_concurrency + 4,
    )
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as nexus_client:
        async with httpx.AsyncClient(timeout=timeout, limits=limits) as vms_client:
            token: Optional[str] = None
            if not fetch_only:
                if admin_email and admin_password:
                    token = await login_to_vms(vms_client, vms_base_url, admin_email, admin_password)
                    print(f"Authenticated to VMS as {admin_email}")
                else:
                    print("Admin credentials not provided. Using /api/nexus/jobs without login.")

            detail_tasks = [
                asyncio.create_task(
                    detail_worker(
                        worker_id=index,
                        client=nexus_client,
                        summary_queue=summary_queue,
                        post_queue=post_queue,
                        csv_store=csv_store,
                        rows=rows,
                        failed_job_ids=failed_job_ids,
                    )
                )
                for index in range(1, detail_concurrency + 1)
            ]
            post_tasks = []
            if post_queue is not None:
                post_tasks = [
                    asyncio.create_task(
                        post_worker(
                            worker_id=index,
                            client=vms_client,
                            post_queue=post_queue,
                            vms_base_url=vms_base_url,
                            token=token,
                            csv_store=csv_store,
                            result=sync_result,
                        )
                    )
                    for index in range(1, post_concurrency + 1)
                ]
                enqueued = await enqueue_ready_csv_jobs(csv_store, post_queue)
                if enqueued:
                    print(f"Queued {enqueued} existing unposted CSV job(s).")

            total_summaries = await produce_summary_jobs(
                nexus_client,
                summary_queue,
                detail_concurrency,
            )
            await summary_queue.join()
            await asyncio.gather(*detail_tasks)

            if post_queue is not None:
                for _ in range(post_concurrency):
                    await post_queue.put(None)
                await post_queue.join()
                await asyncio.gather(*post_tasks)

    print(f"\nTotal summary records fetched: {total_summaries}")
    if failed_job_ids:
        print(f"Failed detail fetches: {len(failed_job_ids)} job(s): {failed_job_ids}")

    return rows, sync_result


async def enqueue_ready_csv_jobs(
    csv_store: CsvJobStore,
    post_queue: "asyncio.Queue[Optional[Dict[str, Any]]]",
) -> int:
    enqueued = 0
    for flat_row in await csv_store.ready_rows():
        row = row_from_flat_csv(flat_row)
        job_id = get_row_job_id(row)
        if not job_id:
            print("Skipping CSV row without a job_id")
            continue
        if await csv_store.claim_for_posting(job_id):
            await post_queue.put(row)
            enqueued += 1
    return enqueued


async def scan_nexus_once(
    client: httpx.AsyncClient,
    csv_store: CsvJobStore,
    summary_queue: "asyncio.Queue[Optional[Tuple[int, Dict[str, Any]]]]",
) -> int:
    start = 0
    total_seen = 0
    queued = 0
    total_count: Optional[int] = None

    while True:
        records, reported_count = await fetch_summary_page(client, start)
        if not records:
            break

        if reported_count:
            total_count = reported_count

        for summary_job in records:
            total_seen += 1
            job_id = first_non_empty(
                summary_job.get("jobId"),
                summary_job.get("jobCode"),
                summary_job.get("id"),
            )
            if not job_id:
                print(f"Skipping summary row {total_seen}: missing jobId")
                continue
            if await csv_store.has_job(job_id):
                continue

            await summary_queue.put((total_seen, summary_job))
            queued += 1

        print(
            f"Nexus scan page fetched {len(records)} summaries "
            f"(seen: {total_seen}, queued new: {queued})"
        )

        if total_count is not None and total_seen >= total_count:
            break

        start += PAGE_SIZE

    return queued


async def nexus_poll_loop(
    client: httpx.AsyncClient,
    csv_store: CsvJobStore,
    summary_queue: "asyncio.Queue[Optional[Tuple[int, Dict[str, Any]]]]",
    poll_seconds: int,
) -> None:
    while True:
        try:
            queued = await scan_nexus_once(client, csv_store, summary_queue)
            print(f"Nexus scan complete. Queued {queued} new job(s).")
        except Exception as exc:
            print(f"Nexus scan failed: {exc}")

        await asyncio.sleep(max(1, poll_seconds))


async def csv_poll_loop(
    csv_store: CsvJobStore,
    post_queue: "asyncio.Queue[Optional[Dict[str, Any]]]",
    poll_seconds: int,
) -> None:
    while True:
        try:
            enqueued = await enqueue_ready_csv_jobs(csv_store, post_queue)
            if enqueued:
                print(f"CSV monitor queued {enqueued} unposted job(s).")
        except Exception as exc:
            print(f"CSV monitor failed: {exc}")

        await asyncio.sleep(max(1, poll_seconds))


async def run_continuous_service(
    vms_base_url: str,
    admin_email: str,
    admin_password: str,
    output_csv: str,
    fetch_only: bool,
    detail_concurrency: int,
    post_concurrency: int,
    nexus_poll_seconds: int,
    csv_poll_seconds: int,
) -> None:
    detail_concurrency = max(1, detail_concurrency)
    post_concurrency = max(1, post_concurrency)

    csv_store = CsvJobStore(output_csv)
    summary_queue: "asyncio.Queue[Optional[Tuple[int, Dict[str, Any]]]]" = asyncio.Queue(
        maxsize=detail_concurrency * 4
    )
    post_queue: Optional["asyncio.Queue[Optional[Dict[str, Any]]]"] = None
    if not fetch_only:
        post_queue = asyncio.Queue()

    rows: List[Dict[str, Any]] = []
    failed_job_ids: List[Any] = []
    result: Dict[str, Any] = {"posted": 0, "failed": [], "total": 0}

    timeout = httpx.Timeout(TIMEOUT)
    limits = httpx.Limits(
        max_connections=detail_concurrency + post_concurrency + 4,
        max_keepalive_connections=detail_concurrency + post_concurrency + 4,
    )

    async with httpx.AsyncClient(timeout=timeout, limits=limits) as nexus_client:
        async with httpx.AsyncClient(timeout=timeout, limits=limits) as vms_client:
            token: Optional[str] = None
            if not fetch_only:
                if admin_email and admin_password:
                    token = await login_to_vms(vms_client, vms_base_url, admin_email, admin_password)
                    print(f"Authenticated to VMS as {admin_email}")
                else:
                    print("Admin credentials not provided. Using /api/nexus/jobs without login.")

            tasks = [
                asyncio.create_task(
                    detail_worker(
                        worker_id=index,
                        client=nexus_client,
                        summary_queue=summary_queue,
                        post_queue=post_queue,
                        csv_store=csv_store,
                        rows=rows,
                        failed_job_ids=failed_job_ids,
                    )
                )
                for index in range(1, detail_concurrency + 1)
            ]
            tasks.append(
                asyncio.create_task(
                    nexus_poll_loop(
                        client=nexus_client,
                        csv_store=csv_store,
                        summary_queue=summary_queue,
                        poll_seconds=nexus_poll_seconds,
                    )
                )
            )

            if post_queue is not None:
                tasks.extend(
                    asyncio.create_task(
                        post_worker(
                            worker_id=index,
                            client=vms_client,
                            post_queue=post_queue,
                            vms_base_url=vms_base_url,
                            token=token,
                            csv_store=csv_store,
                            result=result,
                        )
                    )
                    for index in range(1, post_concurrency + 1)
                )
                tasks.append(
                    asyncio.create_task(
                        csv_poll_loop(
                            csv_store=csv_store,
                            post_queue=post_queue,
                            poll_seconds=csv_poll_seconds,
                        )
                    )
                )

            print("Continuous Nexus/VMS monitor started.")
            await asyncio.gather(*tasks)


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
        help="Unsupported for posting runs; jobs.csv is required for duplicate prevention.",
    )
    parser.add_argument(
        "--fetch-only",
        action="store_true",
        help="Fetch jobs from Nexus but do not push them into the VMS backend.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one scan and exit instead of staying alive as a monitor.",
    )
    parser.add_argument(
        "--detail-concurrency",
        type=int,
        default=DEFAULT_DETAIL_CONCURRENCY,
        help="Number of Nexus detail extraction requests to run concurrently.",
    )
    parser.add_argument(
        "--post-concurrency",
        type=int,
        default=DEFAULT_POST_CONCURRENCY,
        help="Number of VMS posting requests to run concurrently.",
    )
    parser.add_argument(
        "--nexus-poll-seconds",
        type=int,
        default=DEFAULT_NEXUS_POLL_SECONDS,
        help="Seconds to wait between Nexus scans in continuous mode.",
    )
    parser.add_argument(
        "--csv-poll-seconds",
        type=int,
        default=DEFAULT_CSV_POLL_SECONDS,
        help="Seconds to wait between jobs.csv checks in continuous mode.",
    )
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    if args.skip_csv:
        raise SystemExit(
            "--skip-csv cannot be used because jobs.csv now stores posting status "
            "and prevents duplicate submissions."
        )

    if not args.once:
        await run_continuous_service(
            vms_base_url=args.vms_base_url,
            admin_email=args.admin_email,
            admin_password=args.admin_password,
            output_csv=args.output_csv,
            fetch_only=args.fetch_only,
            detail_concurrency=args.detail_concurrency,
            post_concurrency=args.post_concurrency,
            nexus_poll_seconds=args.nexus_poll_seconds,
            csv_poll_seconds=args.csv_poll_seconds,
        )
        return

    rows, result = await fetch_and_optionally_sync_jobs(
        vms_base_url=args.vms_base_url,
        admin_email=args.admin_email,
        admin_password=args.admin_password,
        output_csv=args.output_csv,
        fetch_only=args.fetch_only,
        detail_concurrency=args.detail_concurrency,
        post_concurrency=args.post_concurrency,
    )

    if args.fetch_only:
        print(f"Fetch completed. Saved {len(rows)} detailed job record(s) to {args.output_csv}.")
        return

    print(
        f"\nVMS sync finished. Posted {result['posted']} of {result['total']} jobs."
    )
    if result["failed"]:
        print("Failed posts:")
        for item in result["failed"]:
            print(f"  - {item['job_id']}: {item['error']}")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
