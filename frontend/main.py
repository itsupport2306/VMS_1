import streamlit as st
import requests
import pandas as pd
from datetime import datetime
import plotly.express as px
import plotly.graph_objects as go
from PIL import Image
import io
import base64
import os

# Configuration
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")

# Page Configuration
st.set_page_config(
    page_title="VMS - Vendor Management System",
    page_icon="🏢",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Enterprise CSS
st.markdown("""
<style>
    /* Global Styles */
    .main-header {
        font-size: 2.5rem;
        font-weight: 700;
        color: #1a237e;
        text-align: center;
        margin-bottom: 2rem;
        padding: 1rem;
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
    }
    
    .enterprise-card {
        border: none;
        border-radius: 16px;
        padding: 2rem;
        margin-bottom: 1.5rem;
        background: linear-gradient(145deg, #ffffff, #f8f9fa);
        box-shadow: 0 10px 30px rgba(0,0,0,0.1);
        transition: all 0.3s ease;
    }
    
    .enterprise-card:hover {
        transform: translateY(-2px);
        box-shadow: 0 15px 40px rgba(0,0,0,0.15);
    }
    
    .job-title {
        font-size: 1.5rem;
        font-weight: 600;
        color: #2e7d32;
        margin-bottom: 0.75rem;
    }
    
    .job-meta {
        color: #666;
        font-size: 0.9rem;
        margin-bottom: 1rem;
    }
    
    .badge {
        display: inline-block;
        padding: 0.25rem 0.75rem;
        border-radius: 12px;
        font-size: 0.75rem;
        font-weight: 600;
        margin-right: 0.5rem;
        margin-bottom: 0.5rem;
    }
    
    .badge-primary {
        background-color: #e3f2fd;
        color: #1976d2;
    }
    
    .badge-success {
        background-color: #e8f5e8;
        color: #2e7d32;
    }
    
    .badge-warning {
        background-color: #fff3e0;
        color: #f57c00;
    }
    
    .btn-primary {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
        border: none;
        padding: 0.75rem 1.5rem;
        border-radius: 8px;
        font-weight: 600;
        cursor: pointer;
        transition: all 0.3s ease;
    }
    
    .btn-primary:hover {
        transform: translateY(-1px);
        box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
    }
    
    .sidebar-header {
        font-size: 1.2rem;
        font-weight: 600;
        color: #1a237e;
        margin-bottom: 1rem;
        padding-bottom: 0.5rem;
        border-bottom: 2px solid #e3f2fd;
    }
    
    .metric-card {
        background: linear-gradient(145deg, #ffffff, #f8f9fa);
        border-radius: 12px;
        padding: 1.5rem;
        text-align: center;
        box-shadow: 0 5px 15px rgba(0,0,0,0.08);
    }
    
    .metric-value {
        font-size: 2rem;
        font-weight: 700;
        color: #1a237e;
    }
    
    .metric-label {
        font-size: 0.9rem;
        color: #666;
        margin-top: 0.5rem;
    }
    
    .success-message {
        background: linear-gradient(145deg, #e8f5e8, #f1f8e9);
        border: 1px solid #4caf50;
        color: #2e7d32;
        padding: 1rem;
        border-radius: 8px;
        margin: 1rem 0;
    }
    
    .error-message {
        background: linear-gradient(145deg, #ffebee, #fce4ec);
        border: 1px solid #f44336;
        color: #c62828;
        padding: 1rem;
        border-radius: 8px;
        margin: 1rem 0;
    }
    
    .stButton > button {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
        border: none;
        padding: 0.75rem 1.5rem;
        border-radius: 8px;
        font-weight: 600;
        transition: all 0.3s ease;
    }
    
    .stButton > button:hover {
        transform: translateY(-1px);
        box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
    }
    
    .stSelectbox > div > div {
        background-color: #f8f9fa;
        border-radius: 8px;
    }
    
    .stTextInput > div > div > input {
        border-radius: 8px;
        border: 2px solid #e3f2fd;
    }
    
    .stFileUploader > div {
        border-radius: 8px;
        border: 2px dashed #667eea;
    }
</style>
""", unsafe_allow_html=True)

# Helper Functions
def fetch_jobs():
    """Fetch jobs from backend API"""
    try:
        response = requests.get(f"{BACKEND_URL}/api/jobs")
        if response.status_code == 200:
            return response.json()
        else:
            st.error(f"Failed to fetch jobs: {response.status_code}")
            return None
    except requests.exceptions.RequestException as e:
        st.error(f"Error connecting to backend: {str(e)}")
        return None

def fetch_job_details(job_id):
    """Fetch specific job details"""
    try:
        response = requests.get(f"{BACKEND_URL}/api/jobs/{job_id}")
        if response.status_code == 200:
            return response.json()
        else:
            st.error(f"Failed to fetch job details: {response.status_code}")
            return None
    except requests.exceptions.RequestException as e:
        st.error(f"Error connecting to backend: {str(e)}")
        return None

def submit_candidate(candidate_data, resume_file):
    """Submit candidate with resume"""
    try:
        files = {"resume": resume_file}
        data = {
            "candidate_name": candidate_data["name"],
            "email": candidate_data["email"],
            "phone": candidate_data.get("phone", ""),
            "job_id": candidate_data["job_id"]
        }
        
        response = requests.post(
            f"{BACKEND_URL}/api/candidates/submit",
            files=files,
            data=data
        )
        
        if response.status_code == 200:
            return response.json()
        else:
            st.error(f"Failed to submit candidate: {response.status_code}")
            return None
    except requests.exceptions.RequestException as e:
        st.error(f"Error connecting to backend: {str(e)}")
        return None

def fetch_candidates_for_job(job_id):
    """Fetch candidates for a specific job"""
    try:
        response = requests.get(f"{BACKEND_URL}/api/candidates/job/{job_id}")
        if response.status_code == 200:
            return response.json()
        else:
            return None
    except requests.exceptions.RequestException as e:
        return None

def show_enterprise_sidebar():
    """Show enterprise-style sidebar"""
    st.markdown('<div class="sidebar-header">🏢 VMS Navigation</div>', unsafe_allow_html=True)
    
    page = st.selectbox(
        "Select Module",
        ["📊 Dashboard", "💼 Job Board", "📤 Submit Resume", "👥 Candidates", "📈 Analytics"],
        key="navigation"
    )
    
    st.markdown("---")
    
    # System Status
    st.markdown('<div class="sidebar-header">🔧 System Status</div>', unsafe_allow_html=True)
    
    try:
        response = requests.get(f"{BACKEND_URL}/api/ceipal/test", timeout=5)
        if response.status_code == 200:
            status_data = response.json()
            if status_data.get("status") == "success":
                st.success("✅ Ceipal API Connected")
            else:
                st.error("❌ Ceipal API Failed")
        else:
            st.error("❌ Backend Offline")
    except:
        st.error("❌ Backend Offline")
    
    st.markdown("---")
    
    # Quick Stats
    st.markdown('<div class="sidebar-header">📊 Quick Stats</div>', unsafe_allow_html=True)
    
    jobs_data = fetch_jobs()
    if jobs_data:
        total_jobs = jobs_data.get("total", 0)
        st.metric("Total Jobs", total_jobs)
        
        active_jobs = len([j for j in jobs_data.get("jobs", []) if j.get("status") == "active"])
        st.metric("Active Jobs", active_jobs)

def show_enterprise_dashboard():
    """Show enterprise dashboard"""
    st.markdown('<h1 class="main-header">🏢 Vendor Management System</h1>', unsafe_allow_html=True)
    
    # Key Metrics
    jobs_data = fetch_jobs()
    
    if jobs_data:
        jobs = jobs_data.get("jobs", [])
        total_jobs = jobs_data.get("total", 0)
        
        # Metrics Row
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            st.markdown(f"""
            <div class="metric-card">
                <div class="metric-value">{total_jobs}</div>
                <div class="metric-label">Total Jobs</div>
            </div>
            """, unsafe_allow_html=True)
        
        with col2:
            engineering_jobs = len([j for j in jobs if j.get("department") == "Engineering"])
            st.markdown(f"""
            <div class="metric-card">
                <div class="metric-value">{engineering_jobs}</div>
                <div class="metric-label">Engineering</div>
            </div>
            """, unsafe_allow_html=True)
        
        with col3:
            remote_jobs = len([j for j in jobs if "Remote" in j.get("location", "")])
            st.markdown(f"""
            <div class="metric-card">
                <div class="metric-value">{remote_jobs}</div>
                <div class="metric-label">Remote Jobs</div>
            </div>
            """, unsafe_allow_html=True)
        
        with col4:
            full_time_jobs = len([j for j in jobs if j.get("employment_type") == "Full-time"])
            st.markdown(f"""
            <div class="metric-card">
                <div class="metric-value">{full_time_jobs}</div>
                <div class="metric-label">Full-time</div>
            </div>
            """, unsafe_allow_html=True)
        
        st.markdown("---")
        
        # Recent Jobs
        st.subheader("📋 Recent Job Postings")
        
        if jobs:
            # Sort by posted date (most recent first)
            recent_jobs = sorted(jobs, key=lambda x: x.get("posted_date", ""), reverse=True)[:5]
            
            for i, job in enumerate(recent_jobs):
                with st.container():
                    st.markdown(f"""
                    <div class="enterprise-card">
                        <div class="job-title">{job.get('title', 'N/A')}</div>
                        <div class="job-meta">
                            🏢 {job.get('department', 'N/A')} | 📍 {job.get('location', 'N/A')} | 💼 {job.get('employment_type', 'N/A')}
                        </div>
                        <p style="color: #666; line-height: 1.6;">{job.get('description', 'No description available')[:200]}...</p>
                        <div style="margin-top: 1rem;">
                            <span class="badge badge-primary">{job.get('department', 'N/A')}</span>
                            <span class="badge badge-success">{job.get('employment_type', 'N/A')}</span>
                            <span class="badge badge-warning">{job.get('location', 'N/A')}</span>
                        </div>
                    </div>
                    """, unsafe_allow_html=True)
                    
                    col1, col2, col3 = st.columns([2, 1, 1])
                    with col1:
                        if st.button(f"📝 View Details", key=f"view_job_{job.get('id')}"):
                            st.session_state.selected_job = job
                            st.session_state.page = "job_details"
                            st.rerun()
                    
                    with col2:
                        candidates_data = fetch_candidates_for_job(job.get('id'))
                        candidate_count = len(candidates_data.get("candidates", [])) if candidates_data else 0
                        if st.button(f"👥 Candidates ({candidate_count})", key=f"candidates_{job.get('id')}"):
                            st.session_state.selected_job = job
                            st.session_state.page = "job_candidates"
                            st.rerun()
                    
                    with col3:
                        if st.button(f"📤 Submit Resume", key=f"apply_{job.get('id')}"):
                            st.session_state.selected_job = job
                            st.session_state.page = "submit_resume"
                            st.rerun()
                    
                    st.markdown("---")
        else:
            st.info("No jobs available at the moment.")
    else:
        st.warning("Unable to load job data. Please check if the backend server is running.")

def show_enterprise_job_board():
    """Show enterprise job board"""
    st.markdown('<h1 class="main-header">💼 Job Board</h1>', unsafe_allow_html=True)
    
    # Fetch jobs
    jobs_data = fetch_jobs()
    
    if jobs_data:
        jobs = jobs_data.get("jobs", [])
        total_jobs = jobs_data.get("total", 0)
        
        # Filters
        col1, col2, col3 = st.columns([2, 2, 2])
        
        with col1:
            department_filter = st.selectbox(
                "🏢 Department",
                ["All Departments"] + list(set([j.get("department", "Not specified") for j in jobs]))
            )
        
        with col2:
            type_filter = st.selectbox(
                "💼 Employment Type",
                ["All Types"] + list(set([j.get("employment_type", "Not specified") for j in jobs]))
            )
        
        with col3:
            location_filter = st.selectbox(
                "📍 Location",
                ["All Locations"] + list(set([j.get("location", "Not specified") for j in jobs]))
            )
        
        st.markdown("---")
        
        # Apply filters
        filtered_jobs = jobs
        if department_filter != "All Departments":
            filtered_jobs = [j for j in filtered_jobs if j.get("department") == department_filter]
        if type_filter != "All Types":
            filtered_jobs = [j for j in filtered_jobs if j.get("employment_type") == type_filter]
        if location_filter != "All Locations":
            filtered_jobs = [j for j in filtered_jobs if j.get("location") == location_filter]
        
        # Results summary
        st.markdown(f"### 📊 Showing {len(filtered_jobs)} of {total_jobs} jobs")
        
        if filtered_jobs:
            for job in filtered_jobs:
                with st.container():
                    st.markdown(f"""
                    <div class="enterprise-card">
                        <div class="job-title">{job.get('title', 'N/A')}</div>
                        <div class="job-meta">
                            🏢 {job.get('department', 'N/A')} | 📍 {job.get('location', 'N/A')} | 💼 {job.get('employment_type', 'N/A')}
                        </div>
                        <p style="color: #666; line-height: 1.6; margin: 1rem 0;">{job.get('description', 'No description available')}</p>
                        
                        <div style="margin: 1rem 0;">
                            <span class="badge badge-primary">{job.get('department', 'N/A')}</span>
                            <span class="badge badge-success">{job.get('employment_type', 'N/A')}</span>
                            <span class="badge badge-warning">{job.get('location', 'N/A')}</span>
                            {f'<span class="badge badge-primary">💰 Bill Rate: {job.get("salary_range", "Not specified")}</span>' if job.get('salary_range') else ''}
                        </div>
                        
                        <div class="job-meta">
                            📅 Posted: {job.get('posted_date', 'N/A')} | 🔄 Status: {job.get('status', 'N/A')}
                        </div>
                    </div>
                    """, unsafe_allow_html=True)
                    
                    # Action Buttons
                    col1, col2, col3, col4 = st.columns([2, 1, 1, 1])
                    
                    with col1:
                        if st.button(f"📝 View Full Details", key=f"details_{job.get('id')}"):
                            st.session_state.selected_job = job
                            st.session_state.page = "job_details"
                            st.rerun()
                    
                    with col2:
                        candidates_data = fetch_candidates_for_job(job.get('id'))
                        candidate_count = len(candidates_data.get("candidates", [])) if candidates_data else 0
                        if st.button(f"👥 View Candidates ({candidate_count})", key=f"view_candidates_{job.get('id')}"):
                            st.session_state.selected_job = job
                            st.session_state.page = "job_candidates"
                            st.rerun()
                    
                    with col3:
                        if st.button(f"📤 Submit Resume", key=f"submit_job_{job.get('id')}"):
                            st.session_state.selected_job = job
                            st.session_state.page = "submit_resume"
                            st.rerun()
                    
                    with col4:
                        if st.button(f"🔗 Share", key=f"share_{job.get('id')}"):
                            job_url = f"http://localhost:8501?page=job_details&job_id={job.get('id')}"
                            st.code(job_url, language="text")
                            st.success("Job link copied to clipboard!")
                    
                    st.markdown("---")
        else:
            st.info("No jobs found matching the selected filters.")
    else:
        st.warning("Unable to load jobs. Please check if the backend server is running.")

def show_resume_submission():
    """Show resume submission form"""
    st.markdown('<h1 class="main-header">📤 Submit Candidate Resume</h1>', unsafe_allow_html=True)
    
    # Fetch jobs for dropdown
    jobs_data = fetch_jobs()
    
    if jobs_data:
        jobs = jobs_data.get("jobs", [])
        
        # Job Selection
        if 'selected_job' in st.session_state:
            selected_job = st.session_state.selected_job
            default_index = jobs.index(next((j for j in jobs if j.get('id') == selected_job.get('id')), None)) if selected_job else 0
        else:
            default_index = 0
        
        job_options = [f"{job.get('title')} - {job.get('department')}" for job in jobs]
        selected_job_title = st.selectbox(
            "🎯 Select Job Position",
            job_options,
            index=default_index if default_index is not None else 0
        )
        
        selected_job = jobs[job_options.index(selected_job_title)]
        
        # Display selected job info
        st.markdown(f"""
        <div class="enterprise-card">
            <h3>📋 Selected Position</h3>
            <p><strong>{selected_job.get('title')}</strong></p>
            <p>🏢 {selected_job.get('department')} | 📍 {selected_job.get('location')} | 💼 {selected_job.get('employment_type')}</p>
        </div>
        """, unsafe_allow_html=True)
        
        st.markdown("---")
        
        # Candidate Information Form
        st.subheader("👤 Candidate Information")
        
        col1, col2 = st.columns(2)
        
        with col1:
            candidate_name = st.text_input(
                "👤 Full Name *",
                placeholder="Enter candidate's full name",
                help="Please provide the candidate's full legal name"
            )
            
            email = st.text_input(
                "📧 Email Address *",
                placeholder="candidate@example.com",
                help="Professional email address for communication"
            )
        
        with col2:
            phone = st.text_input(
                "📱 Phone Number",
                placeholder="+1 (555) 123-4567",
                help="Optional: Contact number for follow-up"
            )
            
            experience = st.selectbox(
                "💼 Experience Level",
                ["Entry Level", "Mid Level", "Senior Level", "Lead Level", "Executive"],
                help="Select the candidate's experience level"
            )
        
        # Resume Upload
        st.subheader("📄 Resume Upload")
        
        resume_file = st.file_uploader(
            "📤 Upload Resume (PDF, DOC, DOCX, TXT)",
            type=['pdf', 'doc', 'docx', 'txt'],
            help="Maximum file size: 10MB. Supported formats: PDF, DOC, DOCX, TXT",
            key="resume_upload"
        )
        
        if resume_file:
            # File details
            col1, col2, col3 = st.columns(3)
            
            with col1:
                st.metric("📄 File Name", resume_file.name)
            
            with col2:
                file_size_mb = resume_file.size / (1024 * 1024)
                st.metric("📊 File Size", f"{file_size_mb:.2f} MB")
            
            with col3:
                file_type = resume_file.type or "Unknown"
                st.metric("📋 File Type", file_type)
        
        # Additional Information
        st.subheader("📝 Additional Information")
        
        cover_letter = st.text_area(
            "📄 Cover Letter / Notes",
            placeholder="Enter any additional information about the candidate...",
            help="Optional: Add cover letter content or additional notes about the candidate",
            height=150
        )
        
        # Terms and Conditions
        st.markdown("---")
        
        agree_terms = st.checkbox(
            "✅ I confirm that I have permission to submit this candidate's information and resume",
            help="Please ensure you have proper consent before submitting candidate data"
        )
        
        # Submit Button
        submit_enabled = all([
            candidate_name.strip(),
            email.strip(),
            resume_file,
            agree_terms
        ])
        
        if st.button(
            "🚀 Submit Application",
            type="primary",
            use_container_width=True,
            disabled=not submit_enabled
        ):
            if not submit_enabled:
                st.error("Please fill in all required fields and upload a resume.")
            else:
                candidate_data = {
                    "name": candidate_name,
                    "email": email,
                    "phone": phone,
                    "job_id": selected_job.get('id'),
                    "experience": experience,
                    "cover_letter": cover_letter
                }
                
                with st.spinner("📤 Submitting application..."):
                    result = submit_candidate(candidate_data, resume_file)
                    
                    if result:
                        st.markdown(f"""
                        <div class="success-message">
                            <h4>✅ Application Submitted Successfully!</h4>
                            <p><strong>Candidate ID:</strong> {result.get('candidate_id')}</p>
                            <p><strong>Status:</strong> {result.get('status')}</p>
                            <p><strong>Position:</strong> {selected_job.get('title')}</p>
                            <p><strong>Department:</strong> {selected_job.get('department')}</p>
                        </div>
                        """, unsafe_allow_html=True)
                        
                        # Clear form
                        if st.button("🔄 Submit Another Application"):
                            st.session_state.pop('selected_job', None)
                            st.rerun()
                    else:
                        st.markdown("""
                        <div class="error-message">
                            <h4>❌ Submission Failed</h4>
                            <p>Please try again or contact support if the issue persists.</p>
                        </div>
                        """, unsafe_allow_html=True)
    else:
        st.warning("Unable to load jobs. Please check if the backend server is running.")

def show_job_details():
    """Show detailed job information"""
    if 'selected_job' not in st.session_state:
        st.error("No job selected")
        return
    
    job = st.session_state.selected_job
    
    st.markdown('<h1 class="main-header">📋 Job Details</h1>', unsafe_allow_html=True)
    
    # Job Header
    st.markdown(f"""
    <div class="enterprise-card">
        <div class="job-title">{job.get('title', 'N/A')}</div>
        <div class="job-meta">
            🏢 {job.get('department', 'N/A')} | 📍 {job.get('location', 'N/A')} | 💼 {job.get('employment_type', 'N/A')}
        </div>
        
        <div style="margin: 1rem 0;">
            <span class="badge badge-primary">{job.get('department', 'N/A')}</span>
            <span class="badge badge-success">{job.get('employment_type', 'N/A')}</span>
            <span class="badge badge-warning">{job.get('location', 'N/A')}</span>
            {f'<span class="badge badge-primary">💰 Bill Rate: {job.get("salary_range", "Not specified")}</span>' if job.get('salary_range') else ''}
        </div>
    </div>
    """, unsafe_allow_html=True)
    
    # Job Description and Requirements
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("📝 Job Description")
        st.markdown(f"""
        <div class="enterprise-card">
            <p style="color: #666; line-height: 1.6;">{job.get('description', 'No description available')}</p>
        </div>
        """, unsafe_allow_html=True)
    
    with col2:
        st.subheader("🎯 Requirements")
        st.markdown(f"""
        <div class="enterprise-card">
            <p style="color: #666; line-height: 1.6;">{job.get('requirements', 'No requirements specified')}</p>
        </div>
        """, unsafe_allow_html=True)
    
    # Job Metadata
    st.subheader("📊 Job Information")
    
    metadata_data = {
        "Job ID": job.get('id', 'N/A'),
        "Department": job.get('department', 'N/A'),
        "Location": job.get('location', 'N/A'),
        "Employment Type": job.get('employment_type', 'N/A'),
        "Bill Rate": job.get('salary_range', 'Not specified'),
        "Posted Date": job.get('posted_date', 'N/A'),
        "Status": job.get('status', 'N/A')
    }
    
    df_metadata = pd.DataFrame(list(metadata_data.items()), columns=["Field", "Value"])
    st.dataframe(df_metadata, use_container_width=True, hide_index=True)
    
    # Action Buttons
    st.markdown("---")
    st.subheader("🚀 Take Action")
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        if st.button("📤 Submit Resume for This Job", type="primary", use_container_width=True):
            st.session_state.page = "submit_resume"
            st.rerun()
    
    with col2:
        candidates_data = fetch_candidates_for_job(job.get('id'))
        candidate_count = len(candidates_data.get("candidates", [])) if candidates_data else 0
        if st.button(f"👥 View Candidates ({candidate_count})", use_container_width=True):
            st.session_state.page = "job_candidates"
            st.rerun()
    
    with col3:
        if st.button("🔙 Back to Job Board", use_container_width=True):
            st.session_state.page = "job_board"
            st.rerun()

def show_job_candidates():
    """Show candidates for a specific job"""
    if 'selected_job' not in st.session_state:
        st.error("No job selected")
        return
    
    job = st.session_state.selected_job
    
    st.markdown('<h1 class="main-header">👥 Job Candidates</h1>', unsafe_allow_html=True)
    
    # Job Information
    st.markdown(f"""
    <div class="enterprise-card">
        <div class="job-title">{job.get('title', 'N/A')}</div>
        <div class="job-meta">
            🏢 {job.get('department', 'N/A')} | 📍 {job.get('location', 'N/A')} | 💼 {job.get('employment_type', 'N/A')}
        </div>
    </div>
    """, unsafe_allow_html=True)
    
    # Fetch candidates
    candidates_data = fetch_candidates_for_job(job.get('id'))
    
    if candidates_data:
        candidates = candidates_data.get("candidates", [])
        
        if candidates:
            st.subheader(f"📊 {len(candidates)} Candidates Found")
            
            for candidate in candidates:
                st.markdown(f"""
                <div class="enterprise-card">
                    <div class="job-title">{candidate.get('name', 'N/A')}</div>
                    <div class="job-meta">
                        📧 {candidate.get('email', 'N/A')} | 📱 {candidate.get('phone', 'Not provided')}
                    </div>
                    <div class="job-meta">
                        📅 Submitted: {candidate.get('submitted_date', 'N/A')} | 📄 Resume: {candidate.get('resume_path', 'N/A')}
                    </div>
                    <div style="margin: 1rem 0;">
                        <span class="badge badge-success">🔄 {candidate.get('status', 'N/A')}</span>
                    </div>
                </div>
                """, unsafe_allow_html=True)
                
                # Action buttons
                col1, col2, col3 = st.columns(3)
                
                with col1:
                    if st.button(f"📄 View Resume", key=f"resume_{candidate.get('id')}"):
                        st.info(f"Resume file: {candidate.get('resume_path', 'N/A')}")
                
                with col2:
                    if st.button(f"📧 Contact", key=f"contact_{candidate.get('id')}"):
                        st.info(f"Email: {candidate.get('email', 'N/A')}")
                
                with col3:
                    if st.button(f"📝 Update Status", key=f"status_{candidate.get('id')}"):
                        st.info("Status update feature coming soon!")
                
                st.markdown("---")
        else:
            st.info("No candidates have applied for this job yet.")
            
            # Prompt to submit resume
            st.markdown("---")
            st.subheader("📤 Submit First Candidate")
            
            if st.button("📤 Submit Resume for This Position", type="primary"):
                st.session_state.page = "submit_resume"
                st.rerun()
    else:
        st.warning("Unable to load candidates data.")
    
    # Back button
    st.markdown("---")
    if st.button("🔙 Back to Job Board", use_container_width=True):
        st.session_state.page = "job_board"
        st.rerun()

def show_analytics():
    """Show analytics dashboard"""
    st.markdown('<h1 class="main-header">📈 Analytics Dashboard</h1>', unsafe_allow_html=True)
    
    # Fetch data
    jobs_data = fetch_jobs()
    
    if jobs_data:
        jobs = jobs_data.get("jobs", [])
        
        # Department Distribution
        st.subheader("📊 Job Distribution by Department")
        
        dept_counts = {}
        for job in jobs:
            dept = job.get("department", "Not specified")
            dept_counts[dept] = dept_counts.get(dept, 0) + 1
        
        col1, col2 = st.columns(2)
        
        with col1:
            fig_dept = px.pie(
                values=list(dept_counts.values()),
                names=list(dept_counts.keys()),
                title="Jobs by Department",
                color_discrete_sequence=px.colors.qualitative.Set3
            )
            st.plotly_chart(fig_dept, use_container_width=True)
        
        with col2:
            # Employment Type Distribution
            type_counts = {}
            for job in jobs:
                emp_type = job.get("employment_type", "Not specified")
                type_counts[emp_type] = type_counts.get(emp_type, 0) + 1
            
            fig_type = px.bar(
                x=list(type_counts.keys()),
                y=list(type_counts.values()),
                title="Jobs by Employment Type",
                labels={"x": "Employment Type", "y": "Number of Jobs"},
                color_discrete_sequence=px.colors.qualitative.Set2
            )
            st.plotly_chart(fig_type, use_container_width=True)
        
        # Location Distribution
        st.subheader("📍 Job Locations")
        
        location_counts = {}
        for job in jobs:
            location = job.get("location", "Not specified")
            location_counts[location] = location_counts.get(location, 0) + 1
        
        fig_location = px.bar(
            x=list(location_counts.keys()),
            y=list(location_counts.values()),
            title="Jobs by Location",
            labels={"x": "Location", "y": "Number of Jobs"},
            color_discrete_sequence=px.colors.qualitative.Set1
        )
        st.plotly_chart(fig_location, use_container_width=True)
        
        # Jobs Table
        st.subheader("📋 All Jobs Details")
        
        jobs_df = pd.DataFrame(jobs)
        if not jobs_df.empty:
            # Format the dataframe for display
            display_df = jobs_df[[
                'title', 'department', 'location', 'employment_type', 
                'salary_range', 'status', 'posted_date'
            ]].copy()
            display_df.columns = [
                'Title', 'Department', 'Location', 'Type', 
                'Bill Rate', 'Status', 'Posted Date'
            ]
            st.dataframe(display_df, use_container_width=True)
    else:
        st.warning("Unable to load analytics data. Please check if the backend server is running.")

# Main Application
def main():
    # Initialize session state
    if 'page' not in st.session_state:
        st.session_state.page = "dashboard"
    
    # Show sidebar
    show_enterprise_sidebar()
    
    # Route to appropriate page
    if st.session_state.page == "dashboard":
        show_enterprise_dashboard()
    elif st.session_state.page == "job_board":
        show_enterprise_job_board()
    elif st.session_state.page == "submit_resume":
        show_resume_submission()
    elif st.session_state.page == "job_details":
        show_job_details()
    elif st.session_state.page == "job_candidates":
        show_job_candidates()
    elif st.session_state.page == "analytics":
        show_analytics()
    else:
        # Handle sidebar navigation
        if st.session_state.get("navigation") == "📊 Dashboard":
            show_enterprise_dashboard()
        elif st.session_state.get("navigation") == "💼 Job Board":
            show_enterprise_job_board()
        elif st.session_state.get("navigation") == "📤 Submit Resume":
            show_resume_submission()
        elif st.session_state.get("navigation") == "👥 Candidates":
            show_job_candidates()
        elif st.session_state.get("navigation") == "📈 Analytics":
            show_analytics()

if __name__ == "__main__":
    main()
