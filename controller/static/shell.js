// controller/static/shell.js — the four-tab shell. Each tab (and, for Setup and Experiment,
// each rig) is an embedded frame (iframe) that keeps running while hidden; the shell only
// switches which one is visible, tells frames when they are shown/hidden (so only the visible
// one streams camera video), and paints phase badges from a 2 s /api/fleet poll.

const TABS_WITH_RIGS = {setup: '/setup/', experiment: '/experiment/'};
const SINGLE_PAGES = {network: '/network', data: '/data'};

let state = {tab: 'network', open: {setup: [], experiment: []}, active: {setup: null, experiment: null}};
let rigsInfo = {rigs: [], groups: {}};
let fleet = {};
const frames = {};          // key -> iframe

// ── persistence (which tabs were open) ──
function saveState() { try { localStorage.setItem('vrfarm.shell', JSON.stringify(state)); } catch (e) {} }
function loadState() {
  try {
    const s = JSON.parse(localStorage.getItem('vrfarm.shell') || 'null');
    if (s && s.open) state = {...state, ...s, open: {setup: s.open.setup || [], experiment: s.open.experiment || []}};
  } catch (e) {}
}

// ── frames ──
function frameKey(tab, rig) { return rig ? `${tab}:${rig}` : tab; }

function ensureFrame(tab, rig) {
  const key = frameKey(tab, rig);
  if (frames[key]) return frames[key];
  const f = document.createElement('iframe');
  f.src = rig ? TABS_WITH_RIGS[tab] + encodeURIComponent(rig) : SINGLE_PAGES[tab];
  f.className = 'hidden';
  f.dataset.key = key;
  document.getElementById('frames').appendChild(f);
  frames[key] = f;
  return f;
}

function post(frame, msg) {
  try { frame.contentWindow.postMessage(msg, '*'); } catch (e) {}
}

function showFrame(key) {
  for (const [k, f] of Object.entries(frames)) {
    const vis = k === key;
    if (f.classList.contains('hidden') === vis) {
      f.classList.toggle('hidden', !vis);
      post(f, {type: 'vrfarm:visible', visible: vis});
    }
  }
  document.getElementById('empty').style.display = key ? 'none' : 'flex';
}

function dropFrame(key) {
  const f = frames[key];
  if (f) { f.remove(); delete frames[key]; }
}

// ── tabs ──
function showTab(tab) {
  state.tab = tab;
  document.querySelectorAll('#rail .tab').forEach(el => el.classList.toggle('active', el.dataset.tab === tab));
  const strip = document.getElementById('substrip');
  if (TABS_WITH_RIGS[tab]) {
    strip.classList.add('show');
    renderRigTabs();
    const rig = state.active[tab] || state.open[tab][0] || null;
    if (rig) { state.active[tab] = rig; ensureFrame(tab, rig); showFrame(frameKey(tab, rig)); }
    else showFrame(null);
  } else {
    strip.classList.remove('show');
    ensureFrame(tab, null);
    showFrame(frameKey(tab, null));
  }
  saveState();
}

function openRig(tab, rig) {
  if (!state.open[tab].includes(rig)) state.open[tab].push(rig);
  state.active[tab] = rig;
  ensureFrame(tab, rig);
  if (state.tab === tab) { renderRigTabs(); showFrame(frameKey(tab, rig)); }
  saveState();
}

async function closeRig(tab, rig, ev) {
  if (ev) ev.stopPropagation();
  const other = Object.keys(TABS_WITH_RIGS).find(t => t !== tab);
  // The rig stays loaded in the server while any tab shows it; unload only when both are closed.
  if (!state.open[other].includes(rig)) {
    const r = await fetch(`/api/rigs/${encodeURIComponent(rig)}/unload`, {method: 'POST'}).then(r => r.json()).catch(() => ({ok: false, error: 'server unreachable'}));
    if (r && r.ok === false && r.error && !/not loaded/.test(r.error)) { toast(`Cannot close ${rig}: ${r.error}`); return; }
  }
  state.open[tab] = state.open[tab].filter(r => r !== rig);
  dropFrame(frameKey(tab, rig));
  if (state.active[tab] === rig) state.active[tab] = state.open[tab][0] || null;
  showTab(tab);
}

function activateRig(tab, rig) {
  state.active[tab] = rig;
  ensureFrame(tab, rig);
  renderRigTabs();
  showFrame(frameKey(tab, rig));
  saveState();
}

function badgeFor(rig) {
  const s = fleet[rig];
  if (!s) return '<span class="badge badge-setup">not loaded</span>';
  let info = '';
  if (s.phase === 'running' || s.phase === 'ended') {
    info = ` <span class="info">${s.n_trials}${s.n_planned ? '/' + s.n_planned : ''}` +
           (s.hit_rate != null ? ` · HR ${s.hit_rate.toFixed(2)}` : '') + '</span>';
  }
  return `<span class="badge badge-${s.phase}">${s.phase}</span>${info}`;
}

function renderRigTabs() {
  const tab = state.tab;
  const host = document.getElementById('rigtabs');
  if (!TABS_WITH_RIGS[tab]) { host.innerHTML = ''; return; }
  host.innerHTML = state.open[tab].map(rig =>
    `<span class="rigtab ${state.active[tab] === rig ? 'active' : ''}" onclick="activateRig('${tab}','${rig}')">
       ${rig} ${badgeFor(rig)} <span class="x" title="Close (unloads the rig when closed in both tabs)" onclick="closeRig('${tab}','${rig}',event)">✕</span>
     </span>`).join('');
  document.getElementById('substrip-hint').textContent = state.open[tab].length ? '' :
    (tab === 'setup' ? 'Load a rig or a group to set it up.' : 'Load a rig or a group to run experiments.');
}

// ── load menu (rigs + groups) ──
async function refreshRigs() {
  try { rigsInfo = await (await fetch('/api/rigs')).json(); } catch (e) {}
  const el = document.getElementById('loadmenu-items');
  const groups = Object.entries(rigsInfo.groups || {});
  el.innerHTML =
    '<div class="hdr">Rigs</div>' +
    (rigsInfo.rigs || []).map(r => `<div class="item" onclick="pickRig('${r.name}')">${r.name}<small>${r.phase || ''}</small></div>`).join('') +
    (groups.length ? '<div class="hdr">Groups (super rigs)</div>' +
      groups.map(([g, members]) => `<div class="item" onclick="pickGroup('${g}')">★ ${g}<small>${members.length} rigs</small></div>`).join('') : '') +
    '<div class="hdr"><a href="#" onclick="showTab(\'network\'); return false;">manage rigs and groups in Network</a></div>';
}

function toggleLoadMenu(ev) {
  ev.stopPropagation();
  refreshRigs();
  document.getElementById('loadmenu').classList.toggle('open');
}
document.addEventListener('click', () => document.getElementById('loadmenu').classList.remove('open'));

function pickRig(rig) {
  document.getElementById('loadmenu').classList.remove('open');
  openRig(state.tab, rig);
}

function pickGroup(g) {
  document.getElementById('loadmenu').classList.remove('open');
  const members = (rigsInfo.groups || {})[g] || [];
  members.forEach((rig, i) => { if (i === 0) openRig(state.tab, rig); else { if (!state.open[state.tab].includes(rig)) state.open[state.tab].push(rig); ensureFrame(state.tab, rig); } });
  renderRigTabs(); saveState();
}

// ── fleet poll ──
async function pollFleet() {
  try {
    const d = await (await fetch('/api/fleet')).json();
    fleet = d.rigs || {};
    const running = Object.values(fleet).filter(s => s.phase === 'running').length;
    const loaded = Object.keys(fleet).length;
    document.getElementById('fleet-summary').textContent =
      loaded ? `${loaded} rig${loaded === 1 ? '' : 's'} loaded · ${running} running` : 'no rigs loaded';
    if (TABS_WITH_RIGS[state.tab]) renderRigTabs();
  } catch (e) {}
}
setInterval(pollFleet, 2000);

// ── messages from frames ──
window.addEventListener('message', ev => {
  const m = ev.data || {};
  if (m.type === 'vrfarm:phase' && m.rig) {
    fleet[m.rig] = {...(fleet[m.rig] || {}), phase: m.phase, n_trials: (fleet[m.rig] || {}).n_trials || 0};
    if (TABS_WITH_RIGS[state.tab]) renderRigTabs();
  } else if (m.type === 'vrfarm:rig-saved' && m.rig) {
    const f = frames[frameKey('experiment', m.rig)];
    if (f) post(f, {type: 'vrfarm:reload-config', rig: m.rig});
  } else if (m.type === 'vrfarm:open' && m.tab) {
    if (m.rig) openRig(m.tab, m.rig);
    showTab(m.tab);
  }
});

// ── misc ──
let toastT = null;
function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg; el.style.display = 'block';
  clearTimeout(toastT); toastT = setTimeout(() => el.style.display = 'none', 6000);
}

async function quitApp() {
  if (!confirm('Stop the VRFarm controller server?')) return;
  const r = await fetch('/api/quit', {method: 'POST'}).then(r => r.json()).catch(() => ({ok: true}));
  if (r && r.ok === false) { toast(r.error || 'refused'); return; }
  document.body.innerHTML = '<div style="text-align:center;margin-top:40vh;color:#888;font-size:16px">Server stopped. You can close this tab.</div>';
}

// ── boot ──
loadState();
refreshRigs().then(() => {
  // Re-open the rig frames that were open last time, but only rigs that still exist.
  const known = new Set((rigsInfo.rigs || []).map(r => r.name));
  for (const tab of Object.keys(TABS_WITH_RIGS)) {
    state.open[tab] = state.open[tab].filter(r => known.has(r));
    if (state.active[tab] && !state.open[tab].includes(state.active[tab])) state.active[tab] = null;
    state.open[tab].forEach(r => ensureFrame(tab, r));
  }
  showTab(state.tab || 'network');
  pollFleet();
});
