// NextStep front-end. All state comes from the Flask API (/api/...) backed by Postgres.
// Every piece of text from the database or the user is passed through esc() before
// it is placed in innerHTML, and every external link must be an https URL that the
// server already validated (safeUrl() double-checks on the client).
'use strict';

let currentUser = null;
let resourcesData = null;
let checklistData = [];
let rewardsData = [];
let scholarshipsData = [];
let savedScholarships = [];      // ids (slug or uuid)
let savedScholarshipItems = [];  // full objects for the sidebar (includes retired items)
let tutorsData = [];
let mentorsData = [];
let internshipsData = [];
let myRequests = [];
let requestTopics = [];
let collegeMeta = null;
let savedColleges = [];
let selectedRedeemItem = null;
let currentFilterAudience = 'All';
let authMode = 'login';
let lastFocus = null;
const pending = new Set();   // in-flight keys to prevent double submits

// ------------------------------------------------------------------ helpers
function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"'`]/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;'
  }[c]));
}
function safeUrl(u) {
  try {
    const url = new URL(u);
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : null;
  } catch (e) { return null; }
}
function extLink(url, label, cls) {
  const u = safeUrl(url);
  if (!u) return '';
  return `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer" class="${cls || 'text-sky-700 underline'}">${esc(label)}<span class="sr-only"> (opens in a new tab)</span></a>`;
}
function money(n) { return (n == null) ? null : '$' + Number(n).toLocaleString('en-US'); }
function pct(x) { return (x == null) ? null : Math.round(x * 100) + '%'; }
function fmtDate(iso) {
  if (!iso) return null;
  const d = new Date(iso + 'T12:00:00');
  return d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
}
function icons() { try { lucide.createIcons(); } catch (e) { /* icon CDN unavailable: text labels still work */ } }

function announce(msg) {
  const live = document.getElementById('status-live');
  live.textContent = '';
  setTimeout(() => { live.textContent = msg; }, 50);
}
let toastTimer = null;
function toast(msg, kind) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'fixed bottom-4 left-1/2 -translate-x-1/2 z-[60] max-w-md w-[calc(100%-2rem)] px-4 py-3 rounded-xl shadow-lg text-sm font-medium ' +
    (kind === 'error' ? 'bg-rose-700 text-white' : 'bg-slate-900 text-white');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), 5000);
  announce(msg);
}

class ApiError extends Error {
  constructor(msg, status, data) { super(msg); this.status = status; this.data = data || {}; }
}
async function api(method, url, body) {
  const opts = { method, headers: { 'Accept': 'application/json' }, credentials: 'same-origin' };
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(url, opts); }
  catch (e) { throw new ApiError('Network error: check your internet connection and try again.', 0); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (res.status === 401 && currentUser && url !== '/api/login') sessionExpired();
    throw new ApiError(data.error || ('Request failed (' + res.status + ')'), res.status, data);
  }
  return data;
}
function sessionExpired() {
  toast('Your session expired. Please sign in again so your progress can be saved.', 'error');
  showAuth();
}

// ------------------------------------------------------------------ modals (accessible)
function openModal(id) {
  lastFocus = document.activeElement;
  const m = document.getElementById(id);
  m.classList.remove('hidden');
  const f = m.querySelector('input, select, textarea, button, a[href]');
  if (f) f.focus();
  icons();
}
function closeModal(id) {
  document.getElementById(id).classList.add('hidden');
  if (lastFocus && document.body.contains(lastFocus)) lastFocus.focus();
}
document.addEventListener('keydown', e => {
  const open = [...document.querySelectorAll('.modal')].find(m => !m.classList.contains('hidden'));
  if (!open) return;
  if (e.key === 'Escape') { closeModal(open.id); return; }
  if (e.key === 'Tab') {  // keep focus inside the dialog
    const f = [...open.querySelectorAll('a[href], button:not([disabled]), input, select, textarea')].filter(x => x.offsetParent !== null);
    if (!f.length) return;
    if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
    else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
  }
});
document.addEventListener('click', e => { if (e.target.classList && e.target.classList.contains('modal')) closeModal(e.target.id); });

// ------------------------------------------------------------------ auth
function setAuthMode(mode) {
  authMode = mode;
  const styles = { on: 'bg-sky-500 text-white shadow-md', mentorOn: 'bg-purple-500 text-white font-bold shadow-md', off: 'text-slate-300 hover:text-slate-100' };
  [['login', 'btn-mode-login'], ['signup', 'btn-mode-signup'], ['mentor', 'btn-mode-mentor']].forEach(([m, id]) => {
    const b = document.getElementById(id);
    b.className = 'py-2 font-medium rounded-lg transition-all ' + (m === mode ? (m === 'mentor' ? styles.mentorOn : styles.on) : styles.off);
    b.setAttribute('aria-pressed', String(m === mode));
  });
  document.getElementById('mentor-success').classList.add('hidden');
  document.getElementById('auth-error').classList.add('hidden');
  const isMentor = mode === 'mentor';
  document.getElementById('student-form').classList.toggle('hidden', isMentor);
  document.getElementById('mentor-form-container').classList.toggle('hidden', !isMentor);
  document.getElementById('field-name').classList.toggle('hidden', mode !== 'signup');
  document.getElementById('field-gpa-budget').classList.toggle('hidden', mode !== 'signup');
  document.getElementById('password-hint').classList.toggle('hidden', mode !== 'signup');
  document.getElementById('input-password').setAttribute('autocomplete', mode === 'signup' ? 'new-password' : 'current-password');
  document.getElementById('auth-submit-label').textContent = mode === 'signup' ? 'Create My Account' : 'Sign In to Next Step';
}

async function handleAuthSubmit(e) {
  e.preventDefault();
  const errEl = document.getElementById('auth-error');
  errEl.classList.add('hidden');
  const btn = document.getElementById('btn-auth-submit');
  const email = document.getElementById('input-email').value.trim();
  const password = document.getElementById('input-password').value;
  if (!email || !password) { errEl.textContent = 'Please enter your email and password.'; errEl.classList.remove('hidden'); return; }
  btn.disabled = true;
  const label = document.getElementById('auth-submit-label');
  const oldLabel = label.textContent;
  label.textContent = authMode === 'signup' ? 'Creating your account…' : 'Signing in…';
  try {
    let user;
    if (authMode === 'signup') {
      user = await api('POST', '/api/signup', {
        name: document.getElementById('input-name').value.trim(), email, password,
        gpa: document.getElementById('input-gpa').value, budget: document.getElementById('input-budget').value
      });
    } else {
      user = await api('POST', '/api/login', { email, password });
    }
    await enterDashboard(user);
  } catch (err) {
    errEl.textContent = err.message;
    errEl.classList.remove('hidden');
  } finally { btn.disabled = false; label.textContent = oldLabel; }
}

async function handleMentorSubmit(e) {
  e.preventDefault();
  const errEl = document.getElementById('mentor-apply-error');
  errEl.classList.add('hidden');
  try {
    await api('POST', '/api/mentor-applications', {
      name: document.getElementById('ma-name').value.trim(),
      university: document.getElementById('ma-university').value.trim(),
      major: document.getElementById('ma-major').value.trim(),
      bio: document.getElementById('ma-bio').value.trim()
    });
    e.target.reset();
    document.getElementById('mentor-form-container').classList.add('hidden');
    document.getElementById('mentor-success').classList.remove('hidden');
    announce('Mentor application saved as pending verification.');
  } catch (err) { errEl.textContent = err.message; errEl.classList.remove('hidden'); }
}

function showAuth() {
  currentUser = null;
  document.getElementById('app-dashboard').classList.add('hidden');
  document.getElementById('auth-screen').classList.remove('hidden');
}

async function handleLogout() {
  try { await api('POST', '/api/logout'); } catch (err) { /* signing out locally anyway */ }
  showAuth();
  announce('Signed out.');
}

// ------------------------------------------------------------------ dashboard
async function loadAllData() {
  const results = await Promise.allSettled([
    api('GET', '/api/checklist'), api('GET', '/api/rewards'), api('GET', '/api/scholarships'),
    api('GET', '/api/tutors'), api('GET', '/api/mentors'), api('GET', '/api/internships'),
    api('GET', '/api/resources'), api('GET', '/api/mentor-requests'), api('GET', '/api/colleges/meta'),
    api('GET', '/api/colleges/saved')
  ]);
  const val = (i, fallback) => results[i].status === 'fulfilled' ? results[i].value : fallback;
  checklistData = val(0, []);
  rewardsData = val(1, []);
  const sch = val(2, { scholarships: [], saved: [], savedItems: [] });
  scholarshipsData = sch.scholarships; savedScholarships = sch.saved; savedScholarshipItems = sch.savedItems || [];
  tutorsData = val(3, []); mentorsData = val(4, []); internshipsData = val(5, []);
  resourcesData = val(6, null);
  const reqs = val(7, { requests: [], topics: [] }); myRequests = reqs.requests; requestTopics = reqs.topics;
  collegeMeta = val(8, null);
  savedColleges = val(9, []);
  const failed = results.filter(r => r.status === 'rejected').length;
  if (failed) toast(`Some sections couldn't load (${failed}). Refresh the page to try again.`, 'error');
}

async function enterDashboard(user) {
  currentUser = user;
  await loadAllData();
  updateUserHeader();
  document.getElementById('auth-screen').classList.add('hidden');
  document.getElementById('app-dashboard').classList.remove('hidden');
  initDashboard();
  const tab = (location.hash || '').replace('#', '');
  switchTab(TABS.includes(tab) ? tab : 'fafsa', true);
}

function applyPoints(summary) {
  if (!summary) return;
  currentUser.points = summary.score;
  currentUser.balance = summary.available;
  currentUser.pointsDetail = summary;
  updateUserHeader();
  renderRewardsShop();
}

window.onload = async function () {
  setAuthMode('login');
  icons();
  try {
    const res = await api('GET', '/api/me');
    if (res.user) await enterDashboard(res.user);
  } catch (err) { /* stay on sign-in screen */ }
};

function updateUserHeader() {
  if (!currentUser) return;
  document.getElementById('user-display-name').textContent = currentUser.name;
  document.getElementById('user-display-gpa').textContent = currentUser.gpa == null ? '—' : currentUser.gpa;
  document.getElementById('user-display-budget').textContent = currentUser.budget == null ? '—' : money(currentUser.budget) + '/yr';
  document.getElementById('user-display-points').textContent = currentUser.points;
  document.getElementById('rewards-balance-text').textContent = currentUser.balance;
  const d = currentUser.pointsDetail || {};
  document.getElementById('rewards-balance-detail').textContent =
    `${d.score || 0} earned − ${d.spent || 0} redeemed` + (d.overspent ? ` (${d.overspent} over; complete steps to earn more)` : '');
  document.getElementById('checklist-score-text').textContent = currentUser.points;
}

function openProfileModal() {
  document.getElementById('edit-profile-name').value = currentUser.name;
  document.getElementById('edit-profile-gpa').value = currentUser.gpa == null ? '' : currentUser.gpa;
  document.getElementById('edit-profile-budget').value = currentUser.budget == null ? '' : currentUser.budget;
  document.getElementById('profile-error').classList.add('hidden');
  openModal('profile-modal');
}

async function saveProfileChanges(e) {
  e.preventDefault();
  try {
    currentUser = await api('PUT', '/api/me', {
      name: document.getElementById('edit-profile-name').value.trim(),
      gpa: document.getElementById('edit-profile-gpa').value,
      budget: document.getElementById('edit-profile-budget').value
    });
  } catch (err) {
    const el = document.getElementById('profile-error'); el.textContent = err.message; el.classList.remove('hidden'); return;
  }
  updateUserHeader();
  closeModal('profile-modal');
  toast('Profile saved.');
}

const TABS = ['fafsa', 'rewards', 'scholarships', 'testprep', 'matcher', 'mentors', 'internships', 'advisor'];
const NAV_ON = 'nav-btn flex items-center gap-2 py-3 px-4 text-xs sm:text-sm font-semibold border-b-2 border-sky-400 text-sky-300 bg-sky-500/10 whitespace-nowrap';
const NAV_OFF = 'nav-btn flex items-center gap-2 py-3 px-4 text-xs sm:text-sm font-semibold border-b-2 border-transparent text-slate-300 hover:text-white whitespace-nowrap';
function switchTab(tabName, noFocus) {
  TABS.forEach(t => {
    document.getElementById('tab-' + t).classList.add('hidden');
    const b = document.getElementById('nav-' + t);
    b.className = NAV_OFF; b.removeAttribute('aria-current');
  });
  document.getElementById('tab-' + tabName).classList.remove('hidden');
  const nb = document.getElementById('nav-' + tabName);
  nb.className = NAV_ON; nb.setAttribute('aria-current', 'page');
  if (history.replaceState) history.replaceState(null, '', '#' + tabName);
  if (!noFocus) { const h = document.querySelector('#tab-' + tabName + ' h2'); if (h) { h.setAttribute('tabindex', '-1'); h.focus({ preventScroll: true }); } }
  if (tabName === 'matcher') initMatcher();
  window.scrollTo(0, 0);
  icons();
}
window.addEventListener('hashchange', () => {
  const tab = (location.hash || '').replace('#', '');
  if (currentUser && TABS.includes(tab) && document.getElementById('tab-' + tab).classList.contains('hidden')) switchTab(tab);
});

function initDashboard() {
  renderResources();
  renderChecklist();
  renderRewardsShop();
  renderScholarships();
  renderTutors();
  renderMentors();
  renderMyRequests();
  renderInternships();
  renderSavedColleges();
  initAdvisorChat();
  icons();
}

// ------------------------------------------------------------------ FAFSA / TAP resources
function videoEmbed(v, label) {
  const watch = safeUrl(v.watch_url);
  const thumb = `https://i.ytimg.com/vi/${encodeURIComponent(v.youtube_id)}/hqdefault.jpg`;
  if (!v.embeddable) {
    return `<a class="video-fallback block group" href="${esc(watch)}" target="_blank" rel="noopener noreferrer" aria-label="Watch ${esc(label)} on YouTube (opens in a new tab)">
      <img src="${esc(thumb)}" alt="" loading="lazy">
      <span class="absolute inset-0 flex flex-col items-center justify-center bg-slate-900/55 text-white text-center p-4">
        <i data-lucide="play-circle" class="w-12 h-12" aria-hidden="true"></i>
        <span class="mt-2 text-sm font-bold">Watch on YouTube</span>
        <span class="text-[11px] text-slate-200 max-w-xs">The video's owner doesn't allow playback on other websites, so it opens on YouTube.</span>
      </span></a>`;
  }
  return `<iframe src="https://www.youtube-nocookie.com/embed/${encodeURIComponent(v.youtube_id)}" title="${esc(label)}: ${esc(v.verified_title)}"
    loading="lazy" referrerpolicy="strict-origin-when-cross-origin"
    allow="accelerometer; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" allowfullscreen></iframe>`;
}

function linkChip(url, label, color) {
  const u = safeUrl(url);
  if (!u) return '';
  return `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer" class="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg ${color}">${esc(label)} <i data-lucide="external-link" class="w-3 h-3" aria-hidden="true"></i><span class="sr-only"> (opens in a new tab)</span></a>`;
}

function renderResources() {
  if (!resourcesData) {
    ['fafsa-video', 'tap-video'].forEach(id => document.getElementById(id).innerHTML =
      '<p class="absolute inset-0 flex items-center justify-center text-white text-xs p-4 text-center">Video information could not be loaded. Refresh to try again.</p>');
    return;
  }
  const L = resourcesData.links, V = resourcesData.videos;
  document.getElementById('fafsa-video').innerHTML = videoEmbed(V.fafsa, 'FAFSA Tutorial');
  document.getElementById('tap-video').innerHTML = videoEmbed(V.tap, 'New York TAP Tutorial');
  document.getElementById('fafsa-video-meta').textContent = `"${V.fafsa.verified_title}" (${V.fafsa.length}). ${V.fafsa.publisher_note}`;
  document.getElementById('tap-video-meta').textContent = `"${V.tap.verified_title}" (${V.tap.length}). ${V.tap.publisher_note}`;
  document.getElementById('fafsa-links').innerHTML =
    linkChip(V.fafsa.watch_url, 'Watch FAFSA video on YouTube', 'bg-red-50 text-red-800 border border-red-200') +
    linkChip(L.fafsa_apply.url, 'Start the FAFSA (StudentAid.gov)', 'bg-sky-600 text-white') +
    linkChip(L.studentaid.url, 'Federal Student Aid resources', 'bg-sky-50 text-sky-800 border border-sky-200');
  document.getElementById('tap-links').innerHTML =
    linkChip(V.tap.watch_url, 'Watch TAP video on YouTube', 'bg-red-50 text-red-800 border border-red-200') +
    linkChip(L.tap.url, 'TAP on HESC (official)', 'bg-purple-600 text-white') +
    linkChip(L.hesc.url, 'HESC home', 'bg-purple-50 text-purple-800 border border-purple-200') +
    linkChip(L.nys_grants.url, 'NY grants & scholarships', 'bg-purple-50 text-purple-800 border border-purple-200');
  const cards = [
    ['studentaid', 'Federal', 'All federal aid information, from the U.S. Department of Education.'],
    ['fafsa_apply', 'Federal', 'Fill out and submit the FAFSA form.'],
    ['understand_aid', 'Federal', 'Grants, loans, work-study and how aid works.'],
    ['fafsa_deadlines', 'Federal', 'Federal, state and college FAFSA deadlines.'],
    ['hesc', 'New York', 'New York State Higher Education Services Corporation.'],
    ['nys_grants', 'New York', 'TAP, Excelsior and other New York grants and scholarships.']
  ];
  document.getElementById('official-links').innerHTML = cards.map(([k, scope, desc]) => {
    const l = L[k]; const u = safeUrl(l.url);
    return `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer" class="block border border-slate-200 hover:border-sky-400 rounded-xl p-3">
      <span class="text-[10px] font-bold uppercase ${scope === 'Federal' ? 'text-sky-700' : 'text-purple-700'}">${esc(scope)}</span>
      <span class="block font-semibold text-slate-900 text-sm">${esc(l.title)} <span class="sr-only">(opens in a new tab)</span></span>
      <span class="block text-xs text-slate-600">${esc(desc)}</span>
      <span class="block text-[11px] text-slate-500 mt-1 break-all">${esc(new URL(u).host)}</span></a>`;
  }).join('');
  document.getElementById('resources-verified-note').textContent = `Links and facts checked on the official pages on ${fmtDate(resourcesData.last_verified)}.`;
}

// ------------------------------------------------------------------ checklist
function renderChecklist() {
  const container = document.getElementById('checklist-container');
  if (!checklistData.length) {
    container.innerHTML = '<p class="text-xs text-slate-500">The checklist could not be loaded. Refresh to try again.</p>';
    return;
  }
  const groups = {};
  checklistData.forEach(i => (groups[i.tag] = groups[i.tag] || []).push(i));
  const tagStyle = { 'FAFSA': 'bg-sky-100 text-sky-800', 'TAP': 'bg-purple-100 text-purple-800', 'SAT/ACT': 'bg-emerald-100 text-emerald-800' };
  container.innerHTML = Object.entries(groups).map(([tag, items]) => {
    const done = items.filter(i => i.done).length;
    return `<fieldset class="space-y-2"><legend class="text-xs font-bold text-slate-700 mb-1">${esc(tag)} steps: ${done} of ${items.length} done (${done * 100} pts)</legend>` +
      items.map(item => `
        <label class="flex items-center gap-3 p-3.5 rounded-xl border transition-all cursor-pointer ${item.done ? 'bg-sage-100 border-sage-300' : 'bg-white border-slate-200'}">
          <input type="checkbox" id="chk-${Number(item.id)}" aria-label="${esc(item.tag)} step: ${esc(item.title)} (100 points)" class="w-5 h-5 accent-emerald-700" ${item.done ? 'checked' : ''} ${pending.has('chk' + item.id) ? 'disabled' : ''}
                 onchange="toggleChecklist(${Number(item.id)}, this.checked)">
          <span class="text-xs font-semibold px-2 py-0.5 rounded ${tagStyle[item.tag] || 'bg-slate-100 text-slate-800'}">${esc(item.tag)}</span>
          <span class="text-sm font-medium ${item.done ? 'line-through text-slate-600' : 'text-slate-800'}">${esc(item.title)}</span>
          <span class="ml-auto text-[11px] font-bold text-amber-800 bg-amber-50 px-2 py-0.5 rounded border border-amber-200 whitespace-nowrap">+100 PTS</span>
        </label>`).join('') + '</fieldset>';
  }).join('');
  const doneCount = checklistData.filter(i => i.done).length;
  const p = Math.round((doneCount / checklistData.length) * 100);
  document.getElementById('checklist-progress-text').textContent = `${doneCount} of ${checklistData.length} Complete (${p}%)`;
  document.getElementById('checklist-progress-bar').style.width = p + '%';
  document.getElementById('checklist-progressbar').setAttribute('aria-valuenow', p);
}

async function toggleChecklist(id, done) {
  const item = checklistData.find(i => i.id === id);
  if (!item || pending.has('chk' + id)) return;
  const before = item.done;
  item.done = done;                       // optimistic update...
  pending.add('chk' + id);
  renderChecklist();
  try {
    const res = await api('PUT', '/api/checklist/' + id, { done });
    item.done = res.done;
    applyPoints(res.summary);             // ...confirmed by the server's derived score
    announce(`${done ? 'Completed' : 'Unchecked'}: ${item.title}. Checklist score ${res.summary.score} points.`);
  } catch (err) {
    item.done = before;                   // roll back on failure
    toast("Couldn't save that step: " + err.message, 'error');
  } finally {
    pending.delete('chk' + id);
    renderChecklist();
    const box = document.getElementById('chk-' + id);   // keep keyboard focus on the same step
    if (box) box.focus();
  }
}

async function resetChecklist() {
  if (!confirm('Uncheck every step? Your checklist points will go back to 0.')) return;
  try {
    const res = await api('POST', '/api/checklist/reset');
    checklistData.forEach(i => (i.done = false));
    applyPoints(res.summary);
    toast('Checklist reset.');
  } catch (err) { toast(err.message, 'error'); }
  renderChecklist();
}

// ------------------------------------------------------------------ rewards
function renderRewardsShop() {
  const grid = document.getElementById('rewards-grid');
  const bal = currentUser ? currentUser.balance : 0;
  if (!rewardsData.length) { grid.innerHTML = '<p class="text-xs text-slate-500">No rewards available right now.</p>'; return; }
  grid.innerHTML = rewardsData.map(item => {
    const canAfford = bal >= item.cost;
    return `<div class="bg-white border border-slate-200 rounded-2xl p-5 shadow-sm flex flex-col justify-between">
      <div>
        <div class="flex items-center justify-between mb-3">
          <div class="p-2.5 bg-amber-100 text-amber-800 rounded-xl"><i data-lucide="${esc(item.icon)}" class="w-5 h-5" aria-hidden="true"></i></div>
          <span class="px-2.5 py-1 bg-amber-50 text-amber-900 font-extrabold text-xs rounded-full border border-amber-200">${Number(item.cost)} PTS</span>
        </div>
        <h4 class="font-bold text-slate-900 text-base mb-1">${esc(item.title)}</h4>
        <p class="text-xs text-slate-600 leading-relaxed mb-4">${esc(item.desc)}</p>
      </div>
      <button type="button" onclick="openRedeemModal('${esc(item.id)}')" ${!canAfford ? 'disabled aria-disabled="true"' : ''} class="w-full py-2.5 ${canAfford ? 'bg-gradient-to-r from-amber-600 to-orange-600 hover:from-amber-700 hover:to-orange-700 text-white' : 'bg-slate-100 text-slate-500 cursor-not-allowed'} text-xs font-bold rounded-xl shadow transition-all">
        ${canAfford ? 'Redeem Reward' : 'Need ' + (item.cost - bal) + ' More PTS'}
      </button></div>`;
  }).join('');
  icons();
}

function openRedeemModal(itemId) {
  selectedRedeemItem = rewardsData.find(r => r.id === itemId);
  if (!selectedRedeemItem) return;
  document.getElementById('modal-item-title').textContent = 'Redeem ' + selectedRedeemItem.title;
  document.getElementById('modal-item-cost').textContent = 'Cost: ' + selectedRedeemItem.cost + ' points (you have ' + currentUser.balance + ' available)';
  document.getElementById('redeem-form-content').classList.remove('hidden');
  document.getElementById('redeem-success-content').classList.add('hidden');
  document.getElementById('redeem-error').classList.add('hidden');
  openModal('redeem-modal');
}

async function submitRedeemModal(e) {
  e.preventDefault();
  if (!selectedRedeemItem || pending.has('redeem')) return;
  pending.add('redeem');
  try {
    const res = await api('POST', '/api/redemptions', {
      reward_id: selectedRedeemItem.id,
      address: document.getElementById('redeem-address').value.trim(),
      option: document.getElementById('redeem-option').value
    });
    applyPoints(res.summary);
    document.getElementById('redeem-form-content').classList.add('hidden');
    document.getElementById('redeem-success-content').classList.remove('hidden');
    announce('Redemption request saved.');
  } catch (err) {
    const el = document.getElementById('redeem-error'); el.textContent = err.message; el.classList.remove('hidden');
  } finally { pending.delete('redeem'); }
}

// ------------------------------------------------------------------ scholarships
const STATUS_BADGE = {
  open: ['Open', 'bg-emerald-100 text-emerald-800'], upcoming: ['Opens soon', 'bg-sky-100 text-sky-800'],
  closed: ['Closed for this cycle', 'bg-slate-200 text-slate-700'], unknown: ['Status unknown: check site', 'bg-amber-100 text-amber-900'],
  custom: ['Your entry (not verified)', 'bg-purple-100 text-purple-800']
};
function badge(status) {
  const [label, cls] = STATUS_BADGE[status] || STATUS_BADGE.unknown;
  return `<span class="px-2 py-0.5 text-[11px] font-bold rounded ${cls}">${esc(label)}</span>`;
}

function renderScholarships() {
  const grid = document.getElementById('scholarships-grid');
  const q = (document.getElementById('sch-filter').value || '').toLowerCase().trim();
  const mode = document.getElementById('sch-status-filter').value;
  const list = scholarshipsData.filter(s => {
    if (mode === 'active' && s.status === 'closed') return false;
    if (mode === 'open' && s.status !== 'open') return false;
    if (!q) return true;
    return [s.title, s.provider, s.eligibility, s.desc, s.scope].join(' ').toLowerCase().includes(q);
  });
  if (!list.length) {
    grid.innerHTML = `<p class="md:col-span-2 text-center text-sm text-slate-600 py-8 bg-white border border-dashed border-slate-300 rounded-2xl">No scholarships match these filters. Try "All, including closed" or clear the search box.</p>`;
  } else {
    grid.innerHTML = list.map(scholarshipCard).join('');
  }
  renderSavedSidebar();
  document.getElementById('saved-sch-count').textContent = savedScholarships.length;
  icons();
}

function scholarshipCard(sch) {
  const isSaved = savedScholarships.includes(sch.id);
  const deadline = sch.deadline ? fmtDate(sch.deadline) : (sch.deadlineText ? null : 'See official site');
  const apply = sch.applyUrl
    ? `<a href="${esc(sch.applyUrl)}" target="_blank" rel="noopener noreferrer" class="flex-1 text-center py-2 bg-sky-600 hover:bg-sky-700 text-white text-xs font-semibold rounded-lg">${sch.status === 'closed' ? 'View official page' : 'Apply on official site'}<span class="sr-only"> for ${esc(sch.title)} (opens in a new tab)</span></a>`
    : `<span class="flex-1 text-center py-2 bg-slate-100 text-slate-600 text-xs font-semibold rounded-lg" title="No verified application link">${sch.isCustom ? 'No link: your own entry' : 'Application link unavailable'}</span>`;
  return `<article class="bg-white border border-slate-200 rounded-2xl p-6 shadow-sm flex flex-col justify-between" aria-labelledby="sch-${esc(sch.id)}">
    <div>
      <div class="flex flex-wrap gap-1.5 mb-2">${badge(sch.status)}${sch.scope ? `<span class="px-2 py-0.5 text-[11px] font-semibold rounded bg-slate-100 text-slate-700">${esc(sch.scope)}</span>` : ''}</div>
      <span class="text-xs font-semibold text-sky-700 uppercase">${esc(sch.provider)}</span>
      <h3 id="sch-${esc(sch.id)}" class="text-lg font-bold text-slate-900">${esc(sch.title)}</h3>
      <dl class="grid grid-cols-2 gap-2 my-3 p-3 bg-slate-50 rounded-xl text-xs">
        <div><dt class="text-slate-500">Amount</dt><dd class="font-bold text-sage-800">${esc(sch.amountText || 'See official site')}</dd></div>
        <div><dt class="text-slate-500">Deadline</dt><dd class="font-semibold text-slate-800">${esc(deadline || sch.deadlineText)}</dd></div>
      </dl>
      ${sch.eligibility ? `<p class="text-xs text-slate-700 mb-2"><strong>Who can apply:</strong> ${esc(sch.eligibility)}</p>` : ''}
      <p class="text-xs text-slate-600 mb-2">${esc(sch.desc)}</p>
      ${sch.lastVerified ? `<p class="text-[11px] text-slate-500 mb-3">Last checked ${esc(fmtDate(sch.lastVerified))} on the ${extLink(sch.sourceUrl, 'official page')}. Details can change, so confirm before you apply.</p>` : ''}
    </div>
    <div class="flex gap-2">
      ${apply}
      <button type="button" onclick="toggleSave('${esc(sch.id)}')" aria-pressed="${isSaved}" class="px-3 py-2 rounded-lg text-xs font-semibold flex items-center gap-1 ${isSaved ? 'bg-purple-100 text-purple-800 border border-purple-300' : 'bg-white text-slate-700 border border-slate-300 hover:border-purple-400'}">
        <i data-lucide="${isSaved ? 'bookmark-check' : 'bookmark'}" class="w-4 h-4" aria-hidden="true"></i>${isSaved ? 'Saved' : 'Save'}<span class="sr-only"> ${esc(sch.title)}</span>
      </button>
      ${sch.isCustom ? `<button type="button" onclick="deleteCustomScholarship('${esc(sch.id)}')" class="px-3 py-2 rounded-lg text-xs font-semibold text-rose-700 border border-rose-200" aria-label="Delete ${esc(sch.title)}"><i data-lucide="trash-2" class="w-4 h-4" aria-hidden="true"></i></button>` : ''}
    </div></article>`;
}

function renderSavedSidebar() {
  const ul = document.getElementById('saved-sch-list');
  if (!savedScholarshipItems.length) {
    ul.innerHTML = `<li class="text-xs text-slate-600 bg-slate-50 border border-dashed border-slate-300 rounded-lg p-3">Nothing saved yet. Press <strong>Save</strong> on a scholarship to keep it here.</li>`;
    return;
  }
  ul.innerHTML = savedScholarshipItems.map(s => `
    <li class="border border-slate-200 rounded-lg p-2.5 text-xs">
      <div class="font-semibold text-slate-900">${esc(s.title)}</div>
      <div class="text-slate-600">${esc(s.deadline ? 'Deadline ' + fmtDate(s.deadline) : (s.deadlineText || 'Deadline: see official site'))}${s.active === false ? ' · no longer listed' : ''}</div>
      <div class="flex gap-3 mt-1">
        ${s.applyUrl ? extLink(s.applyUrl, 'Official page') : ''}
        <button type="button" class="text-rose-700 underline" onclick="toggleSave('${esc(s.id)}')">Remove<span class="sr-only"> ${esc(s.title)}</span></button>
      </div></li>`).join('');
}

async function toggleSave(id) {
  if (pending.has('save' + id)) return;
  const wasSaved = savedScholarships.includes(id);
  pending.add('save' + id);
  try {
    const res = await api(wasSaved ? 'DELETE' : 'POST', '/api/scholarships/' + encodeURIComponent(id) + '/save');
    if (wasSaved) {
      savedScholarships = savedScholarships.filter(x => x !== id);
      savedScholarshipItems = savedScholarshipItems.filter(x => x.id !== id);
    } else {
      savedScholarships = [...savedScholarships, id];
      savedScholarshipItems = [...savedScholarshipItems, res.scholarship];
    }
    const s = scholarshipsData.find(x => x.id === id) || res.scholarship || {};
    toast(wasSaved ? `Removed "${s.title || 'scholarship'}" from your saved list.` : `Saved "${s.title}" to your list.`);
  } catch (err) {
    toast(err.status === 401 ? 'Please sign in again. Saving needs your account so the list stays with you.' : "Couldn't update your saved list: " + err.message, 'error');
  } finally {
    pending.delete('save' + id);
    renderScholarships();
  }
}

async function saveCustomScholarship(e) {
  e.preventDefault();
  const body = {
    title: document.getElementById('new-sch-title').value.trim(),
    provider: document.getElementById('new-sch-provider').value.trim(),
    amount: document.getElementById('new-sch-amount').value,
    deadline: document.getElementById('new-sch-deadline').value,
    desc: document.getElementById('new-sch-desc').value.trim()
  };
  try {
    const res = await api('POST', '/api/scholarships', body);
    scholarshipsData.unshift(res.scholarship);
    e.target.reset();
    closeModal('add-sch-modal');
    toast('Custom scholarship added. Only you can see it.');
    renderScholarships();
  } catch (err) {
    const el = document.getElementById('add-sch-error'); el.textContent = err.message; el.classList.remove('hidden');
  }
}

async function deleteCustomScholarship(id) {
  if (!confirm('Delete this custom scholarship?')) return;
  try {
    await api('DELETE', '/api/scholarships/' + encodeURIComponent(id));
    scholarshipsData = scholarshipsData.filter(s => s.id !== id);
    savedScholarships = savedScholarships.filter(x => x !== id);
    savedScholarshipItems = savedScholarshipItems.filter(x => x.id !== id);
    toast('Custom scholarship deleted.');
    renderScholarships();
  } catch (err) { toast(err.message, 'error'); }
}

// ------------------------------------------------------------------ tutors & mentors
function initials(name) { return (name || '?').split(/\s+/).map(w => w[0]).slice(0, 2).join('').toUpperCase(); }
function demoBadge(p) {
  if (p.isDemo) return `<p class="text-[11px] font-semibold text-amber-900 bg-amber-50 border border-amber-200 rounded px-2 py-1">${esc(p.demoLabel)}</p>`;
  if (p.verified) return `<p class="text-[11px] font-semibold text-emerald-800 bg-emerald-50 border border-emerald-200 rounded px-2 py-1">Verified by NextStep</p>`;
  return `<p class="text-[11px] text-slate-600">Self-reported profile (not yet verified)</p>`;
}
function chips(list, cls) {
  return (list || []).map(s => `<span class="px-2 py-0.5 ${cls || 'bg-slate-100 text-slate-700'} rounded text-[11px] font-medium">${esc(s)}</span>`).join(' ');
}
function personCard(p, accent) {
  return `<article class="bg-white border border-slate-200 rounded-2xl p-6 shadow-sm flex flex-col justify-between gap-3">
    <div class="space-y-2">
      <div class="flex items-center gap-3">
        <div class="w-12 h-12 rounded-full bg-${accent}-100 text-${accent}-800 flex items-center justify-center font-bold" aria-hidden="true">${esc(initials(p.name))}</div>
        <div>
          <h3 class="font-bold text-slate-900">${esc(p.name)}${p.pronouns ? ` <span class="text-xs font-normal text-slate-500">(${esc(p.pronouns)})</span>` : ''}</h3>
          <p class="text-xs text-${accent}-700 font-semibold">${esc(p.school)}${p.major && p.kind === 'mentor' ? ' · ' + esc(p.major) : ''}${p.classYear ? ' · ' + esc(p.classYear) : ''}</p>
        </div>
      </div>
      ${demoBadge(p)}
      <p class="text-xs text-slate-700">${esc(p.bio)}</p>
      ${p.testScore ? `<p class="text-[11px] text-slate-600">Test score (${p.isDemo ? 'fictional demo' : 'self-reported'}): ${esc(p.testScore)}</p>` : ''}
      <div class="flex flex-wrap gap-1.5">${chips(p.helpAreas.length ? p.helpAreas : p.specialties)}</div>
      <p class="text-[11px] text-slate-600">${p.languages.length ? 'Languages: ' + esc(p.languages.join(', ')) + ' · ' : ''}${p.availability ? 'Availability: ' + esc(p.availability) : 'Availability not provided'}</p>
    </div>
    <button type="button" onclick="openPerson('${esc(p.id)}')" class="w-full py-2 bg-${accent === 'emerald' ? 'emerald-700 hover:bg-emerald-800' : 'sky-600 hover:bg-sky-700'} text-white text-xs font-semibold rounded-xl">
      View profile<span class="sr-only"> of ${esc(p.name)}</span>
    </button></article>`;
}
function renderTutors() {
  const g = document.getElementById('tutors-grid');
  g.innerHTML = tutorsData.length ? tutorsData.map(t => personCard(t, 'emerald')).join('') : '<p class="text-xs text-slate-600">No tutors listed yet.</p>';
}
function renderMentors() {
  const g = document.getElementById('mentors-grid');
  g.innerHTML = mentorsData.length ? mentorsData.map(m => personCard(m, 'purple')).join('') : '<p class="text-xs text-slate-600">No mentors listed yet.</p>';
  icons();
}

async function openPerson(id) {
  document.getElementById('person-name').textContent = 'Loading…';
  document.getElementById('person-sub').textContent = '';
  document.getElementById('person-body').innerHTML = '<p class="text-xs text-slate-500">Loading profile…</p>';
  document.getElementById('person-request').innerHTML = '';
  openModal('person-modal');
  let p;
  try { p = await api('GET', '/api/mentors/' + encodeURIComponent(id)); }
  catch (err) { document.getElementById('person-body').innerHTML = `<p class="text-rose-700">${esc(err.message)}</p>`; return; }
  document.getElementById('person-name').textContent = p.name + (p.pronouns ? ` (${p.pronouns})` : '');
  document.getElementById('person-sub').textContent = [p.school, p.kind === 'mentor' ? p.major : 'ACT/SAT tutor', p.classYear].filter(Boolean).join(' · ');
  const row = (label, val) => val && (!Array.isArray(val) || val.length) ? `<div><h4 class="text-xs font-bold text-slate-900 uppercase tracking-wide">${esc(label)}</h4><div class="mt-0.5">${Array.isArray(val) ? chips(val) : esc(val)}</div></div>` : '';
  document.getElementById('person-body').innerHTML = demoBadge(p) +
    row('About', p.bio) + row('College journey', p.journey) + row('Can help with', p.helpAreas.length ? p.helpAreas : p.specialties) +
    row('Experience with', p.experienceAreas) + row('Academic interests', p.academicInterests) + row('Career interests', p.careerInterests) +
    row('Clubs & activities', p.activities) + row('Languages', p.languages) +
    (p.testScore ? row(p.isDemo ? 'Test score (fictional demo)' : 'Test score (self-reported)', p.testScore) : '') +
    row('Availability', p.availability || 'Availability not provided') + (p.responseTime ? row('Typical response time', p.responseTime) : '') +
    (p.selfReportedNote ? `<p class="text-[11px] text-slate-500">${esc(p.selfReportedNote)}</p>` : '') +
    `<p class="text-[11px] text-slate-500">NextStep never shows mentors' personal contact information. Use a session request.</p>`;
  const existing = myRequests.find(r => r.mentorId === p.id && r.status === 'pending');
  const req = document.getElementById('person-request');
  if (existing) {
    req.innerHTML = `<p class="text-sm text-slate-700">You already have a <strong>pending</strong> request with ${esc(p.name)} (sent ${esc(fmtDate(existing.requestedAt.slice(0, 10)))}). See "My requests".</p>`;
  } else {
    req.innerHTML = `<button type="button" class="px-4 py-2 bg-sky-600 hover:bg-sky-700 text-white text-sm font-semibold rounded-xl" onclick="showRequestForm('${esc(p.id)}', '${esc(p.kind)}')">${p.kind === 'tutor' ? 'Request a tutoring session' : 'Request a session'}</button>
      <p class="text-[11px] text-slate-500 mt-1">Nothing is sent until you fill in and submit the request form.</p>`;
  }
  icons();
}

function showRequestForm(id, kind) {
  const p = [...mentorsData, ...tutorsData].find(x => x.id === id) || {};
  const req = document.getElementById('person-request');
  req.innerHTML = `<form onsubmit="submitRequest(event, '${esc(id)}', '${esc(kind)}')" class="space-y-3 text-sm">
    <fieldset><legend class="text-xs font-bold text-slate-900 mb-1">What would you like help with?${kind === 'mentor' ? ' *' : ''}</legend>
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-1">${requestTopics.map((t, i) =>
        `<label class="flex items-center gap-1.5 text-xs"><input type="checkbox" name="topic" value="${esc(t)}"> ${esc(t)}</label>`).join('')}</div>
    </fieldset>
    <div><label for="req-note" class="text-xs font-bold text-slate-900">Note (optional)</label>
      <textarea id="req-note" rows="3" maxlength="1000" class="mt-1 w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5 text-xs" placeholder="What are you working on? Don't include phone numbers, addresses or passwords."></textarea></div>
    ${p.isDemo ? '<p class="text-[11px] text-amber-900 bg-amber-50 border border-amber-200 rounded p-2">This is a fictional demo profile. Your request will be saved so you can see how the feature works, but no real person will reply.</p>' : ''}
    <p id="req-error" class="hidden text-xs text-rose-700" role="alert"></p>
    <div class="flex gap-2"><button type="submit" id="req-submit" class="px-4 py-2 bg-sky-600 hover:bg-sky-700 text-white text-sm font-semibold rounded-xl">Send request</button>
      <button type="button" class="px-4 py-2 bg-slate-100 text-slate-700 text-sm font-semibold rounded-xl" onclick="closeModal('person-modal')">Cancel</button></div>
  </form>`;
  req.querySelector('input, textarea').focus();
}

async function submitRequest(e, id, kind) {
  e.preventDefault();
  if (pending.has('req')) return;
  const topics = [...e.target.querySelectorAll('input[name=topic]:checked')].map(x => x.value);
  const errEl = document.getElementById('req-error');
  errEl.classList.add('hidden');
  if (kind === 'mentor' && !topics.length) { errEl.textContent = 'Choose at least one topic.'; errEl.classList.remove('hidden'); return; }
  pending.add('req');
  document.getElementById('req-submit').disabled = true;
  try {
    const res = await api('POST', '/api/mentor-requests', {
      mentor_id: id, type: kind === 'tutor' ? 'act_sat_tutoring' : 'mentorship', topics,
      notes: document.getElementById('req-note').value.trim()
    });
    const reqs = await api('GET', '/api/mentor-requests').catch(() => null);
    if (reqs) myRequests = reqs.requests;
    renderMyRequests();
    // Confirmation only after the server saved the request:
    document.getElementById('person-request').innerHTML = `<div class="bg-emerald-50 border border-emerald-200 rounded-xl p-3 text-sm text-emerald-900" role="status">
      <strong>Request saved</strong> (status: ${esc(res.status)}). ${res.isDemo ? 'This is a demo profile, so no real person will reply.' : 'You can follow its status under "My requests".'}</div>`;
    announce('Request saved. Status pending.');
  } catch (err) {
    errEl.textContent = err.message; errEl.classList.remove('hidden');
    const b = document.getElementById('req-submit'); if (b) b.disabled = false;
  } finally { pending.delete('req'); }
}

function renderMyRequests() {
  const ul = document.getElementById('my-requests');
  if (!myRequests.length) { ul.innerHTML = '<li class="text-slate-600 bg-slate-50 border border-dashed border-slate-300 rounded-lg p-3">No requests yet. Open a profile and choose "Request a session".</li>'; return; }
  const color = { pending: 'bg-amber-100 text-amber-900', accepted: 'bg-emerald-100 text-emerald-800', declined: 'bg-slate-200 text-slate-700', cancelled: 'bg-slate-100 text-slate-600', completed: 'bg-sky-100 text-sky-800' };
  ul.innerHTML = myRequests.map(r => `<li class="border border-slate-200 rounded-lg p-2.5">
      <div class="flex justify-between gap-2"><strong class="text-slate-900">${esc(r.mentorName)}</strong><span class="px-2 py-0.5 rounded text-[11px] font-bold ${color[r.status] || ''}">${esc(r.status[0].toUpperCase() + r.status.slice(1))}</span></div>
      <div class="text-slate-600">${esc(r.type === 'act_sat_tutoring' ? 'Tutoring' : 'Mentorship')} · ${esc(fmtDate(r.requestedAt.slice(0, 10)))}${r.isDemo ? ' · demo profile' : ''}</div>
      ${r.topics.length ? `<div class="text-slate-600">${esc(r.topics.join(', '))}</div>` : ''}
      ${r.status === 'pending' ? `<button type="button" class="mt-1 text-rose-700 underline" onclick="cancelRequest('${esc(r.id)}')">Cancel request</button>` : ''}
    </li>`).join('');
}

async function cancelRequest(id) {
  try {
    await api('POST', '/api/mentor-requests/' + encodeURIComponent(id) + '/cancel');
    const r = myRequests.find(x => x.id === id); if (r) r.status = 'cancelled';
    renderMyRequests();
    toast('Request cancelled.');
  } catch (err) { toast(err.message, 'error'); }
}

// ------------------------------------------------------------------ internships
function setInternshipFilter(type) {
  currentFilterAudience = type;
  [['All', 'All'], ['HS', 'High School'], ['College', 'College']].forEach(([id, val]) => {
    const btn = document.getElementById('int-btn-' + id);
    const on = val === type;
    btn.className = 'px-3 py-1.5 rounded-lg ' + (on ? 'bg-white shadow font-bold text-slate-900' : 'text-slate-700');
    btn.setAttribute('aria-pressed', String(on));
  });
  renderInternships();
}

function renderInternships() {
  const grid = document.getElementById('internships-grid');
  const hideClosed = document.getElementById('int-hide-closed').checked;
  const list = internshipsData.filter(i => (currentFilterAudience === 'All' || i.target === currentFilterAudience) && !(hideClosed && i.status === 'closed'));
  if (!list.length) {
    grid.innerHTML = '<p class="md:col-span-2 text-center text-sm text-slate-600 py-8 bg-white border border-dashed border-slate-300 rounded-2xl">No programs match this filter.</p>';
    return;
  }
  grid.innerHTML = list.map(int => {
    const field = (label, v) => v ? `<div><dt class="text-slate-500">${esc(label)}</dt><dd class="font-semibold text-slate-800">${esc(v)}</dd></div>` : '';
    const deadline = int.deadline ? fmtDate(int.deadline) : (int.deadlineText || 'Not published. Check the official page.');
    return `<article class="bg-white border border-slate-200 rounded-2xl p-6 shadow-sm flex flex-col justify-between gap-3">
      <div>
        <div class="flex flex-wrap gap-1.5 mb-2">${badge(int.status)}<span class="px-2 py-0.5 text-[11px] font-semibold rounded bg-slate-100 text-slate-700">${esc(int.target)}</span></div>
        <span class="text-xs font-bold text-purple-700 uppercase">${esc(int.org)}</span>
        <h3 class="text-lg font-bold text-slate-900 mt-0.5">${esc(int.title)}</h3>
        <dl class="grid grid-cols-2 gap-2 my-3 p-3 bg-slate-50 rounded-xl text-xs">
          ${field('Location', int.location || 'Not specified')}${field('Format', int.format || 'Not specified')}
          ${field('Pay', int.compensationText || int.compensation)}${field('Deadline', deadline)}
        </dl>
        <p class="text-xs text-slate-700 mb-2">${esc(int.desc)}</p>
        ${int.eligibility ? `<p class="text-xs text-slate-700 mb-1"><strong>Eligibility:</strong> ${esc(int.eligibility)}</p>` : ''}
        ${int.ageGrade ? `<p class="text-xs text-slate-700 mb-1"><strong>Age / grade:</strong> ${esc(int.ageGrade)}</p>` : ''}
        ${int.responsibilities ? `<p class="text-xs text-slate-700 mb-1"><strong>What you'd do:</strong> ${esc(int.responsibilities)}</p>` : ''}
        ${int.skills ? `<p class="text-xs text-slate-700 mb-1"><strong>Skills / experience:</strong> ${esc(int.skills)}</p>` : ''}
        ${int.lastVerified ? `<p class="text-[11px] text-slate-500 mt-2">Source: ${extLink(int.sourceUrl, 'official program page')}, last checked ${esc(fmtDate(int.lastVerified))}.</p>` : ''}
      </div>
      ${int.applyUrl
        ? `<a href="${esc(int.applyUrl)}" target="_blank" rel="noopener noreferrer" class="w-full text-center py-2 bg-sky-600 hover:bg-sky-700 text-white text-xs font-semibold rounded-lg">${int.status === 'closed' ? 'View official page (closed now)' : 'Apply on official site'}<span class="sr-only"> for ${esc(int.title)} (opens in a new tab)</span></a>
           <p class="text-[11px] text-slate-500 -mt-1">Opens the program's own website. NextStep doesn't submit applications.</p>`
        : '<span class="w-full text-center py-2 bg-slate-100 text-slate-600 text-xs font-semibold rounded-lg">Application link unavailable</span>'}
    </article>`;
  }).join('');
}

// ------------------------------------------------------------------ college search + matcher
const collegeSearchState = { q: '', page: 1, results: [] };
const matchState = { page: 1, results: [], prefs: null };
let matcherReady = false;
let suggestTimer = null, suggestIndex = -1, suggestAbort = null;

function initMatcher() {
  if (matcherReady) return;
  matcherReady = true;
  const cov = document.getElementById('college-coverage');
  if (collegeMeta && collegeMeta.source) {
    const s = collegeMeta.source;
    cov.innerHTML = `<strong>Data source:</strong> ${extLink(s.url, s.title)}, released ${esc(s.released)} (${Number(s.rowCount).toLocaleString()} institutions, imported ${esc(fmtDate(s.importedAt))}).
      <strong>Coverage:</strong> ${esc(s.coverage)} Admissions figures are for fall 2024, and cost/net price figures are 2023-24 averages. Always confirm with the college.`;
    const st = document.getElementById('college-search-state');
    collegeMeta.states.forEach(x => st.insertAdjacentHTML('beforeend', `<option value="${esc(x)}">${esc(x)}</option>`));
    const inc = document.getElementById('m-income');
    Object.entries(collegeMeta.incomeBands).forEach(([k, v]) => inc.insertAdjacentHTML('beforeend', `<option value="${esc(k)}">${esc(v)}</option>`));
    const maj = document.getElementById('m-major');
    collegeMeta.majors.forEach(m => maj.insertAdjacentHTML('beforeend', `<option value="${esc(m)}">${esc(m)}</option>`));
  } else {
    cov.textContent = "College data couldn't be loaded. Refresh to try again.";
  }
  const p = (currentUser && currentUser.collegePrefs) || {};
  document.getElementById('m-gpa').value = p.gpa != null ? p.gpa : (currentUser.gpa != null ? currentUser.gpa : '');
  document.getElementById('m-budget').value = p.budget != null ? p.budget : (currentUser.budget != null ? currentUser.budget : '');
  if (p.sat) document.getElementById('m-sat').value = p.sat;
  if (p.act) document.getElementById('m-act').value = p.act;
  if (p.income) document.getElementById('m-income').value = p.income;
  if (p.major) document.getElementById('m-major').value = p.major;
  if (p.states) document.getElementById('m-states').value = p.states.join(', ');
  if (p.degree) document.getElementById('m-degree').value = p.degree;
  if (p.control) document.querySelectorAll('input[name=m-control]').forEach(c => { c.checked = p.control.includes(Number(c.value)); });
}

function onCollegeInput() {
  clearTimeout(suggestTimer);
  const q = document.getElementById('college-search').value.trim();
  if (q.length < 2) { hideSuggestions(); return; }
  suggestTimer = setTimeout(() => loadSuggestions(q), 250);   // debounced
}
async function loadSuggestions(q) {
  try {
    if (suggestAbort) suggestAbort.abort();
    suggestAbort = new AbortController();
    const res = await fetch('/api/colleges/search?per_page=6&q=' + encodeURIComponent(q), { signal: suggestAbort.signal, credentials: 'same-origin' });
    if (!res.ok) { hideSuggestions(); return; }
    const data = await res.json();
    const ul = document.getElementById('college-suggestions');
    if (!data.results.length) { hideSuggestions(); return; }
    suggestIndex = -1;
    ul.innerHTML = data.results.map((c, i) => `<li role="option" id="sugg-${i}" data-unitid="${Number(c.unitid)}" aria-selected="false"
        class="px-3 py-2 cursor-pointer hover:bg-sky-50" onmousedown="event.preventDefault(); openCollege(${Number(c.unitid)}); hideSuggestions()">
        <span class="font-medium">${esc(c.name)}</span> <span class="text-xs text-slate-500">${esc(c.city)}, ${esc(c.state)}</span></li>`).join('') +
      (data.didYouMean ? '<li class="px-3 py-1 text-[11px] text-slate-500">Closest matches (check the spelling)</li>' : '');
    ul.classList.remove('hidden');
    document.getElementById('college-search').setAttribute('aria-expanded', 'true');
  } catch (e) { /* aborted or offline: ignore suggestions */ }
}
function hideSuggestions() {
  document.getElementById('college-suggestions').classList.add('hidden');
  document.getElementById('college-search').setAttribute('aria-expanded', 'false');
  document.getElementById('college-search').removeAttribute('aria-activedescendant');
}
function onCollegeKeydown(e) {
  const ul = document.getElementById('college-suggestions');
  const items = [...ul.querySelectorAll('[role=option]')];
  if (ul.classList.contains('hidden') || !items.length) return;
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    suggestIndex = (suggestIndex + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
    items.forEach((li, i) => { li.setAttribute('aria-selected', String(i === suggestIndex)); li.classList.toggle('bg-sky-100', i === suggestIndex); });
    document.getElementById('college-search').setAttribute('aria-activedescendant', items[suggestIndex].id);
  } else if (e.key === 'Enter' && suggestIndex >= 0) {
    e.preventDefault(); openCollege(Number(items[suggestIndex].dataset.unitid)); hideSuggestions();
  } else if (e.key === 'Escape') { hideSuggestions(); }
}

async function collegeSearch(page) {
  hideSuggestions();
  const q = document.getElementById('college-search').value.trim();
  const state = document.getElementById('college-search-state').value;
  const status = document.getElementById('college-search-status');
  const list = document.getElementById('college-search-results');
  const more = document.getElementById('college-search-more');
  if (q.length < 2) { status.textContent = 'Type at least 2 letters of a college name.'; return; }
  if (page === 1) { list.innerHTML = ''; collegeSearchState.results = []; }
  status.textContent = 'Searching…';
  try {
    const data = await api('GET', `/api/colleges/search?q=${encodeURIComponent(q)}&page=${page}&per_page=10${state ? '&state=' + state : ''}`);
    collegeSearchState.page = page; collegeSearchState.q = q;
    if (!data.results.length) {
      status.textContent = `No U.S. colleges in College Scorecard match "${q}". Check the spelling or try part of the name (for example "Lehman" instead of "Lehman College CUNY"). Colleges outside the U.S. aren't included.`;
      more.classList.add('hidden'); return;
    }
    status.textContent = data.didYouMean ? `No exact match for "${q}". Closest names:` : `${data.total} match${data.total === 1 ? '' : 'es'} for "${q}".`;
    list.insertAdjacentHTML('beforeend', data.results.map(c => `<li class="py-2 flex flex-col sm:flex-row sm:items-center justify-between gap-1">
        <div><button type="button" class="font-semibold text-sky-800 underline text-left" onclick="openCollege(${Number(c.unitid)})">${esc(c.name)}</button>
        <div class="text-xs text-slate-600">${esc(c.city)}, ${esc(c.state)} · ${esc(c.type)}${c.highestDegree ? ' · up to ' + esc(c.highestDegree) : ''}</div></div>
        <div class="text-xs text-slate-600">${c.admissionRate != null ? 'Admits ' + pct(c.admissionRate) : (c.openAdmission ? 'Open admission' : 'Admission rate not reported')}</div></li>`).join(''));
    more.classList.toggle('hidden', !data.hasMore);
  } catch (err) {
    status.textContent = err.status === 429 ? err.message : "Search isn't working right now: " + err.message;
  }
}

function readMatchForm() {
  return {
    gpa: document.getElementById('m-gpa').value, budget: document.getElementById('m-budget').value,
    sat: document.getElementById('m-sat').value, act: document.getElementById('m-act').value,
    income: document.getElementById('m-income').value, major: document.getElementById('m-major').value,
    states: document.getElementById('m-states').value, degree: document.getElementById('m-degree').value,
    control: [...document.querySelectorAll('input[name=m-control]:checked')].map(c => c.value)
  };
}

async function runMatch(page) {
  const status = document.getElementById('match-status');
  const btn = document.getElementById('match-submit');
  document.querySelectorAll('.field-err').forEach(el => { el.classList.add('hidden'); el.textContent = ''; });
  document.querySelectorAll('#match-form [aria-invalid]').forEach(el => el.removeAttribute('aria-invalid'));
  const body = Object.assign(readMatchForm(), { page });
  btn.disabled = true; status.textContent = 'Finding matches…';
  try {
    const data = await api('POST', '/api/colleges/match', body);
    matchState.page = page; matchState.prefs = data.prefs;
    if (currentUser) currentUser.collegePrefs = data.prefs;
    renderMatches(data, page > 1);
    status.textContent = data.total ? `${data.total} colleges fit your filters. Showing the best-ranked first.` : '';
  } catch (err) {
    if (err.data && err.data.fields) {
      Object.entries(err.data.fields).forEach(([f, msg]) => {
        const el = document.getElementById('m-' + f + '-err');
        if (el) { el.textContent = msg; el.classList.remove('hidden'); }
        const input = document.getElementById('m-' + f); if (input) input.setAttribute('aria-invalid', 'true');
      });
      status.textContent = 'Please fix the highlighted fields.';
      const first = document.querySelector('#match-form [aria-invalid=true]'); if (first) first.focus();
    } else {
      status.textContent = err.status === 429 ? err.message : "Matching isn't working right now: " + err.message;
    }
  } finally { btn.disabled = false; }
}

const FIT_STYLE = {
  'Strong academic fit': 'bg-emerald-100 text-emerald-800', 'Possible academic fit': 'bg-sky-100 text-sky-800',
  'More competitive': 'bg-amber-100 text-amber-900', 'Fit uncertain': 'bg-slate-200 text-slate-700'
};
function renderMatches(data, append) {
  const grid = document.getElementById('colleges-grid');
  const sum = document.getElementById('match-summary');
  sum.classList.remove('hidden');
  const counts = Object.entries(data.counts).map(([k, v]) => `${esc(k)}: ${v}`).join(' · ');
  sum.innerHTML = `<p><strong>${data.total}</strong> colleges match your filters${counts ? ' (' + counts + ')' : ''}.</p><p class="mt-1 text-slate-600">${esc(data.disclaimer)}</p>`;
  if (!data.total) {
    grid.innerHTML = `<p class="md:col-span-2 lg:col-span-3 text-center text-sm text-slate-600 py-8 bg-white border border-dashed border-slate-300 rounded-2xl">No colleges match these filters. Try more states, another field of study, "2-year or 4-year", or another institution type.</p>`;
    document.getElementById('match-more').classList.add('hidden');
    return;
  }
  const html = data.results.map(c => {
    const a = c.affordability;
    return `<article class="bg-white border border-slate-200 rounded-2xl shadow-sm flex flex-col justify-between">
      <div class="p-5 space-y-2">
        <span class="inline-block px-2 py-0.5 text-[11px] font-bold rounded ${FIT_STYLE[c.fit] || ''}">${esc(c.fit)}</span>
        <h3 class="font-bold text-slate-900 text-base">${esc(c.name)}</h3>
        <p class="text-xs text-slate-600">${esc(c.city)}, ${esc(c.state)} · ${esc(c.type)}</p>
        <dl class="grid grid-cols-2 gap-2 p-3 bg-slate-50 rounded-xl text-xs">
          <div><dt class="text-slate-500">Published cost of attendance</dt><dd class="font-bold text-slate-800">${esc(money(a.sticker) || 'Not reported')}</dd></div>
          <div><dt class="text-slate-500">Avg. net price${matchState.prefs && matchState.prefs.income && a.net_basis && a.net_basis.includes('families') ? ' (your income range)' : ''}</dt><dd class="font-bold ${a.within_budget ? 'text-emerald-800' : 'text-slate-800'}">${esc(money(a.net) || 'Not reported')}</dd></div>
          <div><dt class="text-slate-500">Admission rate</dt><dd class="font-semibold">${esc(c.admissionRate != null ? pct(c.admissionRate) : (c.openAdmission ? 'Open admission' : 'Not reported'))}</dd></div>
          <div><dt class="text-slate-500">Undergrads</dt><dd class="font-semibold">${esc(c.undergrads != null ? Number(c.undergrads).toLocaleString() : 'Not reported')}</dd></div>
        </dl>
        <p class="text-xs text-slate-700">${esc(a.note)}</p>
        <details class="text-xs"><summary class="cursor-pointer font-semibold text-sky-800">Why it was suggested & what to check</summary>
          <ul class="list-disc pl-4 mt-1 space-y-0.5 text-slate-700">${c.reasons.concat(c.matches).map(r => `<li>${esc(r)}</li>`).join('')}</ul>
          ${c.cautions.length ? `<ul class="list-disc pl-4 mt-1 space-y-0.5 text-amber-900">${c.cautions.map(r => `<li>${esc(r)}</li>`).join('')}</ul>` : ''}
          <p class="mt-1 font-semibold text-slate-800">Verify:</p><ul class="list-disc pl-4 text-slate-700">${c.verify.map(r => `<li>${esc(r)}</li>`).join('')}</ul>
        </details>
      </div>
      <div class="px-5 py-3 bg-slate-50 border-t border-slate-100 flex items-center justify-between text-xs gap-2">
        ${c.netPriceCalculator ? extLink(c.netPriceCalculator, 'Net Price Calculator') : '<span></span>'}
        <button type="button" class="text-sky-800 font-semibold underline" onclick="openCollege(${Number(c.unitid)})">View details<span class="sr-only"> for ${esc(c.name)}</span></button>
      </div></article>`;
  }).join('');
  if (append) grid.insertAdjacentHTML('beforeend', html); else grid.innerHTML = html;
  document.getElementById('match-more').classList.toggle('hidden', data.page * data.perPage >= data.total);
}

async function openCollege(unitid) {
  document.getElementById('college-name').textContent = 'Loading…';
  document.getElementById('college-sub').textContent = '';
  document.getElementById('college-body').innerHTML = '<p class="text-xs text-slate-500">Loading College Scorecard data…</p>';
  openModal('college-modal');
  let c;
  try { c = await api('GET', '/api/colleges/' + Number(unitid)); }
  catch (err) { document.getElementById('college-body').innerHTML = `<p class="text-rose-700">${esc(err.message)}</p>`; return; }
  document.getElementById('college-name').textContent = c.name;
  document.getElementById('college-sub').textContent = [c.city + ', ' + c.state, c.type, c.locale].filter(Boolean).join(' · ');
  const nr = 'Not reported';
  const A = c.admissions, C = c.cost, O = c.outcomes, y = (c.dataSource && c.dataSource.years) || {};
  const row = (k, v) => `<div class="flex justify-between gap-3 py-1 border-b border-slate-100"><dt class="text-slate-600">${esc(k)}</dt><dd class="font-semibold text-slate-900 text-right">${esc(v == null || v === '' ? nr : v)}</dd></div>`;
  document.getElementById('college-body').innerHTML = `
    <div class="flex flex-wrap gap-2 text-xs">
      ${c.website ? linkChip(c.website, 'Official website', 'bg-sky-600 text-white') : '<span class="text-slate-500">Official website not listed</span>'}
      ${c.netPriceCalculator ? linkChip(c.netPriceCalculator, 'Net Price Calculator', 'bg-emerald-50 text-emerald-800 border border-emerald-200') : ''}
      <button type="button" id="college-save-btn" aria-pressed="${c.saved}" onclick="toggleCollegeSave(${Number(c.unitid)})" class="px-3 py-1.5 rounded-lg border ${c.saved ? 'bg-amber-100 border-amber-300 text-amber-900' : 'border-slate-300 text-slate-700'}">${c.saved ? 'Saved ★' : 'Save college'}</button>
    </div>
    <div class="grid grid-cols-1 md:grid-cols-2 gap-5">
      <section><h4 class="font-bold text-slate-900 mb-1">Admissions <span class="text-[11px] font-normal text-slate-500">(${esc(y.admissions || '')})</span></h4><dl class="text-xs">
        ${row('Admission rate', A.rate != null ? pct(A.rate) : (A.openAdmission ? 'Open admission' : null))}
        ${row('SAT middle 50% (approx. total)', A.satMiddle50 ? A.satMiddle50.join('–') : null)}
        ${row('ACT middle 50%', A.actMiddle50 ? A.actMiddle50.join('–') : null)}
        ${row('Test scores', A.testPolicy)}
        ${row('Undergraduates', c.undergrads != null ? Number(c.undergrads).toLocaleString() : null)}
        ${row('Highest degree offered', c.highestDegree)}</dl></section>
      <section><h4 class="font-bold text-slate-900 mb-1">Cost</h4><dl class="text-xs">
        ${row('Tuition & fees, in-state (2024-25)', money(C.tuitionInState))}
        ${row('Tuition & fees, out-of-state (2024-25)', money(C.tuitionOutOfState))}
        ${row('Total cost of attendance (2023-24 avg.)', money(C.costOfAttendance))}
        ${row('Average net price after grants (2023-24)', money(C.avgNetPrice))}
        ${C.netPriceByIncome.map(b => row('Net price, family income ' + b.band, money(b.amount))).join('')}</dl>
        <p class="text-[11px] text-slate-500 mt-1">${esc(C.note)} ${esc(C.inStateNote)}</p></section>
      <section><h4 class="font-bold text-slate-900 mb-1">Graduation & outcomes</h4><dl class="text-xs">
        ${row('Graduation rate (within 150% of normal time)', O.completionRate != null ? pct(O.completionRate) : null)}
        ${row('First-year retention', O.retentionRate != null ? pct(O.retentionRate) : null)}
        ${row('Median federal debt at graduation', money(O.medianDebt))}
        ${row('Median earnings 10 years after entry', money(O.medianEarnings10yr))}</dl>
        <p class="text-[11px] text-slate-500 mt-1">Graduation: ${esc(y.completion || '')}. Debt: ${esc(y.median_debt || '')}. Earnings: ${esc(y.median_earnings_10yr || '')}.</p></section>
      <section><h4 class="font-bold text-slate-900 mb-1">Financial aid</h4><dl class="text-xs">
        ${row('Undergraduates receiving Pell Grants', c.aid.pellShare != null ? pct(c.aid.pellShare) : null)}</dl>
        <p class="text-[11px] text-slate-500 mt-1">Your own aid depends on your FAFSA. Use the Net Price Calculator for an estimate.</p></section>
    </div>
    <section><h4 class="font-bold text-slate-900 mb-1">Bachelor's degree fields offered (IPEDS, ${esc((y.name_location_programs || '').split(';')[1] || 'AY2024-25')})</h4>
      <div class="flex flex-wrap gap-1">${c.programs.bachelor.length ? chips(c.programs.bachelor) : '<span class="text-xs text-slate-500">No bachelor\'s programs reported.</span>'}</div>
      ${c.programs.associate.length ? `<h4 class="font-bold text-slate-900 mt-2 mb-1">Associate degree fields</h4><div class="flex flex-wrap gap-1">${chips(c.programs.associate)}</div>` : ''}</section>
    <section class="text-[11px] text-slate-600 bg-slate-50 rounded-xl p-3">
      <strong>Sources:</strong> ${c.sources.map(s => extLink(s.url, s.title)).join(' · ')}.
      Data from ${c.dataSource ? extLink(c.dataSource.url, c.dataSource.title) + ', released ' + esc(c.dataSource.released) : 'College Scorecard'}. These are published figures, not current offers or guarantees. Confirm everything with the college.
    </section>`;
  icons();
}

async function toggleCollegeSave(unitid) {
  const name = document.getElementById('college-name').textContent;
  const btn = document.getElementById('college-save-btn');
  const saved = btn.getAttribute('aria-pressed') === 'true';
  try {
    await api(saved ? 'DELETE' : 'POST', `/api/colleges/${unitid}/save`);
    btn.setAttribute('aria-pressed', String(!saved));
    btn.textContent = saved ? 'Save college' : 'Saved ★';
    btn.className = 'px-3 py-1.5 rounded-lg border ' + (!saved ? 'bg-amber-100 border-amber-300 text-amber-900' : 'border-slate-300 text-slate-700');
    savedColleges = await api('GET', '/api/colleges/saved');
    renderSavedColleges();
    toast(saved ? `Removed ${name} from saved colleges.` : `Saved ${name}.`);
  } catch (err) { toast(err.message, 'error'); }
}

function renderSavedColleges() {
  const ul = document.getElementById('saved-colleges');
  ul.innerHTML = savedColleges.length ? savedColleges.map(c => `<li><button type="button" class="text-sky-800 underline" onclick="openCollege(${Number(c.unitid)})">${esc(c.name)}</button> <span class="text-xs text-slate-500">${esc(c.city)}, ${esc(c.state)}</span></li>`).join('')
    : '<li class="text-xs text-slate-600">No saved colleges yet. Open a college\'s details and choose "Save college".</li>';
}

// ------------------------------------------------------------------ assistant
function initAdvisorChat() {
  document.getElementById('chat-messages').innerHTML = bubble('bot',
    `<p>Hi! I'm the NextStep Assistant. I answer from NextStep's guides and official sources like StudentAid.gov and HESC, and I'll tell you when I don't know something. Ask me about FAFSA, TAP, scholarships, colleges, internships, mentors or how this site works.</p>`);
}
function bubble(who, html) {
  return who === 'me'
    ? `<div class="flex flex-col items-end"><div class="bg-sky-600 text-white rounded-2xl px-4 py-3 text-xs sm:text-sm shadow-sm max-w-[85%]">${html}</div></div>`
    : `<div class="flex flex-col items-start"><div class="chat-text bg-white border border-slate-200 text-slate-800 rounded-2xl px-4 py-3 text-xs sm:text-sm shadow-sm max-w-[90%] space-y-2">${html}</div></div>`;
}
function formatAnswer(text) {
  // Plain text from the server -> escaped HTML with simple lists. No raw HTML is ever trusted.
  const lines = String(text).split('\n');
  let html = '', list = null;
  const close = () => { if (list) { html += `</${list}>`; list = null; } };
  lines.forEach(l => {
    const bullet = l.match(/^- (.*)/), num = l.match(/^\d+\. (.*)/);
    if (bullet || num) {
      const want = bullet ? 'ul' : 'ol';
      if (list !== want) { close(); html += `<${want} class="${want === 'ul' ? 'list-disc' : 'list-decimal'} pl-5 space-y-1">`; list = want; }
      html += `<li>${esc((bullet || num)[1])}</li>`;
    } else { close(); if (l.trim()) html += `<p>${esc(l)}</p>`; }
  });
  close();
  return html;
}
function renderReply(r) {
  let html = formatAnswer(r.answer);
  if (r.tabs && r.tabs.length) html += `<p class="flex flex-wrap gap-2">${r.tabs.map(t => `<button type="button" class="px-2 py-1 rounded-lg bg-slate-100 border border-slate-200 text-xs" onclick="switchTab('${esc(t.tab)}')">Go to ${esc(t.title)}</button>`).join('')}</p>`;
  if (r.sources && r.sources.length) html += `<div class="text-xs"><p class="font-semibold text-slate-700">Sources:</p><ul class="list-disc pl-5">${r.sources.map(s => `<li>${extLink(s.url, s.title)}</li>`).join('')}</ul></div>`;
  if (r.basis_note) html += `<p class="text-[11px] text-slate-500">${esc(r.basis_note)}</p>`;
  return html;
}
let lastQuestion = '';
async function sendChatMessage(retryText) {
  const input = document.getElementById('chat-input');
  const text = (retryText || input.value).trim();
  if (!text || pending.has('chat')) return;
  const box = document.getElementById('chat-messages');
  if (!retryText) { box.insertAdjacentHTML('beforeend', bubble('me', esc(text))); input.value = ''; }
  lastQuestion = text;
  pending.add('chat');
  document.getElementById('chat-send').disabled = true;
  box.insertAdjacentHTML('beforeend', `<div id="chat-loading" class="text-xs text-slate-500 flex items-center gap-2"><span class="inline-block w-3 h-3 rounded-full bg-sky-400 animate-pulse"></span> Looking that up…</div>`);
  box.scrollTop = box.scrollHeight;
  try {
    const r = await api('POST', '/api/assistant', { message: text });
    box.insertAdjacentHTML('beforeend', bubble('bot', renderReply(r)));
  } catch (err) {
    box.insertAdjacentHTML('beforeend', bubble('bot', `<p class="text-rose-700">Sorry, I couldn't answer that: ${esc(err.message)}</p>
      <button type="button" class="px-2 py-1 rounded-lg bg-slate-100 border border-slate-200 text-xs" onclick="this.disabled=true; sendChatMessage(lastQuestion)">Try again</button>`));
  } finally {
    const l = document.getElementById('chat-loading'); if (l) l.remove();
    pending.delete('chat');
    document.getElementById('chat-send').disabled = false;
    box.scrollTop = box.scrollHeight;
  }
}
function sendQuickPrompt(prompt) {
  document.getElementById('chat-input').value = prompt;
  sendChatMessage();
}
