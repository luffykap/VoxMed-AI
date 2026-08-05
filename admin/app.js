const API_BASE = '/api';

// DOM Elements
const loginView = document.getElementById('login-view');
const dashboardView = document.getElementById('dashboard-view');
const loginForm = document.getElementById('login-form');
const loginError = document.getElementById('login-error');
const logoutBtn = document.getElementById('logout-btn');

// Sections
const sections = document.querySelectorAll('.content-section');
const navLinks = document.querySelectorAll('.nav-links a');

// Auth state
let authHeader = '';

// Check if already logged in
const savedAuth = sessionStorage.getItem('voxmed_auth');
if (savedAuth) {
    authHeader = savedAuth;
    showDashboard();
}

// ── Authentication ────────────────────────────────────────────────────────
loginForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const user = document.getElementById('username').value;
    const pass = document.getElementById('password').value;
    
    // Create Basic Auth header
    const token = btoa(`${user}:${pass}`);
    const header = `Basic ${token}`;
    
    try {
        // Test auth by fetching stats
        const res = await fetch(`${API_BASE}/dashboard-stats`, {
            headers: { 'Authorization': header }
        });
        
        if (res.ok) {
            authHeader = header;
            sessionStorage.setItem('voxmed_auth', authHeader);
            showDashboard();
        } else {
            loginError.classList.remove('hidden');
        }
    } catch (err) {
        loginError.textContent = "Network error. Make sure server is running.";
        loginError.classList.remove('hidden');
    }
});

logoutBtn.addEventListener('click', () => {
    sessionStorage.removeItem('voxmed_auth');
    authHeader = '';
    dashboardView.classList.add('hidden');
    loginView.classList.remove('hidden');
    loginForm.reset();
});

function showDashboard() {
    loginView.classList.add('hidden');
    dashboardView.classList.remove('hidden');
    loadDashboardData();
}

// ── Navigation ───────────────────────────────────────────────────────────
navLinks.forEach(link => {
    link.addEventListener('click', (e) => {
        e.preventDefault();
        
        // Update active class
        navLinks.forEach(l => l.classList.remove('active'));
        e.target.classList.add('active');
        
        // Show target section
        const targetId = e.target.getAttribute('data-target');
        sections.forEach(sec => sec.classList.add('hidden'));
        document.getElementById(targetId).classList.remove('hidden');
        
        // Reload data if needed
        if (targetId === 'appointments-section') loadAppointments();
        if (targetId === 'slots-section') loadSlotsAndDoctors();
    });
});

// ── API Fetch Wrapper ───────────────────────────────────────────
async function apiFetch(endpoint, options = {}) {
    const res = await fetch(`${API_BASE}${endpoint}`, {
        ...options,
        headers: {
            'Authorization': authHeader,
            'Content-Type': 'application/json',
            ...options.headers
        }
    });

    if (res.status === 401) {
        logoutBtn.click();
        throw new Error('Unauthorized');
    }

    // Throw for any other non-OK response so callers can catch it properly
    if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
    }

    return res.json();
}

// ── Data Loading ──────────────────────────────────────────
async function loadDashboardData() {
    try {
        const stats = await apiFetch('/dashboard-stats');
        document.getElementById('stat-today-appointments').textContent = stats.today_appointments;
        document.getElementById('stat-total-active').textContent = stats.total_active_appointments;
        document.getElementById('stat-total-doctors').textContent = stats.total_doctors;

        await loadAppointments();  // await so stats and table are always in sync
    } catch (err) {
        console.error('loadDashboardData error:', err);
    }
}

async function loadAppointments() {
    try {
        const appointments = await apiFetch('/appointments');
        const tbody = document.querySelector('#appointments-table tbody');
        tbody.innerHTML = '';

        appointments.forEach(app => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>${app.slot_date}</td>
                <td>${app.slot_time}</td>
                <td>${app.patient_name}</td>
                <td>${app.patient_phone || '-'}</td>
                <td>${app.doctor_name}</td>
                <td><span class="status-badge status-${app.status}">${app.status}</span></td>
                <td>
                    ${app.status === 'BOOKED'
                        ? `<button class="btn danger-btn cancel-btn" data-id="${app.id}">Cancel</button>`
                        : '-'}
                </td>
            `;
            tbody.appendChild(tr);
        });
        // NOTE: no per-button listeners here — handled by event delegation below
    } catch (err) {
        console.error('loadAppointments error:', err);
    }
}

// Event delegation: one listener on tbody survives table re-renders and auto-refresh
document.querySelector('#appointments-table tbody').addEventListener('click', async (e) => {
    const btn = e.target.closest('.cancel-btn');
    if (!btn) return;

    // Two-step inline confirmation — no native dialog needed
    if (!btn.dataset.confirming) {
        // First click: enter confirm state
        btn.dataset.confirming = '1';
        btn.textContent = 'Confirm?';
        btn.style.background = '#c0392b';

        // Auto-reset if user clicks elsewhere within 4 seconds
        const reset = () => {
            if (!btn.dataset.confirming) return;
            delete btn.dataset.confirming;
            btn.textContent = 'Cancel';
            btn.style.background = '';
        };
        btn._resetTimer = setTimeout(reset, 4000);
        document.addEventListener('click', function onOutside(ev) {
            if (!btn.contains(ev.target)) {
                clearTimeout(btn._resetTimer);
                reset();
                document.removeEventListener('click', onOutside);
            }
        });
        return;
    }

    // Second click: execute cancel
    clearTimeout(btn._resetTimer);
    delete btn.dataset.confirming;
    const id = btn.getAttribute('data-id');
    btn.disabled = true;
    btn.textContent = 'Cancelling…';
    btn.style.background = '';

    try {
        await apiFetch(`/appointments/${id}`, { method: 'DELETE' });
        await loadDashboardData(); // Refresh stats + table
    } catch (err) {
        alert(`Failed to cancel: ${err.message}`);
        btn.disabled = false;
        btn.textContent = 'Cancel';
    }
});

async function loadSlotsAndDoctors() {
    try {
        const [doctors, slots] = await Promise.all([
            apiFetch('/doctors'),
            apiFetch('/slots')
        ]);
        
        // Populate doctor selects
        const doctorSelect = document.getElementById('slot-doctor');
        const filterSelect = document.getElementById('filter-doctor');
        
        const populateSelect = (select) => {
            const currentVal = select.value;
            select.innerHTML = select.id === 'filter-doctor' ? '<option value="">All Doctors</option>' : '<option value="">Select Doctor</option>';
            doctors.forEach(doc => {
                const opt = document.createElement('option');
                opt.value = doc.id;
                opt.textContent = `${doc.name} (${doc.department})`;
                select.appendChild(opt);
            });
            select.value = currentVal;
        };
        
        populateSelect(doctorSelect);
        populateSelect(filterSelect);
        
        renderSlotsTable(slots);
        
        // Add filter listener
        filterSelect.addEventListener('change', async (e) => {
            const docId = e.target.value;
            const endpoint = docId ? `/slots?doctor_id=${docId}` : '/slots';
            const filteredSlots = await apiFetch(endpoint);
            renderSlotsTable(filteredSlots);
        });
        
    } catch (err) {
        console.error(err);
    }
}

function renderSlotsTable(slots) {
    const tbody = document.querySelector('#slots-table tbody');
    tbody.innerHTML = '';
    
    slots.forEach(slot => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>${slot.doctor_name}</td>
            <td>${slot.slot_date}</td>
            <td>${slot.slot_time}</td>
            <td>
                <span class="status-badge status-${slot.is_booked ? 'BOOKED' : 'COMPLETED'}">
                    ${slot.is_booked ? 'Booked' : 'Free'}
                </span>
            </td>
        `;
        tbody.appendChild(tr);
    });
}



document.getElementById('add-slot-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    
    const data = {
        doctor_id: parseInt(document.getElementById('slot-doctor').value),
        slot_date: document.getElementById('slot-date').value,
        slot_time: document.getElementById('slot-time').value,
        is_booked: false
    };
    
    const msgDiv = document.getElementById('slot-message');
    
    try {
        await apiFetch('/slots', {
            method: 'POST',
            body: JSON.stringify(data)
        });
        
        msgDiv.textContent = 'Slot created successfully!';
        msgDiv.className = 'success-msg';
        
        // Reset form except doctor
        document.getElementById('slot-date').value = '';
        document.getElementById('slot-time').value = '';
        
        // Refresh table
        await loadSlotsAndDoctors();
        
    } catch (err) {
        msgDiv.textContent = 'Failed to create slot. It may already exist.';
        msgDiv.className = 'error-msg';
    }
    
    setTimeout(() => {
        msgDiv.className = 'hidden';
    }, 3000);
});

// ── Auto Refresh ────────────────────────────────────────────────────────
// Poll the API every 5 seconds to provide real-time updates
setInterval(() => {
    // Only refresh if logged in and currently viewing the appointments dashboard
    if (authHeader && !dashboardView.classList.contains('hidden')) {
        const activeSection = document.querySelector('.nav-links a.active').getAttribute('data-target');
        if (activeSection === 'appointments-section') {
            loadDashboardData();
        }
    }
}, 5000);
