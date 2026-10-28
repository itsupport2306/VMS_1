# Vendor Management System

A comprehensive Vendor Management System (VMS) that connects to ATS APIs to fetch active jobs and allows vendors to submit candidate resumes.

## Features

### 🎯 Core Functionality
- **ATS Integration**: Connects to external ATS APIs to fetch active/open jobs
- **Job Board**: Display all available jobs with detailed information
- **Resume Submission**: Multiple resume submission for candidates
- **Candidate Management**: Track submitted candidates for each job

### 🛠 Technical Stack
- **Backend**: FastAPI with SQLAlchemy ORM
- **Frontend**: HTML/CSS/JS web app served by the backend at `/` (`backend/web/`)
- **Database**: MySQL/PostgreSQL support
- **File Storage**: Local file system for resume uploads
- **API Integration**: HTTP client for ATS connectivity

## Project Structure

```
VMS/
├── backend/
│   ├── main.py              # FastAPI backend with Ceipal integration
│   └── web/                 # Browser UI served at /
├── database/
│   └── models.py            # SQLAlchemy database models
├── uploads/                 # Resume file storage
├── vms_database.db          # SQLite database file (created automatically)
├── requirements.txt         # Python dependencies
└── .env.example            # Environment configuration template
```

## Setup Instructions

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure Environment
Copy `.env.example` to `.env` and set the values for your deployment.

#### Email OTP Sign-In
Users sign in with an email verification code instead of a password. The email must be present in the admin whitelist before a code can be requested. After successful OTP verification, the portal issues a bearer token that stays valid for 7 days by default.

Configure these values for SendGrid:

```env
SESSION_EXPIRE_DAYS=7
OTP_EXPIRE_MINUTES=10
OTP_MAX_ATTEMPTS=5

SENDGRID_API_KEY=your_sendgrid_api_key_here
SENDGRID_FROM_EMAIL=notifications@radixsol.com
APP_URL=https://your-production-vms-url.example.com
```

In `DEBUG` or `TESTING_MODE`, the backend logs OTP codes when email delivery is unavailable so the sign-in flow can be tested locally. Production should use SendGrid and should keep debug logging disabled.

#### Admin Submission Notifications
Candidate submission notifications use SendGrid and can be forced to send from `notifications@radixsol.com` with `SENDGRID_FROM_EMAIL`. Set the admin recipients as a comma-separated list:

```env
SENDGRID_FROM_EMAIL=notifications@radixsol.com
SUBMISSION_NOTIFICATION_RECIPIENTS=it.support@radixsol.com
```

Each successful candidate submission is also written to a separate submission audit log with submitter name, submitter email, candidate details, timestamp, IP address when available, user agent, resume metadata, and job metadata. MongoDB deployments store this in the `submission_logs` collection. Local/fallback deployments store it in `DATA_DIR/submission_logs.json`.

The authenticated submission history view is available from `GET /api/submission-history`. It groups profiles by user and submission date, includes user details and per-user totals, and powers the browser submission history screen.

#### Job Alert Emails
Users choose the jobs they want to hear about under **Job Alerts** in the sidebar: profession, specialty, job type, and state. A job must match every group where the user made a selection; an empty group means "any", so users who never set preferences keep receiving every new job. Users can also turn job emails off and keep the in-app bell notifications.

Posted Open jobs (from `/api/nexus/jobs`, `/api/nexus/jobs/bulk` with `send_notifications: true`, and `/api/admin/jobs`) are queued in the `job_notification_outbox` collection. Every `JOB_NOTIFICATION_DIGEST_SECONDS` (default 300), one API worker takes a MongoDB lease and sends each user a single email listing the matching jobs queued since the last run. A single matching job uses the original "New Job Posted" email. Each email links to the preferences page (`?view=preferences`). Preferences are stored in the `user_preferences` collection (`DATA_DIR/user_preferences.json` without MongoDB).

Endpoints: `GET /api/preferences` returns the user's preferences and the selectable values, and `PUT /api/preferences` saves them.

#### Closed Jobs and Nexus Sync
Vendors only see open jobs: `GET /api/jobs` and `GET /api/jobs/{job_id}` leave out jobs whose status is closed (`Closed`, `Inactive`, `Filled`, `On Hold`, `Cancelled`), and submissions to them are rejected with 409. The admin still sees closed jobs, marked as closed, and can filter for them.

- **Admin close/reopen.** Admin closes a job with the **Close job** button on its card (`POST /api/admin/jobs/{job_id}/close`) and undoes it with **Reopen job** (`POST /api/admin/jobs/{job_id}/reopen`). An admin closure sticks: if the Nexus sync posts the job again, it stays closed.
- **Sync closes removed jobs.** `api_nexus_oauth_jobs.py` records every job id the Nexus OPEN job search returns in each polling cycle. After a cycle that read every page, it closes on the VMS any job marked posted in `oauth_jobs.csv` that Nexus no longer returns (`POST /api/admin/jobs/bulk/close`), and sets its `post_status` to `closed`. A job that comes back on Nexus is posted again and reopens. A job the admin closed does not reopen.
- **Safety limit.** If more than half of the posted jobs disappear in one cycle (for example during a Nexus outage), the sync closes nothing and logs a warning. Change the limit with `--max-close-fraction`, or turn closing off with `--no-close-removed`.

A closed job keeps its record and submissions. Closing a job also takes it out of the pending new-job digest. Jobs that are still on the read-only legacy cluster (`MONGODB_URI`) are closed by writing a closed copy to the current cluster, which takes precedence when jobs are read.

### 3. Run the Application

#### Backend Server
```bash
cd backend
python main.py
```
The backend will start on `http://localhost:8000` and serves the web UI at the same address.

## Docker Deployment

This repo now includes Docker files for a DigitalOcean-friendly deployment.

### Run With Docker Compose

```bash
docker compose up --build
```

Available services:
- Backend API and web UI: `http://localhost:8000`

The old Streamlit frontend has been removed. It no longer worked against the authenticated API and only used memory.

### Container Data Paths

The backend container uses these paths:

```env
DATA_DIR=/app/data
UPLOAD_DIR=/app/data/uploads
CEIPAL_CACHE_DIR=/app/data/cache
```

The `docker-compose.yml` file mounts `./data` into `/app/data` so cache files, JSON-backed runtime files, and uploads persist across restarts.

### DigitalOcean Notes

For a Droplet:

```bash
docker compose up -d --build
```

For App Platform:
- Use the root `Dockerfile` for the backend service. It is the only service.
- Put the MongoDB Atlas cluster in the same region as the app (for example Atlas on AWS `us-east-1` with DigitalOcean `NYC`). `GET /api/health` reports `mongodb_ping_ms`, and the backend logs a warning at startup when it is high.
- Configure secrets and environment variables in DigitalOcean instead of storing them in the image.

### 4. Quick Start (All in One)
```bash
# Setup everything
python main.py setup

# Start backend (web UI at http://localhost:8000)
python main.py backend
```

## API Endpoints

### Jobs
- `GET /api/jobs` - Get all active jobs (admin also gets closed jobs)
- `GET /api/jobs/{job_id}` - Get specific job details
- `POST /api/admin/jobs/{job_id}/close` - Admin: close a job
- `POST /api/admin/jobs/{job_id}/reopen` - Admin: reopen a closed job
- `POST /api/admin/jobs/bulk/close` - Close jobs removed from the source site (`{"job_ids": [...]}`, admin token)
- `POST /api/nexus/jobs/bulk/close` - Same, for the Nexus sync without admin login (ingest token)

### Candidates
- `POST /api/candidates/submit` - Submit candidate with resume
- `GET /api/candidates/job/{job_id}` - Get candidates for specific job
- `GET /api/candidates` - Get all candidates
- `GET /api/submission-history` - Authenticated submission history grouped by user and date
- `GET /api/submission-logs` - Admin-only candidate submission audit log

### Authentication
- `POST /api/auth/request-otp` - Request an email verification code
- `POST /api/auth/verify-otp` - Verify code and receive a 7-day bearer token

## Web UI Features

### Job Board
- View all active jobs from ATS
- Filter by department and employment type
- View job details and requirements
- See candidate count for each job

### Resume Submission
- Select job from dropdown
- Fill candidate information
- Upload resume file (PDF, DOC, DOCX, TXT)
- Get confirmation with candidate ID

## ATS Integration

The system integrates with **Ceipal Custom Reports API** to fetch active job listings:

### Ceipal API Configuration

#### Authentication Details
- **API Name**: Custom Reports API
- **Authentication URL**: `https://api.ceipal.com/v1/createAuthtoken/`
- **Username**: `amir@radixsol.com`
- **API Key**: `2693f0ed28f2250811fe40294e97e108a56afa9043e5336da4`
- **Format**: JSON (`json:1`)
- **Authentication Type**: Bearer Token

#### Reports Endpoint
- **Method**: GET
- **URL**: `https://bi.ceipal.com/ReportDetails/getReportsData/d2RyRHN0Z0s3R29aNWdyN1h2TnBLUT09?response_type=1`

#### Environment Variables
```env
# Ceipal API Configuration
CEIPAL_AUTH_URL=https://api.ceipal.com/v1/createAuthtoken/
CEIPAL_REPORTS_URL=https://bi.ceipal.com/ReportDetails/getReportsData/d2RyRHN0Z0s3R29aNWdyN1h2TnBLUT09
CEIPAL_EMAIL=amir@radixsol.com
CEIPAL_PASSWORD=your_actual_password_here
CEIPAL_API_KEY=2693f0ed28f2250811fe40294e97e108a56afa9043e5336da4
```

### Authentication Flow
1. **Token Generation**: System authenticates using email, password, and API key
2. **Bearer Token**: Receives auth token for API requests
3. **Token Management**: Auto-refreshes tokens when expired (24-hour validity)
4. **Data Fetching**: Uses bearer token to access reports endpoint

### Supported Features
- ✅ **Authentication**: Secure token-based authentication
- ✅ **Job Fetching**: Real-time job data from Ceipal reports
- ✅ **Auto-retry**: Automatic fallback to mock data on API failure
- ✅ **Flexible Parsing**: Handles various response formats from Ceipal
- ✅ **Error Handling**: Comprehensive error management and logging

### API Endpoints for Testing
- `GET /api/ceipal/test` - Test Ceipal API connection and authentication
- `GET /api/ceipal/reports` - Get raw reports data from Ceipal
- `GET /api/jobs` - Get processed job data from Ceipal

### Data Mapping
The system automatically maps Ceipal fields to internal Job model:
- `job_id` / `id` → `id`
- `job_title` / `title` → `title`
- `job_description` / `description` → `description`
- `department` / `category` → `department`
- `location` / `city` → `location`
- `employment_type` / `job_type` → `employment_type`
- `salary_range` / `salary` → `salary_range`
- `posted_date` / `created_date` → `posted_date`
- `status` → `status`
- `requirements` / `skills_required` → `requirements`

### Fallback System
When Ceipal API is unavailable:
- System provides mock job data for demonstration
- Maintains full functionality for testing
- Automatic retry when API becomes available

### Legacy ATS Support
The system still supports generic ATS integration:
```env
# Legacy ATS API Configuration (optional)
ATS_API_BASE_URL=https://api.ats-provider.com/v1
ATS_API_KEY=your_ats_api_key_here
```

## File Upload

### Supported Formats
- PDF (.pdf)
- Microsoft Word (.doc, .docx)
- Text files (.txt)

### File Size Limit
- Maximum: 10MB per file
- Configurable via `MAX_FILE_SIZE` environment variable

### Storage
- Files stored in `./uploads/` directory
- Filename format: `{candidate_name}_{timestamp}.{extension}`

## Database Schema

### Jobs Table
- Job information from ATS
- Department, location, employment type
- Salary range and requirements
- Status tracking

### Candidates Table
- Candidate personal information
- Resume file path
- Job association
- Submission status and date

### Vendors Table (Future)
- Vendor company information
- Contact details
- Status management

## Development

### Running in Development Mode
```bash
# Backend (with auto-reload)
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

### Testing
The system includes mock data for testing when ATS API is not configured.

## Security

### File Upload Security
- File type validation
- Size limit enforcement
- Safe filename generation

### API Security
- CORS configuration
- Request validation
- Error handling

## Future Enhancements

### Planned Features
- User authentication and authorization
- Multiple vendor management
- Advanced analytics and reporting
- Email notifications
- Integration with more ATS providers
- Resume parsing and analysis
- Candidate status tracking
- Interview scheduling

### Scalability
- Database connection pooling
- File storage (AWS S3 integration)
- Caching layer
- Load balancing

## Troubleshooting

### Common Issues

1. **Backend Connection Failed**
   - Check if backend is running on port 8000
   - Verify CORS configuration

2. **Data Storage Error**
   - Verify `MONGODB_URI` in `.env`
   - Verify `DATA_DIR`, `UPLOAD_DIR`, and `CEIPAL_CACHE_DIR`

3. **ATS API Not Working**
   - Verify API credentials
   - Check API endpoint URL
   - System will fallback to mock data

4. **File Upload Failed**
   - Check file size (max 10MB)
   - Verify file format
   - Ensure uploads directory exists

### Logs
- Backend logs show API requests and errors
- Database logs available via SQLAlchemy

## Support

For issues and questions:
1. Check the troubleshooting section
2. Review the logs for error details
3. Verify environment configuration
4. Test with mock data first

## License

This project is licensed under the MIT License.
