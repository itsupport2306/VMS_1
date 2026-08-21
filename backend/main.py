from fastapi import FastAPI, HTTPException, UploadFile, File, Depends, status, Form, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, root_validator
from typing import Any, Dict, List, Optional
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
from email.message import EmailMessage
from urllib.parse import urlencode
from pymongo import MongoClient
from pymongo.server_api import ServerApi
from bson.objectid import ObjectId

# Load environment variables
load_dotenv()

# MongoDB setup for persistent storage
MONGODB_URI = os.getenv("MONGODB_URI", "")
mongo_client = None
db = None
users_collection = None
whitelist_collection = None
candidates_collection = None
notifications_collection = None
jobs_collection = None
job_status_tracker_collection = None
closure_audit_collection = None
manual_jobs_collection = None
submission_logs_collection = None
fs = None


def ensure_mongodb_indexes() -> None:
    """Create the indexes required for fast auth lookups and manual job reads."""
    index_specs = [
        ("users.email", users_collection, "email", {"name": "users_email_idx", "unique": True}),
        ("manual_jobs.created_at", manual_jobs_collection, [("created_at", -1)], {"name": "manual_jobs_created_at_desc_idx"}),
        ("manual_jobs.id", manual_jobs_collection, "id", {"name": "manual_jobs_id_idx", "unique": True}),
        ("manual_jobs.job_id", manual_jobs_collection, "job_id", {"name": "manual_jobs_job_id_idx"}),
    ]
    for label, collection, keys, kwargs in index_specs:
        if collection is None:
            continue
        try:
            collection.create_index(keys, **kwargs)
            print(f"[MongoDB] Ensured index {label}")
        except Exception as e:
            print(f"[MongoDB] Failed to ensure index {label}: {e}")

def init_mongodb():
    """Initialize MongoDB connection"""
    global mongo_client, db, users_collection, whitelist_collection, candidates_collection, notifications_collection, jobs_collection, job_status_tracker_collection, closure_audit_collection, manual_jobs_collection, submission_logs_collection, fs
    
    print(f"[MongoDB] Checking configuration...")
    print(f"[MongoDB] MONGODB_URI present: {bool(MONGODB_URI)}")
    
    if not MONGODB_URI:
        print("[MongoDB] No MONGODB_URI environment variable set!")
        print("[MongoDB] Set MONGODB_URI in your deployment environment")
        print("[MongoDB] Using fallback JSON storage")
        return False
    
    try:
        print(f"[MongoDB] Attempting to connect...")
        mongo_client = MongoClient(MONGODB_URI, server_api=ServerApi('1'))
        db = mongo_client.vms
        users_collection = db.users
        whitelist_collection = db.whitelist
        candidates_collection = db.candidates
        notifications_collection = db.notifications
        jobs_collection = db.jobs  # Track job status changes
        job_status_tracker_collection = db.job_status_tracker  # Fresh tracker for closure detection (separate from legacy `jobs`)
        closure_audit_collection = db.closure_audit  # Audit log for detected closures (dry-run + live)
        manual_jobs_collection = db.manual_jobs  # Direct API-ingested jobs
        submission_logs_collection = db.submission_logs  # Immutable audit log for candidate submissions
        
        # Initialize GridFS for file storage
        import gridfs
        fs = gridfs.GridFS(db)
        
        # Test connection
        mongo_client.admin.command('ping')
        ensure_mongodb_indexes()
        print("[MongoDB] Connected successfully (with GridFS)")
        return True
    except Exception as e:
        print(f"[MongoDB] Connection failed: {e}")
        return False

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
JOB_POSTING_PER_RUN_CAP = int(os.getenv("JOB_POSTING_PER_RUN_CAP", "25"))

# Recipients for new-submission notification emails. Comma-separated env var.
SUBMISSION_NOTIFICATION_RECIPIENTS = [
    addr.strip() for addr in os.getenv(
        "SUBMISSION_NOTIFICATION_RECIPIENTS",
        "it.support@radixsol.com",
    ).split(",") if addr.strip()
]

BILL_RATE_DISPLAY_MULTIPLIER = 0.93
BILL_RATE_NUMBER_PATTERN = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
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


def sanitize_job_for_display(job, is_admin: bool = False) -> dict:
    job_dict = job.dict() if hasattr(job, "dict") else dict(job)
    if job_dict.get("salary_range") and not job_dict.get("bill_rate_discount_applied"):
        job_dict["salary_range"] = display_bill_rate(job_dict.get("salary_range")) or None
    job_dict["bill_rate_discount_applied"] = True
    if "description" in job_dict:
        description = hide_compensation_details(job_dict.get("description") or "")
        job_dict["description"] = sanitize_job_description(description, is_admin)
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


def _hash_otp(otp: str) -> str:
    return hashlib.sha256(otp.encode("utf-8")).hexdigest()


def _smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_PORT and SMTP_USERNAME and SMTP_PASSWORD and SMTP_FROM_EMAIL)


def send_sendgrid_email(to_emails, subject: str, html_content: str, text_content: Optional[str] = None) -> bool:
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
            from_email=SENDGRID_FROM_EMAIL,
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


def send_smtp_email(to_emails, subject: str, html_content: str, text_content: Optional[str] = None) -> bool:
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
        message["From"] = SMTP_FROM_EMAIL
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


def send_app_email(to_emails, subject: str, html_content: str, text_content: Optional[str] = None) -> bool:
    """Production email path is SendGrid; SMTP remains a local fallback."""
    if send_sendgrid_email(to_emails, subject, html_content, text_content):
        return True
    return send_smtp_email(to_emails, subject, html_content, text_content)


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
    now = datetime.now()
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
    """Remove expired password reset tokens"""
    now = datetime.now()
    expired = [token for token, data in _password_reset_tokens.items() if data['expires'] < now]
    for token in expired:
        del _password_reset_tokens[token]
    if expired:
        print(f"[Auth] Cleaned up {len(expired)} expired reset tokens")

def send_submission_notification_email(candidate_data: dict, vendor_info: dict) -> bool:
    """Send candidate submission notification to admin via configured email provider."""
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
        
        html_content = f'''
            <h2>New Candidate Submission</h2>
            <p>A new candidate has been submitted by <strong>{html.escape(vendor_name)}</strong> ({html.escape(vendor_email)}).</p>
            
            <h3>Candidate Details:</h3>
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
        subject = f'New Candidate Submission: {candidate_name} for {job_title}'

        return send_app_email(
            recipients,
            subject,
            html_content,
            text_content=(
                f"New candidate submission\n"
                f"Candidate: {candidate_name}\n"
                f"Email: {candidate_email}\n"
                f"Phone: {candidate_phone}\n"
                f"Job: {job_title} ({job_id})\n"
                f"Submitted by: {vendor_name} ({vendor_email})\n"
                f"Submitted at: {submitted_at}"
            ),
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


def notify_users_about_job_posted(job, source: str = "direct_api") -> int:
    """Notify whitelisted users about a newly posted direct/manual job."""
    if not mongodb_enabled or notifications_collection is None:
        print("[JobPosted] Skipping direct posted-job notifications - MongoDB notifications unavailable.")
        return 0
    if not JOB_POSTING_NOTIFICATIONS_ENABLED:
        print(f"[JobPosted] Posting notifications disabled - skipping {job.id}")
        return 0
    if not WHITELISTED_USERS:
        load_whitelisted_users()
    recipients = [u.lower() for u in WHITELISTED_USERS if u.lower() != ADMIN_EMAIL.lower()]
    sent_count = 0
    for user_email in recipients:
        existing = notifications_collection.find_one({
            "type": "job_posted",
            "job_id": job.id,
            "user_email": user_email,
        })
        if existing:
            continue
        meta_tags = build_job_posted_meta_tags(
            specialty=job.specialty or job.department,
            state=job.state or "",
            employment_type=job.employment_type,
            status=job.status,
            end_client=job.end_client or "",
        )
        email_sent = send_job_posted_notification_email(user_email, job.title, job.id, job.location, meta_tags)
        notifications_collection.insert_one({
            "id": str(uuid4()),
            "type": "job_posted",
            "job_id": job.id,
            "job_title": job.title,
            "user_email": user_email,
            "email_sent": email_sent,
            "source": source,
            "meta_tags": meta_tags,
            "created_at": datetime.now().isoformat(),
            "read": False,
        })
        if email_sent:
            sent_count += 1
    print(f"[JobPosted] Notified {sent_count}/{len(recipients)} users about posted job {job.id}")
    return sent_count


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

        if not WHITELISTED_USERS:
            load_whitelisted_users()
        recipients = [u.lower() for u in WHITELISTED_USERS if u.lower() != ADMIN_EMAIL.lower()]

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

        for t in transitions:
            job_id = t["job_id"]
            title = t["title"]
            audit_doc = {
                "id": str(uuid4()),
                "type": "closure_detected",
                "job_id": job_id,
                "job_title": title,
                "previous_status": t["previous_status"],
                "current_status": t["current_status"],
                "recipients_count": len(recipients),
                "detected_at": datetime.now().isoformat(),
                "dry_run": not JOB_CLOSURE_NOTIFICATIONS_ENABLED,
            }

            if not JOB_CLOSURE_NOTIFICATIONS_ENABLED:
                print(f"[ClosureDetector] DRY RUN — would notify {len(recipients)} users about closure of {job_id} ('{title}')")
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
            print(f"[ClosureDetector] Notified {sent_count}/{len(recipients)} users about closure of {job_id}")

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

def save_whitelisted_users():
    """Save whitelisted users to MongoDB or fallback to Users file"""
    # Try MongoDB first
    if mongodb_enabled and whitelist_collection is not None:
        try:
            # Clear and re-insert all emails
            whitelist_collection.delete_many({})
            for email in WHITELISTED_USERS:
                whitelist_collection.insert_one({"email": email.lower()})
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


def _cache_is_fresh(cache_time: Optional[datetime], ttl_seconds: int) -> bool:
    return bool(cache_time and (datetime.now() - cache_time) < timedelta(seconds=ttl_seconds))


def clear_users_cache() -> None:
    global _users_cache, _users_cache_time
    with _users_cache_lock:
        _users_cache = {}
        _users_cache_time = None
    print("[Auth] User cache cleared")


def clear_manual_jobs_cache() -> None:
    global _manual_jobs_cache, _manual_jobs_cache_time
    with _manual_jobs_cache_lock:
        _manual_jobs_cache = None
        _manual_jobs_cache_time = None
    print("[Manual Jobs] Cache cleared")


def _normalize_user_doc(doc: dict) -> dict:
    return {
        "id": doc["id"],
        "email": doc["email"],
        "full_name": doc["full_name"],
        "hashed_password": doc["hashed_password"],
        "is_active": doc["is_active"],
        "created_at": doc.get("created_at", datetime.now().isoformat()),
    }


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
        age = int((datetime.now() - _users_cache_time).total_seconds())
        print(f"[Auth] Using cached users ({len(_users_cache)} users, cached {age}s ago)")
        return _users_cache

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
            return users
        except Exception as e:
            print(f"[Auth] Error loading users from MongoDB: {e}, falling back to JSON")
    
    # Fallback to JSON file
    if os.path.exists(USERS_JSON_FILE):
        try:
            with open(USERS_JSON_FILE, "r") as f:
                users = json.load(f)
            with _users_cache_lock:
                _users_cache = users
                _users_cache_time = datetime.now()
            return users
        except Exception as e:
            print(f"[Auth] Error loading users JSON: {e}")
    return {}


def load_user_by_email(email: str, prefer_cache: bool = True) -> Optional[dict]:
    """Load one user without forcing a full collection scan."""
    global _users_cache, _users_cache_time

    email_lower = (email or "").strip().lower()
    if not email_lower:
        return None

    if prefer_cache and _users_cache and _cache_is_fresh(_users_cache_time, USERS_CACHE_SECONDS):
        return _users_cache.get(email_lower)

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
                users = load_users_from_json(force_refresh=True)
                return users.get(email_lower)
            user = _normalize_user_doc(doc)
            with _users_cache_lock:
                if not _cache_is_fresh(_users_cache_time, USERS_CACHE_SECONDS):
                    _users_cache = {}
                    _users_cache_time = datetime.now()
                _users_cache[email_lower] = user
            return user
        except Exception as e:
            print(f"[Auth] Error loading user {email_lower} from MongoDB: {e}, falling back to cache/JSON")

    users = load_users_from_json(force_refresh=not prefer_cache)
    return users.get(email_lower)

def save_users_to_json(users):
    """Save users to MongoDB or fallback to JSON file"""
    # Try MongoDB first
    if mongodb_enabled and users_collection is not None:
        try:
            # Update each user individually (upsert)
            for email, user_data in users.items():
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
            print(f"[Auth] Saved {len(users)} users to MongoDB")
            clear_users_cache()
            return True
        except Exception as e:
            print(f"[Auth] Error saving users to MongoDB: {e}, falling back to JSON")
    
    # Fallback to JSON file
    try:
        os.makedirs(os.path.dirname(USERS_JSON_FILE), exist_ok=True)
        with open(USERS_JSON_FILE, "w") as f:
            json.dump(users, f, indent=2)
        clear_users_cache()
        return True
    except Exception as e:
        print(f"[Auth] Error saving users JSON: {e}")
        return False

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
            save_users_to_json(_users)
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
    save_users_to_json(_users)
    
    # Also add to whitelist
    global WHITELISTED_USERS
    WHITELISTED_USERS.add(admin_email.lower())
    save_whitelisted_users()
    
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

@app.on_event("startup")
async def startup_event():
    """Start background job fetch on startup"""
    if CEIPAL_ENABLED:
        print("[Startup] Starting scheduled background job fetch...")
        asyncio.create_task(scheduled_job_fetch())
    else:
        print("[Startup] Ceipal integration disabled; background fetch skipped")

# Web UI (HTML/CSS/JS)
WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
ASSETS_DIR = os.path.join(WEB_DIR, "assets")
if os.path.isdir(ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")

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
    if user.get("is_active") != "true":
        print(f"[Auth] User {email_lower} is inactive: {user.get('is_active')}")
        raise HTTPException(status_code=400, detail="Inactive user")
    print(f"[Auth] User {email_lower} authenticated successfully")
    
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
    if user.get("is_active") != "true":
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
    if existing and existing.get("is_active") == "true":
        raise HTTPException(status_code=400, detail="Account already exists. Please log in.")

    otp = f"{secrets.randbelow(1000000):06d}"
    full_name = (user_data.full_name or email.split("@")[0]).strip() or email_lower
    _email_otp_tokens[email_lower] = {
        "otp_hash": _hash_otp(otp),
        "expires": datetime.now() + timedelta(minutes=OTP_EXPIRE_MINUTES),
        "attempts": 0,
        "purpose": "registration",
        "pending_user": {
            "id": str(uuid4()),
            "email": email_lower,
            "full_name": full_name,
            "hashed_password": get_password_hash(password),
            "is_active": "true",
            "created_at": datetime.now().isoformat(),
        },
        "user_agent": request.headers.get("user-agent", ""),
        "ip_address": get_client_ip(request),
    }

    email_sent = await asyncio.to_thread(send_registration_otp_email, email_lower, otp)
    if not email_sent:
        _email_otp_tokens.pop(email_lower, None)
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
    token_data = _email_otp_tokens.get(email_lower)

    if not token_data or token_data.get("purpose") != "registration":
        raise HTTPException(status_code=400, detail="Invalid or expired verification code")
    if token_data["expires"] < datetime.now():
        del _email_otp_tokens[email_lower]
        raise HTTPException(status_code=400, detail="Verification code has expired")
    if token_data["attempts"] >= OTP_MAX_ATTEMPTS:
        del _email_otp_tokens[email_lower]
        raise HTTPException(status_code=429, detail="Too many failed attempts. Please sign up again.")

    token_data["attempts"] += 1
    if _hash_otp(otp) != token_data["otp_hash"]:
        raise HTTPException(status_code=400, detail="Invalid verification code")

    pending_user = token_data.get("pending_user")
    if not pending_user:
        del _email_otp_tokens[email_lower]
        raise HTTPException(status_code=400, detail="Registration session has expired")

    users = load_users_from_json()
    existing = users.get(email_lower)
    if existing and existing.get("is_active") == "true":
        del _email_otp_tokens[email_lower]
        raise HTTPException(status_code=400, detail="Account already exists. Please log in.")

    users[email_lower] = pending_user
    if not save_users_to_json(users):
        raise HTTPException(status_code=500, detail="Could not complete registration")

    global WHITELISTED_USERS, _users_cache
    WHITELISTED_USERS.add(email_lower)
    save_whitelisted_users()
    _users_cache = load_users_from_json(force_refresh=True)
    del _email_otp_tokens[email_lower]

    return {"message": "Account verified. You can now log in."}

def get_or_create_otp_user(email: str) -> dict:
    """Return an active whitelisted user, creating a profile on first OTP login."""
    global _users_cache

    email = (email or "").strip()
    email_lower = email.lower()
    _users_cache = load_users_from_json(force_refresh=True)
    user = _users_cache.get(email_lower)

    if not user:
        if email_lower not in WHITELISTED_USERS:
            raise HTTPException(status_code=401, detail="Email is not authorized for portal access")

        print(f"[Auth] Auto-creating OTP user: {email}")
        user_id = str(uuid4())
        user = {
            "id": user_id,
            "email": email,
            "full_name": email.split('@')[0],
            "hashed_password": get_password_hash(secrets.token_urlsafe(32)),
            "is_active": "true",
            "created_at": datetime.now().isoformat()
        }
        _users_cache[email_lower] = user
        save_users_to_json(_users_cache)

    if user.get("is_active") != "true":
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
    if email_lower not in WHITELISTED_USERS:
        raise HTTPException(status_code=401, detail="Email is not authorized for portal access")

    otp = f"{secrets.randbelow(1000000):06d}"
    _email_otp_tokens[email_lower] = {
        "otp_hash": _hash_otp(otp),
        "expires": datetime.now() + timedelta(minutes=OTP_EXPIRE_MINUTES),
        "attempts": 0,
        "user_agent": request.headers.get("user-agent", ""),
        "ip_address": get_client_ip(request),
    }

    email_sent = await asyncio.to_thread(send_login_otp_email, email_lower, otp)
    if not email_sent:
        _email_otp_tokens.pop(email_lower, None)
        raise HTTPException(
            status_code=503,
            detail="We could not send the verification code. Please contact support.",
        )

    return {"message": "Email has been sent. Please check your inbox.", "expires_minutes": OTP_EXPIRE_MINUTES}


@app.post("/api/auth/verify-otp", response_model=Token)
async def verify_login_otp(request_data: OtpVerify):
    """Verify email OTP and return a 7-day JWT session."""
    cleanup_expired_otps()

    email_lower = request_data.email.lower().strip()
    otp = (request_data.otp or "").strip()
    token_data = _email_otp_tokens.get(email_lower)

    if not token_data:
        raise HTTPException(status_code=400, detail="Invalid or expired verification code")
    if token_data["expires"] < datetime.now():
        del _email_otp_tokens[email_lower]
        raise HTTPException(status_code=400, detail="Verification code has expired")
    if token_data["attempts"] >= OTP_MAX_ATTEMPTS:
        del _email_otp_tokens[email_lower]
        raise HTTPException(status_code=429, detail="Too many failed attempts. Request a new code.")

    token_data["attempts"] += 1
    if _hash_otp(otp) != token_data["otp_hash"]:
        raise HTTPException(status_code=400, detail="Invalid verification code")

    user = get_or_create_otp_user(email_lower)
    del _email_otp_tokens[email_lower]

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
    email_lower = user_data.email.lower().strip()
    user = await asyncio.to_thread(load_user_by_email, email_lower)

    if not user:
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    if user.get("is_active") != "true":
        raise HTTPException(status_code=400, detail="Inactive user")
    if not verify_password(user_data.password, user["hashed_password"]):
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
    
    # Check if email is whitelisted
    if email_lower not in WHITELISTED_USERS:
        # Don't reveal if email exists for security
        return {"message": "If the email is registered, a password reset link has been sent."}
    
    users = load_users_from_json()
    user = users.get(email_lower)

    # Check if user exists (has logged in before)
    if not user or user.get("is_active") != "true":
        return {"message": "If the email is registered, a password reset link has been sent."}
    
    # Generate reset token
    reset_token = str(uuid4())
    
    # Store token with expiration (1 hour)
    _password_reset_tokens[reset_token] = {
        "email": email_lower,
        "expires": datetime.now() + timedelta(hours=1),
        "used": False
    }
    
    # Send email
    email_sent = await asyncio.to_thread(send_password_reset_email, email_lower, reset_token)
    
    if email_sent:
        print(f"[Auth] Password reset email sent to {email_lower}")
        return {"message": "Password reset email sent. Please check your inbox."}
    else:
        _password_reset_tokens.pop(reset_token, None)
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
    
    token = data.token
    
    # Validate token
    if token not in _password_reset_tokens:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    
    token_data = _password_reset_tokens[token]
    
    if token_data["used"]:
        raise HTTPException(status_code=400, detail="Reset token has already been used")
    
    if token_data["expires"] < datetime.now():
        raise HTTPException(status_code=400, detail="Reset token has expired")
    
    email_lower = token_data["email"]
    
    # Validate password
    if len(data.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    
    users = load_users_from_json()
    if email_lower not in users:
        _password_reset_tokens.pop(token, None)
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    # Update password
    hashed_password = get_password_hash(data.password)

    users[email_lower]["hashed_password"] = hashed_password
    if not save_users_to_json(users):
        raise HTTPException(status_code=500, detail="Could not update password")
    _users_cache = load_users_from_json()
    
    # Mark token as used
    token_data["used"] = True
    
    print(f"[Auth] Password reset successful for {email_lower}")
    return {"message": "Password reset successfully. Please login with your new password."}

@app.get("/api/admin/users")
async def get_whitelisted_users(current_user: UserDB = Depends(get_current_user)):
    """Get list of whitelisted users (admin only)"""
    is_admin = current_user.email.lower() == ADMIN_EMAIL.lower()
    if not is_admin:
        raise HTTPException(status_code=403, detail="Only admin can manage users")
    
    return {
        "users": sorted(list(WHITELISTED_USERS)),
        "count": len(WHITELISTED_USERS)
    }

@app.post("/api/admin/users")
async def add_whitelisted_user(
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
    if email_lower in WHITELISTED_USERS:
        raise HTTPException(status_code=400, detail="User already whitelisted")
    
    # Add to whitelist
    WHITELISTED_USERS.add(email_lower)
    
    # Save to file
    if save_whitelisted_users():
        return {"message": f"User {email} added to whitelist", "email": email_lower}
    else:
        raise HTTPException(status_code=500, detail="Failed to save users file")

@app.delete("/api/admin/users/{email}")
async def remove_whitelisted_user(
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
    if email_lower not in WHITELISTED_USERS:
        raise HTTPException(status_code=404, detail="User not found in whitelist")
    
    # Remove from whitelist
    WHITELISTED_USERS.remove(email_lower)
    
    # Save to file
    if save_whitelisted_users():
        return {"message": f"User {email} removed from whitelist", "email": email_lower}
    else:
        raise HTTPException(status_code=500, detail="Failed to save users file")

def clear_excel_jobs_cache():
    """Clear the Excel jobs cache to force reload on next request"""
    global _excel_jobs_cache, _excel_jobs_cache_time
    _excel_jobs_cache = None
    _excel_jobs_cache_time = None
    print("[Excel] Cache cleared")

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
async def create_job_from_payload(
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
        upsert_manual_job(job)
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
async def create_jobs_from_payload_bulk(
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
        upsert_manual_jobs(stored_jobs)
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
async def delete_job_from_payload(
    job_id: str,
    credentials: AdminJobDeleteRequest
):
    """Delete a direct API-ingested job by job ID using admin credentials."""
    verify_admin_credentials(credentials.email, credentials.password)

    try:
        deleted = delete_manual_job(job_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Direct API job {job_id} not found")

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


@app.post("/api/nexus/jobs")
async def create_nexus_job(payload: DirectJobCreateRequest, background_tasks: BackgroundTasks):
    """Create or update a job from Nexus-style request parameters."""
    try:
        job = build_job_from_direct_input(payload)
        upsert_manual_job(job)
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


@app.post("/api/nexus/jobs/bulk")
async def create_nexus_jobs_bulk(
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
        upsert_manual_jobs(stored_jobs)
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
                    status=status if status and status.lower() != 'nan' else "Active",
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
    global _excel_jobs_cache, _excel_jobs_cache_time
    
    # Return cached jobs if less than cache duration old
    if _excel_jobs_cache and _excel_jobs_cache_time:
        age = datetime.now() - _excel_jobs_cache_time
        if age < timedelta(minutes=EXCEL_CACHE_MINUTES):
            print(f"[Excel] Using cached jobs ({len(_excel_jobs_cache)} jobs, cached {age.seconds}s ago)")
            return _excel_jobs_cache
    
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
        submission_logs_collection.insert_one(audit_doc)
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


def build_job_from_direct_input(payload: DirectJobCreateRequest) -> Job:
    """Map direct endpoint input into the shared Job response model."""
    job_id = (payload.job_id or payload.job_code or "").strip()
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
    global _manual_jobs_cache, _manual_jobs_cache_time

    if not force_refresh and _manual_jobs_cache is not None and _cache_is_fresh(_manual_jobs_cache_time, MANUAL_JOBS_CACHE_SECONDS):
        age = int((datetime.now() - _manual_jobs_cache_time).total_seconds())
        print(f"[Manual Jobs] Using cached jobs ({len(_manual_jobs_cache)} jobs, cached {age}s ago)")
        return _manual_jobs_cache

    jobs: List[Job] = []

    try:
        if mongodb_enabled and manual_jobs_collection is not None:
            for doc in manual_jobs_collection.find().sort("created_at", -1):
                doc.pop("_id", None)
                doc.pop("created_at", None)
                if not doc.get("bill_rate_discount_applied"):
                    doc["salary_range"] = display_bill_rate(doc.get("salary_range")) or None
                doc["bill_rate_discount_applied"] = True
                jobs.append(Job(**doc))
            with _manual_jobs_cache_lock:
                _manual_jobs_cache = jobs
                _manual_jobs_cache_time = datetime.now()
            return jobs

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
            _manual_jobs_cache = jobs
            _manual_jobs_cache_time = datetime.now()
    except Exception as e:
        print(f"[Manual Jobs] Failed to load direct-input jobs: {e}")

    return jobs


def upsert_manual_job(job: Job) -> None:
    """Persist one direct-input job by job ID."""
    upsert_manual_jobs([job])


def upsert_manual_jobs(jobs: List[Job]) -> None:
    """Persist many direct-input jobs with a single backend write pass when possible."""
    if not jobs:
        return

    job_payloads = []
    for job in jobs:
        job_payload = job.dict()
        job_payload["bill_rate_discount_applied"] = True
        job_payloads.append(job_payload)

    if mongodb_enabled and manual_jobs_collection is not None:
        created_at = datetime.now()
        for job_payload in job_payloads:
            manual_jobs_collection.update_one(
                {"id": job_payload["id"]},
                {"$set": {**job_payload, "created_at": created_at}},
                upsert=True,
            )
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
            existing_jobs[existing_index[job_id]] = job_payload
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
        return
    for job in jobs:
        background_tasks.add_task(notify_users_about_job_posted, job, source)


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
                status=job_data.get("JobStatus", "Open"),
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
async def get_jobs(background_tasks: BackgroundTasks, current_user: UserDB = Depends(get_current_user)):
    """Get all active jobs from direct input and Excel files."""
    try:
        manual_jobs, excel_jobs = await asyncio.gather(
            asyncio.to_thread(load_manual_jobs),
            asyncio.to_thread(load_excel_jobs),
        )
        print(f"[API] Loaded {len(manual_jobs)} jobs from direct API input")
        print(f"[API] Loaded {len(excel_jobs)} jobs from Excel")
        all_jobs = combine_jobs_with_priority(manual_jobs, excel_jobs, [])
        is_admin = current_user.email.lower() == ADMIN_EMAIL.lower()
        sanitized_jobs = [sanitize_job_for_display(job, is_admin) for job in all_jobs]

        return JobListResponse(
            jobs=sanitized_jobs,
            total=len(sanitized_jobs),
            total_pages=1,
            next_start_page=2,
            has_more=False,
            is_refreshing=False,
            cache_age_seconds=None,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch jobs: {str(e)}")


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
async def get_job(job_id: str):
    """Get specific job details"""
    try:
        manual_jobs, excel_jobs = await asyncio.gather(
            asyncio.to_thread(load_manual_jobs),
            asyncio.to_thread(load_excel_jobs),
        )
        all_jobs = combine_jobs_with_priority(
            manual_jobs,
            excel_jobs,
            [],
        )

        job = next((job for job in all_jobs if job.id == job_id), None)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return sanitize_job_for_display(job)
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

@app.get("/api/health")
async def health_check():
    """Health check endpoint for MongoDB/JSON-backed runtime state."""
    users = load_users_from_json()
    return {
        "storage_mode": "mongodb_json",
        "mongodb_enabled": mongodb_enabled,
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
        
        # Read resume content
        content = await resume.read()
        
        # Store resume in GridFS (persistent across redeploys)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{candidate_name.replace(' ', '_')}_{timestamp}.{file_extension}"
        
        if mongodb_enabled and fs is not None:
            # Store in GridFS
            resume_file_id = fs.put(
                content,
                filename=filename,
                content_type=resume.content_type or 'application/octet-stream',
                metadata={
                    'candidate_name': candidate_name,
                    'job_id': job_id,
                    'uploaded_by': current_user.email,
                    'uploaded_at': datetime.now().isoformat()
                }
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
            "email": email,
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
        
        if mongodb_enabled and candidates_collection is not None:
            candidates_collection.insert_one(candidate_doc)
            print(f"[Submissions] Candidate {candidate_id} stored in MongoDB")
        else:
            # Fallback: store in local JSON file
            candidates_file = os.path.join(DATA_DIR, "candidates.json")
            existing = []
            if os.path.exists(candidates_file):
                with open(candidates_file, 'r') as f:
                    existing = json.load(f)
            existing.append(candidate_doc)
            with open(candidates_file, 'w') as f:
                json.dump(existing, f, indent=2, default=str)
            print(f"[Submissions] Candidate {candidate_id} stored in JSON (MongoDB not available)")

        audit_doc = {
            "id": f"submission_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(uuid4())[:8]}",
            "candidate_id": candidate_id,
            "candidate_name": candidate_name,
            "candidate_email": email,
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
            record_submission_log(audit_doc)
        except Exception as e:
            print(f"[SubmissionAudit] Failed to write audit log for {candidate_id}: {e}")
        
        # Send email notification to admin
        vendor_info = {
            "full_name": current_user.full_name,
            "email": current_user.email,
            "id": current_user.id
        }
        print(f"[Submissions] Attempting to send notification email to {SUBMISSION_NOTIFICATION_RECIPIENTS}")
        # Run the synchronous SendGrid call off the event loop so it doesn't stall other requests
        # if SendGrid is slow. Failure to email is non-fatal — the candidate is already stored.
        try:
            email_sent = await asyncio.to_thread(send_submission_notification_email, candidate_doc, vendor_info)
        except Exception as e:
            print(f"[Submissions] Exception during notification email dispatch: {e}")
            email_sent = False
        if email_sent:
            print(f"[Submissions] Notification email sent for candidate {candidate_id}")
        else:
            print(f"[Submissions] Failed to send notification email for candidate {candidate_id}")
        
        return {
            "message": "Candidate submitted successfully",
            "candidate_id": candidate_id,
            "status": "submitted",
            "submitted_by": current_user.full_name
        }
        
    except Exception as e:
        print(f"[Submissions] Error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to submit candidate: {str(e)}")

@app.get("/api/candidates/job/{job_id}")
async def get_candidates_for_job(job_id: str):
    """Get all candidates submitted for a specific job - MongoDB persistent storage"""
    try:
        job_candidates = []
        
        if mongodb_enabled and candidates_collection is not None:
            # Query MongoDB
            cursor = candidates_collection.find({"job_id": job_id})
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
                    job_candidates = [c for c in all_candidates if c.get("job_id") == job_id]
                    for c in job_candidates:
                        sanitize_candidate_bill_rate(c)
        
        return {"candidates": job_candidates, "total": len(job_candidates)}
    except Exception as e:
        print(f"[Submissions] Error fetching job candidates: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch candidates: {str(e)}")

@app.get("/api/candidates")
async def get_all_candidates(
    current_user: UserDB = Depends(get_current_user)
):
    """Get submitted candidates with submitter info. Vendors see only their own submissions, admin sees all. - MongoDB persistent storage"""
    try:
        candidates = []
        is_admin = current_user.email.lower() == ADMIN_EMAIL.lower()
        
        if mongodb_enabled and candidates_collection is not None:
            # Query MongoDB
            if is_admin:
                cursor = candidates_collection.find()
            else:
                cursor = candidates_collection.find({"submitted_by_user_id": current_user.id})
            
            for doc in cursor:
                doc["_id"] = str(doc["_id"])
                sanitize_candidate_bill_rate(doc)
                # Build submitted_by info from stored data
                doc["submitted_by"] = {
                    "id": doc.get("submitted_by_user_id"),
                    "full_name": doc.get("submitted_by_name"),
                    "email": doc.get("submitted_by_email")
                } if doc.get("submitted_by_user_id") else None
                candidates.append(doc)
        else:
            # Fallback to JSON file
            candidates_file = os.path.join(DATA_DIR, "candidates.json")
            if os.path.exists(candidates_file):
                with open(candidates_file, 'r') as f:
                    all_candidates = json.load(f)
                    if is_admin:
                        candidates = all_candidates
                    else:
                        candidates = [c for c in all_candidates if c.get("submitted_by_user_id") == current_user.id]
                    
                    for c in candidates:
                        sanitize_candidate_bill_rate(c)
                        c["submitted_by"] = {
                            "id": c.get("submitted_by_user_id"),
                            "full_name": c.get("submitted_by_name"),
                            "email": c.get("submitted_by_email")
                        }
        
        return {"candidates": candidates, "total": len(candidates)}
    except Exception as e:
        print(f"[Submissions] Error fetching candidates: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch candidates: {str(e)}")


@app.get("/api/submission-logs")
async def get_submission_logs(current_user: UserDB = Depends(get_current_user)):
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
async def update_candidate_status(
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
async def download_resume(candidate_id: str):
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
            
            # Create a temporary file for the response
            with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(candidate.get("resume_filename", "resume.pdf"))[1]) as tmp:
                tmp.write(grid_file.read())
                tmp_path = tmp.name
            
            return FileResponse(
                tmp_path,
                filename=candidate.get("resume_filename", "resume.pdf"),
                media_type=grid_file.content_type or "application/octet-stream",
                background=None  # File will be cleaned up after response
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

@app.get("/api/notifications")
async def get_user_notifications(
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
async def mark_notification_read(
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
async def mark_all_notifications_read(
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
