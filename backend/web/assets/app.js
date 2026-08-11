const API_BASE = window.location.origin;

// Infinite scroll state
let currentPage = 1;
let isLoadingMore = false;
let hasMoreJobs = true;
let nextStartPage = 26;  // After initial 25 pages

const els = {
  jobsGrid: document.getElementById('jobsGrid'),
  jobsEmpty: document.getElementById('jobsEmpty'),
  searchInput: document.getElementById('searchInput'),
  statusFilter: document.getElementById('statusFilter'),
  stateFilter: document.getElementById('stateFilter'),
  specialtyFilter: document.getElementById('specialtyFilter'),
  refreshBtn: document.getElementById('refreshBtn'),
  ceipalCacheBtn: document.getElementById('ceipalCacheBtn'),
  ceipalTestBtn: document.getElementById('ceipalTestBtn'),
  openDocsBtn: document.getElementById('openDocsBtn'),
  systemJson: document.getElementById('systemJson'),
  systemStatus: document.getElementById('systemStatus'),
  // Admin user management
  adminUserPanel: document.getElementById('adminUserPanel'),
  newUserEmail: document.getElementById('newUserEmail'),
  addUserBtn: document.getElementById('addUserBtn'),
  addUserAlert: document.getElementById('addUserAlert'),
  userCount: document.getElementById('userCount'),
  usersList: document.getElementById('usersList'),
  apiBase: document.getElementById('apiBase'),
  pageTitle: document.getElementById('pageTitle'),
  pageSubtitle: document.getElementById('pageSubtitle'),
  jobsCount: document.getElementById('jobsCount'),
  viewJobs: document.getElementById('viewJobs'),
  viewSubmissions: document.getElementById('viewSubmissions'),
  viewOffers: document.getElementById('viewOffers'),
  viewDeclines: document.getElementById('viewDeclines'),
  viewStarts: document.getElementById('viewStarts'),
  viewSettings: document.getElementById('viewSettings'),
  viewAuth: document.getElementById('viewAuth'),
  submissionsTable: document.getElementById('submissionsTable'),
  offersTable: document.getElementById('offersTable'),
  declinesTable: document.getElementById('declinesTable'),
  startsTable: document.getElementById('startsTable'),
  // Auth elements
  userInfo: document.getElementById('userInfo'),
  userName: document.getElementById('userName'),
  logoutBtn: document.getElementById('logoutBtn'),
  systemNavBtn: document.getElementById('systemNavBtn'),
  authTitle: document.getElementById('authTitle'),
  authDesc: document.getElementById('authDesc'),
  authMainForm: document.getElementById('authMainForm'),
  authEmail: document.getElementById('authEmail'),
  authEmailLabel: document.getElementById('authEmailLabel'),
  authPasswordField: document.getElementById('authPasswordField'),
  authPassword: document.getElementById('authPassword'),
  authConfirmPasswordField: document.getElementById('authConfirmPasswordField'),
  authConfirmPassword: document.getElementById('authConfirmPassword'),
  authOtpField: document.getElementById('authOtpField'),
  authOtp: document.getElementById('authOtp'),
  authSubmitBtn: document.getElementById('authSubmitBtn'),
  authBackBtn: document.getElementById('authBackBtn'),
  authAlert: document.getElementById('authAlert'),
  authToggleLink: document.getElementById('authToggleLink'),
  forgotPasswordLink: document.getElementById('forgotPasswordLink'),
  forgotPasswordForm: document.getElementById('forgotPasswordForm'),
  forgotEmail: document.getElementById('forgotEmail'),
  forgotSubmitBtn: document.getElementById('forgotSubmitBtn'),
  forgotAlert: document.getElementById('forgotAlert'),
  backToLoginFromForgot: document.getElementById('backToLoginFromForgot'),
  resetPasswordForm: document.getElementById('resetPasswordForm'),
  resetToken: document.getElementById('resetToken'),
  resetPassword: document.getElementById('resetPassword'),
  resetPasswordConfirm: document.getElementById('resetPasswordConfirm'),
  resetSubmitBtn: document.getElementById('resetSubmitBtn'),
  resetAlert: document.getElementById('resetAlert'),
  backToLoginLink: document.getElementById('backToLoginLink'),
  // Job Detail Modal
  jobDetailModal: document.getElementById('jobDetailModal'),
  jobDetailTitle: document.getElementById('jobDetailTitle'),
  jobDetailJobTitle: document.getElementById('jobDetailJobTitle'),
  jobDetailMeta: document.getElementById('jobDetailMeta'),
  jobDetailFullDesc: document.getElementById('jobDetailFullDesc'),
  jobDetailRequirements: document.getElementById('jobDetailRequirements'),
  jobDetailCandidates: document.getElementById('jobDetailCandidates'),
  jobDetailSubmitBtn: document.getElementById('jobDetailSubmitBtn'),
  // Submit Modal
  submitModal: document.getElementById('submitModal'),
  submitModalTitle: document.getElementById('submitModalTitle'),
  submitJobTitle: document.getElementById('submitJobTitle'),
  submitJobMeta: document.getElementById('submitJobMeta'),
  candName: document.getElementById('candName'),
  candEmail: document.getElementById('candEmail'),
  candPhone: document.getElementById('candPhone'),
  candBillRate: document.getElementById('candBillRate'),
  candLocation: document.getElementById('candLocation'),
  candSkills: document.getElementById('candSkills'),
  candJobTitle: document.getElementById('candJobTitle'),
  candExperience: document.getElementById('candExperience'),
  candStartDate: document.getElementById('candStartDate'),
  candRTO: document.getElementById('candRTO'),
  candSummary: document.getElementById('candSummary'),
  candResume: document.getElementById('candResume'),
  submitAnotherBtn: document.getElementById('submitAnotherBtn'),
  submitCloseBtn: document.getElementById('submitCloseBtn'),
  formAlert: document.getElementById('formAlert')
};

els.apiBase.textContent = `API: ${API_BASE}`;

let allJobs = [];
let activeJobId = null;
let activeJob = null;

// Auth state
let authToken = localStorage.getItem('vms_token') || null;
let currentUser = JSON.parse(localStorage.getItem('vms_user') || 'null');
let authMode = 'login';
let pendingOtpEmail = '';

const ADMIN_EMAIL = 'Admin@radixsol.com';

function isAdmin() {
  return currentUser && currentUser.email && currentUser.email.toLowerCase() === ADMIN_EMAIL.toLowerCase();
}

// Admin user management functions
function showAddUserAlert(type, msg) {
  els.addUserAlert.className = 'alert alert--' + type;
  els.addUserAlert.textContent = msg;
  els.addUserAlert.hidden = false;
}

function clearAddUserAlert() {
  els.addUserAlert.hidden = true;
  els.addUserAlert.textContent = '';
}

async function loadWhitelistedUsers() {
  if (!isAdmin()) return;
  
  try {
    const data = await apiGetAuth('/api/admin/users');
    const users = data.users || [];
    
    els.userCount.textContent = users.length;
    
    if (users.length === 0) {
      els.usersList.innerHTML = '<div class="empty">No whitelisted users yet.</div>';
      return;
    }
    
    const list = users.map(email => `
      <div style="display: flex; justify-content: space-between; align-items: center; padding: 10px 12px; background: var(--panel-2); border-radius: 6px; margin-bottom: 8px;">
        <span>${email}</span>
        ${email.toLowerCase() !== ADMIN_EMAIL.toLowerCase() ? `
          <button class="btn btn--secondary btn--small" onclick="removeUser('${email}')" style="font-size: 12px; padding: 4px 12px;">Remove</button>
        ` : '<span style="color: var(--muted); font-size: 12px;">(Admin)</span>'}
      </div>
    `).join('');
    
    els.usersList.innerHTML = list;
  } catch (e) {
    els.usersList.innerHTML = '<div class="empty">Error loading users: ' + e.message + '</div>';
  }
}

async function addUser() {
  clearAddUserAlert();
  
  const email = els.newUserEmail.value.trim();
  if (!email) return showAddUserAlert('error', 'Email is required');
  if (!email.includes('@')) return showAddUserAlert('error', 'Invalid email format');
  
  try {
    const formData = new FormData();
    formData.append('email', email);
    
    const res = await fetch(`${API_BASE}/api/admin/users`, {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${authToken}` },
      body: formData
    });
    
    const data = await res.json();
    
    if (!res.ok) {
      throw new Error(data.detail || data.message || 'Failed to add user');
    }
    
    // Clear input and reload list
    els.newUserEmail.value = '';
    showAddUserAlert('ok', `User ${email} added successfully`);
    await loadWhitelistedUsers();
    
  } catch (e) {
    showAddUserAlert('error', e.message);
  }
}

async function removeUser(email) {
  if (!confirm(`Are you sure you want to remove ${email} from the whitelist?`)) return;
  
  try {
    const res = await fetch(`${API_BASE}/api/admin/users/${encodeURIComponent(email)}`, {
      method: 'DELETE',
      headers: { 'Authorization': `Bearer ${authToken}` }
    });
    
    const data = await res.json();
    
    if (!res.ok) {
      throw new Error(data.detail || data.message || 'Failed to remove user');
    }
    
    await loadWhitelistedUsers();
    
  } catch (e) {
    alert('Error removing user: ' + e.message);
  }
}

function showAuthAlert(kind, msg) {
  els.authAlert.hidden = false;
  els.authAlert.className = `alert ${kind === 'ok' ? 'alert--ok' : 'alert--error'}`;
  els.authAlert.textContent = msg;
}

function clearAuthAlert() {
  els.authAlert.hidden = true;
  els.authAlert.textContent = '';
}

function updateAuthUI() {
  if (authToken && currentUser) {
    // Logged in - show user info and logout
    els.userInfo.hidden = false;
    els.userName.textContent = currentUser.full_name || currentUser.email;
    els.viewAuth.hidden = true;
    els.viewJobs.hidden = false;
    updateNotificationUI();
    
    // Submissions tab visible for all users (vendors see their own, admin sees all)
    const submissionsNav = document.querySelector('[data-view="submissions"]');
    if (submissionsNav) {
      submissionsNav.hidden = false;
    }
    
    // Show System nav button only for admin
    if (els.systemNavBtn) {
      els.systemNavBtn.hidden = !isAdmin();
    }
  } else {
    // Not logged in - show auth form
    els.userInfo.hidden = true;
    els.viewAuth.hidden = false;
    els.viewJobs.hidden = true;
    els.viewSubmissions.hidden = true;
    els.viewSettings.hidden = true;
    // Hide any previous auth alerts
    els.authAlert.hidden = true;
    els.authAlert.textContent = '';
  }
}

function logout() {
  authToken = null;
  currentUser = null;
  pendingOtpEmail = '';
  authMode = 'login';
  localStorage.removeItem('vms_token');
  localStorage.removeItem('vms_user');
  
  // Clear search and filter state to prevent leaking to next user
  if (els.searchInput) els.searchInput.value = '';
  if (els.statusFilter) els.statusFilter.value = '';
  if (els.stateFilter) els.stateFilter.value = '';
  if (els.specialtyFilter) els.specialtyFilter.value = '';
  
  // Clear jobs data so next user starts fresh
  allJobs = [];
  
  updateAuthUI();
}

function setAuthMode(mode, options = {}) {
  authMode = mode;
  clearAuthAlert();

  if (els.forgotPasswordForm) {
    els.forgotPasswordForm.hidden = true;
    els.forgotPasswordForm.style.display = 'none';
  }
  if (els.resetPasswordForm) {
    els.resetPasswordForm.hidden = true;
    els.resetPasswordForm.style.display = 'none';
  }
  if (els.authMainForm) {
    els.authMainForm.hidden = false;
    els.authMainForm.style.display = 'block';
  }

  const isSignup = mode === 'signup';
  const isVerify = mode === 'verify-signup';

  if (els.authTitle) els.authTitle.textContent = isVerify ? 'Verify Email' : (isSignup ? 'Sign Up' : 'Login');
  if (els.authDesc) {
    els.authDesc.textContent = isVerify
      ? 'Enter the OTP verification code sent to your email.'
      : (isSignup ? 'Create your account and verify your email.' : 'Enter your credentials to continue.');
  }
  if (els.authEmailLabel) els.authEmailLabel.textContent = isSignup || isVerify ? 'User Email' : 'Username/Email';
  if (els.authConfirmPasswordField) els.authConfirmPasswordField.hidden = !isSignup;
  if (els.authOtpField) els.authOtpField.hidden = !isVerify;
  if (els.authPasswordField) els.authPasswordField.hidden = isVerify;
  if (els.forgotPasswordLink) els.forgotPasswordLink.parentElement.hidden = mode !== 'login';
  if (els.authToggleLink) {
    els.authToggleLink.textContent = mode === 'login' ? 'New User? Sign Up' : 'Already registered? Login';
  }
  if (els.authSubmitBtn) {
    els.authSubmitBtn.textContent = isVerify ? 'Verify OTP' : (isSignup ? 'Sign Up' : 'Login');
    els.authSubmitBtn.disabled = false;
  }
  if (els.authBackBtn) els.authBackBtn.hidden = !isVerify;
  if (els.authEmail) els.authEmail.disabled = isVerify;
  if (els.authPassword) els.authPassword.disabled = isVerify;
  if (els.authConfirmPassword) els.authConfirmPassword.disabled = isVerify;

  if (!options.keepValues) {
    if (els.authEmail) els.authEmail.value = '';
    if (els.authPassword) els.authPassword.value = '';
    if (els.authConfirmPassword) els.authConfirmPassword.value = '';
    if (els.authOtp) els.authOtp.value = '';
    pendingOtpEmail = '';
  }

  const focusTarget = isVerify ? els.authOtp : els.authEmail;
  if (focusTarget) focusTarget.focus();
}

// Password reset functions
function showForgotPasswordForm() {
  // Hide login form, show forgot password form (step 1)
  if (els.authMainForm) {
    els.authMainForm.hidden = true;
    els.authMainForm.style.display = 'none';
  }
  if (els.resetPasswordForm) {
    els.resetPasswordForm.hidden = true;
    els.resetPasswordForm.style.display = 'none';
  }
  els.forgotPasswordForm.hidden = false;
  els.forgotPasswordForm.style.display = 'block';
  els.forgotAlert.hidden = true;
  els.forgotAlert.textContent = '';
  if (els.forgotEmail) {
    els.forgotEmail.value = els.authEmail ? els.authEmail.value.trim() : '';
    els.forgotEmail.focus();
  }
}

function showResetPasswordForm(token) {
  // Show reset password form (step 2 - from email link)
  if (els.authMainForm) {
    els.authMainForm.hidden = true;
    els.authMainForm.style.display = 'none';
  }
  els.forgotPasswordForm.hidden = true;
  els.forgotPasswordForm.style.display = 'none';
  els.resetPasswordForm.hidden = false;
  els.resetPasswordForm.style.display = 'block';
  els.resetToken.value = token || '';
  els.resetAlert.hidden = true;
  els.resetAlert.textContent = '';
  if (els.resetPassword) els.resetPassword.focus();
}

function showLoginForm() {
  // Hide all forms, show login form
  els.forgotPasswordForm.hidden = true;
  els.forgotPasswordForm.style.display = 'none';
  els.resetPasswordForm.hidden = true;
  els.resetPasswordForm.style.display = 'none';
  setAuthMode('login');
  els.authAlert.hidden = true;
  els.authAlert.textContent = '';
}

function showResetAlert(type, msg) {
  els.resetAlert.className = 'alert alert--' + type;
  els.resetAlert.textContent = msg;
  els.resetAlert.hidden = false;
}

function clearForgotAlert() {
  els.forgotAlert.hidden = true;
  els.forgotAlert.textContent = '';
}

function clearResetAlert() {
  els.resetAlert.hidden = true;
  els.resetAlert.textContent = '';
}

async function handleForgotPassword() {
  clearForgotAlert();
  
  const email = els.forgotEmail.value.trim();
  
  if (!email) return showForgotAlert('error', 'Email is required');
  
  const body = { email };
  
  try {
    const res = await fetch(`${API_BASE}/api/auth/forgot-password`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    
    const data = await res.json();
    
    if (!res.ok) {
      throw new Error(data.detail || data.message || `Error: ${res.status}`);
    }
    
    // Clear form
    els.forgotEmail.value = '';
    
    // Show success message
    showForgotAlert('ok', 'Password reset email sent! Please check your inbox.');
    
  } catch (e) {
    showForgotAlert('error', e.message);
  }
}

function showForgotAlert(type, msg) {
  els.forgotAlert.className = 'alert alert--' + type;
  els.forgotAlert.textContent = msg;
  els.forgotAlert.hidden = false;
}

async function handlePasswordReset() {
  clearResetAlert();
  
  const token = els.resetToken.value;
  const password = els.resetPassword.value;
  const confirmPassword = els.resetPasswordConfirm.value;
  
  if (!token) return showResetAlert('error', 'Reset token is missing. Please use the link from your email.');
  if (!password) return showResetAlert('error', 'Password is required');
  if (password !== confirmPassword) return showResetAlert('error', 'Passwords do not match');
  if (password.length < 6) return showResetAlert('error', 'Password must be at least 6 characters');
  
  const body = { token, password };
  
  try {
    const res = await fetch(`${API_BASE}/api/auth/reset-password`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    
    const data = await res.json();
    
    if (!res.ok) {
      throw new Error(data.detail || data.message || `Error: ${res.status}`);
    }
    
    // Clear form
    els.resetPassword.value = '';
    els.resetPasswordConfirm.value = '';
    
    // Show success and switch back to login
    showResetAlert('ok', 'Password reset successfully! Please login with your new password.');
    
    setTimeout(() => {
      showLoginForm();
    }, 2000);
    
  } catch (e) {
    showResetAlert('error', e.message);
  }
}

async function handleAuthSubmit() {
  clearAuthAlert();
  
  const email = els.authEmail.value.trim();
  const password = els.authPassword ? els.authPassword.value : '';
  const confirmPassword = els.authConfirmPassword ? els.authConfirmPassword.value : '';
  
  if (!email) return showAuthAlert('error', 'Email is required');

  if (authMode === 'login') {
    if (!password) return showAuthAlert('error', 'Password is required');
    return handleLogin(email, password);
  }

  if (authMode === 'signup') {
    if (!password) return showAuthAlert('error', 'Password is required');
    if (password.length < 6) return showAuthAlert('error', 'Password must be at least 6 characters');
    if (password !== confirmPassword) return showAuthAlert('error', 'Passwords do not match');
    return handleSignup(email, password);
  }

  if (authMode === 'verify-signup') {
    const otp = (els.authOtp.value || '').trim();
    if (!otp) return showAuthAlert('error', 'Verification code is required');
    return handleSignupOtpVerify(pendingOtpEmail || email, otp);
  }
}

async function parseJsonResponse(res) {
  const text = await res.text();
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(text.includes('Internal Server Error') ? 'Server error. Please try again.' : text.slice(0, 100));
  }
}

function completeLogin(data) {
  authToken = data.access_token;
  currentUser = data.user;
  localStorage.setItem('vms_token', authToken);
  localStorage.setItem('vms_user', JSON.stringify(currentUser));

  if (els.authEmail) els.authEmail.value = '';
  if (els.authPassword) els.authPassword.value = '';
  if (els.authConfirmPassword) els.authConfirmPassword.value = '';
  if (els.authOtp) els.authOtp.value = '';
  pendingOtpEmail = '';
  setAuthMode('login', { keepValues: true });
  showAuthAlert('ok', 'Logged in successfully!');

  setTimeout(async () => {
    updateAuthUI();
    setView('jobs');
    await loadCeipalStatus();
    await loadJobs();
  }, 800);
}

async function handleLogin(email, password) {
  try {
    if (els.authSubmitBtn) {
      els.authSubmitBtn.disabled = true;
      els.authSubmitBtn.textContent = 'Logging in...';
    }

    const res = await fetch(`${API_BASE}/api/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password })
    });

    const data = await parseJsonResponse(res);
    if (!res.ok) {
      throw new Error(data.detail || data.message || `Error: ${res.status}`);
    }

    completeLogin(data);
  } catch (e) {
    showAuthAlert('error', e.message);
    if (els.authSubmitBtn) els.authSubmitBtn.textContent = 'Login';
  } finally {
    if (els.authSubmitBtn) els.authSubmitBtn.disabled = false;
  }
}

async function handleSignup(email, password) {
  try {
    if (els.authSubmitBtn) {
      els.authSubmitBtn.disabled = true;
      els.authSubmitBtn.textContent = 'Sending OTP...';
    }

    const res = await fetch(`${API_BASE}/api/auth/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password })
    });

    const data = await parseJsonResponse(res);
    if (!res.ok) {
      throw new Error(data.detail || data.message || `Error: ${res.status}`);
    }

    pendingOtpEmail = email;
    setAuthMode('verify-signup', { keepValues: true });
    showAuthAlert('ok', data.message || 'OTP sent. Please check your email.');
  } catch (e) {
    showAuthAlert('error', e.message);
    if (els.authSubmitBtn) els.authSubmitBtn.textContent = 'Sign Up';
  } finally {
    if (els.authSubmitBtn) els.authSubmitBtn.disabled = false;
  }
}

async function handleSignupOtpVerify(email, otp) {
  try {
    if (els.authSubmitBtn) {
      els.authSubmitBtn.disabled = true;
      els.authSubmitBtn.textContent = 'Verifying...';
    }

    const res = await fetch(`${API_BASE}/api/auth/verify-registration-otp`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, otp })
    });

    const data = await parseJsonResponse(res);
    if (!res.ok) {
      throw new Error(data.detail || data.message || `Error: ${res.status}`);
    }

    setAuthMode('login');
    showAuthAlert('ok', data.message || 'Account verified. You can now log in.');
  } catch (e) {
    showAuthAlert('error', e.message);
    if (els.authSubmitBtn) els.authSubmitBtn.textContent = 'Verify OTP';
  } finally {
    if (els.authSubmitBtn) els.authSubmitBtn.disabled = false;
  }
}

function resetOtpFlow() {
  setAuthMode(authMode === 'verify-signup' ? 'signup' : 'login', { keepValues: true });
}

// Modified API function that includes auth token
async function apiPostFormAuth(path, formData) {
  const headers = {};
  if (authToken) {
    headers['Authorization'] = `Bearer ${authToken}`;
  }
  
  const res = await fetch(`${API_BASE}${path}`, { 
    method: 'POST', 
    body: formData,
    headers
  });
  
  if (res.status === 401) {
    // Token expired or invalid
    logout();
    throw new Error('Session expired. Please login again.');
  }
  
  const text = await res.text();
  let json;
  try { json = JSON.parse(text); } catch { json = { _raw: text }; }
  if (!res.ok) {
    const err = new Error(`HTTP ${res.status}`);
    err.data = json;
    throw err;
  }
  return json;
}

function setStatus(kind, text) {
  const dot = els.systemStatus.querySelector('.status__dot');
  const t = els.systemStatus.querySelector('.status__text');
  dot.classList.remove('status__dot--warn', 'status__dot--ok', 'status__dot--bad');
  if (kind === 'ok') dot.classList.add('status__dot--ok');
  else if (kind === 'bad') dot.classList.add('status__dot--bad');
  else dot.classList.add('status__dot--warn');
  t.textContent = text;
}

async function apiGet(path) {
  const res = await fetch(`${API_BASE}${path}`);
  const text = await res.text();
  let json;
  try { json = JSON.parse(text); } catch { json = { _raw: text }; }
  if (!res.ok) {
    const err = new Error(`HTTP ${res.status}`);
    err.data = json;
    throw err;
  }
  return json;
}

async function apiGetAuth(path) {
  const headers = {};
  if (authToken) {
    headers['Authorization'] = `Bearer ${authToken}`;
  }
  
  const res = await fetch(`${API_BASE}${path}`, { headers });
  
  if (res.status === 401) {
    logout();
    throw new Error('Session expired. Please login again.');
  }
  
  const text = await res.text();
  let json;
  try { json = JSON.parse(text); } catch { json = { _raw: text }; }
  if (!res.ok) {
    const err = new Error(`HTTP ${res.status}`);
    err.data = json;
    throw err;
  }
  return json;
}

async function apiPatchAuth(path) {
  const headers = {};
  if (authToken) {
    headers['Authorization'] = `Bearer ${authToken}`;
  }
  
  const res = await fetch(`${API_BASE}${path}`, { 
    method: 'PATCH',
    headers 
  });
  
  if (res.status === 401) {
    logout();
    throw new Error('Session expired. Please login again.');
  }
  
  const text = await res.text();
  let json;
  try { json = JSON.parse(text); } catch { json = { _raw: text }; }
  if (!res.ok) {
    const err = new Error(`HTTP ${res.status}`);
    err.data = json;
    throw err;
  }
  return json;
}

async function apiPostForm(path, formData) {
  const res = await fetch(`${API_BASE}${path}`, { method: 'POST', body: formData });
  const text = await res.text();
  let json;
  try { json = JSON.parse(text); } catch { json = { _raw: text }; }
  if (!res.ok) {
    const err = new Error(`HTTP ${res.status}`);
    err.data = json;
    throw err;
  }
  return json;
}

function normalizeText(v) {
  return (v ?? '').toString().toLowerCase();
}

function escapeHtml(value) {
  return (value ?? '').toString().replace(/[&<>"']/g, ch => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[ch]));
}

function getJobState(job) {
  if (job.state) return job.state.toString().trim();

  const parts = (job.location || '')
    .toString()
    .split(',')
    .map(part => part.trim())
    .filter(Boolean);

  while (parts.length && /^\d{5}(?:-\d{4})?$/.test(parts[parts.length - 1])) {
    parts.pop();
  }

  return parts.length > 1 ? parts[parts.length - 1] : '';
}

function getJobSpecialty(job) {
  if (job.specialty) return job.specialty.toString().trim();
  if (job.department && !/^job code:/i.test(job.department)) return job.department.toString().trim();

  const match = (job.description || '').toString().match(/Special(?:ty|ity):\s*([^|\n]+)/i);
  return match ? match[1].trim() : '';
}

function updateSelectOptions(select, values) {
  if (!select) return;

  const currentValue = select.value;
  const uniqueValues = [...new Set(values.map(v => (v || '').toString().trim()).filter(Boolean))]
    .sort((a, b) => a.localeCompare(b));

  select.innerHTML = '<option value="">All</option>' + uniqueValues
    .map(value => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`)
    .join('');

  select.value = uniqueValues.includes(currentValue) ? currentValue : '';
}

function updateJobFilterOptions() {
  updateSelectOptions(els.stateFilter, allJobs.map(getJobState));
  updateSelectOptions(els.specialtyFilter, allJobs.map(getJobSpecialty));
}

function jobMatches(job) {
  const q = normalizeText(els.searchInput.value).trim();
  const status = normalizeText(els.statusFilter.value).trim();
  const state = normalizeText(els.stateFilter ? els.stateFilter.value : '').trim();
  const specialty = normalizeText(els.specialtyFilter ? els.specialtyFilter.value : '').trim();

  if (q) {
    const hay = [job.title, job.department, job.location, job.employment_type, job.specialty, job.state].map(normalizeText).join(' ');
    if (!hay.includes(q)) return false;
  }

  if (status) {
    const s = normalizeText(job.status);
    if (!s.includes(status)) return false;
  }

  if (state) {
    const s = normalizeText(getJobState(job));
    if (s !== state) return false;
  }

  if (specialty) {
    const s = normalizeText(getJobSpecialty(job));
    if (s !== specialty) return false;
  }

  return true;
}

function renderJobs() {
  els.jobsGrid.innerHTML = '';
  const filtered = allJobs.filter(jobMatches);

  // Update job count display
  if (els.jobsCount) {
    const searchTerm = els.searchInput ? els.searchInput.value.trim() : '';
    const statusFilter = els.statusFilter ? els.statusFilter.value : '';
    const stateFilter = els.stateFilter ? els.stateFilter.value : '';
    const specialtyFilter = els.specialtyFilter ? els.specialtyFilter.value : '';
    const hasFilters = searchTerm || statusFilter || stateFilter || specialtyFilter;
    
    if (hasFilters) {
      els.jobsCount.textContent = `Showing ${filtered.length} of ${allJobs.length} jobs`;
    } else {
      els.jobsCount.textContent = `Total Jobs: ${allJobs.length}`;
    }
  }

  // Show/hide empty state and update message
  if (filtered.length === 0) {
    els.jobsEmpty.hidden = false;
    // If jobs are still loading (allJobs is empty), show loading message
    // If jobs loaded but none match filter, show no jobs message
    els.jobsEmpty.textContent = allJobs.length === 0 ? 'Jobs loading...' : 'No jobs found.';
  } else {
    els.jobsEmpty.hidden = true;
  }

  for (const job of filtered) {
    const card = document.createElement('div');
    card.className = 'card';

    const title = document.createElement('div');
    title.className = 'card__title';
    title.textContent = job.title || 'Untitled job';

    const meta = document.createElement('div');
    meta.className = 'card__meta';
    meta.innerHTML = `
      <span class="tag">${job.department || 'Department: N/A'}</span>
      <span class="tag">${job.location || 'Location: N/A'}</span>
      <span class="tag">${job.employment_type || 'Type: N/A'}</span>
      <span class="tag">Status: ${job.status || 'N/A'}</span>
      ${job.end_client ? `<span class="tag">End Client: ${job.end_client}</span>` : ''}
    `;

    const desc = document.createElement('div');
    desc.className = 'card__desc';
    const d = (job.description || '').toString();
    desc.textContent = d.length > 260 ? `${d.slice(0, 260)}…` : d;

    const actions = document.createElement('div');
    actions.className = 'card__actions';

    const btnDetails = document.createElement('button');
    btnDetails.className = 'btn btn--secondary';
    btnDetails.textContent = 'Job detail';
    btnDetails.addEventListener('click', () => openJobDetailModal(job));

    const btnSubmit = document.createElement('button');
    btnSubmit.className = 'btn';
    btnSubmit.textContent = 'Submit resume';
    btnSubmit.addEventListener('click', () => openSubmitModal(job));

    actions.appendChild(btnDetails);
    actions.appendChild(btnSubmit);

    card.appendChild(title);
    card.appendChild(meta);
    card.appendChild(desc);
    card.appendChild(actions);

    els.jobsGrid.appendChild(card);
  }
}

function setView(view) {
  document.querySelectorAll('.nav__item').forEach(btn => {
    btn.classList.toggle('nav__item--active', btn.dataset.view === view);
  });

  els.viewAuth.hidden = view !== 'auth';
  els.viewJobs.hidden = view !== 'jobs';
  els.viewSubmissions.hidden = view !== 'submissions';
  els.viewOffers.hidden = view !== 'offers';
  els.viewDeclines.hidden = view !== 'declines';
  els.viewStarts.hidden = view !== 'starts';
  els.viewSettings.hidden = view !== 'settings';

  if (view === 'auth') {
    els.pageTitle.textContent = 'Authentication';
    els.pageSubtitle.textContent = 'Login or sign up with email verification.';
  }
  if (view === 'jobs') {
    els.pageTitle.textContent = 'Jobs';
    els.pageSubtitle.textContent = 'Browse open roles and submit resumes per job.';
  }
  if (view === 'submissions') {
    els.pageTitle.textContent = 'Submissions';
    els.pageSubtitle.textContent = 'Recent candidate submissions.';
    loadSubmissions();
  }
  if (view === 'offers') {
    els.pageTitle.textContent = 'Offers';
    els.pageSubtitle.textContent = 'Track candidate offers sent to clients.';
    loadOffers();
  }
  if (view === 'declines') {
    els.pageTitle.textContent = 'Declines';
    els.pageSubtitle.textContent = 'Track declined offers and rejections.';
    loadDeclines();
  }
  if (view === 'starts') {
    els.pageTitle.textContent = 'Starts';
    els.pageSubtitle.textContent = 'Track candidates who have started working.';
    loadStarts();
  }
  if (view === 'settings') {
    els.pageTitle.textContent = 'System';
    els.pageSubtitle.textContent = 'Ceipal connectivity and cached raw responses.';
    // Show admin user panel only for admin
    if (els.adminUserPanel) {
      els.adminUserPanel.hidden = !isAdmin();
      if (isAdmin()) {
        loadWhitelistedUsers();
      }
    }
    loadSystem();
  }
}

function showAlert(kind, msg) {
  els.formAlert.hidden = false;
  els.formAlert.className = `alert ${kind === 'ok' ? 'alert--ok' : 'alert--error'}`;
  els.formAlert.textContent = msg;
}

function clearAlert() {
  els.formAlert.hidden = true;
  els.formAlert.textContent = '';
}

function openJobDetailModal(job) {
  activeJobId = job.id;
  activeJob = job;
  els.jobDetailTitle.textContent = `Job Details • ${job.title || job.id}`;
  els.jobDetailJobTitle.textContent = job.title || 'Selected job';
  
  const parts = [
    job.department ? `Dept: ${job.department}` : null,
    job.location ? `Location: ${job.location}` : null,
    job.employment_type ? `Type: ${job.employment_type}` : null,
    job.status ? `Status: ${job.status}` : null,
    job.salary_range ? `Salary: ${job.salary_range}` : null,
  ].filter(Boolean);
  els.jobDetailMeta.textContent = parts.join(' • ') || '—';
  
  els.jobDetailFullDesc.textContent = job.description || 'No description available.';
  
  // Show/hide candidates panel based on admin status (only admin sees submitted candidates)
  const candidatesPanel = document.getElementById('jobDetailCandidatesPanel');
  if (candidatesPanel) {
    candidatesPanel.hidden = !isAdmin();
  }
  
  els.jobDetailModal.hidden = false;
  
  // Only load candidates for admin
  if (isAdmin()) {
    loadJobDetailCandidates(activeJobId);
  }
}

function closeJobDetailModal() {
  els.jobDetailModal.hidden = true;
}

function openSubmitModal(job) {
  activeJobId = job.id;
  activeJob = job;
  els.submitModalTitle.textContent = `Submit Resume • ${job.title || job.id}`;
  els.submitJobTitle.textContent = job.title || 'Selected job';
  
  const parts = [
    job.department ? `Dept: ${job.department}` : null,
    job.location ? `Location: ${job.location}` : null,
    job.status ? `Status: ${job.status}` : null,
  ].filter(Boolean);
  els.submitJobMeta.textContent = parts.join(' • ') || '—';
  
  // Clear all fields
  els.candName.value = '';
  els.candEmail.value = '';
  els.candPhone.value = '';
  els.candBillRate.value = '';
  els.candLocation.value = '';
  els.candSkills.value = '';
  els.candJobTitle.value = '';
  els.candExperience.value = '';
  els.candStartDate.value = '';
  els.candRTO.value = '';
  els.candSummary.value = '';
  els.candResume.value = '';
  clearAlert();
  els.submitModal.hidden = false;
}

function closeSubmitModal() {
  els.submitModal.hidden = true;
  activeJobId = null;
  activeJob = null;
}

// Job Detail modal close handlers
els.jobDetailModal.addEventListener('click', (e) => {
  const node = e.target;
  if (!node) return;
  if (node.dataset && node.dataset.close === 'jobDetail') {
    closeJobDetailModal();
    return;
  }
  if (node.classList && node.classList.contains('modal__backdrop')) {
    closeJobDetailModal();
  }
});

// Job Detail Submit button opens Submit Modal
if (els.jobDetailSubmitBtn) {
  els.jobDetailSubmitBtn.addEventListener('click', () => {
    closeJobDetailModal();
    if (activeJob) openSubmitModal(activeJob);
  });
}

// Submit modal close handlers
els.submitModal.addEventListener('click', (e) => {
  const node = e.target;
  if (!node) return;
  if (node.dataset && node.dataset.close === 'submit') {
    closeSubmitModal();
    return;
  }
  if (node.classList && node.classList.contains('modal__backdrop')) {
    closeSubmitModal();
  }
});

// Close on Escape for both modals
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (els.jobDetailModal && !els.jobDetailModal.hidden) closeJobDetailModal();
    if (els.submitModal && !els.submitModal.hidden) closeSubmitModal();
  }
});

async function loadJobDetailCandidates(jobId) {
  if (!els.jobDetailCandidates) return;
  try {
    const data = await apiGet(`/api/candidates/job/${encodeURIComponent(jobId)}`);
    const candidates = data.candidates || [];
    els.jobDetailCandidates.textContent = candidates.length 
      ? JSON.stringify(candidates, null, 2) 
      : 'No submissions yet for this job.';
  } catch (e) {
    els.jobDetailCandidates.textContent = 'Failed to load candidates for this job.';
  }
}

async function submitResume({ closeAfter }) {
  clearAlert();

  const name = els.candName.value.trim();
  const email = els.candEmail.value.trim();
  const phone = els.candPhone.value.trim();
  const billRate = els.candBillRate.value.trim();
  const location = els.candLocation.value.trim();
  const skills = els.candSkills.value.trim();
  const jobTitle = els.candJobTitle.value.trim();
  const experience = els.candExperience.value.trim();
  const startDate = els.candStartDate.value;
  const rto = els.candRTO.value.trim();
  const summary = els.candSummary.value.trim();
  const file = els.candResume.files && els.candResume.files[0];

  if (!activeJobId) return showAlert('error', 'No job selected.');
  if (!name) return showAlert('error', 'Candidate name is required.');
  if (!email) return showAlert('error', 'Email is required.');
  if (!phone) return showAlert('error', 'Phone is required.');
  if (!billRate) return showAlert('error', 'Bill Rate is required.');
  if (!location) return showAlert('error', 'Current Location is required.');
  if (!skills) return showAlert('error', 'Primary Skills is required.');
  if (!jobTitle) return showAlert('error', 'Job Title is required.');
  if (!experience) return showAlert('error', 'Years of Experience is required.');
  if (!startDate) return showAlert('error', 'Tentative Start Date is required.');
  if (!rto) return showAlert('error', 'RTO is required.');
  if (!summary) return showAlert('error', 'Candidate Summary is required.');
  if (!file) return showAlert('error', 'Resume file is required.');

  const fd = new FormData();
  fd.append('candidate_name', name);
  fd.append('email', email);
  fd.append('phone', phone);
  fd.append('bill_rate', billRate);
  fd.append('current_location', location);
  fd.append('primary_skills', skills);
  fd.append('job_title', jobTitle);
  fd.append('years_experience', experience);
  fd.append('tentative_start_date', startDate);
  fd.append('rto', rto);
  fd.append('candidate_summary', summary);
  fd.append('job_id', activeJobId);
  fd.append('resume', file);

  if (els.submitAnotherBtn) els.submitAnotherBtn.disabled = true;
  if (els.submitCloseBtn) els.submitCloseBtn.disabled = true;
  if (els.submitAnotherBtn) els.submitAnotherBtn.textContent = 'Submitting…';
  if (els.submitCloseBtn) els.submitCloseBtn.textContent = 'Submitting…';

  try {
    const res = await apiPostFormAuth('/api/candidates/submit', fd);
    // Don't show candidate ID in success message
    showAlert('ok', `Submitted successfully by ${res.submitted_by || 'you'}.`);
    if (closeAfter) {
      setTimeout(() => closeSubmitModal(), 650);
    } else {
      // prepare for another submission
      els.candName.value = '';
      els.candEmail.value = '';
      els.candPhone.value = '';
      els.candBillRate.value = '';
      els.candLocation.value = '';
      els.candSkills.value = '';
      els.candJobTitle.value = '';
      els.candExperience.value = '';
      els.candStartDate.value = '';
      els.candRTO.value = '';
      els.candSummary.value = '';
      els.candResume.value = '';
    }
  } catch (e) {
    const detail = e.data ? JSON.stringify(e.data) : e.message;
    showAlert('error', `Submission failed: ${detail}`);
  } finally {
    if (els.submitAnotherBtn) {
      els.submitAnotherBtn.disabled = false;
      els.submitAnotherBtn.textContent = 'Submit & Add Another';
    }
    if (els.submitCloseBtn) {
      els.submitCloseBtn.disabled = false;
      els.submitCloseBtn.textContent = 'Submit & Close';
    }
  }
}

if (els.submitAnotherBtn) {
  els.submitAnotherBtn.addEventListener('click', () => submitResume({ closeAfter: false }));
}
if (els.submitCloseBtn) {
  els.submitCloseBtn.addEventListener('click', () => submitResume({ closeAfter: true }));
}

let jobsPollInterval = null;

async function loadJobs() {
  els.jobsGrid.innerHTML = '<div class="loading-jobs">Loading jobs...<br><small>Please wait...</small></div>';
  els.jobsEmpty.hidden = true;
  
  // Reset infinite scroll state
  isLoadingMore = false;
  
  // Clear any existing poll
  if (jobsPollInterval) {
    clearInterval(jobsPollInterval);
    jobsPollInterval = null;
  }
  
  try {
    // Fetch jobs (returns immediately with cached data, background fetch continues)
    const data = await apiGetAuth('/api/jobs');
    allJobs = data.jobs || [];
    hasMoreJobs = data.has_more || false;
    console.log(`[Jobs] Loaded ${allJobs.length} jobs. Has more: ${hasMoreJobs}`);
    updateJobFilterOptions();
    renderJobs();
    
    // If no jobs yet or still fetching more, start polling
    if (allJobs.length === 0 || hasMoreJobs) {
      let pollCount = 0;
      const maxPolls = 60; // Poll for up to 3 minutes (60 * 3s)
      
      jobsPollInterval = setInterval(async () => {
        pollCount++;
        console.log(`[Jobs] Polling for jobs... attempt ${pollCount}`);
        
        try {
          const pollData = await apiGetAuth('/api/jobs');
          const newJobs = pollData.jobs || [];
          
          if (newJobs.length > allJobs.length) {
            // New jobs found!
            allJobs = newJobs;
            hasMoreJobs = pollData.has_more || false;
            console.log(`[Jobs] Updated: now ${allJobs.length} jobs`);
            updateJobFilterOptions();
            renderJobs();
          }
          
          // Stop polling if we have jobs and no more to fetch
          if (allJobs.length > 0 && !hasMoreJobs) {
            console.log('[Jobs] All jobs loaded, stopping poll');
            clearInterval(jobsPollInterval);
            jobsPollInterval = null;
          }
          
          // Stop after max polls to avoid infinite polling
          if (pollCount >= maxPolls) {
            console.log('[Jobs] Max polls reached, stopping');
            clearInterval(jobsPollInterval);
            jobsPollInterval = null;
          }
        } catch (err) {
          console.error('[Jobs] Poll error:', err);
        }
      }, 3000); // Poll every 3 seconds
    }
  } catch (e) {
    allJobs = [];
    hasMoreJobs = false;
    renderJobs();
  }
}

async function loadMoreJobs() {
  if (isLoadingMore || !hasMoreJobs) return;
  
  isLoadingMore = true;
  showLoadingSpinner();
  
  try {
    console.log(`[Jobs] Loading more from page ${nextStartPage}...`);
    const data = await apiGet(`/api/jobs/load-more?start_page=${nextStartPage}&max_pages=25`);
    const newJobs = data.jobs || [];
    
    if (newJobs.length > 0) {
      allJobs = [...allJobs, ...newJobs];
      nextStartPage = data.next_start_page || nextStartPage + 25;
      hasMoreJobs = data.has_more || false;
      console.log(`[Jobs] Loaded ${newJobs.length} more jobs. Total: ${allJobs.length}. Has more: ${hasMoreJobs}`);
      updateJobFilterOptions();
      renderJobs();
    } else {
      hasMoreJobs = false;
      console.log('[Jobs] No more jobs to load');
    }
  } catch (e) {
    console.error('Failed to load more jobs:', e);
    hasMoreJobs = false;
    hideLoadingSpinner();
  } finally {
    isLoadingMore = false;
  }
}

function showLoadingSpinner() {
  let spinner = document.getElementById('loadMoreSpinner');
  if (!spinner) {
    spinner = document.createElement('div');
    spinner.id = 'loadMoreSpinner';
    spinner.className = 'loading-spinner';
    spinner.innerHTML = '<div class="spinner"></div><span>Loading more jobs...</span>';
    // Make spinner full width of grid, visible, and positioned at the bottom
    spinner.style.cssText = 'display:flex;align-items:center;justify-content:center;gap:12px;padding:30px;color:#6b7280;grid-column:1/-1;width:100%;background:#f8fafc;border-top:1px solid #e5e7eb;margin-top:20px;';
    els.jobsGrid.appendChild(spinner);
  }
  spinner.style.display = 'flex';
}

function hideLoadingSpinner() {
  const spinner = document.getElementById('loadMoreSpinner');
  if (spinner) spinner.style.display = 'none';
}

async function loadCeipalStatus() {
  try {
    const data = await apiGet('/api/ceipal/test');
    if (data.status === 'success') setStatus('ok', 'Ceipal connected');
    else setStatus('bad', `Ceipal failed: ${data.detail || data.message || 'unknown'}`);
  } catch (e) {
    setStatus('bad', 'Backend offline');
  }
}

function openSystemView(text) {
  setView('settings');
  els.systemJson.textContent = text;
}

async function loadSystem() {
  try {
    const cache = await apiGet('/api/ceipal/cache');
    els.systemJson.textContent = JSON.stringify(cache, null, 2);
  } catch (e) {
    els.systemJson.textContent = e.data ? JSON.stringify(e.data, null, 2) : e.message;
  }
}

async function loadSubmissions() {
  // Clear table immediately to prevent showing stale data
  els.submissionsTable.innerHTML = '<div class="loading-jobs">Loading submissions...</div>';
  
  try {
    const data = await apiGetAuth('/api/candidates');
    const items = data.candidates || [];
    if (!items.length) {
      els.submissionsTable.innerHTML = '<div class="empty">No submissions yet.</div>';
      return;
    }

    const isUserAdmin = isAdmin();
    
    const rows = items.map(c => `
      <tr data-candidate='${JSON.stringify(c).replace(/'/g, "\\'")}'>
        <td>${c.id || ''}</td>
        <td><a href="#" class="candidate-name-link" style="color: #7c3aed; text-decoration: underline; cursor: pointer;">${c.name || ''}</a></td>
        <td>${c.email || ''}</td>
        <td>${c.phone || ''}</td>
        <td>${c.job_id || ''}</td>
        ${isUserAdmin ? `<td><a href="#" class="vendor-name-link" data-vendor='${JSON.stringify(c.submitted_by || {}).replace(/'/g, "\\'")}' style="color: #7c3aed; text-decoration: underline; cursor: pointer;">${c.submitted_by?.full_name || c.submitted_by_user_id || 'Unknown'}</a></td>` : ''}
        <td>${c.submitted_date || ''}</td>
        <td>
          ${isUserAdmin ? `
          <select class="status-dropdown" data-candidate-id="${c.id}" style="padding:4px 8px;border-radius:4px;border:1px solid var(--border);background:white;cursor:pointer;">
            <option value="submitted" ${c.status === 'submitted' ? 'selected' : ''}>Submitted</option>
            <option value="offer" ${c.status === 'offer' ? 'selected' : ''}>Offer</option>
            <option value="decline" ${c.status === 'decline' ? 'selected' : ''}>Decline</option>
            <option value="start" ${c.status === 'start' ? 'selected' : ''}>Start</option>
          </select>
          ` : `<span class="status-badge status-${c.status || 'submitted'}">${(c.status || 'submitted').toUpperCase()}</span>`}
        </td>
        <td><button class="btn btn--secondary view-resume-btn" data-path="${c.resume_path || ''}">View Resume</button></td>
        ${isUserAdmin ? `<td><button class="btn btn--secondary email-vendor-btn" data-candidate-id="${c.id}">Email Vendor</button></td>` : ''}
      </tr>
    `).join('');

    els.submissionsTable.innerHTML = `
      <div class="panel">
        <div class="panel__title">Submissions (${items.length})</div>
        <div class="panel__desc">${isUserAdmin ? 'All submissions by all vendors.' : 'Your submissions only.'}</div>
        <div class="code table-container">
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Name</th>
                <th>Email</th>
                <th>Phone</th>
                <th>Job</th>
                ${isUserAdmin ? '<th>Submitted By</th>' : ''}
                <th>Submitted</th>
                <th>Status</th>
                <th>Resume</th>
                ${isUserAdmin ? '<th>Action</th>' : ''}
              </tr>
            </thead>
            <tbody>
              ${rows}
            </tbody>
          </table>
        </div>
      </div>
    `;
    
    // Add event listeners for view resume buttons
    document.querySelectorAll('.view-resume-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        const candidateId = e.target.closest('tr').querySelector('td').textContent;
        if (candidateId) {
          // Open the resume download endpoint
          window.open(`${API_BASE}/api/resumes/${candidateId}`, '_blank');
        }
      });
    });
    
    // Add event listeners for status dropdown changes (admin only)
    if (isUserAdmin) {
      document.querySelectorAll('.status-dropdown').forEach(dropdown => {
        dropdown.addEventListener('change', async (e) => {
          const candidateId = e.target.dataset.candidateId;
          const newStatus = e.target.value;
          try {
            await apiPatchAuth(`/api/candidates/${candidateId}/status?status=${newStatus}`);
            showAlert('ok', `Status updated to ${newStatus}`);
          } catch (err) {
            showAlert('err', 'Failed to update status');
            // Revert to original status on error
            e.target.value = e.target.querySelector('option[selected]')?.value || 'submitted';
          }
        });
      });

      // Email Vendor buttons (admin only) — open compose modal pre-filled with candidate context.
      document.querySelectorAll('.email-vendor-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
          const row = e.target.closest('tr');
          const candidate = JSON.parse(row.dataset.candidate);
          showEmailVendorModal(candidate);
        });
      });
    }
    
    // Add event listeners for candidate name clicks (show candidate details)
    document.querySelectorAll('.candidate-name-link').forEach(link => {
      link.addEventListener('click', (e) => {
        e.preventDefault();
        const row = e.target.closest('tr');
        const candidateData = JSON.parse(row.dataset.candidate);
        showCandidateDetailsModal(candidateData);
      });
    });
    
    // Add event listeners for vendor name clicks (show vendor details) - admin only
    document.querySelectorAll('.vendor-name-link').forEach(link => {
      link.addEventListener('click', (e) => {
        e.preventDefault();
        const vendorData = JSON.parse(e.currentTarget.dataset.vendor);
        showVendorDetailsModal(vendorData);
      });
    });
  } catch (e) {
    els.submissionsTable.innerHTML = `<div class="empty">Failed to load submissions.</div>`;
  }
}

async function loadOffers() {
  await loadCandidatesByStatus('offer', els.offersTable, 'Offers', 'Candidates with offer status.');
}

async function loadDeclines() {
  await loadCandidatesByStatus('decline', els.declinesTable, 'Declines', 'Candidates who declined or were rejected.');
}

async function loadStarts() {
  await loadCandidatesByStatus('start', els.startsTable, 'Starts', 'Candidates who have started working.');
}

async function loadCandidatesByStatus(status, container, title, description) {
  container.innerHTML = '<div class="loading-jobs">Loading...</div>';
  
  try {
    const data = await apiGetAuth('/api/candidates');
    const isUserAdmin = isAdmin();
    let items = (data.candidates || []).filter(c => c.status === status);
    
    // For non-admin vendors, only show their own candidates
    if (!isUserAdmin && currentUser) {
      items = items.filter(c => c.submitted_by_user_id === currentUser.id);
    }
    
    if (!items.length) {
      container.innerHTML = `<div class="empty">No ${title.toLowerCase()} yet.</div>`;
      return;
    }
    
    const rows = items.map(c => `
      <tr data-candidate='${JSON.stringify(c).replace(/'/g, "\\'")}'>
        <td>${c.id || ''}</td>
        <td><a href="#" class="candidate-name-link" style="color: #7c3aed; text-decoration: underline; cursor: pointer;">${c.name || ''}</a></td>
        <td>${c.email || ''}</td>
        <td>${c.phone || ''}</td>
        <td>${c.job_id || ''}</td>
        ${isUserAdmin ? `<td><a href="#" class="vendor-name-link" data-vendor='${JSON.stringify(c.submitted_by || {}).replace(/'/g, "\\'")}' style="color: #7c3aed; text-decoration: underline; cursor: pointer;">${c.submitted_by?.full_name || c.submitted_by_user_id || 'Unknown'}</a></td>` : ''}
        <td>${c.submitted_date || ''}</td>
        <td><button class="btn btn--secondary view-resume-btn" data-path="${c.resume_path || ''}">View Resume</button></td>
      </tr>
    `).join('');
    
    container.innerHTML = `
      <div class="panel">
        <div class="panel__title">${title} (${items.length})</div>
        <div class="panel__desc">${description}</div>
        <div class="code table-container">
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Name</th>
                <th>Email</th>
                <th>Phone</th>
                <th>Job</th>
                ${isUserAdmin ? '<th>Submitted By</th>' : ''}
                <th>Submitted</th>
                <th>Resume</th>
              </tr>
            </thead>
            <tbody>
              ${rows}
            </tbody>
          </table>
        </div>
      </div>
    `;
    
    // Add event listeners
    container.querySelectorAll('.view-resume-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        const candidateId = e.target.closest('tr').querySelector('td').textContent;
        if (candidateId) {
          window.open(`${API_BASE}/api/resumes/${candidateId}`, '_blank');
        }
      });
    });
    
    container.querySelectorAll('.candidate-name-link').forEach(link => {
      link.addEventListener('click', (e) => {
        e.preventDefault();
        const row = e.target.closest('tr');
        const candidateData = JSON.parse(row.dataset.candidate);
        showCandidateDetailsModal(candidateData);
      });
    });
    
    if (isUserAdmin) {
      container.querySelectorAll('.vendor-name-link').forEach(link => {
        link.addEventListener('click', (e) => {
          e.preventDefault();
          const vendorData = JSON.parse(e.currentTarget.dataset.vendor);
          showVendorDetailsModal(vendorData);
        });
      });
    }
  } catch (e) {
    container.innerHTML = `<div class="empty">Failed to load ${title.toLowerCase()}.</div>`;
  }
}

function showCandidateDetailsModal(candidate) {
  // Remove any existing modal first
  closeCandidateDetailModal();
  
  const details = `
    <div style="margin-bottom: 12px;"><strong>ID:</strong> ${candidate.id || 'N/A'}</div>
    <div style="margin-bottom: 12px;"><strong>Name:</strong> ${candidate.name || 'N/A'}</div>
    <div style="margin-bottom: 12px;"><strong>Email:</strong> ${candidate.email || 'N/A'}</div>
    <div style="margin-bottom: 12px;"><strong>Phone:</strong> ${candidate.phone || 'N/A'}</div>
    <div style="margin-bottom: 12px;"><strong>Job ID:</strong> ${candidate.job_id || 'N/A'}</div>
    <div style="margin-bottom: 12px;"><strong>Job Title:</strong> ${candidate.job_title || 'N/A'}</div>
    <div style="margin-bottom: 12px;"><strong>Primary Skills:</strong> ${candidate.primary_skills || 'N/A'}</div>
    <div style="margin-bottom: 12px;"><strong>Location:</strong> ${candidate.current_location || 'N/A'}</div>
    <div style="margin-bottom: 12px;"><strong>Experience:</strong> ${candidate.years_experience || 'N/A'}</div>
    <div style="margin-bottom: 12px;"><strong>Bill Rate:</strong> ${candidate.bill_rate || 'N/A'}</div>
    <div style="margin-bottom: 12px;"><strong>Tentative Start Date:</strong> ${candidate.tentative_start_date || 'N/A'}</div>
    <div style="margin-bottom: 12px;"><strong>RTO:</strong> ${candidate.rto || 'N/A'}</div>
    <div style="margin-bottom: 12px;"><strong>Candidate Summary:</strong> ${candidate.candidate_summary || 'N/A'}</div>
    <div style="margin-bottom: 12px;"><strong>Status:</strong> ${candidate.status || 'N/A'}</div>
    <div style="margin-bottom: 12px;"><strong>Submitted Date:</strong> ${candidate.submitted_date || 'N/A'}</div>
    <div style="margin-bottom: 12px;"><strong>Resume:</strong> <button class="btn btn--secondary" onclick="window.open('${API_BASE}/api/resumes/${candidate.id}', '_blank')">View Resume</button></div>
  `;
  
  const modal = document.createElement('div');
  modal.className = 'modal';
  modal.id = 'candidateDetailModal';
  modal.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;z-index:10000;display:flex;align-items:center;justify-content:center;';
  modal.innerHTML = `
    <div class="modal__overlay" onclick="closeCandidateDetailModal()" style="position:absolute;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);z-index:1;"></div>
    <div class="modal__content" style="position:relative;z-index:2;max-width:600px;max-height:85vh;background:white;border-radius:12px;box-shadow:0 20px 60px rgba(0,0,0,0.3);display:flex;flex-direction:column;">
      <div class="modal__header" style="padding:16px 20px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;flex-shrink:0;">
        <h3 style="margin:0;font-size:18px;">Candidate Details</h3>
        <button class="modal__close" onclick="closeCandidateDetailModal()" style="background:none;border:none;font-size:24px;cursor:pointer;color:#6b7280;">&times;</button>
      </div>
      <div class="modal__body" style="padding:20px;overflow-y:auto;flex:1;">
        ${details}
      </div>
      <div class="modal__footer" style="padding:16px 20px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;flex-shrink:0;">
        <button class="btn btn--secondary" onclick="closeCandidateDetailModal()">Close</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
}

function closeCandidateDetailModal() {
  const modal = document.getElementById('candidateDetailModal');
  if (modal) modal.remove();
}

function showVendorDetailsModal(vendor) {
  // Remove any existing modal first
  closeVendorDetailModal();
  
  const details = `
    <div style="margin-bottom: 16px;"><strong>Name:</strong> ${vendor.full_name || vendor.name || 'N/A'}</div>
    <div style="margin-bottom: 16px;"><strong>Email:</strong> ${vendor.email || 'N/A'}</div>
    <div style="margin-bottom: 16px;"><strong>ID:</strong> ${vendor.id || 'N/A'}</div>
  `;
  
  const modal = document.createElement('div');
  modal.className = 'modal';
  modal.id = 'vendorDetailModal';
  modal.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;z-index:10000;display:flex;align-items:center;justify-content:center;';
  modal.innerHTML = `
    <div class="modal__overlay" onclick="closeVendorDetailModal()" style="position:absolute;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);z-index:1;"></div>
    <div class="modal__content" style="position:relative;z-index:2;max-width:400px;background:white;border-radius:12px;box-shadow:0 20px 60px rgba(0,0,0,0.3);">
      <div class="modal__header" style="padding:16px 20px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;">
        <h3 style="margin:0;font-size:18px;">Vendor Details</h3>
        <button class="modal__close" onclick="closeVendorDetailModal()" style="background:none;border:none;font-size:24px;cursor:pointer;color:#6b7280;">&times;</button>
      </div>
      <div class="modal__body" style="padding:20px;">
        ${details}
      </div>
      <div class="modal__footer" style="padding:16px 20px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;">
        <button class="btn btn--secondary" onclick="closeVendorDetailModal()">Close</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
}

function closeVendorDetailModal() {
  const modal = document.getElementById('vendorDetailModal');
  if (modal) modal.remove();
}

function showEmailVendorModal(candidate) {
  closeEmailVendorModal();

  const vendorName = candidate.submitted_by_name || 'Vendor';
  const vendorEmail = candidate.submitted_by_email || '';
  const candidateName = candidate.name || '';
  const jobTitle = candidate.job_title || '';
  const jobId = candidate.job_id || '';
  const candidateId = candidate.id || '';
  const defaultSubject = `Update on your submission: ${candidateName} for ${jobTitle}`;

  const modal = document.createElement('div');
  modal.className = 'modal';
  modal.id = 'emailVendorModal';
  modal.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;z-index:10000;display:flex;align-items:center;justify-content:center;';
  modal.innerHTML = `
    <div class="modal__overlay" onclick="closeEmailVendorModal()" style="position:absolute;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);z-index:1;"></div>
    <div class="modal__content" style="position:relative;z-index:2;width:560px;max-width:92vw;max-height:85vh;background:white;border-radius:12px;box-shadow:0 20px 60px rgba(0,0,0,0.3);display:flex;flex-direction:column;">
      <div class="modal__header" style="padding:16px 20px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;flex-shrink:0;">
        <h3 style="margin:0;font-size:18px;">Email Vendor</h3>
        <button class="modal__close" onclick="closeEmailVendorModal()" style="background:none;border:none;font-size:24px;cursor:pointer;color:#6b7280;">&times;</button>
      </div>
      <div class="modal__body" style="padding:20px;overflow-y:auto;flex:1;">
        <div style="margin-bottom:12px;color:#374151;font-size:13px;">
          <div><strong>To:</strong> ${vendorName} &lt;${vendorEmail || '<em>missing</em>'}&gt;</div>
          <div><strong>Re:</strong> ${candidateName} — ${jobTitle} (${jobId})</div>
        </div>
        <label style="display:block;margin-top:12px;font-weight:600;font-size:13px;">Subject</label>
        <input id="emailVendorSubject" type="text" value="${defaultSubject.replace(/"/g, '&quot;')}" style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;margin-top:4px;" />
        <label style="display:block;margin-top:12px;font-weight:600;font-size:13px;">Message</label>
        <textarea id="emailVendorMessage" rows="8" placeholder="Write your message to the vendor..." style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;margin-top:4px;font-family:inherit;font-size:14px;resize:vertical;"></textarea>
        <div id="emailVendorError" style="color:#dc2626;font-size:13px;margin-top:8px;display:none;"></div>
      </div>
      <div class="modal__footer" style="padding:16px 20px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;gap:8px;flex-shrink:0;">
        <button class="btn btn--secondary" onclick="closeEmailVendorModal()">Cancel</button>
        <button class="btn btn--primary" id="emailVendorSendBtn" data-candidate-id="${candidateId}" ${vendorEmail ? '' : 'disabled'}>Send</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);

  document.getElementById('emailVendorSendBtn').addEventListener('click', async (e) => {
    const btn = e.target;
    const subject = (document.getElementById('emailVendorSubject').value || '').trim();
    const message = (document.getElementById('emailVendorMessage').value || '').trim();
    const errEl = document.getElementById('emailVendorError');
    errEl.style.display = 'none';
    if (!subject || !message) {
      errEl.textContent = 'Subject and message are required.';
      errEl.style.display = 'block';
      return;
    }
    btn.disabled = true;
    btn.textContent = 'Sending...';

    // Abort the request after 45s so the modal never appears hung. SendGrid normally responds in
    // under 5s; anything longer almost certainly means a server-side problem worth surfacing.
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 45000);
    try {
      const res = await fetch(`${API_BASE}/api/candidates/${encodeURIComponent(btn.dataset.candidateId)}/notify-vendor`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(authToken ? { 'Authorization': `Bearer ${authToken}` } : {}),
        },
        body: JSON.stringify({ subject, message }),
        signal: controller.signal,
      });
      clearTimeout(timeoutId);
      if (res.status === 401) { logout(); return; }
      const text = await res.text();
      let json; try { json = JSON.parse(text); } catch { json = { detail: text }; }
      if (!res.ok) throw new Error(json.detail || `HTTP ${res.status}`);
      showAlert('ok', `Email sent to ${json.vendor_email}`);
      closeEmailVendorModal();
    } catch (err) {
      clearTimeout(timeoutId);
      const msg = err.name === 'AbortError'
        ? 'Request timed out after 45s. Check Render logs for [NotifyVendor] / [VendorMail] entries.'
        : (err.message || 'Failed to send email.');
      errEl.textContent = msg;
      errEl.style.display = 'block';
      btn.disabled = false;
      btn.textContent = 'Send';
    }
  });
}

function closeEmailVendorModal() {
  const modal = document.getElementById('emailVendorModal');
  if (modal) modal.remove();
}

// UI events
els.searchInput.addEventListener('input', renderJobs);
els.statusFilter.addEventListener('change', renderJobs);
if (els.stateFilter) els.stateFilter.addEventListener('change', renderJobs);
if (els.specialtyFilter) els.specialtyFilter.addEventListener('change', renderJobs);
els.refreshBtn.addEventListener('click', async () => {
  await loadCeipalStatus();
  await loadJobs();
});
els.ceipalCacheBtn.addEventListener('click', async () => {
  try {
    const cache = await apiGet('/api/ceipal/cache');
    openSystemView(JSON.stringify(cache, null, 2));
  } catch (e) {
    openSystemView(e.data ? JSON.stringify(e.data, null, 2) : e.message);
  }
});

if (els.ceipalTestBtn) {
  els.ceipalTestBtn.addEventListener('click', async () => {
    try {
      const res = await apiGet('/api/ceipal/test');
      els.systemJson.textContent = JSON.stringify(res, null, 2);
      await loadCeipalStatus();
    } catch (e) {
      els.systemJson.textContent = e.data ? JSON.stringify(e.data, null, 2) : e.message;
    }
  });
}

if (els.openDocsBtn) {
  els.openDocsBtn.addEventListener('click', () => window.open(`${API_BASE}/docs`, '_blank'));
}

// Nav
document.querySelectorAll('.nav__item').forEach(btn => {
  btn.addEventListener('click', () => setView(btn.dataset.view));
});

if (els.logoutBtn) {
  els.logoutBtn.addEventListener('click', logout);
}

if (els.authSubmitBtn) {
  els.authSubmitBtn.addEventListener('click', handleAuthSubmit);
}

if (els.authBackBtn) {
  els.authBackBtn.addEventListener('click', resetOtpFlow);
}

if (els.authToggleLink) {
  els.authToggleLink.addEventListener('click', (e) => {
    e.preventDefault();
    setAuthMode(authMode === 'login' ? 'signup' : 'login');
  });
}

// Password reset event listeners
if (els.forgotPasswordLink) {
  els.forgotPasswordLink.addEventListener('click', (e) => {
    e.preventDefault();
    showForgotPasswordForm();
  });
}

if (els.backToLoginLink) {
  els.backToLoginLink.addEventListener('click', (e) => {
    e.preventDefault();
    showLoginForm();
  });
}

if (els.backToLoginFromForgot) {
  els.backToLoginFromForgot.addEventListener('click', (e) => {
    e.preventDefault();
    showLoginForm();
  });
}

if (els.forgotSubmitBtn) {
  els.forgotSubmitBtn.addEventListener('click', handleForgotPassword);
}

if (els.resetSubmitBtn) {
  els.resetSubmitBtn.addEventListener('click', handlePasswordReset);
}

// Check for reset token in URL (user clicked email link)
const urlParams = new URLSearchParams(window.location.search);
const resetToken = urlParams.get('token');
if (resetToken && els.resetPasswordForm) {
  // Show reset password form directly
  showResetPasswordForm(resetToken);
  // Clear token from URL
  window.history.replaceState({}, document.title, window.location.pathname);
}

// Admin user management event listener
if (els.addUserBtn) {
  els.addUserBtn.addEventListener('click', addUser);
}

// Register toggle removed - only login allowed
// if (els.authToggleBtn) {
//   els.authToggleBtn.addEventListener('click', toggleAuthMode);
// }

// Infinite scroll disabled - all jobs fetched at once on load
// window.addEventListener('scroll', () => {
//   if (!hasMoreJobs || isLoadingMore) return;
//   
//   const scrollTop = window.scrollY || document.documentElement.scrollTop;
//   const windowHeight = window.innerHeight;
//   const documentHeight = document.documentElement.scrollHeight;
//   
//   // Load more when user scrolls to within 200px of bottom
//   if (scrollTop + windowHeight >= documentHeight - 200) {
//     loadMoreJobs();
//   }
// });

// Toast notifications
function showToast(message, type = 'info', duration = 3000) {
  const container = document.getElementById('toastContainer');
  if (!container) return;
  
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  
  const iconSvg = {
    success: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg>',
    error: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
    info: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>'
  }[type];
  
  toast.innerHTML = `${iconSvg}<span>${message}</span>`;
  container.appendChild(toast);
  
  setTimeout(() => {
    toast.classList.add('hiding');
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

// Notifications handling
let notificationsData = { notifications: [], unread_count: 0 };

async function loadNotifications() {
  if (!authToken || !currentUser) return;
  
  try {
    const response = await fetch(`${API_BASE}/api/notifications`, {
      headers: { 'Authorization': `Bearer ${authToken}` }
    });
    
    if (response.ok) {
      notificationsData = await response.json();
      updateNotificationUI();
    }
  } catch (err) {
    console.error('Failed to load notifications:', err);
  }
}

function updateNotificationUI() {
  const bell = document.getElementById('notificationBell');
  const badge = document.getElementById('notificationBadge');
  const list = document.getElementById('notificationList');
  
  if (!bell) return;
  
  // Show/hide based on auth
  bell.hidden = !authToken || !currentUser;
  
  if (!bell.hidden && notificationsData.unread_count > 0) {
    badge.textContent = notificationsData.unread_count > 9 ? '9+' : notificationsData.unread_count;
    badge.hidden = false;
  } else {
    badge.hidden = true;
  }
  
  // Update list
  if (list) {
    if (notificationsData.notifications.length === 0) {
      list.innerHTML = '<div class="notification-empty">No notifications</div>';
    } else {
      list.innerHTML = notificationsData.notifications.map(n => `
        <div class="notification-item ${n.read ? '' : 'unread'}" data-id="${n.id}">
          <div class="notification-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>
              <path d="M13.73 21a2 2 0 0 1-3.46 0"/>
            </svg>
          </div>
          <div class="notification-content">
            <div class="notification-title">Job Closed: ${escapeHtml(n.job_title || 'Unknown Job')}</div>
            <div class="notification-desc">${n.candidate_count} candidate(s) submitted</div>
            <div class="notification-time">${formatTimeAgo(n.created_at)}</div>
          </div>
        </div>
      `).join('');
      
      // Add click handlers
      list.querySelectorAll('.notification-item').forEach(item => {
        item.addEventListener('click', () => markNotificationRead(item.dataset.id));
      });
    }
  }
}

function formatTimeAgo(timestamp) {
  if (!timestamp) return 'Just now';
  const date = new Date(timestamp);
  const now = new Date();
  const diff = Math.floor((now - date) / 1000);
  
  if (diff < 60) return 'Just now';
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} hours ago`;
  return `${Math.floor(diff / 86400)} days ago`;
}

async function markNotificationRead(notificationId) {
  if (!authToken) return;
  
  try {
    const response = await fetch(`${API_BASE}/api/notifications/${notificationId}/read`, {
      method: 'PATCH',
      headers: { 'Authorization': `Bearer ${authToken}` }
    });
    
    if (response.ok) {
      // Update local state
      const notification = notificationsData.notifications.find(n => n.id === notificationId);
      if (notification && !notification.read) {
        notification.read = true;
        notificationsData.unread_count = Math.max(0, notificationsData.unread_count - 1);
        updateNotificationUI();
      }
    }
  } catch (err) {
    console.error('Failed to mark notification as read:', err);
  }
}

async function markAllNotificationsRead() {
  if (!authToken) return;
  
  try {
    const response = await fetch(`${API_BASE}/api/notifications/read-all`, {
      method: 'PATCH',
      headers: { 'Authorization': `Bearer ${authToken}` }
    });
    
    if (response.ok) {
      // Update local state
      notificationsData.notifications.forEach(n => n.read = true);
      notificationsData.unread_count = 0;
      updateNotificationUI();
      showToast('All notifications marked as read', 'success');
    }
  } catch (err) {
    console.error('Failed to mark all notifications as read:', err);
  }
}

// Notification bell click handler
document.addEventListener('DOMContentLoaded', () => {
  const bell = document.getElementById('notificationBell');
  const panel = document.getElementById('notificationPanel');
  const markAllBtn = document.getElementById('markAllReadBtn');
  
  if (bell && panel) {
    bell.addEventListener('click', (e) => {
      e.stopPropagation();
      panel.classList.toggle('show');
      if (panel.classList.contains('show')) {
        loadNotifications();
      }
    });
    
    // Close when clicking outside
    document.addEventListener('click', (e) => {
      if (!bell.contains(e.target)) {
        panel.classList.remove('show');
      }
    });
  }
  
  if (markAllBtn) {
    markAllBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      markAllNotificationsRead();
    });
  }
});

// init
(async function init() {
  updateAuthUI();
  
  if (authToken && currentUser) {
    // Already logged in
    setView('jobs');
    await loadCeipalStatus();
    await loadJobs();
    await loadNotifications();
    // Poll for notifications every minute
    setInterval(loadNotifications, 60000);
  } else {
    // Not logged in - show auth view
    setView('auth');
  }
})();
