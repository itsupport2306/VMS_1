# Vendor Management System

A comprehensive Vendor Management System (VMS) that connects to ATS APIs to fetch active jobs and allows vendors to submit candidate resumes.

## Features

### 🎯 Core Functionality
- **ATS Integration**: Connects to external ATS APIs to fetch active/open jobs
- **Job Board**: Display all available jobs with detailed information
- **Resume Submission**: Multiple resume submission for candidates
- **Candidate Management**: Track submitted candidates for each job
- **Analytics Dashboard**: Visualize job distribution and candidate metrics

### 🛠 Technical Stack
- **Backend**: FastAPI with SQLAlchemy ORM
- **Frontend**: Streamlit for interactive dashboard
- **Database**: MySQL/PostgreSQL support
- **File Storage**: Local file system for resume uploads
- **API Integration**: HTTP client for ATS connectivity

## Project Structure

```
VMS/
├── backend/
│   └── main.py              # FastAPI backend with Ceipal integration
├── frontend/
│   └── main.py              # Streamlit frontend dashboard
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
Candidate submission notifications use SendGrid from `SENDGRID_FROM_EMAIL`. Set the admin recipients as a comma-separated list:

```env
SUBMISSION_NOTIFICATION_RECIPIENTS=it.support@radixsol.com
```

Each successful candidate submission is also written to a separate submission audit log with submitter name, submitter email, candidate details, timestamp, IP address when available, user agent, resume metadata, and job metadata. MongoDB deployments store this in the `submission_logs` collection. Local/fallback deployments store it in `DATA_DIR/submission_logs.json`.

### 3. Database Setup
```bash
# Create SQLite database and tables
python database/models.py
```

### 4. Run the Application

#### Backend Server
```bash
cd backend
python main.py
```
The backend will start on `http://localhost:8000`

#### Frontend Dashboard
```bash
cd frontend
streamlit run main.py
```
The frontend will start on `http://localhost:8501`

### 5. Quick Start (All in One)
```bash
# Setup everything
python main.py setup

# Start backend
python main.py backend

# In another terminal, start frontend
python main.py frontend

# Or start both at once
python main.py all
```

## API Endpoints

### Jobs
- `GET /api/jobs` - Get all active jobs
- `GET /api/jobs/{job_id}` - Get specific job details

### Candidates
- `POST /api/candidates/submit` - Submit candidate with resume
- `GET /api/candidates/job/{job_id}` - Get candidates for specific job
- `GET /api/candidates` - Get all candidates
- `GET /api/submission-logs` - Admin-only candidate submission audit log

### Authentication
- `POST /api/auth/request-otp` - Request an email verification code
- `POST /api/auth/verify-otp` - Verify code and receive a 7-day bearer token

## Frontend Features

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

### Analytics Dashboard
- Job distribution by department
- Employment type statistics
- Location-based analysis
- Detailed job listings table

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

# Frontend
streamlit run frontend/main.py --server.port 8501
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

2. **Database Connection Error**
   - Verify DATABASE_URL in .env
   - Check database server status

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
- Frontend shows connection status
- Database logs available via SQLAlchemy

## Support

For issues and questions:
1. Check the troubleshooting section
2. Review the logs for error details
3. Verify environment configuration
4. Test with mock data first

## License

This project is licensed under the MIT License.
