from fastapi import FastAPI, HTTPException, UploadFile, File, Depends, status, Form, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPBearer, HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, root_validator
from typing import Any, Callable, Dict, List, Optional, Tuple
from collections import defaultdict
import httpx
import os
from datetime import datetime, timedelta
import aiofiles
import json
import html
from dotenv import load_dotenv
import re
import bcrypt
from jose import JWTError, jwt
from uuid import uuid4
import asyncio
import tempfile
import secrets
import hashlib
import smtplib
from threading import Lock
from time import monotonic as _monotonic, sleep as _sleep
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from urllib.parse import quote, urlencode
from pymongo import MongoClient, ReturnDocument, UpdateOne
from pymongo.errors import DuplicateKeyError
from pymongo.server_api import ServerApi
from bson.objectid import ObjectId

# Load environment variables
load_dotenv()

# MongoDB setup for persistent storage.
#
# Two clusters are kept connected side by side, not as a primary/failover
# pair: MONGODB_URI is the original cluster, now at its Atlas storage quota
# (writes blocked cluster-wide), and MONGODB_URI_2 is a second cluster added
# for headroom. Existing accounts and whitelist entries stay on MONGODB_URI
# so none of that data is migrated or lost; every collection that grows
# quickly (candidates, resumes/GridFS, notifications, logs, auth tokens,
# job data) is served from MONGODB_URI_2 so it has room to grow.
MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB_URI_2 = os.getenv("MONGODB_URI_2", "")
ACTIVE_MONGODB_LABEL: Optional[str] = None  # e.g. "primary+secondary", "primary-only", "secondary-only"
primary_mongo_client = None    # MONGODB_URI   - users/whitelist (existing identity data)
secondary_mongo_client = None  # MONGODB_URI_2 - everything else (growing data, GridFS)
primary_db = None
secondary_db = None
mongo_client = None  # kept for health/ping helpers; points at whichever cluster is connected
db = None
users_collection = None
whitelist_collection = None
candidates_collection = None
notifications_collection = None
jobs_collection = None
job_status_tracker_collection = None
closure_audit_collection = None
manual_jobs_collection = None
primary_manual_jobs_collection = None  # legacy jobs created before the dual-cluster split - read-only, MONGODB_URI is over quota
submission_logs_collection = None
auth_tokens_collection = None
user_preferences_collection = None
job_notification_outbox_collection = None
locks_collection = None
meta_collection = None
fs = None


def ensure_mongodb_indexes() -> None:
    """Create the indexes required for fast auth lookups and manual job reads."""
    index_specs = [
        ("users.email", users_collection, "email", {"name": "users_email_idx", "unique": True}),
        ("candidates.submitted_by_user_id", candidates_collection, "submitted_by_user_id", {"name": "candidates_submitted_by_user_id_idx"}),
        ("candidates.submitted_date", candidates_collection, [("submitted_date", -1)], {"name": "candidates_submitted_date_desc_idx"}),
        ("manual_jobs.created_at", manual_jobs_collection, [("created_at", -1)], {"name": "manual_jobs_created_at_desc_idx"}),
        ("manual_jobs.id", manual_jobs_collection, "id", {"name": "manual_jobs_id_idx", "unique": True}),
        ("manual_jobs.job_id", manual_jobs_collection, "job_id", {"name": "manual_jobs_job_id_idx"}),
        ("auth_tokens.key", auth_tokens_collection, [("kind", 1), ("key", 1)], {"name": "auth_tokens_kind_key_idx", "unique": True}),
        ("auth_tokens.expires_at_ttl", auth_tokens_collection, "expires_at", {"name": "auth_tokens_ttl_idx", "expireAfterSeconds": 0}),
        # Hot read paths that previously ran as full collection scans.
        ("candidates.id", candidates_collection, "id", {"name": "candidates_id_idx"}),
        ("candidates.job_id", candidates_collection, "job_id", {"name": "candidates_job_id_idx"}),
        ("candidates.submitter_date", candidates_collection, [("submitted_by_user_id", 1), ("submitted_date", 1)], {"name": "candidates_submitter_date_idx"}),
        ("notifications.user_created", notifications_collection, [("user_email", 1), ("created_at", -1)], {"name": "notifications_user_created_idx"}),
        ("notifications.user_read", notifications_collection, [("user_email", 1), ("read", 1)], {"name": "notifications_user_read_idx"}),
        ("notifications.type_job_user", notifications_collection, [("type", 1), ("job_id", 1), ("user_email", 1)], {"name": "notifications_type_job_user_idx"}),
        ("submission_logs.submitted_at", submission_logs_collection, [("submitted_at", -1)], {"name": "submission_logs_submitted_at_idx"}),
        ("whitelist.email", whitelist_collection, "email", {"name": "whitelist_email_idx"}),
        ("job_status_tracker.job_id", job_status_tracker_collection, "job_id", {"name": "job_status_tracker_job_id_idx"}),
        ("user_preferences.email", user_preferences_collection, "email", {"name": "user_preferences_email_idx", "unique": True}),
        ("job_notification_outbox.job_id", job_notification_outbox_collection, "job_id", {"name": "job_outbox_job_id_idx", "unique": True}),
        ("job_notification_outbox.status", job_notification_outbox_collection, [("status", 1), ("queued_at", 1)], {"name": "job_outbox_status_idx"}),
    ]
    for label, collection, keys, kwargs in index_specs:
        if collection is None:
            continue
        try:
            collection.create_index(keys, **kwargs)
            print(f"[MongoDB] Ensured index {label}")
        except Exception as e:
            print(f"[MongoDB] Failed to ensure index {label}: {e}")

MONGODB_SLOW_PING_MS = float(os.getenv("MONGODB_SLOW_PING_MS", "15"))
mongodb_startup_ping_ms: Optional[float] = None
MONGODB_WRITE_RETRY_ATTEMPTS = int(os.getenv("MONGODB_WRITE_RETRY_ATTEMPTS", "3"))


def measure_mongodb_ping_ms(samples: int = 5) -> Optional[float]:
    """Median round trip to MongoDB in milliseconds.

    Every database call pays this delay, often several times per request. When
    Atlas runs in the same cloud region as the app it is typically 1-5 ms; a few
    tens of ms or more means the cluster and the droplets are in different regions.
    """
    if mongo_client is None:
        return None
    import time
    timings = []
    for _ in range(samples):
        t0 = time.perf_counter()
        mongo_client.admin.command("ping")
        timings.append((time.perf_counter() - t0) * 1000)
    timings.sort()
    return round(timings[len(timings) // 2], 1)


def _open_mongo_client(uri: str):
    """Connect to a single MongoDB URI and return (client, db) on success.

    Only opens the connection and pings it - does not touch any collection
    globals, so the caller can route collections to whichever cluster(s)
    came up without one cluster's failure clobbering the other's globals.
    """
    client = MongoClient(
        uri,
        server_api=ServerApi('1'),
        maxPoolSize=int(os.getenv("MONGODB_MAX_POOL_SIZE", "20")),
        # Each new connection to a remote cluster costs several round trips
        # (TCP, TLS, auth), and pymongo opens at most two at a time, so a
        # burst of tabs waited on connection setup. Keep some open and ready.
        minPoolSize=int(os.getenv("MONGODB_MIN_POOL_SIZE", "10")),
        serverSelectionTimeoutMS=int(os.getenv("MONGODB_SERVER_SELECTION_TIMEOUT_MS", "10000")),
        # Bound the damage from a slow or hung cluster: without these a stuck
        # socket pins one of the 40 worker threads indefinitely.
        connectTimeoutMS=int(os.getenv("MONGODB_CONNECT_TIMEOUT_MS", "5000")),
        socketTimeoutMS=int(os.getenv("MONGODB_SOCKET_TIMEOUT_MS", "20000")),
        waitQueueTimeoutMS=int(os.getenv("MONGODB_WAIT_QUEUE_TIMEOUT_MS", "5000")),
    )
    new_db = client.vms
    # Test the connection before handing it back - a ping still succeeds on a
    # cluster that is over its storage quota (writes are blocked, not reads).
    client.admin.command('ping')
    return client, new_db


def init_mongodb():
    """Connect to both MongoDB clusters and route collections between them.

    MONGODB_URI (primary) and MONGODB_URI_2 (secondary) are connected
    independently and both stay open - this is not a primary/failover pair.
    MONGODB_URI already holds the existing users and whitelist, so that data
    stays there untouched and readable. MONGODB_URI_2 serves every
    collection that grows quickly (candidates, resumes/GridFS, notifications,
    job data, logs, auth tokens, preferences), since MONGODB_URI is at its
    Atlas storage quota and has writes blocked cluster-wide.
    """
    global mongodb_startup_ping_ms, ACTIVE_MONGODB_LABEL
    global primary_mongo_client, secondary_mongo_client, primary_db, secondary_db, mongo_client, db
    global users_collection, whitelist_collection, candidates_collection, notifications_collection
    global jobs_collection, job_status_tracker_collection, closure_audit_collection, manual_jobs_collection, primary_manual_jobs_collection
    global submission_logs_collection, auth_tokens_collection, user_preferences_collection
    global job_notification_outbox_collection, locks_collection, meta_collection, fs

    print(f"[MongoDB] Checking configuration...")
    print(f"[MongoDB] MONGODB_URI present: {bool(MONGODB_URI)}")
    print(f"[MongoDB] MONGODB_URI_2 present: {bool(MONGODB_URI_2)}")

    if not MONGODB_URI and not MONGODB_URI_2:
        print("[MongoDB] No MONGODB_URI environment variable set!")
        print("[MongoDB] Set MONGODB_URI in your deployment environment")
        print("[MongoDB] Using fallback JSON storage")
        return False

    primary_ok = False
    if MONGODB_URI:
        print("[MongoDB] Connecting to primary cluster (MONGODB_URI) for users/whitelist...")
        try:
            primary_mongo_client, primary_db = _open_mongo_client(MONGODB_URI)
            users_collection = primary_db.users
            whitelist_collection = primary_db.whitelist
            primary_manual_jobs_collection = primary_db.manual_jobs  # jobs created before the split; read-only
            primary_ok = True
            print("[MongoDB] Primary cluster connected.")
        except Exception as e:
            print(f"[MongoDB] Primary cluster (MONGODB_URI) unavailable: {e}")

    secondary_ok = False
    if MONGODB_URI_2:
        print("[MongoDB] Connecting to secondary cluster (MONGODB_URI_2) for job/candidate data...")
        try:
            secondary_mongo_client, secondary_db = _open_mongo_client(MONGODB_URI_2)
            candidates_collection = secondary_db.candidates
            notifications_collection = secondary_db.notifications
            jobs_collection = secondary_db.jobs  # Track job status changes
            job_status_tracker_collection = secondary_db.job_status_tracker  # Fresh tracker for closure detection (separate from legacy `jobs`)
            closure_audit_collection = secondary_db.closure_audit  # Audit log for detected closures (dry-run + live)
            manual_jobs_collection = secondary_db.manual_jobs  # Direct API-ingested jobs
            submission_logs_collection = secondary_db.submission_logs  # Immutable audit log for candidate submissions
            auth_tokens_collection = secondary_db.auth_tokens  # Password-reset tokens and login/registration OTPs shared across workers
            user_preferences_collection = secondary_db.user_preferences  # Per-user job notification preferences
            job_notification_outbox_collection = secondary_db.job_notification_outbox  # Durable queue of posted jobs awaiting digest emails
            locks_collection = secondary_db.locks  # Cross-worker leases (only one worker sends digests at a time)
            meta_collection = secondary_db.meta  # Small shared state: jobs version stamp, status-report snapshot

            import gridfs
            fs = gridfs.GridFS(secondary_db)  # Resume storage - the main driver of quota usage

            secondary_ok = True
            print("[MongoDB] Secondary cluster connected.")
        except Exception as e:
            print(f"[MongoDB] Secondary cluster (MONGODB_URI_2) unavailable: {e}")

    if not primary_ok and not secondary_ok:
        print("[MongoDB] Using fallback JSON storage")
        return False

    # Legacy globals some health/ping helpers still read - point them at
    # whichever cluster carries the heavier traffic (secondary), falling
    # back to primary if that one isn't up.
    mongo_client = secondary_mongo_client if secondary_mongo_client is not None else primary_mongo_client
    db = secondary_db if secondary_db is not None else primary_db

    ACTIVE_MONGODB_LABEL = "+".join(
        label for label, ok in (("primary", primary_ok), ("secondary", secondary_ok)) if ok
    )

    ensure_mongodb_indexes()
    try:
        mongodb_startup_ping_ms = measure_mongodb_ping_ms()
        if mongodb_startup_ping_ms is not None and mongodb_startup_ping_ms > MONGODB_SLOW_PING_MS:
            print(
                f"[MongoDB] WARNING: median round trip is {mongodb_startup_ping_ms} ms "
                f"(threshold {MONGODB_SLOW_PING_MS} ms). The Atlas cluster is probably in a "
                f"different region from this server; every request pays this delay per query."
            )
        else:
            print(f"[MongoDB] Median round trip: {mongodb_startup_ping_ms} ms")
    except Exception as e:
        print(f"[MongoDB] Could not measure round-trip time: {e}")
    return True

# Initialize MongoDB on startup
mongodb_enabled = init_mongodb()

# SendGrid email setup for OTP, password reset, and notifications
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "")
SENDGRID_FROM_EMAIL = os.getenv("SENDGRID_FROM_EMAIL", "notifications@radixsol.com")
APP_URL = os.getenv("APP_URL", "http://localhost:8000")

# Optional SMTP fallback for local development when SendGrid is not configured.
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.office365.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", SMTP_USERNAME or SENDGRID_FROM_EMAIL)
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes", "y"}

# Job closure notification config defaults to dry-run for safety.
# Flip JOB_CLOSURE_NOTIFICATIONS_ENABLED=true only after audit logs confirm real transitions.
JOB_CLOSURE_NOTIFICATIONS_ENABLED = os.getenv("JOB_CLOSURE_NOTIFICATIONS_ENABLED", "false").lower() == "true"
JOB_CLOSURE_PER_RUN_CAP = int(os.getenv("JOB_CLOSURE_PER_RUN_CAP", "25"))
JOB_POSTING_NOTIFICATIONS_ENABLED = os.getenv("JOB_POSTING_NOTIFICATIONS_ENABLED", "true").lower() == "true"

# Periodic "how many jobs are open / closed" e-mail to the listed recipients.
JOB_STATUS_REPORT_ENABLED = os.getenv("JOB_STATUS_REPORT_ENABLED", "true").lower() == "true"
JOB_STATUS_REPORT_SECONDS = int(os.getenv("JOB_STATUS_REPORT_SECONDS", str(2 * 60 * 60)))
JOB_STATUS_REPORT_RECIPIENTS = [
    addr.strip().lower()
    for addr in os.getenv("JOB_STATUS_REPORT_RECIPIENTS", "het@radixsol.com").split(",")
    if addr.strip()
]
JOB_POSTING_PER_RUN_CAP = int(os.getenv("JOB_POSTING_PER_RUN_CAP", "25"))

# Recipients for new-submission notification emails. Comma-separated env var.
SUBMISSION_NOTIFICATION_RECIPIENTS = [
    addr.strip() for addr in os.getenv(
        "SUBMISSION_NOTIFICATION_RECIPIENTS",
        "it.support@radixsol.com",
    ).split(",") if addr.strip()
]
SENDGRID_FROM_EMAIL = os.getenv("SENDGRID_FROM_EMAIL", "notifications@radixsol.com")

BILL_RATE_DISPLAY_MULTIPLIER = 0.93
BILL_RATE_NUMBER_PATTERN = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
COMPENSATION_PATTERN = re.compile(
    r"\b(bill\s*rate|salary|pay\s*rate|hourly\s*pay|weekly\s*pay|regular\s*pay|stipend)\b",
    flags=re.IGNORECASE,
)


def _format_discounted_rate_amount(amount: float) -> str:
    if amount.is_integer():
        return str(int(amount))
    return f"{amount:.2f}"


def display_bill_rate(rate_value) -> str:
    """Return the vendor-facing bill rate with the actual rate reduced by 7%."""
    text = str(rate_value or "").strip()
    if not text or text.lower() in {"nan", "none", "null", "n/a"}:
        return ""

    if not BILL_RATE_NUMBER_PATTERN.search(text):
        return text

    has_currency = "$" in text or re.search(r"\bUSD\b", text, flags=re.IGNORECASE)

    def discount_match(match: re.Match) -> str:
        raw_number = match.group(0)
        try:
            actual_amount = float(raw_number.replace(",", ""))
        except ValueError:
            return raw_number
        return _format_discounted_rate_amount(actual_amount * BILL_RATE_DISPLAY_MULTIPLIER)

    discounted_text = BILL_RATE_NUMBER_PATTERN.sub(discount_match, text)
    if has_currency:
        return discounted_text
    return f"${discounted_text}"


def sanitize_candidate_bill_rate(candidate: dict) -> dict:
    if not candidate.get("bill_rate_discount_applied"):
        candidate["bill_rate"] = display_bill_rate(candidate.get("bill_rate")) or "N/A"
    candidate.pop("bill_rate_discount_applied", None)
    return candidate


def sanitize_submission_log_bill_rate(log_doc: dict) -> dict:
    metadata = log_doc.get("metadata")
    if isinstance(metadata, dict):
        if not metadata.get("bill_rate_discount_applied"):
            metadata["bill_rate"] = display_bill_rate(metadata.get("bill_rate")) or "N/A"
        metadata.pop("bill_rate_discount_applied", None)
    return log_doc


def hide_compensation_details(text: str) -> str:
    """Remove raw salary/pay/bill-rate text from job descriptions before display."""
    if not text:
        return ""
    kept_lines = [
        line for line in str(text).splitlines()
        if not COMPENSATION_PATTERN.search(line)
    ]
    return "\n".join(kept_lines).strip()


@lru_cache(maxsize=8192)
def _sanitize_description_cached(description: str, is_admin: bool) -> str:
    """Regex work is the dominant cost of a job-list rebuild; unchanged
    descriptions are served from this cache instead of being re-processed."""
    return sanitize_job_description(hide_compensation_details(description), is_admin)


def sanitize_job_for_display(job, is_admin: bool = False) -> dict:
    job_dict = job.dict() if hasattr(job, "dict") else dict(job)
    if job_dict.get("salary_range") and not job_dict.get("bill_rate_discount_applied"):
        job_dict["salary_range"] = display_bill_rate(job_dict.get("salary_range")) or None
    job_dict["bill_rate_discount_applied"] = True
    if "description" in job_dict:
        job_dict["description"] = _sanitize_description_cached(job_dict.get("description") or "", is_admin)
    job_dict.pop("bill_rate_discount_applied", None)
    return job_dict

# In-memory password reset token storage (expires after 1 hour)
# Structure: {token: {email: str, expires: datetime, used: bool}}
_password_reset_tokens = {}

# In-memory OTP storage. MongoDB is intentionally not required because codes are short-lived.
# Structure: {email: {otp_hash: str, expires: datetime, attempts: int, user_agent: str}}
_email_otp_tokens = {}

OTP_EXPIRE_MINUTES = int(os.getenv("OTP_EXPIRE_MINUTES", "10"))
OTP_MAX_ATTEMPTS = int(os.getenv("OTP_MAX_ATTEMPTS", "5"))

_auth_token_lock = Lock()


# ---------------------------------------------------------------------------
# Shared auth-token store.
#
# The API runs with multiple uvicorn workers, so a reset token or OTP created by
# one worker used to be invisible to every other worker (and to any worker
# started after a redeploy). That is what produced "Invalid Token" / "Link
# Expired" on perfectly good reset links. Tokens now live in MongoDB when it is
# available, with the previous in-process dicts kept as a single-worker fallback.
# ---------------------------------------------------------------------------

def _auth_tokens_available() -> bool:
    return bool(mongodb_enabled) and auth_tokens_collection is not None


def _memory_store(kind: str) -> dict:
    return _password_reset_tokens if kind == "reset" else _email_otp_tokens


def store_auth_token(kind: str, key: str, payload: dict) -> None:
    """Persist a reset token / OTP so every worker can validate it."""
    expires = payload.get("expires")
    if _auth_tokens_available():
        try:
            doc = dict(payload)
            # `expires` is naive local time and stays authoritative for validation.
            # `expires_at` is the same instant expressed in UTC purely so the TTL
            # index sweeps the document only once it is genuinely dead.
            doc.update({
                "kind": kind,
                "key": key,
                "expires_at": datetime.utcnow() + (expires - datetime.now()),
            })
            auth_tokens_collection.replace_one({"kind": kind, "key": key}, doc, upsert=True)
            return
        except Exception as e:
            print(f"[Auth] Could not persist {kind} token, using in-memory store: {e}")
    with _auth_token_lock:
        _memory_store(kind)[key] = payload


def get_auth_token(kind: str, key: str) -> Optional[dict]:
    """Fetch a token payload, or None when it is missing or already expired."""
    if _auth_tokens_available():
        try:
            doc = auth_tokens_collection.find_one({"kind": kind, "key": key})
            if not doc:
                return None
            doc.pop("_id", None)
            doc.pop("kind", None)
            doc.pop("key", None)
            doc.pop("expires_at", None)
            return doc
        except Exception as e:
            print(f"[Auth] Could not read {kind} token from MongoDB: {e}")
    with _auth_token_lock:
        return _memory_store(kind).get(key)


def update_auth_token(kind: str, key: str, updates: dict) -> None:
    """Apply field updates (attempt counters, used flag) to a stored token."""
    if _auth_tokens_available():
        try:
            auth_tokens_collection.update_one({"kind": kind, "key": key}, {"$set": updates})
            return
        except Exception as e:
            print(f"[Auth] Could not update {kind} token: {e}")
    with _auth_token_lock:
        existing = _memory_store(kind).get(key)
        if existing is not None:
            existing.update(updates)


def delete_auth_token(kind: str, key: str) -> None:
    if _auth_tokens_available():
        try:
            auth_tokens_collection.delete_one({"kind": kind, "key": key})
            return
        except Exception as e:
            print(f"[Auth] Could not delete {kind} token: {e}")
    with _auth_token_lock:
        _memory_store(kind).pop(key, None)


def bump_auth_token_attempts(kind: str, key: str, current_attempts: int) -> None:
    """Record one more failed verification attempt."""
    if _auth_tokens_available():
        try:
            auth_tokens_collection.update_one({"kind": kind, "key": key}, {"$inc": {"attempts": 1}})
            return
        except Exception as e:
            print(f"[Auth] Could not increment attempts for {kind} token: {e}")
    with _auth_token_lock:
        existing = _memory_store(kind).get(key)
        if existing is not None:
            existing["attempts"] = current_attempts + 1


def _hash_otp(otp: str) -> str:
    return hashlib.sha256(otp.encode("utf-8")).hexdigest()


def _smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_PORT and SMTP_USERNAME and SMTP_PASSWORD and SMTP_FROM_EMAIL)


def send_sendgrid_email(to_emails, subject: str, html_content: str, text_content: Optional[str] = None, from_email: Optional[str] = None) -> bool:
    """Send an email using SendGrid."""
    recipients = to_emails if isinstance(to_emails, list) else [to_emails]
    recipients = [addr for addr in recipients if addr]
    if not recipients:
        print("[Email] No recipients provided")
        return False
    if not SENDGRID_API_KEY:
        print(f"[Email] SendGrid not configured. Would send '{subject}' to {recipients}")
        return False

    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail

        message = Mail(
            from_email=from_email or SENDGRID_FROM_EMAIL,
            to_emails=recipients,
            subject=subject,
            html_content=html_content,
            plain_text_content=text_content,
        )

        import time
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        t0 = time.monotonic()
        response = sg.send(message)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        status_code = getattr(response, "status_code", None)
        msg_id = None
        try:
            headers = getattr(response, "headers", {}) or {}
            msg_id = headers.get("X-Message-Id") if hasattr(headers, "get") else None
        except Exception:
            pass
        print(f"[Email] SendGrid responded in {elapsed_ms}ms status={status_code} msg_id={msg_id} recipients={recipients}")
        return status_code == 202
    except Exception as e:
        import traceback
        print(f"[Email] SendGrid send failed: {e}")
        print(f"[Email] Traceback: {traceback.format_exc()}")
        return False


def send_smtp_email(to_emails, subject: str, html_content: str, text_content: Optional[str] = None, from_email: Optional[str] = None) -> bool:
    """Send an email using SMTP settings when configured."""
    recipients = to_emails if isinstance(to_emails, list) else [to_emails]
    recipients = [addr for addr in recipients if addr]
    if not recipients:
        print("[Email] No recipients provided")
        return False
    if not _smtp_configured():
        print(f"[Email] Outlook SMTP not configured. Would send '{subject}' to {recipients}")
        return False

    try:
        message = EmailMessage()
        message["From"] = from_email or SMTP_FROM_EMAIL
        message["To"] = ", ".join(recipients)
        message["Subject"] = subject
        message.set_content(text_content or html_content)
        message.add_alternative(html_content, subtype="html")

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            if SMTP_USE_TLS:
                smtp.starttls()
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(message)
        print(f"[Email] SMTP email sent to {recipients}: {subject}")
        return True
    except Exception as e:
        print(f"[Email] SMTP send failed: {e}")
        return False


def send_app_email(to_emails, subject: str, html_content: str, text_content: Optional[str] = None, from_email: Optional[str] = None) -> bool:
    """Production email path is SendGrid; SMTP remains a local fallback."""
    if send_sendgrid_email(to_emails, subject, html_content, text_content, from_email=from_email):
        return True
    return send_smtp_email(to_emails, subject, html_content, text_content, from_email=from_email)


def send_login_otp_email(email: str, otp: str) -> bool:
    html_content = f"""
        <h2>Your VMS verification code</h2>
        <p>Use this one-time code to sign in:</p>
        <p style="font-size: 28px; font-weight: bold; letter-spacing: 4px;">{html.escape(otp)}</p>
        <p>This code expires in {OTP_EXPIRE_MINUTES} minutes.</p>
        <p>If you did not request this code, you can ignore this email.</p>
    """
    sent = send_app_email(
        email,
        "Your VMS sign-in code",
        html_content,
        text_content=f"Your VMS sign-in code is {otp}. It expires in {OTP_EXPIRE_MINUTES} minutes.",
    )
    if not sent:
        if globals().get("DEBUG", False) or globals().get("TESTING_MODE", False):
            print(f"[Auth] OTP for {email}: {otp} (email not configured or failed)")
        else:
            print(f"[Auth] OTP email failed for {email}")
    return sent


def send_registration_otp_email(email: str, otp: str) -> bool:
    html_content = f"""
        <h2>Your VMS email verification code</h2>
        <p>Use this one-time code to complete your account registration:</p>
        <p style="font-size: 28px; font-weight: bold; letter-spacing: 4px;">{html.escape(otp)}</p>
        <p>This code expires in {OTP_EXPIRE_MINUTES} minutes.</p>
        <p>If you did not request this account, you can ignore this email.</p>
    """
    sent = send_app_email(
        email,
        "Verify your VMS account",
        html_content,
        text_content=f"Your VMS account verification code is {otp}. It expires in {OTP_EXPIRE_MINUTES} minutes.",
    )
    if not sent:
        if globals().get("DEBUG", False) or globals().get("TESTING_MODE", False):
            print(f"[Auth] Registration OTP for {email}: {otp} (email not configured or failed)")
        else:
            print(f"[Auth] Registration OTP email failed for {email}")
    return sent


def cleanup_expired_otps():
    """Drop expired OTPs. MongoDB expires them via a TTL index; this covers the fallback store."""
    now = datetime.now()
    with _auth_token_lock:
        expired = [email for email, data in _email_otp_tokens.items() if data["expires"] < now]
        for email in expired:
            del _email_otp_tokens[email]
    if expired:
        print(f"[Auth] Cleaned up {len(expired)} expired OTPs")

def send_password_reset_email(email: str, reset_token: str) -> bool:
    """Send a password reset email through the configured app mailer."""
    reset_url = f"{APP_URL.rstrip('/')}?{urlencode({'token': reset_token})}"
    html_content = f'''
        <h2>Password Reset Request</h2>
        <p>You requested a password reset for your Vendor Management System account.</p>
        <p><a href="{html.escape(reset_url)}" style="padding: 12px 24px; background: #7c3aed; color: white; text-decoration: none; border-radius: 6px;">Reset Password</a></p>
        <p>Or copy this link: {html.escape(reset_url)}</p>
        <p>This link expires in 1 hour.</p>
        <p>If you didn't request this, please ignore this email.</p>
    '''
    sent = send_app_email(
        email,
        'Password Reset - Vendor Management System',
        html_content,
        text_content=f"Reset your VMS password: {reset_url}\nThis link expires in 1 hour.",
    )
    if not sent:
        if globals().get("DEBUG", False) or globals().get("TESTING_MODE", False):
            print(f"[Email] Password reset email failed. Token for {email}: {reset_token}")
        else:
            print(f"[Email] Password reset email failed for {email}")
    return sent

def cleanup_expired_tokens():
    """Remove expired password reset tokens from the in-process fallback store."""
    now = datetime.now()
    with _auth_token_lock:
        expired = [token for token, data in _password_reset_tokens.items() if data['expires'] < now]
        for token in expired:
            del _password_reset_tokens[token]
    if expired:
        print(f"[Auth] Cleaned up {len(expired)} expired reset tokens")

def send_submission_notification_email(candidate_data: dict, vendor_info: dict, submission_summary: Optional[dict] = None) -> bool:
    """Send candidate submission notification to admin with per-user activity details."""
    try:
        # Build email content
        candidate_name = str(candidate_data.get('name') or 'N/A')
        job_title = str(candidate_data.get('job_title') or 'N/A')
        job_id = str(candidate_data.get('job_id') or 'N/A')
        vendor_name = str(vendor_info.get('full_name') or 'N/A')
        vendor_email = str(vendor_info.get('email') or 'N/A')
        if candidate_data.get("bill_rate_discount_applied"):
            bill_rate = str(candidate_data.get('bill_rate') or 'N/A')
        else:
            bill_rate = display_bill_rate(candidate_data.get('bill_rate')) or 'N/A'
        location = str(candidate_data.get('current_location') or 'N/A')
        skills = str(candidate_data.get('primary_skills') or 'N/A')
        candidate_email = str(candidate_data.get('email') or 'N/A')
        candidate_phone = str(candidate_data.get('phone') or 'N/A')
        submitted_at = str(candidate_data.get('submitted_date') or 'N/A')
        ip_address = str(candidate_data.get('submission_ip_address') or 'N/A')
        user_agent = str(candidate_data.get('submission_user_agent') or 'N/A')
        submission_summary = submission_summary or {}
        total_profiles = int(submission_summary.get("total_profiles_submitted") or 0)
        profiles_on_date = int(submission_summary.get("profiles_submitted_on_date") or 0)
        submission_date = str(submission_summary.get("submission_date") or submitted_at[:10] or 'N/A')
        first_submission_at = str(submission_summary.get("first_submission_at") or submitted_at)
        last_submission_at = str(submission_summary.get("last_submission_at") or submitted_at)
        
        html_content = f'''
            <h2>Profile Submission Activity Report</h2>
            <p><strong>{html.escape(vendor_name)}</strong> ({html.escape(vendor_email)}) submitted a new profile.</p>
            
            <h3>User Submission Summary:</h3>
            <table style="border-collapse: collapse; width: 100%; max-width: 600px; margin-bottom: 18px;">
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">User Name:</td><td style="padding: 8px; border: 1px solid #ddd;">{html.escape(vendor_name)}</td></tr>
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">User Email:</td><td style="padding: 8px; border: 1px solid #ddd;">{html.escape(vendor_email)}</td></tr>
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Total Profiles Submitted:</td><td style="padding: 8px; border: 1px solid #ddd;">{total_profiles}</td></tr>
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Profiles Submitted On {html.escape(submission_date)}:</td><td style="padding: 8px; border: 1px solid #ddd;">{profiles_on_date}</td></tr>
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">First Submission Time:</td><td style="padding: 8px; border: 1px solid #ddd;">{html.escape(first_submission_at)}</td></tr>
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Latest Submission Time:</td><td style="padding: 8px; border: 1px solid #ddd;">{html.escape(last_submission_at)}</td></tr>
            </table>
             
            <h3>Submitted Profile Details:</h3>
            <table style="border-collapse: collapse; width: 100%; max-width: 600px;">
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Name:</td><td style="padding: 8px; border: 1px solid #ddd;">{html.escape(candidate_name)}</td></tr>
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Email:</td><td style="padding: 8px; border: 1px solid #ddd;">{html.escape(candidate_email)}</td></tr>
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Phone:</td><td style="padding: 8px; border: 1px solid #ddd;">{html.escape(candidate_phone)}</td></tr>
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Job Title:</td><td style="padding: 8px; border: 1px solid #ddd;">{html.escape(job_title)}</td></tr>
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Job ID:</td><td style="padding: 8px; border: 1px solid #ddd;">{html.escape(job_id)}</td></tr>
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Bill Rate:</td><td style="padding: 8px; border: 1px solid #ddd;">{html.escape(bill_rate)}</td></tr>
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Location:</td><td style="padding: 8px; border: 1px solid #ddd;">{html.escape(location)}</td></tr>
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Skills:</td><td style="padding: 8px; border: 1px solid #ddd;">{html.escape(skills)}</td></tr>
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Submitted By:</td><td style="padding: 8px; border: 1px solid #ddd;">{html.escape(vendor_name)} ({html.escape(vendor_email)})</td></tr>
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Submitted At:</td><td style="padding: 8px; border: 1px solid #ddd;">{html.escape(submitted_at)}</td></tr>
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">IP Address:</td><td style="padding: 8px; border: 1px solid #ddd;">{html.escape(ip_address)}</td></tr>
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">User Agent:</td><td style="padding: 8px; border: 1px solid #ddd;">{html.escape(user_agent)}</td></tr>
            </table>
            
            <p style="margin-top: 20px;">
                <a href="{html.escape(APP_URL)}" style="padding: 12px 24px; background: #7c3aed; color: white; text-decoration: none; border-radius: 6px;">View in VMS Dashboard</a>
            </p>
            
            <p style="color: #666; font-size: 12px; margin-top: 30px;">
                This is an automated notification from the Vendor Management System.
            </p>
        '''
        
        recipients = SUBMISSION_NOTIFICATION_RECIPIENTS or [ADMIN_EMAIL]
        subject = f'Submission Activity: {vendor_name} now at {total_profiles} profile(s)'

        return send_app_email(
            recipients,
            subject,
            html_content,
            text_content=(
                f"Profile submission activity report\n"
                f"User: {vendor_name} ({vendor_email})\n"
                f"Total profiles submitted: {total_profiles}\n"
                f"Profiles submitted on {submission_date}: {profiles_on_date}\n"
                f"First submission time: {first_submission_at}\n"
                f"Latest submission time: {last_submission_at}\n"
                f"Candidate: {candidate_name}\n"
                f"Email: {candidate_email}\n"
                f"Phone: {candidate_phone}\n"
                f"Job: {job_title} ({job_id})\n"
                f"Submitted at: {submitted_at}"
            ),
            from_email=SENDGRID_FROM_EMAIL,
        )
    except Exception as e:
        import traceback
        print(f"[Email] Error sending submission notification: {e}")
        print(f"[Email] Traceback: {traceback.format_exc()}")
        print(f"[Email] SendGrid API Key present: {bool(SENDGRID_API_KEY)}")
        print(f"[Email] From email: {SENDGRID_FROM_EMAIL}")
        print(f"[Email] Recipients: {SUBMISSION_NOTIFICATION_RECIPIENTS}")
        return False


def send_vendor_message_email(vendor_email: str, vendor_name: str, subject: str, message_body: str,
                              candidate_name: str, job_title: str, job_id: str) -> tuple:
    """Send a free-form message from admin to a vendor about one of their candidate submissions.

    Used by the "Email Vendor" action in the admin submissions UI so admins can update vendors
    on submission status (interview, decline, offer details, etc.).

    Returns (success: bool, detail: str). detail carries the SendGrid status or error reason for
    surfacing to the admin UI so silent failures don't look like a hung "Sending..." state.
    """
    print(f"[VendorMail] Begin send to '{vendor_email}' subject='{subject[:60]}' from={SENDGRID_FROM_EMAIL}")
    if not SENDGRID_API_KEY:
        print("[VendorMail] SendGrid API key not configured")
        return False, "SendGrid not configured on server"
    if not vendor_email:
        return False, "Vendor email is empty"

    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail
        from html import escape

        safe_body = escape(message_body).replace("\n", "<br>")
        safe_vendor_name = escape(vendor_name) if vendor_name else "Vendor"

        html_content = f'''
            <p>Hello {safe_vendor_name},</p>
            <p>This message is regarding your submission below.</p>
            <table style="border-collapse: collapse; width: 100%; max-width: 600px; margin: 12px 0;">
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Candidate:</td><td style="padding: 8px; border: 1px solid #ddd;">{escape(candidate_name)}</td></tr>
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Job Title:</td><td style="padding: 8px; border: 1px solid #ddd;">{escape(job_title)}</td></tr>
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Job ID:</td><td style="padding: 8px; border: 1px solid #ddd;">{escape(job_id)}</td></tr>
            </table>
            <div style="padding: 12px; border-left: 3px solid #7c3aed; background: #faf9ff; margin: 12px 0;">
                {safe_body}
            </div>
            <p style="color: #666; font-size: 12px; margin-top: 30px;">
                Sent from the Vendor Management System. Reply to this email to contact us.
            </p>
        '''

        message = Mail(
            from_email=SENDGRID_FROM_EMAIL,
            to_emails=vendor_email,
            subject=subject,
            html_content=html_content,
        )

        import time
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        t0 = time.monotonic()
        response = sg.send(message)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        status = getattr(response, "status_code", None)
        # Log response headers for debugging delivery issues (X-Message-Id helps trace in SendGrid dashboard).
        msg_id = None
        try:
            headers = getattr(response, "headers", {}) or {}
            msg_id = headers.get("X-Message-Id") if hasattr(headers, "get") else None
        except Exception:
            pass
        print(f"[VendorMail] SendGrid responded in {elapsed_ms}ms status={status} msg_id={msg_id}")
        if status == 202:
            return True, f"Accepted by SendGrid (msg_id={msg_id or 'n/a'})"
        return False, f"SendGrid rejected with status {status}"
    except Exception as e:
        import traceback
        print(f"[VendorMail] Exception sending to {vendor_email}: {e}")
        print(f"[VendorMail] Traceback: {traceback.format_exc()}")
        return False, f"SendGrid error: {type(e).__name__}: {e}"


# Statuses that count as "open" upstream and "closed" downstream.
# A genuine closure is any status crossing from OPEN_STATUSES → CLOSED_STATUSES.
OPEN_STATUSES = {"open", "active"}
CLOSED_STATUSES = {"closed", "inactive", "filled", "on hold", "cancelled"}


def normalize_job_status(status_value: str | None, default: str = "Open") -> str:
    """Collapse equivalent upstream open statuses into a single display value."""
    status_text = (status_value or "").strip()
    if not status_text:
        return default
    if status_text.lower() in OPEN_STATUSES:
        return "Open"
    return status_text


def is_job_open(status_value: str | None) -> bool:
    """Whether a job may be shown to vendors and receive submissions."""
    return normalize_job_status(status_value).lower() not in CLOSED_STATUSES


def notify_job_closures(transitions: List[dict], source: str = "ceipal") -> None:
    """Send closure emails + in-app notifications for detected Open/Active -> Closed transitions.

    Shared by the (disabled-by-default) Ceipal poller and the manual/Nexus job-ingest
    path, which is what actually drives production since CEIPAL_ENABLED is False.
    """
    if not transitions:
        return
    global notifications_collection, closure_audit_collection

    if len(transitions) > JOB_CLOSURE_PER_RUN_CAP:
        print(f"[ClosureDetector] ABORT ({source}) — {len(transitions)} closures exceeds cap of {JOB_CLOSURE_PER_RUN_CAP}. No notifications sent.")
        if closure_audit_collection is not None:
            closure_audit_collection.insert_one({
                "id": str(uuid4()),
                "type": "abort_cap_exceeded",
                "source": source,
                "detected_count": len(transitions),
                "cap": JOB_CLOSURE_PER_RUN_CAP,
                "sample_job_ids": [t["job_id"] for t in transitions[:50]],
                "detected_at": datetime.now().isoformat(),
            })
        return

    recipients = sorted(
        {u.lower() for u in get_whitelisted_emails(force_refresh=True) if u.lower() != ADMIN_EMAIL.lower()}
    )

    for t in transitions:
        job_id = t["job_id"]
        title = t["title"]
        audit_doc = {
            "id": str(uuid4()),
            "type": "closure_detected",
            "source": source,
            "job_id": job_id,
            "job_title": title,
            "previous_status": t["previous_status"],
            "current_status": t["current_status"],
            "recipients_count": len(recipients),
            "detected_at": datetime.now().isoformat(),
            "dry_run": not JOB_CLOSURE_NOTIFICATIONS_ENABLED,
        }

        if not JOB_CLOSURE_NOTIFICATIONS_ENABLED:
            print(f"[ClosureDetector] DRY RUN ({source}) — would notify {len(recipients)} users about closure of {job_id} ('{title}')")
            if closure_audit_collection is not None:
                closure_audit_collection.insert_one(audit_doc)
            continue

        sent_count = 0
        for user_email in recipients:
            if notifications_collection is not None:
                existing = notifications_collection.find_one({
                    "type": "job_closed",
                    "job_id": job_id,
                    "user_email": user_email,
                })
                if existing:
                    continue
            email_sent = send_job_closure_notification_email(user_email, title, job_id)
            if notifications_collection is not None:
                notifications_collection.insert_one({
                    "id": str(uuid4()),
                    "type": "job_closed",
                    "job_id": job_id,
                    "job_title": title,
                    "user_email": user_email,
                    "email_sent": email_sent,
                    "created_at": datetime.now().isoformat(),
                    "read": False,
                })
            if email_sent:
                sent_count += 1

        audit_doc["emails_sent"] = sent_count
        if closure_audit_collection is not None:
            closure_audit_collection.insert_one(audit_doc)
        print(f"[ClosureDetector] Notified ({source}) {sent_count}/{len(recipients)} users about closure of {job_id}")


def send_job_closure_notification_email(user_email: str, job_title: str, job_id: str) -> bool:
    """Send a single job-closure notification email via SendGrid."""
    if not SENDGRID_API_KEY:
        print(f"[Email] SendGrid not configured — cannot send closure notification to {user_email}")
        return False
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail

        html_content = f'''
            <h2>Job Closure Notification</h2>
            <p>The following job has been <strong>closed</strong> and is no longer accepting submissions.</p>
            <table style="border-collapse: collapse; width: 100%; max-width: 600px;">
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Job Title:</td><td style="padding: 8px; border: 1px solid #ddd;">{job_title}</td></tr>
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Job ID:</td><td style="padding: 8px; border: 1px solid #ddd;">{job_id}</td></tr>
            </table>
            <p style="color: #666; font-size: 12px; margin-top: 30px;">
                This is an automated notification from the Vendor Management System.<br>
                For questions, please contact admin@radixsol.com
            </p>
        '''
        message = Mail(
            from_email=SENDGRID_FROM_EMAIL,
            to_emails=user_email,
            subject=f'Job Closed: {job_title}',
            html_content=html_content,
        )
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)
        return response.status_code == 202
    except Exception as e:
        print(f"[Email] Error sending closure notification to {user_email}: {e}")
        return False


def send_job_posted_notification_email(
    user_email: str,
    job_title: str,
    job_id: str,
    location: str = "",
    meta_tags: Optional[List[str]] = None,
) -> bool:
    """Send a single new-job notification email via the configured mail provider."""
    try:
        tags_html = ""
        tags = [tag for tag in (meta_tags or []) if tag]
        if tags:
            tags_html = f'''
                <div style="margin: 16px 0 8px;">
                    {"".join(
                        f'<span style="display:inline-block;margin:0 6px 8px 0;padding:6px 10px;border:1px solid #d1d5db;border-radius:999px;background:#f9fafb;color:#111827;font-size:13px;">{html.escape(tag)}</span>'
                        for tag in tags
                    )}
                </div>
            '''
        html_content = f'''
            <h2>New Job Posted</h2>
            <p>A new job has been <strong>posted</strong> and is accepting submissions.</p>
            {tags_html}
            <table style="border-collapse: collapse; width: 100%; max-width: 600px;">
                <tr><td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Job Title:</td><td style="padding: 8px; border: 1px solid #ddd;">{html.escape(job_title)}</td></tr>
            </table>
            <p style="margin-top: 20px;">
                <a href="{html.escape(APP_URL)}" style="padding: 12px 24px; background: #7c3aed; color: white; text-decoration: none; border-radius: 6px;">View Job</a>
            </p>
            <p style="color: #666; font-size: 12px; margin-top: 30px;">
                Getting jobs you are not interested in? <a href="{html.escape(preferences_url())}">Update your job alert preferences</a>.<br>
                This is an automated notification from the Vendor Management System.<br>
                For questions, please contact admin@radixsol.com
            </p>
        '''
        return send_app_email(
            user_email,
            subject=f'New Job Posted: {job_title}',
            html_content=html_content,
        )
    except Exception as e:
        print(f"[Email] Error sending posted-job notification to {user_email}: {e}")
        return False


def build_job_posted_meta_tags(
    specialty: str = "",
    state: str = "",
    employment_type: str = "",
    status: str = "",
    end_client: str = "",
) -> List[str]:
    tags = []
    for value in [specialty, state, employment_type]:
        text = str(value or "").strip()
        if text:
            tags.append(text)
    if status:
        tags.append(f"Status: {str(status).strip()}")
    if end_client:
        tags.append(f"End Client: {str(end_client).strip()}")
    return tags


# ---------------------------------------------------------------------------
# Job alert preferences.
#
# Each user can narrow which posted jobs they hear about by profession,
# specialty, job type and state. An empty list on a dimension means "any", so a
# user who never sets preferences keeps receiving every job, exactly as before.
# ---------------------------------------------------------------------------

PREFERENCE_LIST_FIELDS = ("professions", "specialties", "job_types", "states")
PREFERENCE_MAX_VALUES = 200
PREFERENCE_MAX_VALUE_LENGTH = 120
_preferences_file_lock = Lock()

US_STATE_CODES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _match_key(value: Any) -> str:
    return _clean_text(value).lower()


def normalize_state(value: Any) -> str:
    """Map 'Michigan' / 'mi' / 'MI' to 'MI' so Excel and Nexus jobs compare equal."""
    text = _clean_text(value)
    if not text:
        return ""
    return US_STATE_CODES.get(text.lower(), text.upper() if len(text) == 2 else text)


def default_job_preferences(email: str) -> dict:
    return {
        "email": (email or "").strip().lower(),
        "email_notifications": True,
        **{field: [] for field in PREFERENCE_LIST_FIELDS},
        "updated_at": None,
    }


def clean_job_preferences(email: str, raw: dict) -> dict:
    prefs = default_job_preferences(email)
    prefs["email_notifications"] = bool(raw.get("email_notifications", True))
    for field in PREFERENCE_LIST_FIELDS:
        seen = set()
        values = []
        for value in raw.get(field) or []:
            text = _clean_text(value)[:PREFERENCE_MAX_VALUE_LENGTH]
            if field == "states":
                text = normalize_state(text)
            if text and _match_key(text) not in seen:
                seen.add(_match_key(text))
                values.append(text)
        prefs[field] = values[:PREFERENCE_MAX_VALUES]
    return prefs


def _preferences_file() -> str:
    return os.path.join(DATA_DIR, "user_preferences.json")


def _read_preferences_file() -> dict:
    path = _preferences_file()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[Preferences] Could not read {path}: {e}")
        return {}


def get_preferences_for_users(emails: List[str]) -> Dict[str, dict]:
    """Preferences for many users in one query; users without a record get defaults."""
    emails = [(e or "").strip().lower() for e in emails if e]
    stored: Dict[str, dict] = {}
    if mongodb_enabled and user_preferences_collection is not None:
        for doc in user_preferences_collection.find({"email": {"$in": emails}}, {"_id": 0}):
            stored[doc["email"]] = doc
    else:
        with _preferences_file_lock:
            file_data = _read_preferences_file()
        stored = {email: file_data[email] for email in emails if email in file_data}
    return {
        email: clean_job_preferences(email, stored[email]) if email in stored else default_job_preferences(email)
        for email in emails
    }


def get_user_preferences(email: str) -> dict:
    email_lower = (email or "").strip().lower()
    prefs = get_preferences_for_users([email_lower]).get(email_lower) or default_job_preferences(email_lower)
    if mongodb_enabled and user_preferences_collection is not None:
        doc = user_preferences_collection.find_one({"email": email_lower}, {"_id": 0, "updated_at": 1})
        prefs["updated_at"] = (doc or {}).get("updated_at")
    return prefs


def save_user_preferences(email: str, raw: dict) -> dict:
    prefs = clean_job_preferences(email, raw)
    prefs["updated_at"] = datetime.now().isoformat()
    if mongodb_enabled and user_preferences_collection is not None:
        user_preferences_collection.update_one({"email": prefs["email"]}, {"$set": prefs}, upsert=True)
        return prefs
    with _preferences_file_lock:
        data = _read_preferences_file()
        data[prefs["email"]] = prefs
        os.makedirs(os.path.dirname(_preferences_file()), exist_ok=True)
        with open(_preferences_file(), "w") as f:
            json.dump(data, f, indent=2)
    return prefs


def job_preference_attributes(job: dict) -> dict:
    """The fields of a job that preferences filter on, normalized for matching."""
    specialty = job.get("specialty") or ""
    department = job.get("department") or ""
    if not specialty and department and not department.lower().startswith("job code:"):
        specialty = department
    state = job.get("state") or ""
    if not state:
        parts = [p.strip() for p in str(job.get("location") or "").split(",") if p.strip()]
        state = parts[-1] if len(parts) > 1 else ""
    return {
        "professions": _match_key(job.get("profession")),
        "specialties": _match_key(specialty),
        "job_types": _match_key(job.get("title")),
        "states": _match_key(normalize_state(state)),
    }


def job_matches_preferences(job_attrs: dict, prefs: dict) -> bool:
    for field in PREFERENCE_LIST_FIELDS:
        wanted = {_match_key(v) for v in prefs.get(field) or []}
        if wanted and job_attrs.get(field) not in wanted:
            return False
    return True


# ---------------------------------------------------------------------------
# Posted-job notification outbox.
#
# Posting a job used to email every whitelisted user immediately, one email per
# job, from a BackgroundTask inside the request's worker: a bulk post of 25 jobs
# meant 25 emails to each user, sent serially, and the queue was lost on any
# restart or redeploy. Jobs now go into a MongoDB outbox. A periodic flush,
# guarded by a cross-worker lease so only one of the API workers sends, groups
# everything queued since the last flush into one digest per user, filtered by
# that user's preferences.
# ---------------------------------------------------------------------------

JOB_NOTIFICATION_DIGEST_SECONDS = int(os.getenv("JOB_NOTIFICATION_DIGEST_SECONDS", "300"))
JOB_NOTIFICATION_DIGEST_MAX_LISTED = int(os.getenv("JOB_NOTIFICATION_DIGEST_MAX_LISTED", "25"))
JOB_NOTIFICATION_FLUSH_BATCH = int(os.getenv("JOB_NOTIFICATION_FLUSH_BATCH", "500"))
JOB_NOTIFICATION_SEND_CONCURRENCY = int(os.getenv("JOB_NOTIFICATION_SEND_CONCURRENCY", "4"))
JOB_NOTIFICATION_LEASE_SECONDS = 15 * 60
WORKER_ID = f"{os.getpid()}-{uuid4().hex[:8]}"


def preferences_url() -> str:
    return f"{APP_URL.rstrip('/')}/?{urlencode({'view': 'preferences'})}"


def job_notification_snapshot(job) -> dict:
    return {
        "id": job.id,
        "title": job.title,
        "location": job.location,
        "department": job.department,
        "profession": job.profession or "",
        "specialty": job.specialty or "",
        "state": job.state or "",
        "employment_type": job.employment_type,
        "status": job.status,
        "end_client": job.end_client or "",
    }


def enqueue_job_post_notifications(jobs: List["Job"], source: str) -> int:
    """Queue posted jobs for the next digest. Re-posting a queued job is a no-op."""
    if not JOB_POSTING_NOTIFICATIONS_ENABLED:
        print(f"[JobPosted] Posting notifications disabled - not queueing {len(jobs)} job(s) from {source}")
        return 0
    if not mongodb_enabled or job_notification_outbox_collection is None:
        print("[JobPosted] Skipping posted-job notifications - MongoDB unavailable.")
        return 0

    open_jobs = [job for job in jobs if normalize_job_status(job.status) == "Open"]
    if not open_jobs:
        return 0
    now = datetime.now()
    result = job_notification_outbox_collection.bulk_write(
        [
            UpdateOne(
                {"job_id": job.id},
                {"$setOnInsert": {
                    "job_id": job.id,
                    "job": job_notification_snapshot(job),
                    "source": source,
                    "status": "pending",
                    "queued_at": now,
                }},
                upsert=True,
            )
            for job in open_jobs
        ],
        ordered=False,
    )
    queued = result.upserted_count
    print(f"[JobPosted] Queued {queued} new job(s) from {source} for the next digest ({len(open_jobs) - queued} already queued/sent)")
    return queued


def _acquire_lease(name: str, seconds: int) -> bool:
    now = datetime.utcnow()
    try:
        locks_collection.find_one_and_update(
            {"_id": name, "$or": [{"locked_until": {"$lt": now}}, {"locked_until": {"$exists": False}}]},
            {"$set": {"locked_until": now + timedelta(seconds=seconds), "owner": WORKER_ID}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return True
    except DuplicateKeyError:
        return False  # another worker holds an unexpired lease


def _release_lease(name: str) -> None:
    try:
        locks_collection.update_one(
            {"_id": name, "owner": WORKER_ID},
            # In the past, not "now": Mongo keeps millisecond precision, so a lease
            # released and re-acquired within the same millisecond would look held.
            {"$set": {"locked_until": datetime.utcnow() - timedelta(seconds=1)}},
        )
    except Exception as e:
        print(f"[JobDigest] Could not release lease {name}: {e}")


def send_job_digest_email(user_email: str, jobs: List[dict]) -> bool:
    """One email listing every new job that matched this user's preferences."""
    if len(jobs) == 1:
        job = jobs[0]
        return send_job_posted_notification_email(
            user_email,
            job["title"],
            job["id"],
            job.get("location") or "",
            build_job_posted_meta_tags(
                specialty=job.get("specialty") or job.get("department") or "",
                state=job.get("state") or "",
                employment_type=job.get("employment_type") or "",
                status=job.get("status") or "",
                end_client=job.get("end_client") or "",
            ),
        )

    listed = jobs[:JOB_NOTIFICATION_DIGEST_MAX_LISTED]
    remaining = len(jobs) - len(listed)
    rows = "".join(
        f'''<tr>
                <td style="padding: 8px; border: 1px solid #ddd;">{html.escape(job["title"])}</td>
                <td style="padding: 8px; border: 1px solid #ddd;">{html.escape(job.get("specialty") or job.get("profession") or "")}</td>
                <td style="padding: 8px; border: 1px solid #ddd;">{html.escape(job.get("location") or "")}</td>
            </tr>'''
        for job in listed
    )
    more_html = f"<p>…and {remaining} more. Open the portal to see them all.</p>" if remaining else ""
    html_content = f'''
        <h2>{len(jobs)} New Jobs Posted</h2>
        <p>These newly posted jobs match your job alert preferences and are accepting submissions.</p>
        <table style="border-collapse: collapse; width: 100%; max-width: 640px;">
            <tr>
                <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">Job</th>
                <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">Specialty</th>
                <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">Location</th>
            </tr>
            {rows}
        </table>
        {more_html}
        <p style="margin-top: 20px;">
            <a href="{html.escape(APP_URL)}" style="padding: 12px 24px; background: #7c3aed; color: white; text-decoration: none; border-radius: 6px;">View Jobs</a>
        </p>
        <p style="color: #666; font-size: 12px; margin-top: 30px;">
            Getting jobs you are not interested in? <a href="{html.escape(preferences_url())}">Update your job alert preferences</a>.<br>
            This is an automated notification from the Vendor Management System.
        </p>
    '''
    text_lines = [f"{len(jobs)} new jobs matching your preferences:"]
    text_lines += [f"- {job['title']} ({job.get('location') or 'location n/a'})" for job in listed]
    if remaining:
        text_lines.append(f"...and {remaining} more.")
    text_lines.append(f"Manage your job alert preferences: {preferences_url()}")
    return send_app_email(
        user_email,
        subject=f"{len(jobs)} new jobs matching your preferences",
        html_content=html_content,
        text_content="\n".join(text_lines),
    )


def flush_job_notification_outbox() -> None:
    """Send one digest per user for everything queued since the last flush."""
    if not mongodb_enabled or job_notification_outbox_collection is None or locks_collection is None:
        return
    if not _acquire_lease("job_notification_digest", JOB_NOTIFICATION_LEASE_SECONDS):
        return
    try:
        pending = list(
            job_notification_outbox_collection.find({"status": "pending"})
            .sort("queued_at", 1)
            .limit(JOB_NOTIFICATION_FLUSH_BATCH)
        )
        if not pending:
            return

        jobs = [doc["job"] for doc in pending]
        job_ids = [job["id"] for job in jobs]
        job_attrs = {job["id"]: job_preference_attributes(job) for job in jobs}
        recipients = sorted(
            {u.lower() for u in get_whitelisted_emails(force_refresh=True) if u.lower() != ADMIN_EMAIL.lower()}
        )
        prefs_by_user = get_preferences_for_users(recipients)

        # Who already heard about which job (earlier runs, or the pre-outbox sender).
        already_notified = {
            (doc["user_email"], doc["job_id"])
            for doc in notifications_collection.find(
                {"type": "job_posted", "job_id": {"$in": job_ids}},
                {"_id": 0, "user_email": 1, "job_id": 1},
            )
        } if notifications_collection is not None else set()

        deliveries = []
        for user_email in recipients:
            prefs = prefs_by_user[user_email]
            matched = [
                job for job in jobs
                if (user_email, job["id"]) not in already_notified
                and job_matches_preferences(job_attrs[job["id"]], prefs)
            ]
            if matched:
                deliveries.append((user_email, prefs, matched))

        def deliver(user_email: str, prefs: dict, matched: List[dict]) -> bool:
            email_sent = False
            if prefs.get("email_notifications", True):
                try:
                    email_sent = send_job_digest_email(user_email, matched)
                except Exception as e:
                    print(f"[JobDigest] Email to {user_email} failed: {e}")
            if notifications_collection is not None:
                created_at = datetime.now().isoformat()
                notifications_collection.insert_many([
                    {
                        "id": str(uuid4()),
                        "type": "job_posted",
                        "job_id": job["id"],
                        "job_title": job["title"],
                        "user_email": user_email,
                        "email_sent": email_sent,
                        "digest_size": len(matched),
                        "source": "digest",
                        "created_at": created_at,
                        "read": False,
                    }
                    for job in matched
                ])
            return email_sent

        emails_sent = 0
        if deliveries:
            # Bounded parallelism: fast enough for many users without tripping
            # SendGrid rate limits or starving the API's own threadpool.
            with ThreadPoolExecutor(max_workers=JOB_NOTIFICATION_SEND_CONCURRENCY) as pool:
                for sent in pool.map(lambda d: deliver(*d), deliveries):
                    emails_sent += int(bool(sent))

        job_notification_outbox_collection.update_many(
            {"_id": {"$in": [doc["_id"] for doc in pending]}},
            {"$set": {"status": "sent", "processed_at": datetime.now()}},
        )
        print(
            f"[JobDigest] Processed {len(jobs)} job(s): {len(deliveries)} user(s) matched, "
            f"{emails_sent} digest email(s) sent, {len(recipients) - len(deliveries)} user(s) had no matches"
        )
    finally:
        _release_lease("job_notification_digest")


async def job_notification_digest_loop() -> None:
    # Stagger workers so they do not all race for the lease at the same instant.
    await asyncio.sleep(5 + secrets.randbelow(25))
    while True:
        try:
            await asyncio.to_thread(flush_job_notification_outbox)
        except Exception as e:
            import traceback
            print(f"[JobDigest] Flush failed: {e}\n{traceback.format_exc()}")
        await asyncio.sleep(JOB_NOTIFICATION_DIGEST_SECONDS)


def summarize_job_statuses(force_refresh: bool = True) -> Dict[str, int]:
    """Count jobs by status. Reads the store directly so the numbers are current."""
    if force_refresh:
        load_manual_jobs(force_refresh=True)
    counts = {"open": 0, "closed": 0, "other": 0}
    jobs = get_all_jobs()
    for job in jobs:
        status = normalize_job_status(job.status).strip().lower()
        if status in OPEN_STATUSES:
            counts["open"] += 1
        elif status in CLOSED_STATUSES:
            counts["closed"] += 1
        else:
            counts["other"] += 1
    counts["total"] = len(jobs)
    return counts


def _format_age(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, rem = divmod(minutes, 60)
    return f"{hours} h {rem} min" if rem else f"{hours} h"


def _signed(value: int) -> str:
    return f"+{value}" if value > 0 else str(value)


def build_job_status_report(counts: Dict[str, int], previous: Optional[dict]) -> Dict[str, str]:
    generated = datetime.now()
    if JOB_STATUS_REPORT_SECONDS % 3600 == 0:
        interval_label = f"{JOB_STATUS_REPORT_SECONDS // 3600} hours"
    else:
        interval_label = f"{JOB_STATUS_REPORT_SECONDS // 60} minutes"
    change_html = change_text = ""
    if previous and previous.get("sent_at"):
        age = generated - previous["sent_at"]
        d_open = counts["open"] - int(previous.get("open", 0))
        d_closed = counts["closed"] - int(previous.get("closed", 0))
        change_html = (
            f'<p style="color:#555;">Since the last report ({_format_age(age)} ago): '
            f'open {_signed(d_open)}, closed {_signed(d_closed)}.</p>'
        )
        change_text = f"Since the last report ({_format_age(age)} ago): open {_signed(d_open)}, closed {_signed(d_closed)}."
    other_row = (
        f'<tr><td style="padding:8px;border:1px solid #ddd;">Other statuses</td>'
        f'<td style="padding:8px;border:1px solid #ddd;text-align:right;">{counts["other"]}</td></tr>'
        if counts["other"] else ""
    )
    html_content = f'''
        <h2>Job status report</h2>
        <p>Current job counts on the Vendor Management System.</p>
        <table style="border-collapse:collapse;width:100%;max-width:360px;">
            <tr><td style="padding:8px;border:1px solid #ddd;"><strong>Open jobs</strong></td>
                <td style="padding:8px;border:1px solid #ddd;text-align:right;"><strong>{counts["open"]}</strong></td></tr>
            <tr><td style="padding:8px;border:1px solid #ddd;"><strong>Closed jobs</strong></td>
                <td style="padding:8px;border:1px solid #ddd;text-align:right;"><strong>{counts["closed"]}</strong></td></tr>
            {other_row}
            <tr><td style="padding:8px;border:1px solid #ddd;">Total</td>
                <td style="padding:8px;border:1px solid #ddd;text-align:right;">{counts["total"]}</td></tr>
        </table>
        {change_html}
        <p><a href="{html.escape(APP_URL)}">Open the portal</a></p>
        <p style="color:#666;font-size:12px;margin-top:24px;">
            Automated report, sent every {interval_label}.
            Generated {generated.strftime("%Y-%m-%d %H:%M")} (server time).
        </p>
    '''
    text_lines = [
        "Job status report",
        f"Open jobs: {counts['open']}",
        f"Closed jobs: {counts['closed']}",
    ]
    if counts["other"]:
        text_lines.append(f"Other statuses: {counts['other']}")
    text_lines.append(f"Total: {counts['total']}")
    if change_text:
        text_lines.append(change_text)
    text_lines.append(APP_URL)
    return {
        "subject": f"Job status: {counts['open']} open, {counts['closed']} closed",
        "html": html_content,
        "text": "\n".join(text_lines),
    }


def send_job_status_report(ignore_schedule: bool = False) -> Dict[str, Any]:
    """Send the open/closed report. Scheduled calls take a MongoDB lease so only
    one worker sends per interval, and a restart does not send a duplicate."""
    if not JOB_STATUS_REPORT_RECIPIENTS:
        return {"sent": False, "reason": "no recipients configured"}

    leased = False
    use_lease = mongodb_enabled and locks_collection is not None and not ignore_schedule
    if use_lease:
        if not _acquire_lease("job_status_report", JOB_STATUS_REPORT_SECONDS):
            return {"sent": False, "reason": "not due"}
        leased = True
    elif not ignore_schedule:
        global _status_report_last_sent
        if _status_report_last_sent and _monotonic() - _status_report_last_sent < JOB_STATUS_REPORT_SECONDS:
            return {"sent": False, "reason": "not due"}

    try:
        counts = summarize_job_statuses()
        previous = None
        if mongodb_enabled and meta_collection is not None:
            previous = meta_collection.find_one({"_id": "job_status_report"})
        report = build_job_status_report(counts, previous)
        sent = send_app_email(
            JOB_STATUS_REPORT_RECIPIENTS,
            subject=report["subject"],
            html_content=report["html"],
            text_content=report["text"],
        )
        if not sent:
            raise RuntimeError("email provider did not accept the message")
        if mongodb_enabled and meta_collection is not None:
            meta_collection.update_one(
                {"_id": "job_status_report"},
                {"$set": {"open": counts["open"], "closed": counts["closed"], "sent_at": datetime.now()}},
                upsert=True,
            )
        if not ignore_schedule:
            _status_report_last_sent = _monotonic()  # a manual test send must not delay the schedule
        print(f"[JobStatusReport] Sent to {', '.join(JOB_STATUS_REPORT_RECIPIENTS)}: {counts}")
        return {"sent": True, "counts": counts, "recipients": JOB_STATUS_REPORT_RECIPIENTS}
    except Exception as e:
        if leased:
            _release_lease("job_status_report")  # let the next check retry
        print(f"[JobStatusReport] Failed: {e}")
        return {"sent": False, "reason": str(e)}


_status_report_last_sent: Optional[float] = None
JOB_STATUS_REPORT_RETRY_SECONDS = 300


async def job_status_report_loop() -> None:
    """Check once a minute; the lease decides whether a report is actually due."""
    await asyncio.sleep(20 + secrets.randbelow(40))
    while True:
        delay = 60
        try:
            result = await asyncio.to_thread(send_job_status_report)
            if not result.get("sent") and result.get("reason") not in ("not due", None):
                delay = JOB_STATUS_REPORT_RETRY_SECONDS  # do not hammer the mail provider
        except Exception as e:
            print(f"[JobStatusReport] Loop error: {e}")
            delay = JOB_STATUS_REPORT_RETRY_SECONDS
        await asyncio.sleep(delay)


def extract_ceipal_status_entries(reports_data) -> list:
    """Extract (job_id, status, title) from a raw Ceipal reports page WITHOUT filtering by status.

    The regular parser drops everything that isn't Open/Active, so closure detection
    cannot rely on it — it must read JobStatus straight from the raw response.
    """
    entries = []
    if not isinstance(reports_data, dict):
        return entries
    result = reports_data.get("result")
    if not isinstance(result, list):
        result = reports_data.get("data", reports_data.get("jobs", reports_data.get("records", [])))
    if not isinstance(result, list):
        return entries
    for row in result:
        if not isinstance(row, dict):
            continue
        job_id = row.get("JobCode") or row.get("job_id") or row.get("JobID")
        status = (row.get("JobStatus") or "").strip()
        title = (row.get("JobTitle") or "").strip()
        location = (row.get("Location") or row.get("States") or "").strip()
        specialty = (
            row.get("Specialty")
            or row.get("JobSpecialty")
            or row.get("Speciality")
            or row.get("JobSpeciality")
            or ""
        )
        state = row.get("States") or row.get("State") or ""
        employment_type = row.get("Duration") or row.get("EmploymentType") or "Contract"
        end_client = row.get("EndClient") or ""
        if job_id and status:
            entries.append({
                "job_id": str(job_id),
                "status": status,
                "title": title,
                "location": location,
                "specialty": str(specialty).strip(),
                "state": str(state).strip(),
                "employment_type": str(employment_type).strip(),
                "end_client": str(end_client).strip(),
            })
    return entries


def _update_status_tracker(current_status_map: dict):
    """Upsert each job's current status into job_status_tracker for next-run comparison."""
    if job_status_tracker_collection is None:
        return
    now = datetime.now().isoformat()
    for job_id, info in current_status_map.items():
        job_status_tracker_collection.update_one(
            {"job_id": job_id},
            {"$set": {
                "job_id": job_id,
                "status": info.get("status", ""),
                "title": info.get("title", ""),
                "location": info.get("location", ""),
                "specialty": info.get("specialty", ""),
                "state": info.get("state", ""),
                "employment_type": info.get("employment_type", ""),
                "end_client": info.get("end_client", ""),
                "updated_at": now,
            }},
            upsert=True,
        )
    print(f"[ClosureDetector] Updated tracker with {len(current_status_map)} job statuses")


def detect_and_notify_closures(current_status_map: dict, fetch_complete: bool):
    """Detect Open/Active → Closed transitions from a Ceipal fetch and notify whitelisted users.

    Safety design (after the 2000+ false-notification incident):
      - Only runs on COMPLETE fetches (no rate-limit truncation, all records seen). Partial fetches skip entirely.
      - Only acts on explicit transitions for jobs present in BOTH previous tracker AND current fetch. Never infers closure from "missing" rows.
      - Per-run circuit breaker (JOB_CLOSURE_PER_RUN_CAP, default 25): if exceeded, abort + audit + DO NOT update tracker so next run can re-evaluate after the underlying issue is fixed.
      - Default dry-run: writes to closure_audit only, no emails. Flip JOB_CLOSURE_NOTIFICATIONS_ENABLED=true only after verifying audit output.
      - First run with empty tracker fires nothing — just populates baseline.
      - Per-user dedupe via notifications_collection so retries can't double-send to the same recipient.
    """
    global mongodb_enabled, job_status_tracker_collection, notifications_collection, closure_audit_collection, WHITELISTED_USERS

    if not fetch_complete:
        print("[ClosureDetector] Skipping — fetch was not complete (rate-limited or partial). Tracker NOT updated.")
        return
    if not mongodb_enabled or job_status_tracker_collection is None:
        print("[ClosureDetector] Skipping — MongoDB not available.")
        return
    if not current_status_map:
        print("[ClosureDetector] Skipping — empty status map.")
        return

    try:
        prev_docs = {doc["job_id"]: doc for doc in job_status_tracker_collection.find({})}
        print(f"[ClosureDetector] Tracker has {len(prev_docs)} previous entries; current Ceipal fetch has {len(current_status_map)} jobs.")

        # First-run guard: empty tracker means we have no baseline. Populate and exit without firing.
        if not prev_docs:
            print("[ClosureDetector] First run — populating tracker only, no transitions checked.")
            _update_status_tracker(current_status_map)
            return

        transitions = []
        new_postings = []
        for job_id, curr in current_status_map.items():
            curr_status = (curr.get("status") or "").strip().lower()
            prev_doc = prev_docs.get(job_id)
            if curr_status in OPEN_STATUSES and not prev_doc:
                new_postings.append({
                    "job_id": job_id,
                    "title": curr.get("title") or "Unknown Job",
                    "status": curr.get("status"),
                    "location": curr.get("location") or "",
                    "meta_tags": build_job_posted_meta_tags(
                        specialty=curr.get("specialty") or "",
                        state=curr.get("state") or "",
                        employment_type=curr.get("employment_type") or "Contract",
                        status=curr.get("status") or "",
                        end_client=curr.get("end_client") or "",
                    ),
                })
                continue
            if curr_status not in CLOSED_STATUSES:
                continue
            if not prev_doc:
                continue  # job_id absent from previous tracker — not a transition we'll act on
            prev_status = (prev_doc.get("status") or "").strip().lower()
            if prev_status in OPEN_STATUSES:
                transitions.append({
                    "job_id": job_id,
                    "title": curr.get("title") or prev_doc.get("title") or "Unknown Job",
                    "previous_status": prev_doc.get("status"),
                    "current_status": curr.get("status"),
                })

        print(f"[ClosureDetector] Detected {len(transitions)} genuine Open/Active → Closed transitions")

        print(f"[ClosureDetector] Detected {len(new_postings)} new Open/Active job postings")

        if len(transitions) > JOB_CLOSURE_PER_RUN_CAP:
            print(f"[ClosureDetector] ABORT — {len(transitions)} closures exceeds cap of {JOB_CLOSURE_PER_RUN_CAP}. No notifications sent. Tracker NOT updated.")
            if closure_audit_collection is not None:
                closure_audit_collection.insert_one({
                    "id": str(uuid4()),
                    "type": "abort_cap_exceeded",
                    "detected_count": len(transitions),
                    "cap": JOB_CLOSURE_PER_RUN_CAP,
                    "sample_job_ids": [t["job_id"] for t in transitions[:50]],
                    "detected_at": datetime.now().isoformat(),
                })
            return

        if len(new_postings) > JOB_POSTING_PER_RUN_CAP:
            print(f"[ClosureDetector] ABORT postings - {len(new_postings)} new postings exceeds cap of {JOB_POSTING_PER_RUN_CAP}. No posted-job notifications sent. Tracker NOT updated.")
            if closure_audit_collection is not None:
                closure_audit_collection.insert_one({
                    "id": str(uuid4()),
                    "type": "posting_abort_cap_exceeded",
                    "detected_count": len(new_postings),
                    "cap": JOB_POSTING_PER_RUN_CAP,
                    "sample_job_ids": [t["job_id"] for t in new_postings[:50]],
                    "detected_at": datetime.now().isoformat(),
                })
            return

        recipients = sorted(
            {u.lower() for u in get_whitelisted_emails(force_refresh=True) if u.lower() != ADMIN_EMAIL.lower()}
        )

        for t in new_postings:
            job_id = t["job_id"]
            title = t["title"]
            audit_doc = {
                "id": str(uuid4()),
                "type": "posting_detected",
                "job_id": job_id,
                "job_title": title,
                "current_status": t["status"],
                "meta_tags": t.get("meta_tags") or [],
                "recipients_count": len(recipients),
                "detected_at": datetime.now().isoformat(),
                "notifications_enabled": JOB_POSTING_NOTIFICATIONS_ENABLED,
            }

            if not JOB_POSTING_NOTIFICATIONS_ENABLED:
                print(f"[ClosureDetector] Posting notifications disabled - would notify {len(recipients)} users about new job {job_id} ('{title}')")
                if closure_audit_collection is not None:
                    closure_audit_collection.insert_one(audit_doc)
                continue

            sent_count = 0
            for user_email in recipients:
                if notifications_collection is not None:
                    existing = notifications_collection.find_one({
                        "type": "job_posted",
                        "job_id": job_id,
                        "user_email": user_email,
                    })
                    if existing:
                        continue
                email_sent = send_job_posted_notification_email(
                    user_email,
                    title,
                    job_id,
                    t.get("location") or "",
                    t.get("meta_tags") or [],
                )
                if notifications_collection is not None:
                    notifications_collection.insert_one({
                        "id": str(uuid4()),
                        "type": "job_posted",
                        "job_id": job_id,
                        "job_title": title,
                        "user_email": user_email,
                        "email_sent": email_sent,
                        "meta_tags": t.get("meta_tags") or [],
                        "created_at": datetime.now().isoformat(),
                        "read": False,
                    })
                if email_sent:
                    sent_count += 1

            audit_doc["emails_sent"] = sent_count
            if closure_audit_collection is not None:
                closure_audit_collection.insert_one(audit_doc)
            print(f"[ClosureDetector] Notified {sent_count}/{len(recipients)} users about new posting {job_id}")

        notify_job_closures(transitions, source="ceipal")

        # Update tracker AFTER acting on transitions. Crash mid-batch ⇒ next run re-detects;
        # per-user dedupe prevents double-sends to recipients who already received the email.
        _update_status_tracker(current_status_map)

    except Exception as e:
        import traceback
        print(f"[ClosureDetector] Error: {e}")
        print(f"[ClosureDetector] Traceback: {traceback.format_exc()}")


# Load whitelisted users from Users file
WHITELISTED_USERS = set()
USERS_FILE_PATH = os.path.join(os.path.dirname(__file__), "..", "Users")

def load_whitelisted_users():
    """Load whitelisted users from MongoDB or fallback to Users file"""
    global WHITELISTED_USERS
    WHITELISTED_USERS = set()
    
    # Try MongoDB first
    if mongodb_enabled and whitelist_collection is not None:
        try:
            for doc in whitelist_collection.find():
                WHITELISTED_USERS.add(doc["email"].lower())
            print(f"[Auth] Loaded {len(WHITELISTED_USERS)} whitelisted users from MongoDB")
            return
        except Exception as e:
            print(f"[Auth] Error loading from MongoDB: {e}, falling back to file")
    
    # Fallback to file
    try:
        if os.path.exists(USERS_FILE_PATH):
            with open(USERS_FILE_PATH, "r") as f:
                for line in f:
                    email = line.strip()
                    if email and not email.startswith("#"):
                        WHITELISTED_USERS.add(email.lower())
        print(f"[Auth] Loaded {len(WHITELISTED_USERS)} whitelisted users from file")
    except Exception as e:
        print(f"[Auth] Error loading Users file: {e}")

WHITELIST_CACHE_SECONDS = int(os.getenv("WHITELIST_CACHE_SECONDS", "60"))
_whitelist_loaded_at: Optional[datetime] = None


def get_whitelisted_emails(force_refresh: bool = False) -> set:
    """Return the whitelist, re-reading MongoDB when the in-process copy is stale.

    Each worker keeps its own copy, so an email whitelisted by another worker
    (registration, admin add) was invisible here until restart - which blocked
    logins and suppressed job notification emails for brand-new accounts.
    """
    global _whitelist_loaded_at
    if force_refresh or _whitelist_loaded_at is None or (
        datetime.now() - _whitelist_loaded_at
    ) >= timedelta(seconds=WHITELIST_CACHE_SECONDS):
        load_whitelisted_users()
        _whitelist_loaded_at = datetime.now()
    return WHITELISTED_USERS


def is_whitelisted(email: str) -> bool:
    email_lower = (email or "").strip().lower()
    if email_lower in WHITELISTED_USERS:
        return True
    # Miss on the local copy: confirm against the shared store before rejecting.
    return email_lower in get_whitelisted_emails(force_refresh=True)


def save_whitelisted_users():
    """Save whitelisted users to MongoDB or fallback to Users file"""
    # Try MongoDB first
    if mongodb_enabled and whitelist_collection is not None:
        try:
            # Upsert instead of clear-and-reinsert. With several workers, a worker
            # holding a stale set used to delete every whitelist entry it had not
            # seen, silently cutting newly registered users off from portal access
            # and from job notification emails.
            for email in WHITELISTED_USERS:
                whitelist_collection.update_one(
                    {"email": email.lower()}, {"$set": {"email": email.lower()}}, upsert=True
                )
            print(f"[Auth] Saved {len(WHITELISTED_USERS)} whitelisted users to MongoDB")
            return True
        except Exception as e:
            print(f"[Auth] Error saving to MongoDB: {e}, falling back to file")
    
    # Fallback to file
    try:
        with open(USERS_FILE_PATH, "w") as f:
            for email in sorted(WHITELISTED_USERS):
                f.write(f"{email}\n")
        print(f"[Auth] Saved {len(WHITELISTED_USERS)} whitelisted users to file")
        return True
    except Exception as e:
        print(f"[Auth] Error saving Users file: {e}")
        return False

# JSON-based user storage for persistence without disk
USERS_JSON_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "users.json")

# Initial load
load_whitelisted_users()

# Migrate users from JSON to MongoDB if MongoDB is empty
USERS_CACHE_SECONDS = int(os.getenv("USERS_CACHE_SECONDS", "300"))
MANUAL_JOBS_CACHE_SECONDS = int(os.getenv("MANUAL_JOBS_CACHE_SECONDS", "300"))

_users_cache: Dict[str, Dict[str, Any]] = {}
_users_cache_time: Optional[datetime] = None
_users_cache_lock = Lock()

_manual_jobs_cache: Optional[List["Job"]] = None
_manual_jobs_cache_time: Optional[datetime] = None
_manual_jobs_cache_lock = Lock()
# Held while the manual jobs collection is being re-read, so that when the cache
# expires only one request pays for the scan and the rest reuse its result.
_manual_jobs_rebuild_lock = Lock()

# Fully sanitized + serialized /api/jobs payloads, keyed by role ("admin"/"vendor").
# Rebuilt only when the underlying job lists change, instead of re-running the
# compensation/client-name regexes over every job on every request.
_jobs_response_cache: Dict[str, Dict[str, Any]] = {}
_jobs_response_cache_lock = Lock()
_jobs_response_generation = 0
# Last payload built per role. Never cleared: while the background refresher
# (jobs_refresh_loop) rebuilds after a change, requests keep getting this one
# instead of waiting ~0.5-1 s for MongoDB round trips to the remote cluster.
_jobs_response_last_good: Dict[str, Dict[str, Any]] = {}
_jobs_refresh_loop_ref: Optional[asyncio.AbstractEventLoop] = None
_jobs_refresh_wakeup: Optional[asyncio.Event] = None


def wake_jobs_refresher() -> None:
    """Ask the background refresher to rebuild now. Safe to call from any thread."""
    loop, event = _jobs_refresh_loop_ref, _jobs_refresh_wakeup
    if loop is not None and event is not None and not loop.is_closed():
        loop.call_soon_threadsafe(event.set)


def clear_jobs_response_cache() -> None:
    global _jobs_response_generation
    with _jobs_response_cache_lock:
        _jobs_response_cache.clear()
        _jobs_response_generation += 1
    wake_jobs_refresher()


def _cache_is_fresh(cache_time: Optional[datetime], ttl_seconds: int) -> bool:
    return bool(cache_time and (datetime.now() - cache_time) < timedelta(seconds=ttl_seconds))


# Per-user lookups for authenticated requests. get_current_user used to reach
# MongoDB on most requests once the all-users cache aged out (and that cache is
# never refilled per user). Kept short so deactivations still take effect quickly.
USER_LOOKUP_CACHE_SECONDS = int(os.getenv("USER_LOOKUP_CACHE_SECONDS", "60"))
USER_LOOKUP_CACHE_MAX = 2000
_user_lookup_cache: Dict[str, Any] = {}


def clear_users_cache() -> None:
    global _users_cache, _users_cache_time
    with _users_cache_lock:
        _users_cache = {}
        _users_cache_time = None
        _user_lookup_cache.clear()
    print("[Auth] User cache cleared")


# Cross-worker coherence without Redis: every job write bumps one tiny shared
# document, and each worker polls it (at most every JOBS_VERSION_POLL_SECONDS)
# instead of waiting out a 5 minute TTL.
JOBS_VERSION_POLL_SECONDS = float(os.getenv("JOBS_VERSION_POLL_SECONDS", "5"))
_jobs_version_seen: Optional[int] = None
_jobs_version_checked_at = 0.0
_jobs_version_lock = Lock()


def bump_jobs_version() -> None:
    """Tell every worker the job set changed. Call after a successful job write."""
    global _jobs_version_seen
    if not (mongodb_enabled and meta_collection is not None):
        return
    try:
        doc = meta_collection.find_one_and_update(
            {"_id": "jobs"},
            {"$inc": {"version": 1}, "$set": {"updated_at": datetime.utcnow()}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        with _jobs_version_lock:
            # This worker clears its own cache right after, so don't treat our
            # own bump as a foreign change on the next poll.
            _jobs_version_seen = (doc or {}).get("version")
    except Exception as e:
        print(f"[JobsVersion] Could not bump jobs version: {e}")


def refresh_jobs_if_version_changed() -> None:
    """Drop this worker's job caches when another worker or the sync changed jobs."""
    global _jobs_version_seen, _jobs_version_checked_at
    if not (mongodb_enabled and meta_collection is not None):
        return
    now = _monotonic()
    with _jobs_version_lock:
        if now - _jobs_version_checked_at < JOBS_VERSION_POLL_SECONDS:
            return
        _jobs_version_checked_at = now
    try:
        doc = meta_collection.find_one({"_id": "jobs"}, {"version": 1})
    except Exception as e:
        print(f"[JobsVersion] Could not read jobs version: {e}")
        return
    version = (doc or {}).get("version", 0)
    with _jobs_version_lock:
        previous, _jobs_version_seen = _jobs_version_seen, version
    if previous is not None and previous != version:
        clear_manual_jobs_cache(bump=False)


def clear_manual_jobs_cache(bump: bool = True) -> None:
    global _manual_jobs_cache, _manual_jobs_cache_time
    if bump:
        bump_jobs_version()
    with _manual_jobs_cache_lock:
        _manual_jobs_cache = None
        _manual_jobs_cache_time = None
    clear_jobs_response_cache()


def _normalize_is_active(value: Any) -> str:
    """Coerce legacy active flags into the string format used by the current API."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "false"
    return "true" if str(value).strip().lower() in {"1", "true", "yes", "y"} else "false"


def is_user_active(user: Optional[dict]) -> bool:
    return _normalize_is_active((user or {}).get("is_active")) == "true"


def _normalize_user_record(email_key: str, user_data: dict) -> dict:
    email = (user_data.get("email") or email_key or "").strip().lower()
    full_name = (user_data.get("full_name") or email.split("@")[0] or email).strip()
    return {
        "id": user_data.get("id") or str(uuid4()),
        "email": email,
        "full_name": full_name,
        "hashed_password": user_data.get("hashed_password", ""),
        "is_active": _normalize_is_active(user_data.get("is_active")),
        "created_at": user_data.get("created_at", datetime.now().isoformat()),
    }


def _normalize_user_doc(doc: dict) -> dict:
    return _normalize_user_record(doc.get("email", ""), doc)


def migrate_users_to_mongodb():
    """Migrate users from local JSON to MongoDB if MongoDB is connected but empty"""
    global _users
    if mongodb_enabled and users_collection is not None:
        # Check if MongoDB has any users
        mongo_user_count = users_collection.count_documents({})
        if mongo_user_count == 0:
            # Check if we have users in JSON file
            if os.path.exists(USERS_JSON_FILE):
                try:
                    with open(USERS_JSON_FILE, "r") as f:
                        json_users = json.load(f)
                    if json_users:
                        # Migrate to MongoDB
                        for email, user_data in json_users.items():
                            users_collection.update_one(
                                {"email": email.lower()},
                                {"$set": {
                                    "id": user_data["id"],
                                    "email": user_data["email"],
                                    "full_name": user_data["full_name"],
                                    "hashed_password": user_data["hashed_password"],
                                    "is_active": user_data["is_active"],
                                    "created_at": user_data.get("created_at", datetime.now().isoformat())
                                }},
                                upsert=True
                            )
                        print(f"[Migration] Migrated {len(json_users)} users from JSON to MongoDB")
                        # Reload users
                        _users = load_users_from_json()
                    else:
                        print("[Migration] JSON file exists but is empty")
                except Exception as e:
                    print(f"[Migration] Error migrating users: {e}")
        else:
            print(f"[Migration] MongoDB already has {mongo_user_count} users, skipping migration")
    else:
        print("[Migration] MongoDB not available, skipping migration")

# Users loading function
def load_users_from_json(force_refresh: bool = False):
    """Load users from MongoDB or fallback to JSON file"""
    global _users_cache, _users_cache_time

    if not force_refresh and _users_cache and _cache_is_fresh(_users_cache_time, USERS_CACHE_SECONDS):
        with _users_cache_lock:
            return dict(_users_cache)

    # Try MongoDB first
    if mongodb_enabled and users_collection is not None:
        try:
            users = {}
            for doc in users_collection.find():
                email = doc["email"].lower()
                users[email] = _normalize_user_doc(doc)
            print(f"[Auth] Loaded {len(users)} users from MongoDB")
            with _users_cache_lock:
                _users_cache = users
                _users_cache_time = datetime.now()
            return dict(users)
        except Exception as e:
            print(f"[Auth] Error loading users from MongoDB: {e}, falling back to JSON")
    
    # Fallback to JSON file
    if os.path.exists(USERS_JSON_FILE):
        try:
            with open(USERS_JSON_FILE, "r") as f:
                raw_users = json.load(f)
            if not isinstance(raw_users, dict):
                print(f"[Auth] Users JSON must be an object, got {type(raw_users).__name__}; ignoring file")
                return {}
            users = {
                (email or "").strip().lower(): _normalize_user_record(email, user_data)
                for email, user_data in raw_users.items()
                if isinstance(user_data, dict)
            }
            with _users_cache_lock:
                _users_cache = users
                _users_cache_time = datetime.now()
            return dict(users)
        except json.JSONDecodeError:
            try:
                with open(USERS_JSON_FILE, "r") as f:
                    raw_text = f.read()
                if "@" in raw_text and "hashed_password" not in raw_text:
                    print("[Auth] Users JSON contains a plain email list, not password records; ignoring malformed file")
                else:
                    print("[Auth] Users JSON is malformed; ignoring file")
            except Exception as read_error:
                print(f"[Auth] Error reading malformed users JSON: {read_error}")
        except Exception as e:
            print(f"[Auth] Error loading users JSON: {e}")
    return {}


def load_user_by_email(email: str, prefer_cache: bool = True) -> Optional[dict]:
    """Load one user without forcing a full collection scan."""
    global _users_cache, _users_cache_time

    email_lower = (email or "").strip().lower()
    if not email_lower:
        return None

    if prefer_cache:
        hit = _user_lookup_cache.get(email_lower)
        if hit is not None and _monotonic() - hit[0] < USER_LOOKUP_CACHE_SECONDS:
            return hit[1]

    if prefer_cache and _users_cache and _cache_is_fresh(_users_cache_time, USERS_CACHE_SECONDS):
        cached = _users_cache.get(email_lower)
        if cached is not None:
            return cached
        # A cache miss is not proof the account is absent - it may have been
        # created by another worker after this cache was built. Fall through to
        # the shared store rather than reporting the user as unknown.

    if mongodb_enabled and users_collection is not None:
        try:
            doc = users_collection.find_one({"email": email_lower})
            if doc is None:
                doc = users_collection.find_one({
                    "email": {
                        "$regex": f"^{re.escape(email_lower)}$",
                        "$options": "i",
                    }
                })
            if doc is None:
                # Unknown account. This used to reload the whole users collection
                # (a full scan) for every unknown or mistyped e-mail.
                return None
            user = _normalize_user_doc(doc)
            if prefer_cache:
                with _users_cache_lock:
                    if len(_user_lookup_cache) >= USER_LOOKUP_CACHE_MAX:
                        _user_lookup_cache.clear()
                    _user_lookup_cache[email_lower] = (_monotonic(), user)
            with _users_cache_lock:
                # Only ever add to a cache that is already fresh and complete.
                # Seeding a brand-new cache from a single lookup used to mark a
                # one-user dict as "fresh", after which every other user looked
                # unknown and valid logins failed with 401.
                if _cache_is_fresh(_users_cache_time, USERS_CACHE_SECONDS):
                    _users_cache[email_lower] = user
            return user
        except Exception as e:
            print(f"[Auth] Error loading user {email_lower} from MongoDB: {e}, falling back to cache/JSON")

    users = load_users_from_json(force_refresh=not prefer_cache)
    return users.get(email_lower)

def save_users_to_json(users):
    """Save users to MongoDB or fallback to JSON file"""
    normalized_users = {
        (email or "").strip().lower(): _normalize_user_record(email, user_data)
        for email, user_data in users.items()
        if isinstance(user_data, dict)
    }

    # Try MongoDB first
    if mongodb_enabled and users_collection is not None:
        try:
            # Update each user individually (upsert)
            for email, user_data in normalized_users.items():
                users_collection.update_one(
                    {"email": email.lower()},
                    {"$set": {
                        "id": user_data["id"],
                        "email": user_data["email"],
                        "full_name": user_data["full_name"],
                        "hashed_password": user_data["hashed_password"],
                        "is_active": user_data["is_active"],
                        "created_at": user_data.get("created_at", datetime.now().isoformat())
                    }},
                    upsert=True
                )
            print(f"[Auth] Saved {len(normalized_users)} users to MongoDB")
            clear_users_cache()
            return True
        except Exception as e:
            print(f"[Auth] Error saving users to MongoDB: {e}, falling back to JSON")
    
    # Fallback to JSON file
    try:
        os.makedirs(os.path.dirname(USERS_JSON_FILE), exist_ok=True)
        with open(USERS_JSON_FILE, "w") as f:
            json.dump(normalized_users, f, indent=2)
        clear_users_cache()
        return True
    except Exception as e:
        print(f"[Auth] Error saving users JSON: {e}")
        return False

def save_user_record(user_data: dict) -> bool:
    """Persist exactly one user.

    Writing a single account through save_users_to_json() meant rewriting the
    whole user set from whatever the caller happened to be holding, which could
    drop accounts created by another worker. This touches only one record.
    """
    user = _normalize_user_record(user_data.get("email", ""), user_data)
    email_lower = user["email"]
    if not email_lower:
        return False

    if mongodb_enabled and users_collection is not None:
        try:
            users_collection.update_one(
                {"email": email_lower},
                {"$set": {
                    "id": user["id"],
                    "email": user["email"],
                    "full_name": user["full_name"],
                    "hashed_password": user["hashed_password"],
                    "is_active": user["is_active"],
                    "created_at": user["created_at"],
                }},
                upsert=True,
            )
            clear_users_cache()
            return True
        except Exception as e:
            print(f"[Auth] Error saving user {email_lower} to MongoDB: {e}, falling back to JSON")

    # JSON fallback: merge into whatever is on disk instead of overwriting it.
    try:
        existing = {}
        if os.path.exists(USERS_JSON_FILE):
            with open(USERS_JSON_FILE, "r") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                existing = loaded
        existing[email_lower] = user
        os.makedirs(os.path.dirname(USERS_JSON_FILE), exist_ok=True)
        with open(USERS_JSON_FILE, "w") as f:
            json.dump(existing, f, indent=2)
        clear_users_cache()
        return True
    except Exception as e:
        print(f"[Auth] Error saving user {email_lower} to JSON: {e}")
        return False


def add_whitelisted_email(email: str) -> bool:
    """Whitelist one email without rewriting the whole whitelist collection."""
    global WHITELISTED_USERS
    email_lower = (email or "").strip().lower()
    if not email_lower:
        return False

    WHITELISTED_USERS.add(email_lower)
    if mongodb_enabled and whitelist_collection is not None:
        try:
            whitelist_collection.update_one(
                {"email": email_lower}, {"$set": {"email": email_lower}}, upsert=True
            )
            return True
        except Exception as e:
            print(f"[Auth] Error whitelisting {email_lower} in MongoDB: {e}, falling back to file")
    return save_whitelisted_users()


def remove_whitelisted_email(email: str) -> bool:
    """Remove one email from the whitelist without rewriting the collection."""
    global WHITELISTED_USERS
    email_lower = (email or "").strip().lower()
    WHITELISTED_USERS.discard(email_lower)
    if mongodb_enabled and whitelist_collection is not None:
        try:
            whitelist_collection.delete_many({"email": email_lower})
            return True
        except Exception as e:
            print(f"[Auth] Error removing {email_lower} from MongoDB whitelist: {e}, falling back to file")
    return save_whitelisted_users()


# Initialize users cache
_users = load_users_from_json()

# Seed admin user if no admin exists
def seed_admin_user():
    """Create admin@radixsol.com with default password if not exists"""
    global _users
    admin_email = "admin@radixsol.com"
    
    # Check if admin already exists
    if admin_email.lower() in _users:
        existing_user = _users[admin_email.lower()]
        # Fix is_active if it's boolean instead of string
        if existing_user.get("is_active") == True:
            print(f"[Seed] Fixing is_active field for admin user...")
            existing_user["is_active"] = "true"
            save_user_record(existing_user)
        print(f"[Seed] Admin user {admin_email} already exists")
        return
    
    # Create admin user with default password
    default_password = "Admin@123"  # User should change this after first login
    hashed_password = bcrypt.hashpw(default_password.encode(), bcrypt.gensalt()).decode()
    
    admin_user = {
        "id": str(uuid4()),
        "email": admin_email,
        "full_name": "System Administrator",
        "hashed_password": hashed_password,
        "is_active": "true",
        "created_at": datetime.now().isoformat()
    }
    
    _users[admin_email.lower()] = admin_user
    save_user_record(admin_user)

    # Also add to whitelist
    add_whitelisted_email(admin_email.lower())
    
    print(f"[Seed] Created admin user: {admin_email} with password: {default_password}")
    print(f"[Seed] IMPORTANT: Please change this password after first login!")

seed_admin_user()

# Migrate users from JSON to MongoDB (run after seeding admin)
migrate_users_to_mongodb()

# In-memory user cache (loaded from MongoDB/JSON on startup)
_users_cache = dict(_users)
_users_cache_time = datetime.now()

# Legacy SQLAlchemy SQLite support has been removed.
# The application uses MongoDB/JSON-backed storage only.
UserDB = Dict[str, Any]

# JWT Configuration
SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
SESSION_EXPIRE_DAYS = int(os.getenv("SESSION_EXPIRE_DAYS", "7"))


# Password hashing - use bcrypt directly (passlib incompatible with bcrypt 4.x)
def verify_password(plain_password: str, hashed_password: str) -> bool:
    # bcrypt has 72-byte limit, truncate if necessary
    password_bytes = plain_password.encode('utf-8')
    if len(password_bytes) > 72:
        password_bytes = password_bytes[:72]
    hashed_bytes = hashed_password.encode('utf-8')
    return bcrypt.checkpw(password_bytes, hashed_bytes)

def get_password_hash(password: str) -> str:
    # bcrypt has 72-byte limit, truncate if necessary
    password_bytes = password.encode('utf-8')
    if len(password_bytes) > 72:
        password_bytes = password_bytes[:72]
    hashed = bcrypt.hashpw(password_bytes, bcrypt.gensalt())
    return hashed.decode('utf-8')


app = FastAPI(title="VMS Backend API", version="1.0.0")

# Background task to fetch jobs continuously
async def scheduled_job_fetch():
    """Fetch jobs every 5 minutes in background"""
    if not CEIPAL_ENABLED:
        print("[Scheduled] Ceipal integration disabled; scheduler not started")
        return
    while True:
        try:
            print("[Scheduled] Starting background job fetch...")
            await ceipal_client.fetch_all_jobs_background()
            print("[Scheduled] Background job fetch completed")
        except Exception as e:
            print(f"[Scheduled] Error in background fetch: {e}")
        
        # Wait 5 minutes before next fetch
        await asyncio.sleep(300)

async def run_startup_purge() -> None:
    try:
        await asyncio.to_thread(purge_closed_jobs_at_startup)
    except Exception as e:
        print(f"[PurgeClosed] Startup purge failed; closed jobs stay hidden and it retries next start: {e}")


@app.on_event("startup")
async def startup_event():
    """Start background job fetch on startup"""
    if CEIPAL_ENABLED:
        print("[Startup] Starting scheduled background job fetch...")
        asyncio.create_task(scheduled_job_fetch())
    else:
        print("[Startup] Ceipal integration disabled; background fetch skipped")
    # asyncio.to_thread (auth lookups, job payloads) defaults to cpu_count + 4
    # threads: only 5 on the 1-vCPU production instance. That work is mostly
    # waiting on MongoDB, so a burst of tabs queued behind five threads.
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=int(os.getenv("ASYNC_THREAD_POOL_SIZE", "32")), thread_name_prefix="vms-io")
    )
    if mongodb_enabled:
        asyncio.create_task(jobs_refresh_loop())
        asyncio.create_task(run_startup_purge())
        print(f"[Startup] Job notification digest every {JOB_NOTIFICATION_DIGEST_SECONDS}s")
        asyncio.create_task(job_notification_digest_loop())
    if JOB_STATUS_REPORT_ENABLED and JOB_STATUS_REPORT_RECIPIENTS:
        print(f"[Startup] Job status report to {', '.join(JOB_STATUS_REPORT_RECIPIENTS)} every {JOB_STATUS_REPORT_SECONDS}s")
        asyncio.create_task(job_status_report_loop())

# Web UI (HTML/CSS/JS)
WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
ASSETS_DIR = os.path.join(WEB_DIR, "assets")
if os.path.isdir(ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")

# Compress JSON responses. /api/jobs carries every job with its full description,
# and was previously shipped uncompressed to every tab on every load.
app.add_middleware(GZipMiddleware, minimum_size=1024)

SLOW_REQUEST_MS = float(os.getenv("SLOW_REQUEST_MS", "500"))


@app.middleware("http")
async def timing_and_cache_headers(request: Request, call_next):
    """Time every request (X-Process-Time-Ms) and log the slow ones, so lag can be
    traced to a route instead of guessed at. Also lets browsers reuse static assets."""
    started = _monotonic()
    response = await call_next(request)
    elapsed_ms = (_monotonic() - started) * 1000
    response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.0f}"
    path = request.url.path
    if elapsed_ms >= SLOW_REQUEST_MS and path not in ("/healthz", "/readyz"):
        print(f"[Slow] {request.method} {path} -> {response.status_code} in {elapsed_ms:.0f} ms")
    if path.startswith("/assets/") and "cache-control" not in response.headers:
        # Revalidated by ETag/Last-Modified after a day, so a deploy is picked up.
        response.headers["Cache-Control"] = "public, max-age=3600, stale-while-revalidate=86400"
    return response


# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security
security = HTTPBearer()


@app.get("/")
async def serve_web_app():
    index_path = os.path.join(WEB_DIR, "index.html")
    if not os.path.isfile(index_path):
        raise HTTPException(status_code=404, detail="Web UI not found")
    return FileResponse(index_path)

# Configuration
ATS_API_BASE_URL = os.getenv("ATS_API_BASE_URL", "https://api.ats-provider.com/v1")
ATS_API_KEY = os.getenv("ATS_API_KEY", "")

# Ceipal API Configuration
CEIPAL_AUTH_URL = os.getenv("CEIPAL_AUTH_URL", "https://api.ceipal.com/v1/createAuthtoken/")
# New API endpoint with 50 limit
CEIPAL_REPORTS_URL = os.getenv("CEIPAL_REPORTS_URL", "https://bi.ceipal.com/ReportDetails/getReportsData/ekZMUmhQVVhCNzRhbzcwcEpwZnN6Zz09")
CEIPAL_EMAIL = os.getenv("CEIPAL_EMAIL", "amir@radixsol.com")
CEIPAL_PASSWORD = os.getenv("CEIPAL_PASSWORD", "")
CEIPAL_API_KEY = os.getenv("CEIPAL_API_KEY", "2693f0ed28f2250811fe40294e97e108a56afa9043e5336da4")
CEIPAL_CACHE_DIR = os.getenv("CEIPAL_CACHE_DIR", "./data/cache")
CEIPAL_ENABLED = False
DEBUG = os.getenv("DEBUG", "False").lower() in {"1", "true", "yes", "y"}

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "./data/uploads")
DATA_DIR = os.getenv("DATA_DIR", "./data")
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 10485760))  # 10MB

# Excel Jobs File Configuration
EXCEL_JOBS_FILE = os.getenv("EXCEL_JOBS_FILE", "VMS Job Fiule.xlsx")
UPLOADED_EXCEL_FILES: List[str] = []  # Track admin-uploaded Excel files (multiple files supported)
MANUAL_JOBS_FILE = os.path.join(DATA_DIR, "manual_jobs.json")

# Ensure directories exist
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

# Pydantic Models
class Job(BaseModel):
    id: str
    title: str
    description: str
    requirements: Optional[str] = None
    department: str
    location: str
    employment_type: str
    salary_range: Optional[str] = None
    posted_date: datetime
    status: str
    end_client: Optional[str] = None
    job_id: Optional[str] = None
    profession: Optional[str] = None
    specialty: Optional[str] = None
    state: Optional[str] = None
    bill_rate_discount_applied: bool = False

class DirectJobCreateRequest(BaseModel):
    job_id: Optional[str] = None
    job_code: Optional[str] = None
    job_type: str
    status: str
    profession: str
    specialty: str
    city: str
    state: str
    jobdescription: str
    billrate: str
    client: Optional[str] = None

    @root_validator(pre=True)
    def normalize_job_id(cls, values):
        if not values.get("job_id") and values.get("job_code"):
            values["job_id"] = values["job_code"]
        if not values.get("job_code") and values.get("job_id"):
            values["job_code"] = values["job_id"]
        return values


class BulkDirectJobCreateRequest(BaseModel):
    jobs: List[DirectJobCreateRequest]
    send_notifications: bool = False


class AdminJobDeleteRequest(BaseModel):
    email: str
    password: str


class BulkJobCloseRequest(BaseModel):
    job_ids: List[str]


class Candidate(BaseModel):
    id: str
    name: str
    email: str
    phone: Optional[str] = None
    job_id: str
    resume_path: str
    submitted_date: datetime
    status: str = "submitted"

class JobListResponse(BaseModel):
    jobs: List[Job]
    total: int
    total_pages: int = 0
    next_start_page: int = 0
    has_more: bool = False
    is_refreshing: bool = False
    cache_age_seconds: Optional[int] = None

class CandidateSubmission(BaseModel):
    candidate_name: str
    email: str
    phone: Optional[str] = None
    job_id: str

# Auth Pydantic Models
class UserCreate(BaseModel):
    email: str
    full_name: Optional[str] = None
    password: str

class UserLogin(BaseModel):
    email: str
    password: str

class OtpRequest(BaseModel):
    email: str

class OtpVerify(BaseModel):
    email: str
    otp: str

class UserResponse(BaseModel):
    id: str
    email: str
    full_name: str
    is_active: str
    created_at: datetime

class Token(BaseModel):
    access_token: str
    token_type: str
    user: UserResponse


class TokenData(BaseModel):
    email: Optional[str] = None

# Auth Utility Functions - now using bcrypt directly (defined above)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(token: str = Depends(HTTPBearer())):
    """Get current user from JSON storage"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
        token_data = TokenData(email=email)
    except JWTError:
        raise credentials_exception
    
    email_lower = token_data.email.lower()
    user = await asyncio.to_thread(load_user_by_email, email_lower)
    
    if user is None:
        print(f"[Auth] User not found for email: {email_lower}")
        raise credentials_exception
    if not is_user_active(user):
        print(f"[Auth] User {email_lower} is inactive: {user.get('is_active')}")
        raise HTTPException(status_code=400, detail="Inactive user")
    
    # Return as dict-like object for compatibility
    return type('User', (), {
        'id': user["id"],
        'email': user["email"],
        'full_name': user["full_name"],
        'is_active': user["is_active"],
        'hashed_password': user["hashed_password"],
        'created_at': user["created_at"],
    })()


def verify_admin_credentials(email: str, password: str) -> None:
    """Validate an admin email/password pair without requiring a bearer token."""
    email_lower = (email or "").lower().strip()
    if email_lower != ADMIN_EMAIL.lower():
        raise HTTPException(status_code=403, detail="Only admin can delete jobs")

    user = load_user_by_email(email_lower)
    if not user:
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    if not is_user_active(user):
        raise HTTPException(status_code=400, detail="Inactive user")
    if not verify_password(password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Incorrect email or password")


# Auth Endpoints
@app.post("/api/auth/register")
async def register(user_data: UserCreate, request: Request):
    """Start registration by emailing an OTP. The account is created only after OTP verification."""
    cleanup_expired_otps()

    email = user_data.email.strip()
    email_lower = email.lower()
    password = user_data.password or ""
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email is required")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    existing = await asyncio.to_thread(load_user_by_email, email_lower)
    if existing and is_user_active(existing):
        raise HTTPException(status_code=400, detail="Account already exists. Please log in.")

    otp = f"{secrets.randbelow(1000000):06d}"
    full_name = (user_data.full_name or email.split("@")[0]).strip() or email_lower
    store_auth_token("otp", email_lower, {
        "otp_hash": _hash_otp(otp),
        "expires": datetime.now() + timedelta(minutes=OTP_EXPIRE_MINUTES),
        "attempts": 0,
        "purpose": "registration",
        "pending_user": {
            "id": str(uuid4()),
            "email": email_lower,
            "full_name": full_name,
            "hashed_password": await asyncio.to_thread(get_password_hash, password),
            "is_active": "true",
            "created_at": datetime.now().isoformat(),
        },
        "user_agent": request.headers.get("user-agent", ""),
        "ip_address": get_client_ip(request),
    })

    email_sent = await asyncio.to_thread(send_registration_otp_email, email_lower, otp)
    if not email_sent:
        delete_auth_token("otp", email_lower)
        raise HTTPException(
            status_code=503,
            detail="We could not send the verification code. Please contact support.",
        )

    return {"message": "OTP sent. Please check your email.", "expires_minutes": OTP_EXPIRE_MINUTES}


@app.post("/api/auth/verify-registration-otp")
async def verify_registration_otp(request_data: OtpVerify):
    """Create and verify the account only when the registration OTP is correct."""
    cleanup_expired_otps()

    email_lower = request_data.email.lower().strip()
    otp = (request_data.otp or "").strip()
    token_data = get_auth_token("otp", email_lower)

    if not token_data or token_data.get("purpose") != "registration":
        raise HTTPException(status_code=400, detail="Invalid or expired verification code")
    if token_data["expires"] < datetime.now():
        delete_auth_token("otp", email_lower)
        raise HTTPException(status_code=400, detail="Verification code has expired")
    if token_data.get("attempts", 0) >= OTP_MAX_ATTEMPTS:
        delete_auth_token("otp", email_lower)
        raise HTTPException(status_code=429, detail="Too many failed attempts. Please sign up again.")

    if _hash_otp(otp) != token_data["otp_hash"]:
        bump_auth_token_attempts("otp", email_lower, token_data.get("attempts", 0))
        raise HTTPException(status_code=400, detail="Invalid verification code")

    pending_user = token_data.get("pending_user")
    if not pending_user:
        delete_auth_token("otp", email_lower)
        raise HTTPException(status_code=400, detail="Registration session has expired")

    existing = await asyncio.to_thread(load_user_by_email, email_lower, False)
    if existing and is_user_active(existing):
        delete_auth_token("otp", email_lower)
        raise HTTPException(status_code=400, detail="Account already exists. Please log in.")

    if not save_user_record(pending_user):
        raise HTTPException(status_code=500, detail="Could not complete registration")

    add_whitelisted_email(email_lower)
    clear_users_cache()
    delete_auth_token("otp", email_lower)

    return {"message": "Account verified. You can now log in."}

def get_or_create_otp_user(email: str) -> dict:
    """Return an active whitelisted user, creating a profile on first OTP login."""
    global _users_cache

    email = (email or "").strip()
    email_lower = email.lower()
    user = load_user_by_email(email_lower, prefer_cache=False)

    if not user:
        if not is_whitelisted(email_lower):
            raise HTTPException(status_code=401, detail="Email is not authorized for portal access")

        print(f"[Auth] Auto-creating OTP user: {email}")
        user_id = str(uuid4())
        user = {
            "id": user_id,
            "email": email_lower,
            "full_name": email.split('@')[0],
            "hashed_password": get_password_hash(secrets.token_urlsafe(32)),
            "is_active": "true",
            "created_at": datetime.now().isoformat()
        }
        save_user_record(user)

    if not is_user_active(user):
        raise HTTPException(status_code=400, detail="Inactive user")
    return user


@app.post("/api/auth/request-otp")
async def request_login_otp(request_data: OtpRequest, request: Request):
    """Send a one-time email verification code to a whitelisted user."""
    cleanup_expired_otps()

    email = request_data.email.strip()
    email_lower = email.lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email is required")

    # Do not create a user until the OTP is verified, but require whitelist before sending.
    if not is_whitelisted(email_lower):
        raise HTTPException(status_code=401, detail="Email is not authorized for portal access")

    otp = f"{secrets.randbelow(1000000):06d}"
    store_auth_token("otp", email_lower, {
        "otp_hash": _hash_otp(otp),
        "expires": datetime.now() + timedelta(minutes=OTP_EXPIRE_MINUTES),
        "attempts": 0,
        "purpose": "login",
        "user_agent": request.headers.get("user-agent", ""),
        "ip_address": get_client_ip(request),
    })

    email_sent = await asyncio.to_thread(send_login_otp_email, email_lower, otp)
    if not email_sent:
        delete_auth_token("otp", email_lower)
        raise HTTPException(
            status_code=503,
            detail="We could not send the verification code. Please contact support.",
        )

    return {"message": "Email has been sent. Please check your inbox.", "expires_minutes": OTP_EXPIRE_MINUTES}


@app.post("/api/auth/verify-otp", response_model=Token)
def verify_login_otp(request_data: OtpVerify):
    """Verify email OTP and return a 7-day JWT session."""
    cleanup_expired_otps()

    email_lower = request_data.email.lower().strip()
    otp = (request_data.otp or "").strip()
    token_data = get_auth_token("otp", email_lower)

    if not token_data or token_data.get("purpose") == "registration":
        raise HTTPException(status_code=400, detail="Invalid or expired verification code")
    if token_data["expires"] < datetime.now():
        delete_auth_token("otp", email_lower)
        raise HTTPException(status_code=400, detail="Verification code has expired")
    if token_data.get("attempts", 0) >= OTP_MAX_ATTEMPTS:
        delete_auth_token("otp", email_lower)
        raise HTTPException(status_code=429, detail="Too many failed attempts. Request a new code.")

    if _hash_otp(otp) != token_data["otp_hash"]:
        bump_auth_token_attempts("otp", email_lower, token_data.get("attempts", 0))
        raise HTTPException(status_code=400, detail="Invalid verification code")

    user = get_or_create_otp_user(email_lower)
    delete_auth_token("otp", email_lower)

    access_token = create_access_token(
        data={"sub": user["email"]},
        expires_delta=timedelta(days=SESSION_EXPIRE_DAYS),
    )

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "id": user["id"],
            "email": user["email"],
            "full_name": user["full_name"],
            "is_active": user["is_active"],
            "created_at": user["created_at"]
        }
    }


@app.post("/api/auth/login", response_model=Token)
async def login(user_data: UserLogin):
    """Login existing users with email and password."""
    global _users_cache

    email_lower = user_data.email.lower().strip()
    user = await asyncio.to_thread(load_user_by_email, email_lower, False)

    if not user:
        if not is_whitelisted(email_lower):
            raise HTTPException(status_code=401, detail="Incorrect email or password")

        # Re-check the shared store, bypassing this worker's cache entirely,
        # before concluding the account does not exist.
        user = await asyncio.to_thread(load_user_by_email, email_lower, False)

    if not user:
        # Restore legacy behavior: whitelisted users without a stored password
        # can be provisioned again on first password login.
        print(f"[Auth] Recreating missing password user for whitelisted email: {email_lower}")
        user = {
            "id": str(uuid4()),
            "email": email_lower,
            "full_name": email_lower.split("@")[0],
            "hashed_password": get_password_hash(user_data.password),
            "is_active": "true",
            "created_at": datetime.now().isoformat(),
        }
        if not await asyncio.to_thread(save_user_record, user):
            raise HTTPException(status_code=500, detail="Could not restore account access")

    if not is_user_active(user):
        raise HTTPException(status_code=400, detail="Inactive user")
    # bcrypt is deliberately slow (~100ms+); keep it off the event loop.
    if not await asyncio.to_thread(verify_password, user_data.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Incorrect email or password")

    access_token = create_access_token(
        data={"sub": user["email"]},
        expires_delta=timedelta(days=SESSION_EXPIRE_DAYS),
    )

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "id": user["id"],
            "email": user["email"],
            "full_name": user["full_name"],
            "is_active": user["is_active"],
            "created_at": user["created_at"]
        }
    }

class ForgotPasswordRequest(BaseModel):
    email: str

class VendorMessageRequest(BaseModel):
    subject: str
    message: str


class ResetPasswordWithToken(BaseModel):
    token: str
    password: str

@app.post("/api/auth/forgot-password")
async def forgot_password(request: ForgotPasswordRequest):
    """Send password reset email to user"""
    global _password_reset_tokens
    
    # Clean up expired tokens first
    cleanup_expired_tokens()
    
    email_lower = request.email.lower().strip()
    if not email_lower or "@" not in email_lower:
        raise HTTPException(status_code=400, detail="Valid email is required")
    
    # Read the shared store directly: a stale per-worker cache must never make a
    # real account look unregistered.
    user = await asyncio.to_thread(load_user_by_email, email_lower, False)

    # A registered, active account may reset its password. Anything else gets the
    # same non-revealing response.
    if not user or not is_user_active(user):
        if not is_whitelisted(email_lower):
            print(f"[Auth] Password reset requested for unknown email {email_lower}")
        return {"message": "If the email is registered, a password reset link has been sent."}

    # Generate reset token
    reset_token = str(uuid4())

    # Store token with expiration (1 hour), shared across workers
    store_auth_token("reset", reset_token, {
        "email": email_lower,
        "expires": datetime.now() + timedelta(hours=1),
        "used": False,
    })
    
    # Send email
    email_sent = await asyncio.to_thread(send_password_reset_email, email_lower, reset_token)
    
    if email_sent:
        print(f"[Auth] Password reset email sent to {email_lower}")
        return {"message": "Password reset email sent. Please check your inbox."}
    else:
        delete_auth_token("reset", reset_token)
        print(f"[Auth] Password reset email failed for {email_lower}")
        raise HTTPException(
            status_code=503,
            detail="We could not send the password reset email. Please contact support.",
        )

@app.post("/api/auth/reset-password")
async def reset_password_with_token(data: ResetPasswordWithToken):
    """Reset password using token from email"""
    global _users_cache, _password_reset_tokens
    
    # Clean up expired tokens
    cleanup_expired_tokens()
    
    token = (data.token or "").strip()

    # Validate token against the shared store so it resolves on any worker.
    token_data = get_auth_token("reset", token) if token else None
    if not token_data:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    if token_data.get("used"):
        raise HTTPException(status_code=400, detail="Reset token has already been used")

    if token_data["expires"] < datetime.now():
        delete_auth_token("reset", token)
        raise HTTPException(status_code=400, detail="Reset token has expired")

    email_lower = token_data["email"]

    # Validate password
    if len(data.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    user = await asyncio.to_thread(load_user_by_email, email_lower, False)
    if not user:
        delete_auth_token("reset", token)
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    # Update password on that one record only
    user["hashed_password"] = await asyncio.to_thread(get_password_hash, data.password)
    if not await asyncio.to_thread(save_user_record, user):
        raise HTTPException(status_code=500, detail="Could not update password")

    # Mark token as used so the link cannot be replayed
    update_auth_token("reset", token, {"used": True})
    
    print(f"[Auth] Password reset successful for {email_lower}")
    return {"message": "Password reset successfully. Please login with your new password."}

@app.get("/api/admin/users")
def get_whitelisted_users(current_user: UserDB = Depends(get_current_user)):
    """Get list of whitelisted users (admin only)"""
    is_admin = current_user.email.lower() == ADMIN_EMAIL.lower()
    if not is_admin:
        raise HTTPException(status_code=403, detail="Only admin can manage users")
    
    emails = get_whitelisted_emails(force_refresh=True)
    return {
        "users": sorted(emails),
        "count": len(emails)
    }

@app.post("/api/admin/users")
def add_whitelisted_user(
    email: str = Form(...),
    current_user: UserDB = Depends(get_current_user)
):
    """Add a new user to whitelist (admin only)"""
    global WHITELISTED_USERS
    
    is_admin = current_user.email.lower() == ADMIN_EMAIL.lower()
    if not is_admin:
        raise HTTPException(status_code=403, detail="Only admin can add users")
    
    email_lower = email.lower().strip()
    
    # Validate email format
    if not email_lower or "@" not in email_lower:
        raise HTTPException(status_code=400, detail="Invalid email format")
    
    # Check if already exists
    if email_lower in get_whitelisted_emails(force_refresh=True):
        raise HTTPException(status_code=400, detail="User already whitelisted")
    
    # Add to whitelist
    if add_whitelisted_email(email_lower):
        return {"message": f"User {email} added to whitelist", "email": email_lower}
    else:
        raise HTTPException(status_code=500, detail="Failed to save users file")

@app.delete("/api/admin/users/{email}")
def remove_whitelisted_user(
    email: str,
    current_user: UserDB = Depends(get_current_user)
):
    """Remove a user from whitelist (admin only)"""
    global WHITELISTED_USERS
    
    is_admin = current_user.email.lower() == ADMIN_EMAIL.lower()
    if not is_admin:
        raise HTTPException(status_code=403, detail="Only admin can remove users")
    
    email_lower = email.lower().strip()
    
    # Cannot remove admin
    if email_lower == ADMIN_EMAIL.lower():
        raise HTTPException(status_code=400, detail="Cannot remove admin user")
    
    # Check if exists
    if email_lower not in get_whitelisted_emails(force_refresh=True):
        raise HTTPException(status_code=404, detail="User not found in whitelist")
    
    # Remove from whitelist
    if remove_whitelisted_email(email_lower):
        return {"message": f"User {email} removed from whitelist", "email": email_lower}
    else:
        raise HTTPException(status_code=500, detail="Failed to save users file")

@app.post("/api/admin/jobs/upload-excel")
async def upload_excel_jobs(
    file: UploadFile = File(...),
    current_user: UserDB = Depends(get_current_user)
):
    """Upload Excel file with job data (admin only).
    
    Jobs from uploaded files are COMBINED with original VMS Job Fiule.xlsx jobs.
    
    Expected columns: Job Code, Location, Job title, Status, EndClient, Salary, 
    Job Description, Start Date, Profession, Specialty, State, # of Open Positions,
    # of Total Positions, Duration Description, Segment Names
    
    Note: 'Job title' column is supported in addition to 'Job Type'
    """
    global UPLOADED_EXCEL_FILES
    
    # Verify admin
    is_admin = current_user.email.lower() == ADMIN_EMAIL.lower()
    if not is_admin:
        raise HTTPException(status_code=403, detail="Only admin can upload Excel files")
    
    # Validate file type
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="Only Excel files (.xlsx, .xls) are allowed")
    
    try:
        # Create upload directory if not exists
        excel_upload_dir = os.path.join(DATA_DIR, "excel_uploads")
        os.makedirs(excel_upload_dir, exist_ok=True)
        
        # Save file with timestamp to avoid conflicts
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_path = os.path.join(excel_upload_dir, f"jobs_{timestamp}_{file.filename}")
        
        # Write file
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)
        
        # Add to list of uploaded files (append, don't replace)
        UPLOADED_EXCEL_FILES.append(file_path)
        
        # Clear cache to force reload
        clear_excel_jobs_cache()
        
        # Test loading all files
        all_jobs = load_excel_jobs()
        
        # Get count from just this file
        new_file_jobs = load_excel_jobs_from_file(file_path)
        
        return {
            "message": f"Excel file uploaded successfully. Added {len(new_file_jobs)} jobs from this file. Total jobs now: {len(all_jobs)}",
            "filename": file.filename,
            "new_jobs_count": len(new_file_jobs),
            "total_jobs_count": len(all_jobs),
            "uploaded_files_count": len(UPLOADED_EXCEL_FILES),
            "file_path": file_path
        }
        
    except Exception as e:
        print(f"[Excel Upload] Error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to upload Excel file: {str(e)}")


@app.post("/api/admin/jobs")
def create_job_from_payload(
    background_tasks: BackgroundTasks,
    payload: DirectJobCreateRequest,
    current_user: UserDB = Depends(get_current_user)
):
    """Create or update a job directly from API payload fields (admin only)."""
    is_admin = current_user.email.lower() == ADMIN_EMAIL.lower()
    if not is_admin:
        raise HTTPException(status_code=403, detail="Only admin can create jobs")

    try:
        job = build_job_from_direct_input(payload)
        closures = detect_manual_job_closures([job])
        upsert_manual_job(job)
        if closures:
            background_tasks.add_task(notify_job_closures, closures, "direct_api")
        queue_job_post_notifications(background_tasks, [job], "direct_api", True)

        return {
            "message": "Job stored successfully",
            "job": job,
            "source": "direct_api",
        }
    except Exception as e:
        print(f"[Manual Jobs] Error storing direct-input job {payload.job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to store job: {str(e)}")


@app.post("/api/admin/jobs/bulk")
def create_jobs_from_payload_bulk(
    payload: BulkDirectJobCreateRequest,
    background_tasks: BackgroundTasks,
    current_user: UserDB = Depends(get_current_user)
):
    """Create or update many jobs directly from API payload fields (admin only)."""
    is_admin = current_user.email.lower() == ADMIN_EMAIL.lower()
    if not is_admin:
        raise HTTPException(status_code=403, detail="Only admin can create jobs")

    stored_jobs: List[Job] = []
    failed_jobs = []

    for item in payload.jobs:
        try:
            stored_jobs.append(build_job_from_direct_input(item))
        except Exception as exc:
            job_ref = item.job_id or item.job_code or "unknown"
            failed_jobs.append({"job_id": job_ref, "error": str(exc)})

    try:
        closures = detect_manual_job_closures(stored_jobs)
        upsert_manual_jobs(stored_jobs)
        if closures:
            background_tasks.add_task(notify_job_closures, closures, "direct_api_bulk")
        queue_job_post_notifications(
            background_tasks,
            stored_jobs,
            "direct_api_bulk",
            payload.send_notifications,
        )
        return build_bulk_job_response(stored_jobs, failed_jobs, "direct_api_bulk")
    except Exception as e:
        print(f"[Manual Jobs] Error storing bulk direct-input jobs: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to store bulk jobs: {str(e)}")


@app.delete("/api/admin/jobs/{job_id}")
def delete_job_from_payload(
    job_id: str,
    credentials: AdminJobDeleteRequest,
    background_tasks: BackgroundTasks,
):
    """Delete a direct API-ingested job by job ID using admin credentials."""
    verify_admin_credentials(credentials.email, credentials.password)

    try:
        prior_doc = None
        if mongodb_enabled and manual_jobs_collection is not None:
            prior_doc = manual_jobs_collection.find_one({"id": job_id}, {"id": 1, "title": 1, "status": 1})

        deleted = delete_manual_job(job_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Direct API job {job_id} not found")

        if prior_doc and (prior_doc.get("status") or "").strip().lower() in OPEN_STATUSES:
            closure = [{
                "job_id": job_id,
                "title": prior_doc.get("title") or job_id,
                "previous_status": prior_doc.get("status"),
                "current_status": "Closed",
            }]
            background_tasks.add_task(notify_job_closures, closure, "direct_api_delete")

        return {
            "message": "Job deleted successfully",
            "job_id": job_id,
            "source": "direct_api",
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"[Manual Jobs] Error deleting direct-input job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete job: {str(e)}")


def close_jobs_for_sync(job_ids: List[str], source: str, background_tasks: BackgroundTasks) -> dict:
    """Close (remove) jobs the sync found are no longer listed on the source site."""
    try:
        result = set_manual_jobs_closed(job_ids)
    except Exception as e:
        print(f"[Manual Jobs] Error closing jobs from {source}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to close jobs: {str(e)}")
    transitions = result.pop("transitions")
    if transitions:
        background_tasks.add_task(notify_job_closures, transitions, source)
    print(
        f"[Manual Jobs] {source}: closed {len(result['closed'])}, already closed "
        f"{len(result['already_closed'])}, not found {len(result['not_found'])}"
    )
    return {"message": "Bulk job close completed", "source": source, **result}


# Declared before /api/admin/jobs/{job_id}/close so "bulk" is not taken as a job id.
@app.post("/api/admin/jobs/bulk/close")
def close_jobs_bulk(
    payload: BulkJobCloseRequest,
    background_tasks: BackgroundTasks,
    current_user: UserDB = Depends(get_current_user),
):
    """Close many jobs that are no longer available on the source site (admin token)."""
    if current_user.email.lower() != ADMIN_EMAIL.lower():
        raise HTTPException(status_code=403, detail="Only admin can close jobs")
    return close_jobs_for_sync(payload.job_ids, "direct_api_sync_close", background_tasks)


@app.post("/api/admin/jobs/{job_id}/close")
def close_job_by_admin(
    job_id: str,
    background_tasks: BackgroundTasks,
    current_user: UserDB = Depends(get_current_user),
):
    """Admin: close a job. Closed jobs are removed from the database.

    The source-site sync posts the job again if it is still listed there.
    """
    if current_user.email.lower() != ADMIN_EMAIL.lower():
        raise HTTPException(status_code=403, detail="Only admin can close jobs")
    try:
        result = set_manual_jobs_closed([job_id])
    except Exception as e:
        print(f"[Manual Jobs] Error closing job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to close job: {str(e)}")
    if result["not_found"]:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if result["transitions"]:
        background_tasks.add_task(notify_job_closures, result["transitions"], "admin_close")
    return {"message": "Job closed", "job_id": job_id, "status": "Closed"}


class JobDescriptionBackfillRequest(BaseModel):
    description: str


@app.put("/api/admin/jobs/{job_id}/description")
def backfill_job_description(
    job_id: str,
    payload: JobDescriptionBackfillRequest,
    current_user: UserDB = Depends(get_current_user),
):
    """Admin: fill in a job's description only if it is currently missing.

    Touches nothing else (posted date, status, notifications), unlike re-posting
    the job. A job that already has a real description is left unchanged.
    """
    if current_user.email.lower() != ADMIN_EMAIL.lower():
        raise HTTPException(status_code=403, detail="Only admin can update job descriptions")
    new_description = strip_html_job_description((payload.description or "").strip())
    if not new_description or new_description == NO_DESCRIPTION_PLACEHOLDER:
        raise HTTPException(status_code=400, detail="Description is empty")
    try:
        outcome = set_manual_job_description_if_missing(job_id.strip(), new_description)
    except Exception as e:
        print(f"[Manual Jobs] Error updating description for {job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update description: {str(e)}")
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return {"job_id": job_id, "result": outcome}


NEXUS_INGEST_TOKEN = os.getenv("NEXUS_INGEST_TOKEN", "")


def require_ingest_access(request: Request) -> None:
    """Protect the unauthenticated Nexus ingest routes with a shared token.

    Opt-in so the existing sync keeps working: set NEXUS_INGEST_TOKEN on both the
    API and the sync process (X-Ingest-Token header) to close these routes.
    """
    if not NEXUS_INGEST_TOKEN:
        return
    supplied = request.headers.get("x-ingest-token", "")
    if not secrets.compare_digest(supplied.encode(), NEXUS_INGEST_TOKEN.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing ingest token")


@app.post("/api/nexus/jobs", dependencies=[Depends(require_ingest_access)])
def create_nexus_job(payload: DirectJobCreateRequest, background_tasks: BackgroundTasks):
    """Create or update a job from Nexus-style request parameters."""
    try:
        job = build_job_from_direct_input(payload)
        closures = detect_manual_job_closures([job])
        upsert_manual_job(job)
        if closures:
            background_tasks.add_task(notify_job_closures, closures, "nexus_api")
        queue_job_post_notifications(background_tasks, [job], "nexus_api", True)

        return {
            "message": "Job stored successfully",
            "job": job,
            "source": "nexus_api",
        }
    except Exception as e:
        job_ref = payload.job_id or payload.job_code or "unknown"
        print(f"[Nexus Jobs] Error storing Nexus job {job_ref}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to store job: {str(e)}")


@app.post("/api/nexus/jobs/bulk", dependencies=[Depends(require_ingest_access)])
def create_nexus_jobs_bulk(
    payload: BulkDirectJobCreateRequest,
    background_tasks: BackgroundTasks,
):
    """Create or update many jobs from Nexus-style request parameters."""
    stored_jobs: List[Job] = []
    failed_jobs = []

    for item in payload.jobs:
        try:
            stored_jobs.append(build_job_from_direct_input(item))
        except Exception as exc:
            job_ref = item.job_id or item.job_code or "unknown"
            failed_jobs.append({"job_id": job_ref, "error": str(exc)})

    try:
        closures = detect_manual_job_closures(stored_jobs)
        upsert_manual_jobs(stored_jobs)
        if closures:
            background_tasks.add_task(notify_job_closures, closures, "nexus_api_bulk")
        queue_job_post_notifications(
            background_tasks,
            stored_jobs,
            "nexus_api_bulk",
            payload.send_notifications,
        )
        return build_bulk_job_response(stored_jobs, failed_jobs, "nexus_api_bulk")
    except Exception as e:
        print(f"[Nexus Jobs] Error storing bulk Nexus jobs: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to store bulk jobs: {str(e)}")


@app.post("/api/nexus/jobs/bulk/close", dependencies=[Depends(require_ingest_access)])
def close_nexus_jobs_bulk(payload: BulkJobCloseRequest, background_tasks: BackgroundTasks):
    """Close many jobs that are no longer listed on Nexus."""
    return close_jobs_for_sync(payload.job_ids, "nexus_api_sync_close", background_tasks)

@app.get("/api/admin/jobs/excel-files")
async def list_uploaded_excel_files(current_user: UserDB = Depends(get_current_user)):
    """List all uploaded Excel files (admin only)"""
    is_admin = current_user.email.lower() == ADMIN_EMAIL.lower()
    if not is_admin:
        raise HTTPException(status_code=403, detail="Only admin can view uploaded files")
    
    files_info = []
    for idx, file_path in enumerate(UPLOADED_EXCEL_FILES):
        if os.path.exists(file_path):
            files_info.append({
                "index": idx,
                "filename": os.path.basename(file_path),
                "path": file_path,
                "exists": True
            })
        else:
            files_info.append({
                "index": idx,
                "filename": os.path.basename(file_path),
                "path": file_path,
                "exists": False
            })
    
    return {
        "original_file": EXCEL_JOBS_FILE,
        "original_exists": os.path.exists(EXCEL_JOBS_FILE),
        "uploaded_files": files_info,
        "total_uploaded": len(UPLOADED_EXCEL_FILES)
    }

@app.delete("/api/admin/jobs/excel-files/{file_index}")
async def remove_uploaded_excel_file(
    file_index: int,
    current_user: UserDB = Depends(get_current_user)
):
    """Remove a specific uploaded Excel file by index (admin only)"""
    global UPLOADED_EXCEL_FILES
    
    is_admin = current_user.email.lower() == ADMIN_EMAIL.lower()
    if not is_admin:
        raise HTTPException(status_code=403, detail="Only admin can remove uploaded files")
    
    if file_index < 0 or file_index >= len(UPLOADED_EXCEL_FILES):
        raise HTTPException(status_code=404, detail=f"File index {file_index} not found")
    
    file_path = UPLOADED_EXCEL_FILES[file_index]
    filename = os.path.basename(file_path)
    
    # Remove file from disk if exists
    if os.path.exists(file_path):
        os.remove(file_path)
        print(f"[Excel] Deleted file: {file_path}")
    
    # Remove from list
    UPLOADED_EXCEL_FILES.pop(file_index)
    
    # Clear cache to force reload
    clear_excel_jobs_cache()
    
    return {
        "message": f"Excel file '{filename}' removed successfully",
        "removed_file": filename,
        "remaining_uploaded_files": len(UPLOADED_EXCEL_FILES)
    }

@app.get("/api/auth/me", response_model=UserResponse)
async def get_current_user_info(current_user: UserDB = Depends(get_current_user)):
    """Get current logged in user info"""
    return {
        "id": current_user.id,
        "email": current_user.email,
        "full_name": current_user.full_name,
        "is_active": current_user.is_active,
        "created_at": current_user.created_at
    }

# Admin configuration
ADMIN_EMAIL = "admin@radixsol.com"

# Client names to filter from job descriptions (hidden from vendors)
CLIENT_NAMES_TO_FILTER = [
    "Adaptive", "AHSA", "CareerStaff", "HWL", "Medefis", "Staffing Engine",
    "Aya", "Dedicated Nurses", "Hallmark and Vibra Healthcare", "Focusoneconnect",
    "RTG Medical", "Stability Healthcare", "Sunburst Workforce Solutions",
    "Supplemental Healthcare", "TRS Healthcare", "Windsor", "Expedient",
    "Snapcare", "MedicalSolutions", "OHT", "Gracedale"
]
CLIENT_NAME_PATTERNS = [
    re.compile(rf"\b{re.escape(client_name)}\b", flags=re.IGNORECASE)
    for client_name in CLIENT_NAMES_TO_FILTER
]

# Excel jobs cache
_excel_jobs_cache: Optional[List[Job]] = None
_excel_jobs_cache_time: Optional[datetime] = None
_excel_jobs_rebuild_lock = Lock()

EXCEL_CACHE_MINUTES = 1440  # Cache Excel jobs for 24 hours (1 day) - Excel jobs are static

def load_excel_jobs_from_file(file_path: str) -> List[Job]:
    """Load jobs from a single Excel file.
    
    Returns list of Job models parsed from the file.
    """
    jobs: List[Job] = []
    
    if not os.path.exists(file_path):
        print(f"[Excel] File not found: {file_path}")
        return jobs
    
    try:
        import pandas as pd
        
        print(f"[Excel] Reading jobs from {file_path}...")
        
        # Read only necessary columns to reduce memory
        df = pd.read_excel(file_path)
        
        print(f"[Excel] Loaded {len(df)} rows from {os.path.basename(file_path)}")
        
        # Normalize column names (strip spaces, lowercase for matching)
        col_mapping = {}
        for col in df.columns:
            col_mapping[col.strip().lower()] = col
        
        def get_val(row, possible_names, default=''):
            """Get value from row trying multiple possible column names"""
            for name in possible_names:
                name_clean = name.strip().lower()
                if name_clean in col_mapping:
                    val = row.get(col_mapping[name_clean])
                    if pd.notna(val):
                        return str(val).strip()
            return default
        
        for idx, row in df.iterrows():
            try:
                # Extract fields from Excel row (with flexible column matching)
                job_code = get_val(row, ['Job Code', 'JobCode', 'Job ID', 'JobID'])
                job_type = get_val(row, ['Job Type', 'JobType', 'Title', 'Job Title'])
                location = get_val(row, ['Location', 'City'])
                state = get_val(row, ['State', 'Province'])
                status = get_val(row, ['Status', 'Job Status'])
                end_client = get_val(row, ['EndClient', 'End Client', 'Client', 'EndClient '])
                salary = get_val(row, ['Salary', 'Pay', 'Rate', 'Bill Rate'])
                displayed_salary = display_bill_rate(salary)
                job_description = get_val(row, ['Job Description', 'Description', 'Desc'])
                
                # Other fields for description
                start_date = get_val(row, ['Start Date', 'StartDate'])
                profession = get_val(row, ['Profession', 'Prof'])
                specialty = get_val(row, ['Specialty', 'Spec'])
                open_positions = get_val(row, ['# of Open Positions', 'Open Positions', 'Openings'])
                total_positions = get_val(row, ['# of Total Positions', 'Total Positions'])
                duration_desc = get_val(row, ['Duration Description', 'Duration'])
                segment_names = get_val(row, ['Segment Names', 'Segment'])
                
                # Skip if no job code (required for ID)
                if not job_code or job_code.lower() in ['nan', 'none', 'null', '']:
                    continue
                
                # Build location (Location + State)
                full_location = location
                if state and state.lower() != 'nan':
                    full_location = f"{location}, {state}" if location else state
                
                # Build description
                description_parts = []
                
                # First: Job Description if present
                if job_description and job_description.lower() != 'nan':
                    description_parts.append(job_description)
                
                # Then: Other details (excluding the main display fields)
                other_details = []
                if start_date and start_date.lower() != 'nan':
                    other_details.append(f"Start Date: {start_date}")
                if profession and profession.lower() != 'nan':
                    other_details.append(f"Profession: {profession}")
                if specialty and specialty.lower() != 'nan':
                    other_details.append(f"Specialty: {specialty}")
                if open_positions and open_positions.lower() != 'nan':
                    other_details.append(f"Open Positions: {open_positions}")
                if total_positions and total_positions.lower() != 'nan':
                    other_details.append(f"Total Positions: {total_positions}")
                if duration_desc and duration_desc.lower() != 'nan':
                    other_details.append(f"Duration: {duration_desc}")
                if segment_names and segment_names.lower() != 'nan':
                    other_details.append(f"Segment: {segment_names}")
                if end_client and end_client.lower() != 'nan':
                    other_details.append(f"End Client: {end_client}")
                
                if other_details:
                    if description_parts:
                        description_parts.append("\n" + " | ".join(other_details))
                    else:
                        description_parts.append(" | ".join(other_details))
                
                full_description = "\n".join(description_parts) if description_parts else "No description available"
                
                # Create Job model
                job = Job(
                    id=job_code,
                    title=job_type if job_type and job_type.lower() != 'nan' else job_code,
                    description=full_description,
                    requirements=None,  # Not included in display
                    department=f"Job Code: {job_code}",
                    location=full_location if full_location else "Not specified",
                    employment_type="Contract",  # Default or can be derived
                    salary_range=displayed_salary or None,
                    posted_date=datetime.now(),  # Default to now
                    status=normalize_job_status(status if status and status.lower() != 'nan' else None),
                    end_client=end_client if end_client and end_client.lower() != 'nan' else None,
                    job_id=job_code,
                    profession=profession if profession and profession.lower() != 'nan' else None,
                    specialty=specialty if specialty and specialty.lower() != 'nan' else None,
                    state=state if state and state.lower() != 'nan' else None,
                    bill_rate_discount_applied=True,
                )
                
                jobs.append(job)
                
            except Exception as e:
                print(f"[Excel] Error parsing row {idx} in {os.path.basename(file_path)}: {e}")
                continue
        
        print(f"[Excel] Successfully parsed {len(jobs)} jobs from {os.path.basename(file_path)}")
        
    except Exception as e:
        print(f"[Excel] Error reading file {file_path}: {e}")
    
    return jobs


def load_excel_jobs() -> List[Job]:
    """Load jobs from ALL Excel files (original + uploaded) and combine them.
    
    Loads from:
    1. Original file: VMS Job Fiule.xlsx
    2. All uploaded files in UPLOADED_EXCEL_FILES list
    
    Returns combined list of all jobs from all files.
    """
    if _excel_jobs_cache_is_fresh():
        return _excel_jobs_cache
    # Single-flight: a cold worker used to parse the spreadsheet with pandas once
    # per concurrent request, spiking memory right after every deploy.
    with _excel_jobs_rebuild_lock:
        if _excel_jobs_cache_is_fresh():
            return _excel_jobs_cache
        return _read_all_excel_jobs()


def _excel_jobs_cache_is_fresh() -> bool:
    # `is not None`, not truthiness: with no Excel file the cached result is an
    # empty list, which used to count as "not cached" and re-ran the loader (and
    # printed two log lines) on every /api/jobs request.
    return bool(
        _excel_jobs_cache is not None
        and _excel_jobs_cache_time
        and datetime.now() - _excel_jobs_cache_time < timedelta(minutes=EXCEL_CACHE_MINUTES)
    )


def _read_all_excel_jobs() -> List[Job]:
    global _excel_jobs_cache, _excel_jobs_cache_time

    all_jobs: List[Job] = []
    
    # Load from original file first
    if os.path.exists(EXCEL_JOBS_FILE):
        original_jobs = load_excel_jobs_from_file(EXCEL_JOBS_FILE)
        all_jobs.extend(original_jobs)
        print(f"[Excel] Original file: {len(original_jobs)} jobs")
    else:
        print(f"[Excel] Original file not found: {EXCEL_JOBS_FILE}")
    
    # Load from all uploaded files
    uploaded_count = 0
    for uploaded_file in UPLOADED_EXCEL_FILES:
        if os.path.exists(uploaded_file):
            uploaded_jobs = load_excel_jobs_from_file(uploaded_file)
            all_jobs.extend(uploaded_jobs)
            uploaded_count += len(uploaded_jobs)
            print(f"[Excel] Uploaded file {os.path.basename(uploaded_file)}: {len(uploaded_jobs)} jobs")
        else:
            print(f"[Excel] Uploaded file not found: {uploaded_file}")
    
    print(f"[Excel] Total jobs from all files: {len(all_jobs)} (original + {uploaded_count} from uploads)")
    
    # Cache the combined jobs
    _excel_jobs_cache = all_jobs
    _excel_jobs_cache_time = datetime.now()
    
    return all_jobs


def clear_excel_jobs_cache():
    """Clear the Excel jobs cache to force reload on next request"""
    global _excel_jobs_cache, _excel_jobs_cache_time
    _excel_jobs_cache = None
    _excel_jobs_cache_time = None
    clear_jobs_response_cache()
    print("[Excel] Cache cleared")


def get_client_ip(request: Request) -> Optional[str]:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    if request.client:
        return request.client.host
    return None


def record_submission_log(audit_doc: dict) -> None:
    """Persist a candidate-submission audit log in MongoDB or JSON fallback."""
    if mongodb_enabled and submission_logs_collection is not None:
        # replace_one(upsert=True) by the doc's own id rather than insert_one: a
        # retry after a client-side timeout (server may have actually applied the
        # first attempt) then overwrites with identical data instead of raising
        # a duplicate-key error or leaving two audit rows for one submission.
        _with_mongo_retries(
            f"Submission log write ({audit_doc.get('id')})",
            lambda: submission_logs_collection.replace_one(
                {"id": audit_doc["id"]}, dict(audit_doc), upsert=True
            ),
            attempts=MONGODB_WRITE_RETRY_ATTEMPTS,
        )
        print(f"[SubmissionAudit] Logged submission {audit_doc.get('id')} in MongoDB")
        return

    logs_file = os.path.join(DATA_DIR, "submission_logs.json")
    existing = []
    if os.path.exists(logs_file):
        try:
            with open(logs_file, "r") as f:
                existing = json.load(f)
        except Exception as e:
            print(f"[SubmissionAudit] Could not read existing JSON log: {e}")
    existing.append(audit_doc)
    with open(logs_file, "w") as f:
        json.dump(existing, f, indent=2, default=str)
    print(f"[SubmissionAudit] Logged submission {audit_doc.get('id')} in JSON")


def load_candidate_submissions(submitter_user_id: Optional[str] = None) -> List[dict]:
    """Load candidate submissions with optional filtering by submitter."""
    candidates: List[dict] = []

    if mongodb_enabled and candidates_collection is not None:
        query = {"submitted_by_user_id": submitter_user_id} if submitter_user_id else {}
        cursor = candidates_collection.find(query).sort("submitted_date", -1)
        for doc in cursor:
            doc["_id"] = str(doc["_id"])
            sanitize_candidate_bill_rate(doc)
            candidates.append(doc)
        return candidates

    candidates_file = os.path.join(DATA_DIR, "candidates.json")
    if os.path.exists(candidates_file):
        with open(candidates_file, "r") as f:
            loaded = json.load(f)
        for candidate in loaded:
            if submitter_user_id and candidate.get("submitted_by_user_id") != submitter_user_id:
                continue
            sanitize_candidate_bill_rate(candidate)
            candidates.append(candidate)

    candidates.sort(key=lambda row: row.get("submitted_date", ""), reverse=True)
    return candidates


def build_submission_history(candidates: List[dict]) -> List[dict]:
    """Group candidate submissions by user and submission date."""
    users = load_users_from_json()
    grouped: Dict[str, dict] = {}

    for candidate in candidates:
        user_id = candidate.get("submitted_by_user_id") or "unknown"
        user_email = (candidate.get("submitted_by_email") or "").strip().lower()
        user_name = (candidate.get("submitted_by_name") or "Unknown User").strip() or "Unknown User"
        submitted_at = str(candidate.get("submitted_date") or "")
        submission_date = submitted_at[:10] if len(submitted_at) >= 10 else "unknown"
        user_record = users.get(user_email) if user_email else None

        if user_id not in grouped:
            grouped[user_id] = {
                "user": {
                    "id": user_id if user_id != "unknown" else None,
                    "full_name": user_record.get("full_name") if user_record else user_name,
                    "email": user_record.get("email") if user_record else (user_email or None),
                    "is_active": user_record.get("is_active") if user_record else None,
                    "created_at": user_record.get("created_at") if user_record else None,
                },
                "summary": {
                    "total_profiles_submitted": 0,
                    "total_submission_days": 0,
                    "first_submission_at": None,
                    "last_submission_at": None,
                },
                "_dates": defaultdict(list),
            }

        group = grouped[user_id]
        group["summary"]["total_profiles_submitted"] += 1
        first_submission_at = group["summary"]["first_submission_at"]
        last_submission_at = group["summary"]["last_submission_at"]
        if submitted_at:
            if not first_submission_at or submitted_at < first_submission_at:
                group["summary"]["first_submission_at"] = submitted_at
            if not last_submission_at or submitted_at > last_submission_at:
                group["summary"]["last_submission_at"] = submitted_at
        group["_dates"][submission_date].append(candidate)

    history = []
    for group in grouped.values():
        dates = []
        for submission_date, profiles in sorted(group["_dates"].items(), key=lambda item: item[0], reverse=True):
            ordered_profiles = sorted(profiles, key=lambda row: row.get("submitted_date", ""), reverse=True)
            dates.append({
                "date": submission_date,
                "total_profiles_submitted": len(ordered_profiles),
                "first_submission_at": ordered_profiles[-1].get("submitted_date") if ordered_profiles else None,
                "last_submission_at": ordered_profiles[0].get("submitted_date") if ordered_profiles else None,
                "profiles": ordered_profiles,
            })
        group["summary"]["total_submission_days"] = len(dates)
        group["dates"] = dates
        group.pop("_dates", None)
        history.append(group)

    history.sort(
        key=lambda item: (
            item.get("summary", {}).get("last_submission_at") or "",
            item.get("summary", {}).get("total_profiles_submitted") or 0,
        ),
        reverse=True,
    )
    return history


def store_candidate_doc(candidate_doc: dict) -> None:
    """Persist one candidate submission in MongoDB or the JSON fallback."""
    candidate_id = candidate_doc.get("id")
    if mongodb_enabled and candidates_collection is not None:
        # replace_one(upsert=True) by our own id instead of insert_one: candidate_id
        # is already a unique, client-generated key, so a retry after a timed-out
        # write (the first attempt may have landed server-side) safely overwrites
        # the same document instead of raising a duplicate-key error or, worse,
        # creating a second candidate record for one submission.
        _with_mongo_retries(
            f"Candidate write ({candidate_id})",
            lambda: candidates_collection.replace_one(
                {"id": candidate_id}, dict(candidate_doc), upsert=True
            ),
            attempts=MONGODB_WRITE_RETRY_ATTEMPTS,
        )
        print(f"[Submissions] Candidate {candidate_id} stored in MongoDB")
        return

    candidates_file = os.path.join(DATA_DIR, "candidates.json")
    existing = []
    if os.path.exists(candidates_file):
        with open(candidates_file, 'r') as f:
            existing = json.load(f)
    existing.append(candidate_doc)
    with open(candidates_file, 'w') as f:
        json.dump(existing, f, indent=2, default=str)
    print(f"[Submissions] Candidate {candidate_id} stored in JSON (MongoDB not available)")


def send_submission_notification_and_log(candidate_doc: dict, vendor_info: dict, summary: dict) -> None:
    candidate_id = candidate_doc.get("id")
    try:
        email_sent = send_submission_notification_email(candidate_doc, vendor_info, summary)
    except Exception as e:
        print(f"[Submissions] Exception during notification email dispatch: {e}")
        email_sent = False
    if email_sent:
        print(f"[Submissions] Notification email sent for candidate {candidate_id}")
    else:
        print(f"[Submissions] Failed to send notification email for candidate {candidate_id}")


def compute_user_submission_summary(submitter_user_id: Optional[str], submitted_at: str) -> dict:
    """Per-user submission counts, computed with indexed queries in MongoDB.

    Previously every submission loaded the submitter's entire history into memory
    just to count it.
    """
    if not (mongodb_enabled and candidates_collection is not None and submitter_user_id):
        return get_user_submission_summary(
            load_candidate_submissions(submitter_user_id),
            submitter_user_id,
            submitted_at,
        )

    submission_date = submitted_at[:10] if len(submitted_at) >= 10 else ""
    base = {"submitted_by_user_id": submitter_user_id}
    projection = {"_id": 0, "submitted_date": 1}
    total = _with_mongo_retries(
        "Submission summary (total count)",
        lambda: candidates_collection.count_documents(base),
        attempts=MONGODB_WRITE_RETRY_ATTEMPTS,
    )
    on_date = 0
    if submission_date:
        # submitted_date is an ISO string, so one day is a contiguous string range.
        on_date = _with_mongo_retries(
            "Submission summary (on-date count)",
            lambda: candidates_collection.count_documents({
                **base,
                "submitted_date": {"$gte": submission_date, "$lt": submission_date + "\uffff"},
            }),
            attempts=MONGODB_WRITE_RETRY_ATTEMPTS,
        )
    first = _with_mongo_retries(
        "Submission summary (first)",
        lambda: candidates_collection.find_one(base, projection, sort=[("submitted_date", 1)]),
        attempts=MONGODB_WRITE_RETRY_ATTEMPTS,
    )
    last = _with_mongo_retries(
        "Submission summary (last)",
        lambda: candidates_collection.find_one(base, projection, sort=[("submitted_date", -1)]),
        attempts=MONGODB_WRITE_RETRY_ATTEMPTS,
    )
    return {
        "total_profiles_submitted": total,
        "profiles_submitted_on_date": on_date,
        "submission_date": submission_date,
        "first_submission_at": (first or {}).get("submitted_date") or submitted_at,
        "last_submission_at": (last or {}).get("submitted_date") or submitted_at,
    }


def get_user_submission_summary(candidates: List[dict], submitter_user_id: Optional[str], submitted_at: str) -> dict:
    """Derive per-user submission counts around the current submission event."""
    submission_date = submitted_at[:10] if len(submitted_at) >= 10 else ""
    if submitter_user_id:
        user_candidates = [candidate for candidate in candidates if candidate.get("submitted_by_user_id") == submitter_user_id]
    else:
        user_candidates = candidates

    same_date_candidates = [
        candidate for candidate in user_candidates
        if submission_date and str(candidate.get("submitted_date") or "").startswith(submission_date)
    ]

    ordered = sorted(user_candidates, key=lambda row: row.get("submitted_date", ""))
    return {
        "total_profiles_submitted": len(user_candidates),
        "profiles_submitted_on_date": len(same_date_candidates),
        "submission_date": submission_date,
        "first_submission_at": ordered[0].get("submitted_date") if ordered else submitted_at,
        "last_submission_at": ordered[-1].get("submitted_date") if ordered else submitted_at,
    }


def first_present(*values) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def summarize_json_job_description(raw_description: str) -> Optional[str]:
    """Convert accidental raw job JSON descriptions into readable text."""
    text = (raw_description or "").strip()
    if not text.startswith("{"):
        return None

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    real_description = first_present(
        data.get("jobDescription"),
        data.get("description"),
        data.get("jobdescription"),
    )
    if real_description:
        return real_description

    shift_details = data.get("jobShiftDetails") if isinstance(data.get("jobShiftDetails"), dict) else {}
    profession = first_present(data.get("profession"))
    specialty = first_present(data.get("specialty"))
    job_type = first_present(data.get("jobType"))
    client = first_present(data.get("clientName"))
    city = first_present(data.get("city"))
    state = first_present(data.get("state"))
    start_date = first_present(data.get("startDate"))
    end_date = first_present(data.get("endDate"))
    shift = first_present(shift_details.get("shift_1_name"))
    bill_rate = display_bill_rate(first_present(data.get("billRate")))

    lead_parts = [part for part in [profession, specialty] if part]
    lead = " - ".join(lead_parts) if lead_parts else job_type

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
    return "No description provided by Nexus."


def strip_html_job_description(raw_description: str) -> str:
    """Convert raw HTML job descriptions into readable plain text."""
    text = (raw_description or "").strip()
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


def generate_unique_job_id() -> str:
    """A job ID for jobs the caller didn't supply one for.

    upsert_manual_jobs keys every job by its id, so any two jobs that both land
    here with an empty id would collide on {"id": ""} and silently overwrite
    one another. Generate a collision-proof one instead.
    """
    return f"JOB-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:6].upper()}"


def build_job_from_direct_input(payload: DirectJobCreateRequest) -> Job:
    """Map direct endpoint input into the shared Job response model."""
    job_id = (payload.job_id or payload.job_code or "").strip() or generate_unique_job_id()
    location_parts = [payload.city.strip(), payload.state.strip()]
    full_location = ", ".join([part for part in location_parts if part])
    raw_description = (
        summarize_json_job_description(payload.jobdescription)
        or payload.jobdescription.strip()
        or "No description provided by Nexus."
    )
    description = strip_html_job_description(raw_description) or "No description provided by Nexus."

    return Job(
        id=job_id,
        title=payload.job_type.strip() or job_id,
        description=description,
        requirements=None,
        department=payload.specialty.strip(),
        location=full_location or "Not specified",
        employment_type="Contract",
        salary_range=display_bill_rate(payload.billrate) or None,
        posted_date=datetime.now(),
        status=payload.status.strip(),
        job_id=job_id,
        profession=payload.profession.strip(),
        specialty=payload.specialty.strip(),
        state=payload.state.strip(),
        end_client=(payload.client or "").strip() or None,
        bill_rate_discount_applied=True,
    )


def load_manual_jobs(force_refresh: bool = False) -> List[Job]:
    """Load jobs created through the direct API endpoint."""
    if not force_refresh and _manual_jobs_cache is not None and _cache_is_fresh(_manual_jobs_cache_time, MANUAL_JOBS_CACHE_SECONDS):
        return _manual_jobs_cache

    with _manual_jobs_rebuild_lock:
        # Another request may have rebuilt the cache while this one waited.
        if not force_refresh and _manual_jobs_cache is not None and _cache_is_fresh(_manual_jobs_cache_time, MANUAL_JOBS_CACHE_SECONDS):
            return _manual_jobs_cache
        return _read_manual_jobs_from_store()


_manual_jobs_last_good: Optional[List[Job]] = None
MANUAL_JOBS_READ_ATTEMPTS = int(os.getenv("MANUAL_JOBS_READ_ATTEMPTS", "3"))
MANUAL_JOBS_BATCH_SIZE = int(os.getenv("MANUAL_JOBS_BATCH_SIZE", "200"))


MANUAL_JOBS_READ_CONCURRENCY = int(os.getenv("MANUAL_JOBS_READ_CONCURRENCY", "4"))


def _with_mongo_retries(label: str, operation: Callable[[], Any], attempts: int = MANUAL_JOBS_READ_ATTEMPTS, backoff_seconds: float = 0.25) -> Any:
    """Retry a blocking MongoDB call against the transient timeouts a remote
    Atlas cluster produces under load (socket timeouts, brief network blips).
    A short backoff between attempts avoids retrying straight into the same
    congestion that caused the first timeout.
    """
    last_error: Optional[Exception] = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return operation()
        except Exception as e:
            last_error = e
            print(f"[MongoDB] {label} attempt {attempt} failed: {e}")
            if attempt < attempts:
                _sleep(backoff_seconds * attempt)
    raise last_error  # type: ignore[misc]


# Back-compat alias: the retry helper used to be read-only and manual-jobs-specific.
_with_read_retries = _with_mongo_retries


def _read_manual_jobs_from_mongo() -> List[Job]:
    """Read every direct-input job across both clusters, or raise. Never
    returns a partial list.

    manual_jobs now spans two collections: the legacy one on MONGODB_URI's
    cluster (read-only - that cluster is over its Atlas storage quota, so
    every job created before the dual-cluster split still lives there,
    frozen) and the current one on MONGODB_URI_2 (every job created since).
    Both are read and merged so jobs from before the split don't silently
    vanish from listings.

    One long cursor over every full document (descriptions included) used to be
    a single point of failure: a timeout on any later batch either cut the list
    off after the first batch (~100 jobs) or, with retries, re-read everything
    from scratch while requests waited. Instead, read the ordered _id list once
    (tiny, one round trip) per source, then fetch the documents in fixed-size
    chunks a few at a time. Each chunk is a short, bounded query retried on its
    own. A single malformed document is skipped and logged without dropping
    the jobs after it.
    """
    sources = [c for c in (manual_jobs_collection, primary_manual_jobs_collection) if c is not None]

    entries: List[Tuple[Any, Any, Any]] = []  # (source collection, _id, created_at)
    current_ids: set = set()
    for collection in sources:
        rows = _with_read_retries(
            "ID list read",
            lambda c=collection: list(c.find({}, {"_id": 1, "id": 1, "created_at": 1}).batch_size(100_000)),
        )
        if collection is manual_jobs_collection:
            current_ids = {row.get("id") for row in rows}
        else:
            # A legacy job that was re-posted or closed since the split has a
            # newer copy on the current cluster; that copy is the live one.
            rows = [row for row in rows if row.get("id") not in current_ids]
        entries.extend((collection, row["_id"], row.get("created_at")) for row in rows)
    entries.sort(key=lambda e: e[2] or datetime.min, reverse=True)

    chunk_size = max(1, MANUAL_JOBS_BATCH_SIZE)
    chunks = [entries[i:i + chunk_size] for i in range(0, len(entries), chunk_size)]

    def read_chunk(chunk_entries: List[Tuple[Any, Any, Any]]) -> List[dict]:
        ids_by_collection: Dict[Any, List[Any]] = defaultdict(list)
        for collection, _id, _created_at in chunk_entries:
            ids_by_collection[collection].append(_id)
        docs: List[dict] = []
        for collection, id_list in ids_by_collection.items():
            docs.extend(_with_read_retries(
                f"Chunk read ({len(id_list)} jobs)",
                lambda c=collection, ids=id_list: list(c.find({"_id": {"$in": ids}})),
            ))
        return docs

    if len(chunks) > 1 and MANUAL_JOBS_READ_CONCURRENCY > 1:
        with ThreadPoolExecutor(max_workers=min(MANUAL_JOBS_READ_CONCURRENCY, len(chunks))) as pool:
            chunk_docs = list(pool.map(read_chunk, chunks))
    else:
        chunk_docs = [read_chunk(chunk) for chunk in chunks]

    # $in returns documents in no particular order; restore created_at order.
    # A document deleted between the two reads is simply absent.
    docs_by_id = {doc["_id"]: doc for docs in chunk_docs for doc in docs}
    jobs: List[Job] = []
    skipped = 0
    for _collection, _id, _created_at in entries:
        doc = docs_by_id.get(_id)
        if doc is None:
            continue
        try:
            doc.pop("_id", None)
            doc.pop("created_at", None)
            if not doc.get("bill_rate_discount_applied"):
                doc["salary_range"] = display_bill_rate(doc.get("salary_range")) or None
            doc["bill_rate_discount_applied"] = True
            jobs.append(Job(**doc))
        except Exception as e:
            skipped += 1
            print(f"[Manual Jobs] Skipping malformed job {doc.get('id')!r}: {e}")
    if skipped:
        print(f"[Manual Jobs] Loaded {len(jobs)} of {len(entries)} jobs; {skipped} malformed skipped")
    return jobs


def _read_manual_jobs_from_store() -> List[Job]:
    global _manual_jobs_cache, _manual_jobs_cache_time, _manual_jobs_last_good

    jobs: List[Job] = []

    if mongodb_enabled and (manual_jobs_collection is not None or primary_manual_jobs_collection is not None):
        try:
            jobs = _read_manual_jobs_from_mongo()
        except Exception as e:
            print(f"[Manual Jobs] Failed to load direct-input jobs: {e}")
            # Never publish a truncated list. Keep serving the last complete one;
            # with none yet, raise so no payload is built or cached from nothing.
            if _manual_jobs_last_good is not None:
                return _manual_jobs_last_good
            raise
        with _manual_jobs_cache_lock:
            unchanged = _manual_jobs_cache is not None and _manual_jobs_cache == jobs
            _manual_jobs_cache = jobs
            _manual_jobs_cache_time = datetime.now()
            _manual_jobs_last_good = jobs
        # Keep the sanitised/serialised payloads when nothing changed, so a
        # periodic reload no longer forces every request to rebuild them.
        if not unchanged:
            clear_jobs_response_cache()
            print(f"[Manual Jobs] Reloaded {len(jobs)} jobs from MongoDB (changed)")
        return jobs

    try:
        if not os.path.exists(MANUAL_JOBS_FILE):
            return jobs

        with open(MANUAL_JOBS_FILE, "r") as f:
            raw_jobs = json.load(f)

        for job_data in raw_jobs:
            if not job_data.get("bill_rate_discount_applied"):
                job_data["salary_range"] = display_bill_rate(job_data.get("salary_range")) or None
            job_data["bill_rate_discount_applied"] = True
            jobs.append(Job(**job_data))
        with _manual_jobs_cache_lock:
            unchanged = _manual_jobs_cache is not None and _manual_jobs_cache == jobs
            _manual_jobs_cache = jobs
            _manual_jobs_cache_time = datetime.now()
        if not unchanged:
            clear_jobs_response_cache()
    except Exception as e:
        print(f"[Manual Jobs] Failed to load direct-input jobs: {e}")

    return jobs


JOB_LIST_PREVIEW_CHARS = 260


def _manual_jobs_cache_is_current() -> bool:
    """True while the manual-job cache is within its TTL, so no reload is due."""
    return _manual_jobs_cache is not None and _cache_is_fresh(_manual_jobs_cache_time, MANUAL_JOBS_CACHE_SECONDS)


def to_list_item(job: dict) -> dict:
    """Slim a sanitised job for /api/jobs: a short preview instead of the full text.

    The full description is fetched from /api/jobs/{id} when a job is opened.
    The specialty filter used to fall back to parsing the description, so that is
    resolved here, server-side, before the text is cut.
    """
    item = dict(job)
    description = item.get("description") or ""
    if not item.get("specialty"):
        match = re.search(r"Special(?:ty|ity):\s*([^|\n]+)", description, flags=re.IGNORECASE)
        if match:
            item["specialty"] = match.group(1).strip()
    truncated = len(description) > JOB_LIST_PREVIEW_CHARS
    item["description"] = description[:JOB_LIST_PREVIEW_CHARS] if truncated else description
    item["description_truncated"] = truncated
    return item


def get_all_jobs() -> List[Job]:
    """Open direct/Nexus jobs first, then open Excel jobs, de-duplicated by ID.

    Closed jobs are purged from the database, but a copy can survive on the
    read-only legacy cluster or in an Excel file, so they are dropped here too.
    The closed copy is filtered after de-duplication, so it still shadows an
    older open copy of the same id.
    """
    combined = combine_jobs_with_priority(load_manual_jobs(), load_excel_jobs(), [])
    return [job for job in combined if is_job_open(job.status)]


def get_jobs_response_payload(is_admin: bool) -> Dict[str, Any]:
    """Return the cached, serialized /api/jobs body and its ETag for one role.

    The ETag is a hash of the body itself, so it is identical on every worker
    and instance that holds the same jobs - a browser revalidating against a
    different worker still gets a correct 304 or a fresh body.
    """
    role = "admin" if is_admin else "vendor"
    if _jobs_refresh_wakeup is not None:
        # The background refresher polls the version stamp and reloads jobs, so
        # requests never wait on MongoDB once this worker has built a payload.
        cached = _cached_jobs_payload(role)
        if cached is not None:
            return cached
        return build_jobs_response_payload(is_admin)

    refresh_jobs_if_version_changed()  # cheap: one tiny read per worker per few seconds
    with _jobs_response_cache_lock:
        cached = _jobs_response_cache.get(role)
    if cached is not None and _manual_jobs_cache_is_current():
        return cached
    return build_jobs_response_payload(is_admin)


def _cached_jobs_payload(role: str) -> Optional[Dict[str, Any]]:
    with _jobs_response_cache_lock:
        return _jobs_response_cache.get(role) or _jobs_response_last_good.get(role)


# How long a request waits for this worker's first job list after startup.
JOBS_FIRST_LOAD_WAIT_SECONDS = float(os.getenv("JOBS_FIRST_LOAD_WAIT_SECONDS", "25"))


async def get_jobs_response_payload_async(is_admin: bool) -> Dict[str, Any]:
    """get_jobs_response_payload for request handlers.

    While the background refresher runs, only it reads MongoDB and builds
    payloads. Before its first build (right after startup) requests used to
    start their own full reads: each pinned a pool thread, queued on the
    rebuild lock, then re-sanitised every job - a burst of tabs hung the worker.
    Now they wait here without holding a thread, and get 503 (the page retries)
    if the list is still not ready.
    """
    if _jobs_refresh_wakeup is None:
        return await asyncio.to_thread(get_jobs_response_payload, is_admin)
    role = "admin" if is_admin else "vendor"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + JOBS_FIRST_LOAD_WAIT_SECONDS
    while True:
        cached = _cached_jobs_payload(role)
        if cached is not None:
            return cached
        if loop.time() >= deadline:
            raise HTTPException(
                status_code=503,
                detail="Jobs are still loading. Please retry shortly.",
                headers={"Retry-After": "3"},
            )
        await asyncio.sleep(0.1)


def build_jobs_response_payload(is_admin: bool) -> Dict[str, Any]:
    role = "admin" if is_admin else "vendor"
    all_jobs = get_all_jobs()  # refreshes the source caches (and clears ours) when stale
    with _jobs_response_cache_lock:
        cached = _jobs_response_cache.get(role)
        generation = _jobs_response_generation
    if cached is not None:
        return cached

    # get_all_jobs returns open jobs only, for admin and vendors alike.
    sanitized_jobs = [sanitize_job_for_display(job, is_admin) for job in all_jobs]
    slim_jobs = [to_list_item(job) for job in sanitized_jobs]
    body = json.dumps(
        {
            "jobs": slim_jobs,
            "total": len(slim_jobs),
            "total_pages": 1,
            "next_start_page": 2,
            "has_more": False,
            "is_refreshing": False,
            "cache_age_seconds": None,
        },
        default=str,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    payload = {
        "body": body,
        "etag": f'"jobs-{role}-{hashlib.sha1(body).hexdigest()[:20]}"',
        "jobs_by_id": {job["id"]: job for job in sanitized_jobs},
    }
    with _jobs_response_cache_lock:
        # Skip caching if the job lists changed while this payload was built;
        # the refresher was woken and rebuilds. It is still a complete list, so
        # keep it as the fallback rather than leave requests waiting meanwhile.
        if generation == _jobs_response_generation:
            _jobs_response_cache[role] = payload
        _jobs_response_last_good[role] = payload
    return payload


# The whole users collection is small (tens of vendors), so each worker keeps it
# in memory and re-reads it on this interval. Authenticated requests then find
# the user in memory instead of paying a ~200 ms MongoDB round trip roughly once
# a minute per user. Deactivations still reach every worker within this interval.
USERS_REFRESH_SECONDS = max(1, min(USER_LOOKUP_CACHE_SECONDS, USERS_CACHE_SECONDS // 2))
_users_refreshed_at = 0.0


def refresh_users_in_background() -> None:
    global _users_refreshed_at, _users_cache, _users_cache_time
    if not (mongodb_enabled and users_collection is not None):
        return
    if _monotonic() - _users_refreshed_at < USERS_REFRESH_SECONDS:
        return
    _users_refreshed_at = _monotonic()
    # Not load_users_from_json: on a MongoDB error that falls back to the JSON
    # file and would replace the good in-memory list. Here an error propagates
    # and the current list is kept.
    users = {doc["email"].lower(): _normalize_user_doc(doc) for doc in users_collection.find()}
    with _users_cache_lock:
        _users_cache = users
        _users_cache_time = datetime.now()


def refresh_jobs_in_background() -> None:
    """One refresher pass: pick up foreign changes, honour the TTL, rebuild payloads."""
    refresh_jobs_if_version_changed()
    if not _manual_jobs_cache_is_current():
        load_manual_jobs()  # clears the payloads only if the jobs actually changed
    for is_admin in (False, True):
        with _jobs_response_cache_lock:
            current = ("admin" if is_admin else "vendor") in _jobs_response_cache
        if not current:
            build_jobs_response_payload(is_admin)


async def jobs_refresh_loop() -> None:
    """Keep this worker's job list current off the request path.

    App and Atlas are ~200 ms apart, so each MongoDB call made inside a request
    was visible to users: the version poll, a full reload every TTL, and a
    rebuild after every change. Runs right away at startup (warming both role
    payloads), then every JOBS_VERSION_POLL_SECONDS or when a local write wakes it.
    Also keeps the in-memory users list current (refresh_users_in_background).
    """
    global _jobs_refresh_loop_ref, _jobs_refresh_wakeup
    _jobs_refresh_loop_ref = asyncio.get_running_loop()
    _jobs_refresh_wakeup = asyncio.Event()
    while True:
        _jobs_refresh_wakeup.clear()
        try:
            await asyncio.to_thread(refresh_jobs_in_background)
        except Exception as e:
            print(f"[JobsRefresh] Refresh failed, serving the last good list: {e}")
        try:
            await asyncio.to_thread(refresh_users_in_background)
        except Exception as e:
            print(f"[JobsRefresh] Users refresh failed: {e}")
        try:
            await asyncio.wait_for(_jobs_refresh_wakeup.wait(), timeout=min(JOBS_VERSION_POLL_SECONDS, USERS_REFRESH_SECONDS))
        except asyncio.TimeoutError:
            pass


def detect_manual_job_closures(jobs: List[Job]) -> List[dict]:
    """Compare incoming jobs' statuses against what's currently stored to find
    Open/Active -> Closed transitions. Call BEFORE upsert_manual_jobs(jobs)
    overwrites the stored status, with the same job list.

    This is the live closure-notification trigger: the Ceipal poller that
    detect_and_notify_closures was built for is disabled (CEIPAL_ENABLED=False),
    and jobs only enter the system through this direct/Nexus ingest path.
    """
    if not jobs:
        return []
    ids = [job.id for job in jobs]
    prev_status_by_id = {}
    if mongodb_enabled and manual_jobs_collection is not None:
        try:
            for doc in manual_jobs_collection.find({"id": {"$in": ids}}, {"id": 1, "status": 1}):
                prev_status_by_id[doc["id"]] = doc.get("status") or ""
        except Exception as e:
            print(f"[ClosureDetector] Failed to read prior statuses for manual jobs: {e}")
            return []
    else:
        for existing in load_manual_jobs():
            if existing.id in ids:
                prev_status_by_id[existing.id] = existing.status or ""

    transitions = []
    for job in jobs:
        if job.id not in prev_status_by_id:
            continue  # brand-new job — not a transition
        prev_status = (prev_status_by_id.get(job.id) or "").strip().lower()
        curr_status = (job.status or "").strip().lower()
        if prev_status in OPEN_STATUSES and curr_status in CLOSED_STATUSES:
            transitions.append({
                "job_id": job.id,
                "title": job.title or job.job_id,
                "previous_status": prev_status_by_id.get(job.id),
                "current_status": job.status,
            })
    return transitions


def _read_manual_jobs_file() -> List[dict]:
    if not os.path.exists(MANUAL_JOBS_FILE):
        return []
    with open(MANUAL_JOBS_FILE, "r") as f:
        return json.load(f)


def _find_manual_job_docs(job_ids: List[str]) -> Dict[str, Tuple[Any, dict]]:
    """Map job id -> (collection, stored doc), preferring the current cluster's copy."""
    found: Dict[str, Tuple[Any, dict]] = {}
    for collection in (manual_jobs_collection, primary_manual_jobs_collection):
        missing = [job_id for job_id in job_ids if job_id not in found]
        if collection is None or not missing:
            continue
        for doc in collection.find({"id": {"$in": missing}}):
            found[doc["id"]] = (collection, doc)
    return found


def purge_manual_jobs(job_ids: List[str]) -> dict:
    """Delete direct-input jobs from the store, on both clusters.

    Returns "jobs" (id -> stored title and status of every job found),
    "deleted" (gone from the store), "hidden" and "not_found". The legacy
    cluster is over its storage quota and may refuse the delete. Deleting only
    the current copy would then bring the legacy copy back as a live job, so a
    closed copy is kept on the current cluster to shadow it. It is never shown,
    and the startup purge retries the delete on each deploy.
    """
    ids = list(dict.fromkeys((job_id or "").strip() for job_id in job_ids if (job_id or "").strip()))
    result = {"jobs": {}, "deleted": [], "hidden": [], "not_found": []}
    if not ids:
        return result

    if mongodb_enabled and manual_jobs_collection is not None:
        found = _find_manual_job_docs(ids)
        result["jobs"] = {
            job_id: {"title": doc.get("title") or job_id, "status": doc.get("status") or ""}
            for job_id, (_, doc) in found.items()
        }
        result["not_found"] = [job_id for job_id in ids if job_id not in found]

        stuck_on_legacy: set = set()
        if primary_manual_jobs_collection is not None:
            try:
                legacy_ids = sorted(
                    doc["id"] for doc in primary_manual_jobs_collection.find({"id": {"$in": ids}}, {"id": 1})
                )
                stuck_on_legacy = set(legacy_ids)
                if legacy_ids:
                    primary_manual_jobs_collection.delete_many({"id": {"$in": legacy_ids}})
                    stuck_on_legacy = {
                        doc["id"] for doc in primary_manual_jobs_collection.find({"id": {"$in": legacy_ids}}, {"id": 1})
                    }
            except Exception as e:
                print(f"[Manual Jobs] Legacy cluster refused the delete; hiding those jobs instead: {e}")

        deletable = [job_id for job_id in ids if job_id not in stuck_on_legacy]
        if deletable:
            manual_jobs_collection.delete_many({"id": {"$in": deletable}})
        shadows = []
        for job_id in sorted(stuck_on_legacy & set(found)):
            _, doc = found[job_id]
            shadow = {k: v for k, v in doc.items() if k != "_id"}
            shadow["status"] = "Closed"
            shadows.append(UpdateOne({"id": job_id}, {"$set": shadow}, upsert=True))
        if shadows:
            manual_jobs_collection.bulk_write(shadows, ordered=False)
        result["hidden"] = sorted(stuck_on_legacy & set(found))
        result["deleted"] = [job_id for job_id in found if job_id not in stuck_on_legacy]
    else:
        stored_jobs = _read_manual_jobs_file()
        result["jobs"] = {
            job.get("id"): {"title": job.get("title") or job.get("id"), "status": job.get("status") or ""}
            for job in stored_jobs
            if job.get("id") in ids
        }
        result["not_found"] = [job_id for job_id in ids if job_id not in result["jobs"]]
        remaining = [job for job in stored_jobs if job.get("id") not in ids]
        if len(remaining) != len(stored_jobs):
            with open(MANUAL_JOBS_FILE, "w") as f:
                json.dump(remaining, f, indent=2, default=str)
        result["deleted"] = list(result["jobs"])

    if result["jobs"]:
        clear_manual_jobs_cache()
        drop_pending_job_notifications(list(result["jobs"]))
    return result


PURGE_CLOSED_JOBS_ON_STARTUP = os.getenv("PURGE_CLOSED_JOBS_ON_STARTUP", "true").strip().lower() in ("1", "true", "yes", "on")
PURGE_BATCH_SIZE = 500


def purge_closed_jobs_in_store() -> int:
    """Delete every closed job already stored, on both clusters. Returns the count.

    Runs at startup, so closed jobs that predate the purge-on-close behaviour
    disappear on the next deploy. Idempotent: with nothing closed it only reads
    the id and status of each job. Jobs the legacy cluster would not delete are
    reported by purge_manual_jobs as hidden, and are retried on the next startup.
    """
    if not (mongodb_enabled and manual_jobs_collection is not None):
        stored = _read_manual_jobs_file()
        closed_ids = [job["id"] for job in stored if job.get("id") and not is_job_open(job.get("status"))]
    else:
        live: Dict[str, Any] = {}
        for collection in (primary_manual_jobs_collection, manual_jobs_collection):  # current copy wins
            if collection is None:
                continue
            for doc in collection.find({}, {"id": 1, "status": 1}):
                if doc.get("id"):
                    live[doc["id"]] = doc.get("status")
        closed_ids = [job_id for job_id, status in live.items() if not is_job_open(status)]

    hidden: List[str] = []
    for start in range(0, len(closed_ids), PURGE_BATCH_SIZE):
        hidden.extend(purge_manual_jobs(closed_ids[start:start + PURGE_BATCH_SIZE])["hidden"])
    print(
        f"[PurgeClosed] {len(closed_ids)} closed job(s) found; "
        f"{len(closed_ids) - len(hidden)} deleted, {len(hidden)} hidden (legacy cluster refused the delete)"
    )
    return len(closed_ids)


def purge_closed_jobs_at_startup() -> None:
    """One worker purges per deploy; the lease keeps the others from repeating it."""
    if not PURGE_CLOSED_JOBS_ON_STARTUP:
        return
    if mongodb_enabled and locks_collection is not None and not _acquire_lease("purge_closed_jobs", 600):
        return  # another worker is already doing it
    purge_closed_jobs_in_store()


def set_manual_jobs_closed(job_ids: List[str]) -> dict:
    """Close direct-input jobs by removing them from the database.

    Closed jobs are not kept: they are purged (purge_manual_jobs), and the next
    sync cannot bring them back because upsert_manual_jobs skips closed jobs.
    Returns the closed, already-closed and unknown ids plus the Open -> Closed
    transitions for notify_job_closures.
    """
    purged = purge_manual_jobs(job_ids)
    result = {"closed": [], "already_closed": [], "not_found": purged["not_found"], "transitions": []}
    for job_id, stored in purged["jobs"].items():
        if not is_job_open(stored["status"]):
            result["already_closed"].append(job_id)
            continue
        result["closed"].append(job_id)
        result["transitions"].append({
            "job_id": job_id,
            "title": stored["title"],
            "previous_status": stored["status"],
            "current_status": "Closed",
        })
    return result


NO_DESCRIPTION_PLACEHOLDER = "No description provided by Nexus."


def set_manual_job_description_if_missing(job_id: str, description: str) -> str:
    """Set only the description of a stored job, and only if it has none.

    Returns "updated", "already_has_description" or "not_found". A job still on
    the read-only legacy cluster is copied to the current cluster, which takes
    precedence when reading (same approach as set_manual_jobs_closed).
    """
    def is_missing(current: Any) -> bool:
        text = (current or "").strip()
        return not text or text == NO_DESCRIPTION_PLACEHOLDER

    if mongodb_enabled and manual_jobs_collection is not None:
        found = _find_manual_job_docs([job_id])
        if job_id not in found:
            return "not_found"
        collection, doc = found[job_id]
        if not is_missing(doc.get("description")):
            return "already_has_description"
        if collection is manual_jobs_collection:
            manual_jobs_collection.update_one({"id": job_id}, {"$set": {"description": description}})
        else:
            copy = {k: v for k, v in doc.items() if k != "_id"}
            manual_jobs_collection.update_one(
                {"id": job_id}, {"$set": {**copy, "description": description}}, upsert=True
            )
    else:
        stored_jobs = _read_manual_jobs_file()
        doc = next((job for job in stored_jobs if job.get("id") == job_id), None)
        if doc is None:
            return "not_found"
        if not is_missing(doc.get("description")):
            return "already_has_description"
        doc["description"] = description
        with open(MANUAL_JOBS_FILE, "w") as f:
            json.dump(stored_jobs, f, indent=2, default=str)
    clear_manual_jobs_cache()
    return "updated"


def drop_pending_job_notifications(job_ids: List[str]) -> None:
    """Keep closed jobs out of the next new-job digest email."""
    if not mongodb_enabled or job_notification_outbox_collection is None:
        return
    try:
        job_notification_outbox_collection.delete_many({"job_id": {"$in": job_ids}, "status": "pending"})
    except Exception as e:
        print(f"[JobPosted] Failed to drop pending notifications for closed jobs: {e}")


def upsert_manual_job(job: Job) -> None:
    """Persist one direct-input job by job ID."""
    upsert_manual_jobs([job])


def upsert_manual_jobs(jobs: List[Job]) -> None:
    """Persist many direct-input jobs with a single backend write pass when possible.

    Closed jobs are never stored. One posted with a closed status is dropped,
    and any copy already stored is purged. Callers run detect_manual_job_closures
    first, so the closure notification still sees the stored status.
    """
    closed_ids = [job.id for job in jobs if not is_job_open(job.status)]
    if closed_ids:
        purge_manual_jobs(closed_ids)
    jobs = [job for job in jobs if is_job_open(job.status)]
    if not jobs:
        return

    job_payloads_by_id = {}
    for job in jobs:
        job_payload = job.dict()
        job_payload["bill_rate_discount_applied"] = True
        # Last one wins. A single unordered bulk_write with two upserts matching
        # the same not-yet-existing "id" can race into an E11000 duplicate-key
        # error against the unique index, failing the whole batch with a 500.
        job_payloads_by_id[job_payload["id"]] = job_payload
    job_payloads = list(job_payloads_by_id.values())

    if mongodb_enabled and manual_jobs_collection is not None:
        created_at = datetime.now()
        # One round trip for the whole batch instead of one per job.
        operations = []
        for job_payload in job_payloads:
            operations.append(UpdateOne(
                {"id": job_payload["id"]}, {"$set": {**job_payload, "created_at": created_at}}, upsert=True
            ))
        manual_jobs_collection.bulk_write(operations, ordered=False)
        clear_manual_jobs_cache()
        return

    existing_jobs = []
    if os.path.exists(MANUAL_JOBS_FILE):
        with open(MANUAL_JOBS_FILE, "r") as f:
            existing_jobs = json.load(f)

    existing_index = {}
    for idx, existing_job in enumerate(existing_jobs):
        existing_id = (existing_job.get("id") or "").strip()
        if existing_id:
            existing_index[existing_id] = idx

    new_jobs = []
    for job_payload in job_payloads:
        job_id = (job_payload.get("id") or "").strip()
        if job_id and job_id in existing_index:
            idx = existing_index[job_id]
            existing_jobs[idx] = {**existing_jobs[idx], **job_payload}
        else:
            new_jobs.append(job_payload)

    with open(MANUAL_JOBS_FILE, "w") as f:
        json.dump(new_jobs + existing_jobs, f, indent=2, default=str)
    clear_manual_jobs_cache()


def build_bulk_job_response(stored_jobs: List[Job], failed_jobs: List[dict], source: str) -> dict:
    return {
        "message": "Bulk job store completed",
        "source": source,
        "stored_count": len(stored_jobs),
        "failed_count": len(failed_jobs),
        "stored_job_ids": [job.id for job in stored_jobs],
        "failed": failed_jobs,
    }


def queue_job_post_notifications(
    background_tasks: BackgroundTasks,
    jobs: List[Job],
    source: str,
    send_notifications: bool,
) -> None:
    if not send_notifications:
        # Log the skip. This used to return silently, so a caller that left
        # send_notifications off looked identical in the logs to one where the
        # emails had genuinely been dispatched.
        print(
            f"[JobPosted] send_notifications=false for {len(jobs)} job(s) from {source} "
            f"- no notification emails queued"
        )
        return
    print(f"[JobPosted] Queueing notifications for {len(jobs)} job(s) from {source}")
    # Delivery happens in the periodic digest (job_notification_digest_loop), so
    # this only records the jobs. It runs after the response is sent.
    background_tasks.add_task(enqueue_job_post_notifications, jobs, source)


def delete_manual_job(job_id: str) -> bool:
    """Delete one direct-input job by job ID."""
    normalized_job_id = (job_id or "").strip()
    if not normalized_job_id:
        return False

    if mongodb_enabled and manual_jobs_collection is not None:
        result = manual_jobs_collection.delete_one({"id": normalized_job_id})
        if result.deleted_count:
            clear_manual_jobs_cache()
            return True
        result = manual_jobs_collection.delete_one({"job_id": normalized_job_id})
        if result.deleted_count > 0:
            clear_manual_jobs_cache()
            return True
        return False

    if not os.path.exists(MANUAL_JOBS_FILE):
        return False

    with open(MANUAL_JOBS_FILE, "r") as f:
        existing_jobs = json.load(f)

    remaining_jobs = [
        job for job in existing_jobs
        if job.get("id") != normalized_job_id and job.get("job_id") != normalized_job_id
    ]

    if len(remaining_jobs) == len(existing_jobs):
        return False

    with open(MANUAL_JOBS_FILE, "w") as f:
        json.dump(remaining_jobs, f, indent=2, default=str)
    clear_manual_jobs_cache()

    return True


def combine_jobs_with_priority(*job_groups: List[Job]) -> List[Job]:
    """Merge job lists, keeping the first instance of each job ID."""
    combined = []
    seen_ids = set()

    for group in job_groups:
        for job in group:
            if job.id in seen_ids:
                continue
            seen_ids.add(job.id)
            combined.append(job)

    return combined

def sanitize_job_description(description: str, is_admin: bool = False) -> str:
    """Remove client names from job description for non-admin users"""
    if is_admin or not description:
        return description
    
    sanitized = description
    for pattern in CLIENT_NAME_PATTERNS:
        sanitized = pattern.sub('[Client Name Hidden]', sanitized)
    
    return sanitized

if os.path.isdir(UPLOAD_DIR):
    app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# Ceipal API Integration
class CeipalClient:
    def __init__(self):
        self.auth_url = CEIPAL_AUTH_URL
        self.reports_url = CEIPAL_REPORTS_URL
        self.email = CEIPAL_EMAIL
        self.password = CEIPAL_PASSWORD
        self.api_key = CEIPAL_API_KEY
        self.auth_token = None
        self.token_expires = None
        self.cache_dir = CEIPAL_CACHE_DIR
        self.last_auth_error = None
        self._jobs_cache = None
        self._jobs_cache_time = None
        self._fetch_lock = asyncio.Lock()  # Prevent concurrent fetches
        # Single-flight flag for fetch_all_jobs_background. Concurrent triggers (scheduler + every
        # /api/jobs request) used to start parallel paginated fetches that hammered Ceipal into
        # 429 retry loops. Safe to read/write without a lock since asyncio is single-threaded
        # and the check + set happens between awaits.
        self._background_fetch_running = False
        self._last_background_trigger_time = None
        self._last_fetched_pages = 0  # Track how many pages were fetched
        os.makedirs(self.cache_dir, exist_ok=True)

    def _get_cached_jobs(self) -> Optional[List[Job]]:
        """Return cached jobs if less than 5 minutes old"""
        if self._jobs_cache and self._jobs_cache_time:
            age = datetime.now() - self._jobs_cache_time
            if age < timedelta(minutes=5):
                return self._jobs_cache
        return None

    def _set_cached_jobs(self, jobs: List[Job]):
        """Cache jobs with timestamp"""
        self._jobs_cache = jobs
        self._jobs_cache_time = datetime.now()
    
    def clear_cache(self):
        """Clear the jobs cache to force fresh fetch"""
        self._jobs_cache = None
        self._jobs_cache_time = None

    def get_cache_age_seconds(self) -> Optional[int]:
        if not self._jobs_cache_time:
            return None
        return max(0, int((datetime.now() - self._jobs_cache_time).total_seconds()))

    def is_cache_stale(self, max_age_seconds: int = 300) -> bool:
        age_seconds = self.get_cache_age_seconds()
        return age_seconds is None or age_seconds > max_age_seconds

    def should_trigger_background_refresh(self, max_age_seconds: int = 300) -> bool:
        if self._background_fetch_running:
            return False
        if not self._jobs_cache:
            return True
        if not self.is_cache_stale(max_age_seconds=max_age_seconds):
            return False
        if not self._last_background_trigger_time:
            return True
        seconds_since_last_trigger = (datetime.now() - self._last_background_trigger_time).total_seconds()
        return seconds_since_last_trigger >= max_age_seconds

    def mark_background_refresh_requested(self):
        self._last_background_trigger_time = datetime.now()

    async def _get_reports_page_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict,
        *,
        context: str,
        timeout: float = 60.0,
        max_transport_retries: int = 3,
    ) -> httpx.Response:
        """Retry transient transport failures that Ceipal intermittently returns mid-pagination."""
        transport_attempt = 0

        while True:
            try:
                response = await client.get(url, headers=headers, timeout=timeout)
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError:
                raise
            except httpx.RequestError as e:
                transport_attempt += 1
                if transport_attempt > max_transport_retries:
                    raise

                wait_time = min(2 ** transport_attempt, 20)
                print(
                    f"[{context}] Transport error on {url}: {type(e).__name__}: {e}. "
                    f"Retrying in {wait_time}s ({transport_attempt}/{max_transport_retries})..."
                )
                await asyncio.sleep(wait_time)

    def _cache_path(self, filename: str) -> str:
        return os.path.join(self.cache_dir, filename)

    def _write_json_cache(self, filename: str, payload) -> None:
        """Write cache to disk atomically using temp file to prevent corruption"""
        try:
            cache_path = self._cache_path(filename)
            temp_path = cache_path + ".tmp"
            
            # Write to temp file first
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
            
            # Atomic rename (prevents partial/corrupted files)
            os.replace(temp_path, cache_path)
            
            if DEBUG:
                print(f"[Cache] Successfully wrote {filename} ({len(json.dumps(payload))} bytes)")
                
        except Exception as e:
            print(f"[Cache] Failed to write cache {filename}: {e}")

    def _read_json_cache(self, filename: str):
        """Read cache from disk with error handling"""
        try:
            cache_path = self._cache_path(filename)
            
            # Check if file exists and has content
            if not os.path.exists(cache_path):
                return None
                
            file_size = os.path.getsize(cache_path)
            if file_size == 0:
                return None
            
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
                
        except json.JSONDecodeError as e:
            print(f"[Cache] Corrupted cache file {filename}: {e}")
            return None
        except Exception as e:
            if DEBUG:
                print(f"[Cache] Failed to read cache {filename}: {e}")
            return None

    def _extract_authtoken(self, auth_result: dict) -> Optional[str]:
        if not isinstance(auth_result, dict):
            return None
        raw = auth_result.get("_raw")
        if isinstance(raw, str) and raw.strip():
            token = self._extract_token_from_raw(raw)
            if token:
                return token
        # Common shapes we’ve seen in similar APIs
        for key in ("authtoken", "authToken", "token", "access_token", "accessToken"):
            if auth_result.get(key):
                return auth_result.get(key)
        data = auth_result.get("data")
        if isinstance(data, dict):
            for key in ("authtoken", "authToken", "token", "access_token", "accessToken"):
                if data.get(key):
                    return data.get(key)
        return None

    def _extract_token_from_raw(self, raw: str) -> Optional[str]:
        """Ceipal may return XML even when json=1 is sent."""
        try:
            # Example:
            # <root><access_token>...</access_token><refresh_token>...</refresh_token></root>
            m = re.search(r"<access_token>([^<]+)</access_token>", raw)
            if m:
                return m.group(1).strip()
            # Fallback for other token tags
            m = re.search(r"<authtoken>([^<]+)</authtoken>", raw)
            if m:
                return m.group(1).strip()
        except Exception:
            return None
        return None
    
    async def authenticate(self) -> bool:
        """Authenticate with Ceipal API and get auth token"""
        try:
            async with httpx.AsyncClient() as client:
                auth_data = {
                    "email": self.email,
                    "password": self.password,
                    "api_key": self.api_key,
                    "json": 1
                }

                # Do not print secrets; store raw responses to cache for inspection.
                self.last_auth_error = None

                # Attempt 1: JSON body (many APIs expect this)
                response = await client.post(
                    self.auth_url,
                    json=auth_data,
                    headers={"Content-Type": "application/json"}
                )

                # If API expects form-encoded it may return 4xx; retry with form.
                if response.status_code >= 400:
                    response = await client.post(
                        self.auth_url,
                        data=auth_data,
                        headers={"Content-Type": "application/x-www-form-urlencoded"}
                    )

                auth_text = response.text
                auth_json = None
                try:
                    auth_json = response.json()
                except Exception:
                    auth_json = {"_raw": auth_text}

                self._write_json_cache(
                    "ceipal_auth_last.json",
                    {
                        "timestamp": datetime.now().isoformat(),
                        "status_code": response.status_code,
                        "url": self.auth_url,
                        "response": auth_json,
                    },
                )

                if response.status_code >= 400:
                    self.last_auth_error = f"HTTP {response.status_code}"
                    return False

                token = self._extract_authtoken(auth_json if isinstance(auth_json, dict) else {})
                if token:
                    self.auth_token = token
                    self.token_expires = datetime.now() + timedelta(hours=24)
                    return True

                # Some APIs signal success differently; keep raw response cached.
                self.last_auth_error = "Token not found in response"
                return False
                    
        except httpx.HTTPError as e:
            self.last_auth_error = str(e)
            if DEBUG:
                print(f"Authentication HTTP error: {e}")
            return False
        except Exception as e:
            self.last_auth_error = str(e)
            if DEBUG:
                print(f"Unexpected authentication error: {e}")
            return False
    
    async def get_auth_token(self) -> str:
        """Get valid auth token, refresh if needed"""
        if not self.auth_token or (self.token_expires and datetime.now() >= self.token_expires):
            if not await self.authenticate():
                raise HTTPException(status_code=401, detail="Failed to authenticate with Ceipal API")
        return self.auth_token
    
    async def fetch_jobs(self) -> List[Job]:
        """Fetch all jobs from Ceipal Reports API with pagination support and caching"""
        # Check cache first (outside lock for performance)
        cached_jobs = self._get_cached_jobs()
        if cached_jobs:
            return cached_jobs
        
        # Use lock to prevent multiple concurrent fetches
        async with self._fetch_lock:
            # Double-check cache after acquiring lock
            cached_jobs = self._get_cached_jobs()
            if cached_jobs:
                return cached_jobs
            
            all_jobs: List[Job] = []
            
            try:
                token = await self.get_auth_token()
                
                async with httpx.AsyncClient() as client:
                    headers = {
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json"
                    }
                    
                    page = 1
                    has_next = True
                    total_records = 0
                    
                    consecutive_429_errors = 0
                    max_429_retries = 3
                    
                    while has_next:  # Fetch ALL pages until has_next is false
                        # Fetch current page
                        url = f"{self.reports_url}?response_type=1&page={page}"
                        print(f"[Ceipal] Fetching page {page}...")
                        
                        try:
                            response = await self._get_reports_page_with_retry(
                                client,
                                url,
                                headers,
                                context="Ceipal",
                            )
                            consecutive_429_errors = 0  # Reset on success
                        except httpx.HTTPStatusError as e:
                            if e.response.status_code == 429:
                                consecutive_429_errors += 1
                                if consecutive_429_errors > max_429_retries:
                                    print(f"[Ceipal] Too many 429 errors, stopping at page {page}. Got {len(all_jobs)} jobs.")
                                    has_next = False
                                    break
                                
                                # Exponential backoff
                                wait_time = min(2 ** consecutive_429_errors, 30)
                                print(f"[Ceipal] Rate limited (429). Waiting {wait_time}s...")
                                await asyncio.sleep(wait_time)
                                continue  # Retry same page
                            else:
                                raise  # Re-raise other errors
                        
                        reports_data = response.json()
                        
                        # Get total records from first page
                        if page == 1:
                            total_records = int(reports_data.get("record_count", 0))
                            print(f"[Ceipal] Total records available: {total_records}")
                            self._write_json_cache(
                                "ceipal_reports_last.json",
                                {
                                    "timestamp": datetime.now().isoformat(),
                                    "status_code": response.status_code,
                                    "url": url,
                                    "response": reports_data,
                                },
                            )
                        
                        # Parse jobs from this page
                        page_jobs = await self._parse_jobs_from_reports(reports_data)
                        all_jobs.extend(page_jobs)
                        print(f"[Ceipal] Page {page}: fetched {len(page_jobs)} jobs, total so far: {len(all_jobs)}")
                        
                        # Check if there's a next page
                        has_next_page_val = reports_data.get("has_next_page")
                        next_page_val = reports_data.get("next_page")
                        has_next = bool(has_next_page_val) or bool(next_page_val)
                        
                        print(f"[Ceipal] has_next_page={has_next_page_val}, next_page exists={bool(next_page_val)}, has_next={has_next}")
                        
                        # Stop if we have all records
                        if len(all_jobs) >= total_records and total_records > 0:
                            print(f"[Ceipal] Got all {total_records} records, stopping pagination")
                            has_next = False
                        
                        page += 1
                    
                    print(f"[Ceipal] Finished fetching {len(all_jobs)} jobs from {page-1} pages")
                    
                    # Track how many pages we fetched for pagination info
                    self._last_fetched_pages = page - 1
                    self._last_total_records = total_records  # Store total available from Ceipal
                    
                    # Cache the results
                    self._set_cached_jobs(all_jobs)
                    return all_jobs
                    
            except httpx.HTTPError as e:
                if DEBUG:
                    print(f"Ceipal API Error: {e}")
            except Exception as e:
                if DEBUG:
                    print(f"Error fetching jobs: {e}")
            
            # Fallback: try cached reports
            cached = self._read_json_cache("ceipal_reports_last.json")
            if isinstance(cached, dict) and isinstance(cached.get("response"), (dict, list)):
                try:
                    cached_data = cached.get("response")
                    return await self._parse_jobs_from_reports(cached_data)
                except Exception:
                    pass
            
            # Last resort: mock jobs
            return self._get_mock_jobs()
    
    async def fetch_all_jobs_background(self):
        """Background task to fetch ALL jobs progressively using disk cache to manage memory.

        Fetches all pages without limit, saving to disk periodically to prevent memory issues.

        Single-flight: if a previous invocation is still running, this trigger is dropped.
        Both the 5-minute scheduler and the on-demand /api/jobs route call this method, and
        a single fetch can take longer than 5 minutes when Ceipal throttles, so concurrent
        triggers must NOT spawn parallel paginated fetches (they cascade into 429 storms).
        """
        if self._background_fetch_running:
            print("[Background] Fetch already in progress — dropping duplicate trigger.")
            return
        self._background_fetch_running = True

        print("[Background] Starting progressive job fetch (all pages)...")
        all_jobs = []  # In-memory batch
        consecutive_429_errors = 0
        max_429_retries = 5
        total_jobs_fetched = 0
        disk_batch = []  # Accumulate all jobs on disk

        # Status map for closure detection — captures JobStatus from raw response across ALL pages,
        # before the Open/Active filter applied by _parse_jobs_from_reports.
        current_status_map: dict = {}
        fetch_complete = True  # Flips to False on 429-truncation, exceptions, or record-count mismatch.

        try:
            token = await self.get_auth_token()
            
            async with httpx.AsyncClient() as client:
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"
                }
                
                page = 1
                has_next = True
                total_records = 0
                
                while has_next:
                    url = f"{self.reports_url}?response_type=1&page={page}"
                    print(f"[Background] Fetching page {page}...")
                    
                    try:
                        response = await self._get_reports_page_with_retry(
                            client,
                            url,
                            headers,
                            context="Background",
                        )
                        consecutive_429_errors = 0  # Reset on success
                        
                    except httpx.HTTPStatusError as e:
                        if e.response.status_code == 429:
                            consecutive_429_errors += 1
                            if consecutive_429_errors > max_429_retries:
                                print(f"[Background] Too many 429 errors, stopping. Got {total_jobs_fetched} jobs.")
                                fetch_complete = False
                                break
                            
                            # Exponential backoff: 2^errors seconds (2, 4, 8, 16, 32...)
                            wait_time = min(2 ** consecutive_429_errors, 60)
                            print(f"[Background] Rate limited (429). Waiting {wait_time}s before retry...")
                            await asyncio.sleep(wait_time)
                            continue  # Retry same page
                        else:
                            raise  # Re-raise other errors
                    
                    reports_data = response.json()

                    if page == 1:
                        total_records = int(reports_data.get("record_count", 0))
                        print(f"[Background] Total records available: {total_records}")

                    # Capture raw JobStatus for ALL jobs on this page (before any filter) for closure detection.
                    for entry in extract_ceipal_status_entries(reports_data):
                        current_status_map[entry["job_id"]] = entry

                    # Parse jobs from this page
                    page_jobs = await self._parse_jobs_from_reports(reports_data)
                    all_jobs.extend(page_jobs)
                    total_jobs_fetched += len(page_jobs)
                    print(f"[Background] Page {page}: fetched {len(page_jobs)} jobs, total so far: {total_jobs_fetched}")
                    
                    # Every 3 pages: save to disk and clear memory
                    if page % 3 == 0:
                        # Save current batch to disk
                        disk_batch.extend([job.dict() for job in all_jobs])
                        self._write_json_cache("jobs_full_list.json", disk_batch)
                        # Cache the CUMULATIVE list (everything fetched so far), not just the
                        # 3-page batch about to be cleared. Otherwise /api/jobs sees only the
                        # latest ~50 jobs while a long fetch is in flight, even though disk
                        # already has hundreds.
                        self._set_cached_jobs([Job(**job_data) for job_data in disk_batch])
                        self._last_fetched_pages = page
                        self._last_total_records = total_records
                        print(f"[Background] Saved {len(disk_batch)} jobs to disk, cached cumulative list, cleared memory batch")
                        # Clear in-memory list to free memory
                        all_jobs = []
                    
                    # Check if there's a next page. Ceipal sometimes sends next_page="" (empty string) on the
                    # final page, so int(next_page_val) on a non-None falsy value used to crash the loop.
                    has_next_page_val = reports_data.get("has_next_page")
                    next_page_val = reports_data.get("next_page")
                    next_page_int = None
                    if next_page_val not in (None, "", False):
                        try:
                            next_page_int = int(next_page_val)
                        except (TypeError, ValueError):
                            next_page_int = None
                    has_next = (
                        (has_next_page_val in (1, "1", True)) or
                        (next_page_int is not None and next_page_int > page)
                    )
                    
                    if has_next:
                        page += 1
                        # Longer delay to avoid rate limiting (2 seconds between requests)
                        await asyncio.sleep(2.0)
                
                # Final save: combine remaining in-memory jobs with disk cache
                if all_jobs:
                    disk_batch.extend([job.dict() for job in all_jobs])
                
                # Save full list to disk
                self._write_json_cache("jobs_full_list.json", disk_batch)
                
                # Update in-memory cache with all jobs (reload from disk to ensure consistency)
                all_jobs_for_cache = [Job(**job_data) for job_data in disk_batch]
                self._set_cached_jobs(all_jobs_for_cache)
                self._last_fetched_pages = page - 1
                self._last_total_records = total_records
                print(f"[Background] Completed fetching {total_jobs_fetched} jobs from {page-1} pages")

                # Strict completeness check: unique-jobs-seen must cover Ceipal's record_count.
                # Anything less ⇒ partial fetch ⇒ skip closure detection (and don't update tracker).
                if total_records > 0 and len(current_status_map) < total_records:
                    fetch_complete = False
                    print(f"[Background] Fetch incomplete: {len(current_status_map)} unique jobs vs {total_records} expected. Skipping closure detection.")

                detect_and_notify_closures(current_status_map, fetch_complete=fetch_complete)

        except Exception as e:
            print(f"[Background] Error fetching jobs: {e}")
            import traceback
            print(f"[Background] Traceback: {traceback.format_exc()}")
            # Save whatever we got to disk
            if all_jobs or disk_batch:
                if all_jobs:
                    disk_batch.extend([job.dict() for job in all_jobs])
                self._write_json_cache("jobs_full_list.json", disk_batch)
                all_jobs_for_cache = [Job(**job_data) for job_data in disk_batch]
                self._set_cached_jobs(all_jobs_for_cache)
            # Exception path = partial fetch; do NOT run closure detection.
        finally:
            self._background_fetch_running = False

    async def fetch_more_jobs(self, start_page: int, max_pages: int = 25) -> dict:
        """Fetch additional pages of jobs beyond initial load."""
        more_jobs: List[Job] = []
        consecutive_429_errors = 0
        max_429_retries = 3
        next_start_page = start_page
        has_more = False
        
        try:
            token = await self.get_auth_token()
            
            async with httpx.AsyncClient() as client:
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"
                }
                
                page = start_page
                end_page = start_page + max_pages - 1
                has_next = True
                
                while has_next and page <= end_page:
                    url = f"{self.reports_url}?response_type=1&page={page}"
                    print(f"[Ceipal] Loading more - page {page}...")
                    
                    try:
                        response = await self._get_reports_page_with_retry(
                            client,
                            url,
                            headers,
                            context="Ceipal load-more",
                        )
                        consecutive_429_errors = 0  # Reset on success
                    except httpx.HTTPStatusError as e:
                        if e.response.status_code == 429:
                            consecutive_429_errors += 1
                            if consecutive_429_errors > max_429_retries:
                                print(f"[Ceipal] Too many 429 errors, stopping. Got {len(more_jobs)} jobs.")
                                break
                            
                            # Exponential backoff
                            wait_time = min(2 ** consecutive_429_errors, 30)
                            print(f"[Ceipal] Rate limited (429). Waiting {wait_time}s...")
                            await asyncio.sleep(wait_time)
                            continue  # Retry same page
                        else:
                            raise
                    
                    reports_data = response.json()
                    page_jobs = await self._parse_jobs_from_reports(reports_data)
                    more_jobs.extend(page_jobs)
                    
                    has_next_page_val = reports_data.get("has_next_page")
                    next_page_val = reports_data.get("next_page")
                    has_next = bool(has_next_page_val) or bool(next_page_val)
                    next_start_page = page + 1
                    has_more = has_next
                    
                    page += 1
                
                print(f"[Ceipal] Loaded {len(more_jobs)} more jobs from pages {start_page}-{page-1}")
                return {
                    "jobs": more_jobs,
                    "next_start_page": next_start_page,
                    "has_more": has_more,
                }
                
        except Exception as e:
            print(f"[Ceipal] Error loading more jobs: {e}")
            return {
                "jobs": [],
                "next_start_page": start_page,
                "has_more": False,
            }

    async def _parse_jobs_from_reports(self, reports_data) -> List[Job]:
        """Parse Ceipal reports data into Job models.
        
        Ceipal response structure:
        {
            "success": 1,
            "message": "Records Found",
            "record_count": "35625",
            "result": [
                {
                    "JobCode": "JPC - 267008",
                    "JobTitle": "Registered Nurse - PACU",
                    "JobStatus": "Open",  # or "Active"
                    "States": "New Mexico",
                    "Location": "[Albuquerque, NM, 87106]",
                    "Client": "Aya Healthcare",
                    "EndClient": "University of New Mexico Hospital",
                    "Duration": "13Weeks",
                    "ClientBillRateSalary": "USD/76",
                    ...
                }
            ]
        }
        """
        jobs: List[Job] = []
        
        if not isinstance(reports_data, dict):
            return jobs
            
        # Get the result array from Ceipal response
        job_data_list = reports_data.get("result", [])
        if not job_data_list:
            # Try other common keys
            job_data_list = reports_data.get("data", reports_data.get("jobs", reports_data.get("records", [])))
        
        if not isinstance(job_data_list, list):
            return jobs
        
        for job_data in job_data_list:
            if not isinstance(job_data, dict):
                continue
                
            # Only include jobs with status "Open" or "Active"
            job_status = job_data.get("JobStatus", "").lower()
            if job_status not in ["open", "active"]:
                continue
            
            # Parse location from Ceipal format: "[City, State, ZIP]" or just use States
            location_raw = job_data.get("Location", "")
            states = job_data.get("States", "")
            specialty = (
                job_data.get("Specialty")
                or job_data.get("JobSpecialty")
                or job_data.get("Speciality")
                or job_data.get("JobSpeciality")
                or ""
            )
            location = states
            if location_raw and location_raw != "N/A":
                # Clean up location format: "[City, State, ZIP]" -> "City, State"
                location_clean = location_raw.strip("[]")
                location = location_clean
            
            # Get the actual job description from Ceipal
            job_description = job_data.get("JobDescription", "").strip()
            requirements = job_data.get("Requirements", "").strip() or job_data.get("JobRequirements", "").strip()
            
            # Combine description and requirements for full details
            full_description_parts = []
            if job_description:
                full_description_parts.append(job_description)
            if requirements:
                full_description_parts.append(f"Requirements:\n{requirements}")
            
            if full_description_parts:
                description = "\n\n".join(full_description_parts)
            else:
                # Build description from available fields as fallback
                description_parts = []
                end_client = job_data.get("EndClient", "")
                duration = job_data.get("Duration", "")
                if end_client:
                    description_parts.append(f"End Client: {end_client}")
                if duration:
                    description_parts.append(f"Duration: {duration}")
                description = " | ".join(description_parts) if description_parts else job_data.get("JobTitle", "")
            
            # Calculate displayed bill rate by hiding the actual rate behind a flat 7% reduction.
            actual_bill_rate_str = job_data.get("ClientBillRateSalary", job_data.get("BillRate", "0"))
            salary_range_display = display_bill_rate(actual_bill_rate_str)
            if salary_range_display and "/hr" not in salary_range_display.lower():
                salary_range_display = f"{salary_range_display}/hr"
            if not salary_range_display:
                salary_range_display = "Contact for rate"
            
            # Get actual job code
            actual_job_code = str(job_data.get("JobCode", f"job_{len(jobs)+1}"))
            
            # Map Ceipal fields to our Job model
            job = Job(
                id=actual_job_code,
                title=job_data.get("JobTitle", "Position Not Specified"),
                description=description,
                requirements=requirements,
                department=f"Job Code: {actual_job_code}",
                location=location if location else "Not specified",
                employment_type=job_data.get("Duration", "Contract"),  # Duration as employment type
                salary_range=salary_range_display,  # Show updated rate to vendors
                posted_date=self._parse_date(job_data.get("JobCreated", job_data.get("CreatedDate"))),
                status=normalize_job_status(job_data.get("JobStatus")),
                end_client=job_data.get("EndClient", None),
                specialty=specialty.strip() or None,
                state=states.strip() or None,
                bill_rate_discount_applied=True,
            )
            jobs.append(job)
            
        return jobs
    
    def _parse_date(self, date_str: str) -> datetime:
        """Parse date string from various formats"""
        if not date_str:
            return datetime.now()
        
        try:
            # Try different date formats
            for fmt in ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y", "%m/%d/%Y", "%Y%m%d"]:
                try:
                    return datetime.strptime(date_str, fmt)
                except ValueError:
                    continue
            
            # If all formats fail, return current date
            return datetime.now()
        except Exception:
            return datetime.now()
    
    def _get_mock_jobs(self) -> List[Job]:
        """Mock data for demonstration when Ceipal API is not available"""
        return [
            Job(
                id="ceipal_001",
                title="Senior Software Developer",
                description="Looking for an experienced Software Developer to join our development team.",
                department="Engineering",
                location="New York, NY",
                employment_type="Full-time",
                salary_range=None,
                posted_date=datetime.now(),
                status="active",
                requirements="5+ years of software development experience, strong knowledge of modern frameworks.",
                bill_rate_discount_applied=True,
            ),
            Job(
                id="ceipal_002",
                title="Business Analyst",
                description="Seeking a Business Analyst to analyze business requirements and create solutions.",
                department="Business Analysis",
                location="Remote",
                employment_type="Contract",
                salary_range=None,
                posted_date=datetime.now(),
                status="active",
                requirements="3+ years of business analysis experience, excellent communication skills.",
                bill_rate_discount_applied=True,
            ),
            Job(
                id="ceipal_003",
                title="Project Manager",
                description="Experienced Project Manager needed to oversee multiple projects and teams.",
                department="Project Management",
                location="Chicago, IL",
                employment_type="Full-time",
                salary_range=None,
                posted_date=datetime.now(),
                status="active",
                requirements="PMP certification preferred, 5+ years of project management experience.",
                bill_rate_discount_applied=True,
            )
        ]

ceipal_client = CeipalClient()

# API Endpoints
@app.get("/")
async def root():
    return {"message": "VMS Backend API is running"}

@app.get("/api/jobs", response_model=JobListResponse)
async def get_jobs(request: Request, current_user: UserDB = Depends(get_current_user)):
    """Get all active jobs from direct input and Excel files.

    Returns a pre-serialized body with an ETag. Browsers revalidate with
    If-None-Match automatically, so an unchanged job list costs a 304 instead
    of re-downloading every job description on every tab load and refresh.
    """
    try:
        is_admin = current_user.email.lower() == ADMIN_EMAIL.lower()
        payload = await get_jobs_response_payload_async(is_admin)
        headers = {
            "ETag": payload["etag"],
            # private: the body differs per role, so shared caches must not store it.
            # no-cache: always revalidate, so new jobs show up immediately.
            "Cache-Control": "private, no-cache",
            "Vary": "Authorization",
        }
        if request.headers.get("if-none-match") == payload["etag"]:
            return Response(status_code=304, headers=headers)
        return Response(content=payload["body"], media_type="application/json", headers=headers)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch jobs: {str(e)}")


@app.post("/api/admin/job-status-report/send")
async def send_job_status_report_now(current_user: UserDB = Depends(get_current_user)):
    """Admin: send the open/closed report immediately (ignores the 2-hour schedule)."""
    if current_user.email.lower() != ADMIN_EMAIL.lower():
        raise HTTPException(status_code=403, detail="Only admin can send the job status report")
    return await asyncio.to_thread(send_job_status_report, True)


@app.get("/api/ceipal/status")
async def get_ceipal_status():
    return {
        "status": "disabled",
        "enabled": CEIPAL_ENABLED,
        "is_refreshing": False,
        "has_cached_jobs": False,
        "cached_jobs_count": 0,
        "cache_age_seconds": None,
        "last_fetched_pages": 0,
        "total_records": 0,
        "last_auth_error": None,
    }

@app.get("/api/jobs/{job_id}", response_model=Job)
async def get_job(job_id: str, current_user: UserDB = Depends(get_current_user)):
    """Get one job with its full description (the list endpoint only sends a preview)."""
    try:
        is_admin = current_user.email.lower() == ADMIN_EMAIL.lower()
        payload = await get_jobs_response_payload_async(is_admin)
        job = payload["jobs_by_id"].get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch job: {str(e)}")

@app.get("/api/jobs/load-more")
async def load_more_jobs(start_page: int = 26, max_pages: int = 25):
    """CEIPAL pagination is disabled."""
    return {
        "jobs": [],
        "total": 0,
        "start_page": start_page,
        "next_start_page": start_page,
        "has_more": False,
    }

@app.get("/api/ceipal/test")
async def test_ceipal_connection():
    return {
        "status": "disabled",
        "enabled": CEIPAL_ENABLED,
        "message": "Ceipal integration is disabled",
    }

@app.get("/api/ceipal/cache")
async def get_ceipal_cache_status():
    return {
        "enabled": CEIPAL_ENABLED,
        "message": "Ceipal integration is disabled",
        "auth_cached": None,
        "reports_cached": None,
    }

@app.get("/healthz")
async def liveness():
    """Liveness: no I/O, safe to poll every few seconds."""
    return {"status": "ok"}


_readyz_cache: Dict[str, Any] = {"at": 0.0, "ok": True}


@app.get("/readyz")
def readiness():
    """Readiness: one MongoDB ping, cached for 5 s so a monitor cannot load the database."""
    if not mongodb_enabled:
        return {"status": "ok", "mongodb": "disabled"}
    if _monotonic() - _readyz_cache["at"] > 5:
        try:
            mongo_client.admin.command("ping")
            _readyz_cache["ok"] = True
        except Exception as e:
            print(f"[Health] readyz ping failed: {e}")
            _readyz_cache["ok"] = False
        _readyz_cache["at"] = _monotonic()
    if not _readyz_cache["ok"]:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")
    return {"status": "ok", "mongodb": "ok"}


@app.get("/api/health")
def health_check():
    """Diagnostics for MongoDB/JSON-backed runtime state. Prefer /healthz or /readyz for monitors."""
    users = _users_cache if _users_cache else load_users_from_json()
    mongodb_ping_ms = None
    if mongodb_enabled:
        try:
            mongodb_ping_ms = measure_mongodb_ping_ms(samples=1)
        except Exception as e:
            print(f"[Health] MongoDB ping failed: {e}")
    return {
        "storage_mode": "mongodb_json",
        "mongodb_enabled": mongodb_enabled,
        "mongodb_active_cluster": ACTIVE_MONGODB_LABEL,
        "mongodb_ping_ms": mongodb_ping_ms,
        "mongodb_startup_ping_ms": mongodb_startup_ping_ms,
        "mongodb_slow": bool(mongodb_ping_ms is not None and mongodb_ping_ms > MONGODB_SLOW_PING_MS),
        "user_count": len(users),
        "upload_dir": UPLOAD_DIR,
        "upload_dir_exists": os.path.exists(UPLOAD_DIR),
        "data_dir": DATA_DIR,
        "data_dir_exists": os.path.exists(DATA_DIR),
        "ceipal_enabled": CEIPAL_ENABLED,
        "cache_dir": CEIPAL_CACHE_DIR,
        "cache_dir_exists": os.path.exists(CEIPAL_CACHE_DIR),
    }

@app.post("/api/ceipal/refresh")
async def force_refresh_jobs():
    return {
        "status": "disabled",
        "enabled": CEIPAL_ENABLED,
        "message": "Ceipal integration is disabled",
    }

@app.get("/api/ceipal/reports")
async def get_ceipal_reports():
    return {
        "status": "disabled",
        "enabled": CEIPAL_ENABLED,
        "message": "Ceipal integration is disabled",
    }

@app.post("/api/candidates/submit")
async def submit_candidate(
    request: Request,
    background_tasks: BackgroundTasks,
    candidate_name: str = Form(...),
    email: str = Form(...), 
    phone: str = Form(...),
    bill_rate: str = Form(...),
    current_location: str = Form(...),
    primary_skills: str = Form(...),
    job_title: str = Form(...),
    years_experience: str = Form(...),
    tentative_start_date: str = Form(...),
    rto: str = Form(...),
    candidate_summary: str = Form(...),
    job_id: str = Form(...),
    resume: UploadFile = File(...),
    current_user: UserDB = Depends(get_current_user)
):
    """Submit candidate resume for a job (requires authentication) - MongoDB persistent storage"""
    try:
        # Validate file
        if resume.size > MAX_FILE_SIZE:
            raise HTTPException(status_code=413, detail="File too large")

        file_extension = resume.filename.split(".")[-1].lower()
        allowed_extensions = ["pdf", "doc", "docx", "txt"]
        if file_extension not in allowed_extensions:
            raise HTTPException(status_code=400, detail="Invalid file type")

        # Basic shape checks so garbage form data fails fast with a clear 400
        # instead of being written to the candidate record as-is.
        email_norm = email.strip()
        if not EMAIL_PATTERN.match(email_norm):
            raise HTTPException(status_code=400, detail="Invalid email address")
        try:
            years_experience_num = float(years_experience.strip())
            if years_experience_num < 0 or years_experience_num > 80:
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=400, detail="Years of Experience must be a non-negative number")

        # A vendor may still have a job open in the browser after it closed.
        # Closed jobs are purged, so a job missing from a loaded list is one that
        # closed. A job list that is still loading must not block submissions.
        admin_payload = _cached_jobs_payload("admin")
        if admin_payload and job_id not in admin_payload["jobs_by_id"]:
            raise HTTPException(status_code=409, detail="This job is closed and no longer accepts submissions")

        # Guard against duplicate submissions: a client retry after a transient
        # error (the request timing out before the response arrives, or a user
        # re-clicking Submit after seeing a false failure) would otherwise create
        # a second, identical candidate record and a second resume upload. If
        # this vendor already submitted this candidate for this job in the last
        # few minutes, treat the retry as a no-op success rather than duplicating it.
        if mongodb_enabled and candidates_collection is not None:
            dedupe_cutoff = (datetime.now() - timedelta(minutes=5)).isoformat()
            existing_submission = await asyncio.to_thread(
                _with_mongo_retries,
                "Duplicate submission check",
                lambda: candidates_collection.find_one(
                    {
                        "submitted_by_user_id": current_user.id,
                        "job_id": job_id,
                        "email": email_norm,
                        "submitted_date": {"$gte": dedupe_cutoff},
                    },
                    sort=[("submitted_date", -1)],
                ),
                MONGODB_WRITE_RETRY_ATTEMPTS,
            )
            if existing_submission:
                return {
                    "message": "Candidate already submitted",
                    "candidate_id": existing_submission.get("id"),
                    "status": existing_submission.get("status", "submitted"),
                    "submitted_by": current_user.full_name,
                    "duplicate": True,
                }

        # Read resume content
        content = await resume.read()

        # Store resume in GridFS (persistent across redeploys)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{candidate_name.replace(' ', '_')}_{timestamp}.{file_extension}"

        if mongodb_enabled and fs is not None:
            # Store in GridFS (off the event loop - a multi-MB upload to Atlas
            # used to stall every other request on this worker). Retried: a
            # socket timeout here previously failed the whole submission outright.
            resume_file_id = await asyncio.to_thread(
                _with_mongo_retries,
                "GridFS resume upload",
                lambda: fs.put(
                    content,
                    filename=filename,
                    content_type=resume.content_type or 'application/octet-stream',
                    metadata={
                        'candidate_name': candidate_name,
                        'job_id': job_id,
                        'uploaded_by': current_user.email,
                        'uploaded_at': datetime.now().isoformat()
                    }
                ),
                MONGODB_WRITE_RETRY_ATTEMPTS,
            )
            resume_storage_id = str(resume_file_id)
            storage_type = "gridfs"
            print(f"[Submissions] Resume stored in GridFS: {resume_storage_id}")
        else:
            # Fallback to local filesystem if MongoDB not available
            file_path = os.path.join(UPLOAD_DIR, filename)
            async with aiofiles.open(file_path, 'wb') as f:
                await f.write(content)
            resume_storage_id = file_path
            storage_type = "local"
            print(f"[Submissions] Resume stored locally: {file_path}")
        
        # Generate unique candidate ID
        candidate_id = f"candidate_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(uuid4())[:8]}"
        submitted_at = datetime.now().isoformat()
        submission_ip_address = get_client_ip(request)
        submission_user_agent = request.headers.get("user-agent", "")
        displayed_bill_rate = display_bill_rate(bill_rate) or "N/A"
        
        # Store candidate in MongoDB with submitter info
        candidate_doc = {
            "id": candidate_id,
            "name": candidate_name,
            "email": email_norm,
            "phone": phone,
            "job_id": job_id,
            "resume_storage_id": resume_storage_id,
            "resume_storage_type": storage_type,
            "resume_filename": filename,
            "submitted_date": submitted_at,
            "status": "submitted",
            "submitted_by_user_id": current_user.id,
            "submitted_by_email": current_user.email,
            "submitted_by_name": current_user.full_name,
            "submission_ip_address": submission_ip_address,
            "submission_user_agent": submission_user_agent,
            "bill_rate": displayed_bill_rate,
            "bill_rate_discount_applied": True,
            "current_location": current_location,
            "primary_skills": primary_skills,
            "job_title": job_title,
            "years_experience": years_experience,
            "tentative_start_date": tentative_start_date,
            "rto": rto,
            "candidate_summary": candidate_summary
        }
        
        await asyncio.to_thread(store_candidate_doc, candidate_doc)

        audit_doc = {
            "id": f"submission_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(uuid4())[:8]}",
            "candidate_id": candidate_id,
            "candidate_name": candidate_name,
            "candidate_email": email_norm,
            "candidate_phone": phone,
            "job_id": job_id,
            "job_title": job_title,
            "submitted_at": submitted_at,
            "submitted_by_user_id": current_user.id,
            "submitted_by_name": current_user.full_name,
            "submitted_by_email": current_user.email,
            "ip_address": submission_ip_address,
            "user_agent": submission_user_agent,
            "metadata": {
                "bill_rate": displayed_bill_rate,
                "bill_rate_discount_applied": True,
                "current_location": current_location,
                "primary_skills": primary_skills,
                "years_experience": years_experience,
                "tentative_start_date": tentative_start_date,
                "rto": rto,
                "resume_filename": filename,
                "resume_storage_type": storage_type,
            },
        }
        try:
            await asyncio.to_thread(record_submission_log, audit_doc)
        except Exception as e:
            print(f"[SubmissionAudit] Failed to write audit log for {candidate_id}: {e}")

        # The candidate record above is already durably saved; a failure computing
        # these display-only counts must not make the API report the submission
        # as failed, since that would push the vendor to resubmit and hit the
        # duplicate-submission guard instead of just seeing their confirmation.
        try:
            user_submission_summary = await asyncio.to_thread(
                compute_user_submission_summary,
                current_user.id,
                submitted_at,
            )
        except Exception as e:
            print(f"[Submissions] Could not compute submission summary for {candidate_id}: {e}")
            submission_date = submitted_at[:10] if len(submitted_at) >= 10 else ""
            user_submission_summary = {
                "total_profiles_submitted": None,
                "profiles_submitted_on_date": None,
                "submission_date": submission_date,
                "first_submission_at": submitted_at,
                "last_submission_at": submitted_at,
            }

        # Send email notification to admin
        vendor_info = {
            "full_name": current_user.full_name,
            "email": current_user.email,
            "id": current_user.id
        }
        # Send the admin email after the response goes out: the candidate is already
        # stored, so the vendor should not wait on SendGrid. Sync background tasks run
        # in the threadpool, off the event loop.
        background_tasks.add_task(
            send_submission_notification_and_log,
            candidate_doc,
            vendor_info,
            user_submission_summary,
        )
        
        return {
            "message": "Candidate submitted successfully",
            "candidate_id": candidate_id,
            "status": "submitted",
            "submitted_by": current_user.full_name,
            "submission_summary": user_submission_summary,
        }
        
    except HTTPException:
        # Validation errors raised above (413/400) already carry the right
        # status code and message; don't let the generic handler below
        # flatten them into a misleading 500.
        raise
    except Exception as e:
        print(f"[Submissions] Error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to submit candidate: {str(e)}")

@app.get("/api/candidates/job/{job_id}")
def get_candidates_for_job(job_id: str, current_user: UserDB = Depends(get_current_user)):
    """Get candidates submitted for a specific job. Admin sees all; vendors only their own."""
    try:
        job_candidates = []
        is_admin = current_user.email.lower() == ADMIN_EMAIL.lower()
        
        if mongodb_enabled and candidates_collection is not None:
            # Query MongoDB
            query = {"job_id": job_id}
            if not is_admin:
                query["submitted_by_user_id"] = current_user.id
            cursor = candidates_collection.find(query)
            for doc in cursor:
                doc["_id"] = str(doc["_id"])  # Convert ObjectId to string
                sanitize_candidate_bill_rate(doc)
                job_candidates.append(doc)
        else:
            # Fallback to JSON file
            candidates_file = os.path.join(DATA_DIR, "candidates.json")
            if os.path.exists(candidates_file):
                with open(candidates_file, 'r') as f:
                    all_candidates = json.load(f)
                    job_candidates = [
                        c for c in all_candidates
                        if c.get("job_id") == job_id
                        and (is_admin or c.get("submitted_by_user_id") == current_user.id)
                    ]
                    for c in job_candidates:
                        sanitize_candidate_bill_rate(c)
        
        return {"candidates": job_candidates, "total": len(job_candidates)}
    except Exception as e:
        print(f"[Submissions] Error fetching job candidates: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch candidates: {str(e)}")

@app.get("/api/candidates")
def get_all_candidates(
    current_user: UserDB = Depends(get_current_user)
):
    """Get submitted candidates with submitter info. Vendors see only their own submissions, admin sees all. - MongoDB persistent storage"""
    try:
        is_admin = current_user.email.lower() == ADMIN_EMAIL.lower()
        candidates = load_candidate_submissions(None if is_admin else current_user.id)
        for candidate in candidates:
            candidate["submitted_by"] = {
                "id": candidate.get("submitted_by_user_id"),
                "full_name": candidate.get("submitted_by_name"),
                "email": candidate.get("submitted_by_email")
            } if candidate.get("submitted_by_user_id") else None

        return {"candidates": candidates, "total": len(candidates)}
    except Exception as e:
        print(f"[Submissions] Error fetching candidates: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch candidates: {str(e)}")


@app.get("/api/submission-history")
def get_submission_history(current_user: UserDB = Depends(get_current_user)):
    """Return submission history grouped by user and submission date."""
    try:
        is_admin = current_user.email.lower() == ADMIN_EMAIL.lower()
        candidates = load_candidate_submissions(None if is_admin else current_user.id)
        history = build_submission_history(candidates)
        return {
            "history": history,
            "total_profiles": len(candidates),
            "total_users": len(history),
            "generated_at": datetime.now().isoformat(),
        }
    except Exception as e:
        print(f"[SubmissionHistory] Error fetching history: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch submission history: {str(e)}")


@app.get("/api/submission-logs")
def get_submission_logs(current_user: UserDB = Depends(get_current_user)):
    """Admin-only submission audit log."""
    is_admin = current_user.email.lower() == ADMIN_EMAIL.lower()
    if not is_admin:
        raise HTTPException(status_code=403, detail="Only admin can view submission logs")

    try:
        logs = []
        if mongodb_enabled and submission_logs_collection is not None:
            cursor = submission_logs_collection.find().sort("submitted_at", -1).limit(500)
            for doc in cursor:
                doc["_id"] = str(doc["_id"])
                sanitize_submission_log_bill_rate(doc)
                logs.append(doc)
        else:
            logs_file = os.path.join(DATA_DIR, "submission_logs.json")
            if os.path.exists(logs_file):
                with open(logs_file, "r") as f:
                    logs = json.load(f)
                logs = sorted(logs, key=lambda row: row.get("submitted_at", ""), reverse=True)[:500]
                for log_doc in logs:
                    sanitize_submission_log_bill_rate(log_doc)
        return {"logs": logs, "total": len(logs)}
    except Exception as e:
        print(f"[SubmissionAudit] Error fetching logs: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch submission logs: {str(e)}")

@app.patch("/api/candidates/{candidate_id}/status")
def update_candidate_status(
    candidate_id: str,
    status: str,
    current_user: UserDB = Depends(get_current_user)
):
    """Update candidate status. Only admin can update status. - MongoDB persistent storage"""
    try:
        # Check permissions - only admin can update status
        is_admin = current_user.email.lower() == ADMIN_EMAIL.lower()
        if not is_admin:
            raise HTTPException(status_code=403, detail="Only admin can update candidate status")
        
        # Validate status
        valid_statuses = ["submitted", "offer", "decline", "start"]
        if status.lower() not in valid_statuses:
            raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {', '.join(valid_statuses)}")
        
        if mongodb_enabled and candidates_collection is not None:
            # Update in MongoDB
            result = candidates_collection.update_one(
                {"id": candidate_id},
                {"$set": {"status": status.lower()}}
            )
            if result.matched_count == 0:
                raise HTTPException(status_code=404, detail="Candidate not found")
        else:
            # Fallback: Update in JSON file
            candidates_file = os.path.join(DATA_DIR, "candidates.json")
            if os.path.exists(candidates_file):
                with open(candidates_file, 'r') as f:
                    all_candidates = json.load(f)
                
                candidate_found = False
                for c in all_candidates:
                    if c.get("id") == candidate_id:
                        c["status"] = status.lower()
                        candidate_found = True
                        break
                
                if not candidate_found:
                    raise HTTPException(status_code=404, detail="Candidate not found")
                
                with open(candidates_file, 'w') as f:
                    json.dump(all_candidates, f, indent=2)
        
        return {"message": "Status updated successfully", "candidate_id": candidate_id, "status": status.lower()}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[Submissions] Error updating status: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update status: {str(e)}")


@app.post("/api/candidates/{candidate_id}/notify-vendor")
async def notify_vendor_about_submission(
    candidate_id: str,
    payload: VendorMessageRequest,
    current_user: UserDB = Depends(get_current_user),
):
    """Admin-only: email the vendor who submitted this candidate with a free-form message."""
    print(f"[NotifyVendor] Request for candidate_id={candidate_id} by user={current_user.email}")
    is_admin = current_user.email.lower() == ADMIN_EMAIL.lower()
    if not is_admin:
        raise HTTPException(status_code=403, detail="Only admin can email vendors")

    subject = (payload.subject or "").strip()
    message_body = (payload.message or "").strip()
    if not subject or not message_body:
        raise HTTPException(status_code=400, detail="Subject and message are required")

    # Look up candidate (Mongo first, JSON fallback)
    candidate = None
    if mongodb_enabled and candidates_collection is not None:
        candidate = candidates_collection.find_one({"id": candidate_id})
    if candidate is None:
        candidates_file = os.path.join(DATA_DIR, "candidates.json")
        if os.path.exists(candidates_file):
            try:
                with open(candidates_file, "r") as f:
                    for c in json.load(f):
                        if c.get("id") == candidate_id:
                            candidate = c
                            break
            except Exception:
                pass

    if candidate is None:
        print(f"[NotifyVendor] Candidate {candidate_id} not found")
        raise HTTPException(status_code=404, detail="Candidate not found")

    vendor_email = (candidate.get("submitted_by_email") or "").strip()
    if not vendor_email:
        print(f"[NotifyVendor] Candidate {candidate_id} has no submitted_by_email")
        raise HTTPException(status_code=400, detail="Vendor email is missing on this submission")

    print(f"[NotifyVendor] Dispatching SendGrid send to {vendor_email}")
    # Run the blocking SendGrid call in a worker thread so it doesn't block the FastAPI event loop
    # (the SendGrid Python SDK is synchronous and can take several seconds, which would otherwise
    # stall every other request on this API worker, making login + /api/jobs feel hung).
    success, detail = await asyncio.to_thread(
        send_vendor_message_email,
        vendor_email,
        candidate.get("submitted_by_name") or "",
        subject,
        message_body,
        candidate.get("name") or "",
        candidate.get("job_title") or "",
        candidate.get("job_id") or "",
    )
    print(f"[NotifyVendor] Result success={success} detail={detail}")

    if not success:
        raise HTTPException(status_code=502, detail=detail)

    return {"message": "Email sent", "vendor_email": vendor_email, "candidate_id": candidate_id, "detail": detail}


@app.get("/api/resumes/{candidate_id}")
def download_resume(candidate_id: str):
    """Download resume file for a candidate - MongoDB GridFS or local fallback"""
    try:
        # Find candidate
        candidate = None
        if mongodb_enabled and candidates_collection is not None:
            candidate = candidates_collection.find_one({"id": candidate_id})
        else:
            # Fallback to JSON file
            candidates_file = os.path.join(DATA_DIR, "candidates.json")
            if os.path.exists(candidates_file):
                with open(candidates_file, 'r') as f:
                    all_candidates = json.load(f)
                    for c in all_candidates:
                        if c.get("id") == candidate_id:
                            candidate = c
                            break
        
        if not candidate:
            raise HTTPException(status_code=404, detail="Candidate not found")
        
        storage_type = candidate.get("resume_storage_type", "local")
        
        if storage_type == "gridfs" and mongodb_enabled and fs is not None:
            # Retrieve from GridFS
            file_id = candidate.get("resume_storage_id")
            if not file_id:
                raise HTTPException(status_code=404, detail="Resume file reference not found")
            
            grid_file = fs.get(ObjectId(file_id))
            if not grid_file:
                raise HTTPException(status_code=404, detail="Resume file not found in storage")
            
            # Serve straight from memory (resumes are capped at MAX_FILE_SIZE). This used
            # to write a NamedTemporaryFile(delete=False) per download that was never removed.
            download_name = candidate.get("resume_filename", "resume.pdf")
            return Response(
                content=grid_file.read(),
                media_type=grid_file.content_type or "application/octet-stream",
                # RFC 5987 form, as FileResponse uses, so non-ASCII candidate names stay valid headers.
                headers={"Content-Disposition": f"attachment; filename*=utf-8''{quote(download_name)}"},
            )
        else:
            # Fallback: Retrieve from local filesystem
            resume_path = candidate.get("resume_storage_id")
            if not resume_path or not os.path.exists(resume_path):
                raise HTTPException(status_code=404, detail="Resume file not found")
            
            return FileResponse(
                resume_path,
                filename=os.path.basename(resume_path),
                media_type="application/octet-stream"
            )
    except HTTPException:
        raise
    except Exception as e:
        print(f"[Submissions] Error downloading resume: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to download resume: {str(e)}")

class JobPreferencesUpdate(BaseModel):
    email_notifications: bool = True
    professions: List[str] = []
    specialties: List[str] = []
    job_types: List[str] = []
    states: List[str] = []


def build_preference_options(prefs: dict) -> dict:
    """Choices for the preferences form: values present on current jobs, plus
    anything the user already selected (so a selection never silently vanishes)."""
    options = {field: {} for field in PREFERENCE_LIST_FIELDS}

    def add(field: str, value: str) -> None:
        text = _clean_text(value)
        if text and text.lower() != "not specified":
            options[field].setdefault(text.lower(), text)

    for job in get_all_jobs():
        job_dict = {
            "title": job.title, "profession": job.profession, "specialty": job.specialty,
            "department": job.department, "state": job.state, "location": job.location,
        }
        attrs = job_preference_attributes(job_dict)
        add("professions", job.profession or "")
        specialty = job.specialty or ("" if (job.department or "").lower().startswith("job code:") else job.department or "")
        add("specialties", specialty)
        add("states", attrs["states"].upper() if len(attrs["states"]) == 2 else "")
    # Direct/Nexus jobs store the job type (Locum, Permanent, ...) as the title.
    # Excel titles are free text, so they are not offered as job types.
    for job in load_manual_jobs():
        add("job_types", job.title)
    for field in PREFERENCE_LIST_FIELDS:
        for value in prefs.get(field) or []:
            add(field, value)
    return {field: sorted(values.values(), key=str.lower) for field, values in options.items()}


@app.get("/api/preferences")
def get_job_preferences(current_user: UserDB = Depends(get_current_user)):
    """Current user's job alert preferences, plus the selectable values."""
    prefs = get_user_preferences(current_user.email)
    return {"preferences": prefs, "options": build_preference_options(prefs)}


@app.put("/api/preferences")
def update_job_preferences(
    payload: JobPreferencesUpdate,
    current_user: UserDB = Depends(get_current_user),
):
    """Save job alert preferences. Empty lists mean 'any'."""
    prefs = save_user_preferences(current_user.email, payload.dict())
    return {"message": "Preferences saved", "preferences": prefs}


@app.get("/api/notifications")
def get_user_notifications(
    current_user: UserDB = Depends(get_current_user),
    unread_only: bool = False
):
    """Get notifications for the current user"""
    try:
        if not mongodb_enabled or notifications_collection is None:
            return {"notifications": [], "unread_count": 0}
        
        user_email = current_user.email.lower()
        
        # Build query
        query = {"user_email": user_email}
        if unread_only:
            query["read"] = False
        
        # Get notifications sorted by date (newest first)
        notifications = list(notifications_collection.find(query).sort("created_at", -1).limit(50))
        
        # Count unread
        unread_count = notifications_collection.count_documents({
            "user_email": user_email,
            "read": False
        })
        
        # Convert ObjectId to string for JSON serialization
        # Use 'id' field (not '_id') for frontend compatibility
        for n in notifications:
            n["_id"] = str(n["_id"])
            # Ensure 'id' field exists (from our stored notification_doc)
            if "id" not in n:
                n["id"] = n["_id"]
        
        return {
            "notifications": notifications,
            "unread_count": unread_count,
            "total_count": len(notifications)
        }
    except Exception as e:
        print(f"[Notifications] Error fetching notifications: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch notifications: {str(e)}")

@app.patch("/api/notifications/{notification_id}/read")
def mark_notification_read(
    notification_id: str,
    current_user: UserDB = Depends(get_current_user)
):
    """Mark a notification as read"""
    try:
        if not mongodb_enabled or notifications_collection is None:
            raise HTTPException(status_code=404, detail="Notification not found")
        
        user_email = current_user.email.lower()
        
        result = notifications_collection.update_one(
            {"id": notification_id, "user_email": user_email},
            {"$set": {"read": True}}
        )
        
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Notification not found")
        
        return {"message": "Notification marked as read"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[Notifications] Error marking notification as read: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update notification: {str(e)}")

@app.patch("/api/notifications/read-all")
def mark_all_notifications_read(
    current_user: UserDB = Depends(get_current_user)
):
    """Mark all notifications as read"""
    try:
        if not mongodb_enabled or notifications_collection is None:
            return {"message": "No notifications to update"}
        
        user_email = current_user.email.lower()
        
        result = notifications_collection.update_many(
            {"user_email": user_email, "read": False},
            {"$set": {"read": True}}
        )
        
        return {"message": f"Marked {result.modified_count} notifications as read"}
    except Exception as e:
        print(f"[Notifications] Error marking all notifications as read: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update notifications: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
