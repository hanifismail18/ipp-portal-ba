/* Admin Portal BA. All calls send header X-Admin-Token (kept in sessionStorage only). */
const SS_KEY = 'ba.admin';
const PROV = {
  gemini: ['Gemini', 'Utama, Google AI Studio'],
  gemini2: ['Gemini cadangan', 'Project Google kedua'],
  openrouter: ['OpenRouter', 'Hanya model gratis'],
  groq: ['Groq', 'Cadangan terakhir'],
};
const LAYER = {ok:['ok','Berfungsi'], 'missing-key':['n','Belum diisi'], 'no-free-model':['bad','Tidak ada model gratis'],
  'daily-cap':['w','Batas harian tercapai, pulih besok'], limited:['w','Kena limit, pulih otomatis'], down:['bad','Tidak tersambung']};
const A = {token: null, err: '', busy: false, keys: [], subs: [], usage: null, health: null, ui: {}, loaded: false};
const ssGet = () => { try { return sessionStorage.getItem(SS_KEY); } catch (e) { return null; } };
const ssSet = (v) => { try { v ? sessionStorage.setItem(SS_KEY, v) : sessionStorage.removeItem(SS_KEY); } catch (e) {} };

async function api(path, opts = {}){
  const r = await fetch(path, {...opts, headers: {...(opts.headers || {}), 'X-Admin-Token': A.token}});
  if (r.status === 401 || r.status === 429) {
    const msg = await errText(r);
    A.token = null; ssSet(null); A.err = r.status === 401 && A.loaded ? 'Sesi berakhir. Masukkan kode lagi.' : msg; A.loaded = false; render();
    throw new Error('auth');
  }
  return r;
}
async function loadAll(){
  const [k, s, u, h] = await Promise.all([
    api('/api/admin/keys').then(r => r.json()),
    api('/api/admin/submissions').then(r => r.json()),
    api('/api/admin/usage?days=31&limit=1').then(r => r.ok ? r.json() : null).catch(() => null),
    fetch('/api/health').then(r => r.json()).catch(() => null),
  ]);
  A.keys = k; A.subs = s.slice().reverse(); A.usage = u; A.health = h; A.loaded = true; render();
}
async function loadKeys(){ A.keys = await (await api('/api/admin/keys')).json(); render(); }

/* ---------- render ---------- */
function gate(){
  return `<main class="gut"><div class="wrap gate"><div class="gate-c">
    <div class="brand-mark" style="width:44px;height:44px;border-radius:12px">${ICON.lock}</div>
    <h1>Admin Portal BA</h1><p>Masukkan kode akses admin.</p>
    <form id="gate"><input class="input${A.err ? ' err' : ''}" type="password" id="code" placeholder="Kode akses" autocomplete="current-password" aria-label="Kode akses" required>
    ${A.err ? `<div class="err-t" role="alert">${esc(A.err)}</div>` : ''}<button class="btn btn-primary" ${A.busy ? 'disabled' : ''}>${A.busy ? 'Memeriksa…' : 'Masuk'}</button></form>
    <p style="margin-top:18px;font-size:13px"><a href="./">Kembali ke Portal BA</a></p><div style="margin-top:14px">${themeCtl()}</div></div></div></main>`;
}
function keyRow(k){
  const [name, role] = PROV[k.provider] || [k.provider, ''];
  const u = A.ui[k.provider] || {};
  let mid, acts;
  if (u.editing) {
    mid = `<div class="key-v"><div class="key-edit"><input class="input${u.err ? ' err' : ''}" type="text" data-draft="${k.provider}" value="${esc(u.draft || '')}" placeholder="Tempel key baru" autocomplete="off" autocapitalize="off" spellcheck="false" data-lpignore="true" data-1p-ignore aria-label="Key ${esc(name)}"></div>
      <div class="key-msg ${u.err ? 'bad' : ''}">${esc(u.err || (k.set ? 'Key lama diganti saat key baru disimpan.' : 'Terlihat penuh hanya saat diketik.'))}</div></div>`;
    acts = `<button class="btn btn-primary btn-sm" data-ksave="${k.provider}" ${u.busy ? 'disabled' : ''}>${u.busy ? 'Menyimpan…' : 'Simpan'}</button><button class="btn btn-sm" data-kcancel="${k.provider}">Batal</button>`;
  } else if (k.set) {
    const ls = LAYER[k.layer_status] || (k.layer_status ? ['n', k.layer_status] : ['n', 'Belum dicek']);
    const meta = k.source === 'admin' ? `Disimpan ${esc(fmtDT(k.updated_at))}` : 'Dari file .env server. Ganti key untuk menimpanya.';
    mid = `<div class="key-v"><div class="mask"><span class="dots">••••••••••••</span> ${esc(k.last4 || '')}</div><div class="key-st ${ls[0]}"><span class="dot"></span>${esc(ls[1])}</div>${u.test ? `<div class="key-msg ${u.test.c}">${esc(u.test.t)}</div>` : `<div class="key-meta">${meta}</div>`}</div>`;
    acts = `<button class="btn btn-sm" data-kedit="${k.provider}">Ganti key</button><button class="btn btn-sm" data-ktest="${k.provider}" ${u.testing ? 'disabled' : ''}>${u.testing ? 'Mengetes…' : 'Tes key'}</button>${k.source === 'admin' ? `<button class="btn btn-sm btn-danger${u.arm ? ' armed' : ''}" data-kdel="${k.provider}">${u.arm ? 'Yakin hapus?' : 'Hapus'}</button>` : ''}`;
  } else {
    mid = `<div class="key-v"><div class="key-st n"><span class="dot"></span>Belum diisi</div><div class="key-meta">Dilewati sampai key diisi.</div></div>`;
    acts = `<button class="btn btn-sm" data-kedit="${k.provider}">Isi key</button>`;
  }
  return `<div class="key"><div><div class="key-n">${esc(name)}</div><div class="key-r">${esc(role)}</div></div>${mid}<div class="key-a">${acts}</div></div>`;
}
function dash(){
  const now = new Date(), pad = (n) => String(n).padStart(2, '0');
  const today = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`, month = today.slice(0, 7);
  const subsToday = A.subs.filter(s => (s.submitted_at || '').startsWith(today)).length;
  const workersMonth = A.subs.filter(s => (s.submitted_at || '').startsWith(month)).reduce((n, s) => n + ((s.workers || []).length || 1), 0);
  const ditunda = A.health ? A.health.ditunda : null;
  const calls = A.usage ? `${A.usage.today_calls}${A.usage.cap_per_day ? `<span style="font-size:15px;color:var(--ink-3);font-weight:500"> / ${A.usage.cap_per_day}</span>` : ''}` : '–';
  const rows = A.subs.map(s => {
    const ws = s.workers || [], u = A.ui['rev:' + s.id] || {};
    const who = ws.length ? esc(ws[0].nama || '–') + (ws.length > 1 ? ` <span class="sub-t">+${ws.length - 1}</span>` : '') : '–';
    const link = s.public_url ? '<span class="lk on"><span class="dot"></span>Aktif</span>' : s.revoked ? '<span class="lk off"><span class="dot"></span>Dicabut</span>' : '<span class="lk n">Tidak ada</span>';
    return `<tr><td>${esc(fmtDT(s.submitted_at))}</td><td>${esc(s.submitter || '–')}</td><td class="file-c" style="max-width:280px" title="${esc(s.file)}">${esc(s.file)}</td><td>${who}</td><td>${link}</td>
      <td><div style="display:flex;gap:6px;justify-content:flex-end">${s.public_url ? `<button class="btn btn-sm" data-copy="${esc(s.public_url)}">${ICON.copy}Salin</button><button class="btn btn-sm btn-danger${u.arm ? ' armed' : ''}" data-rev="${s.id}">${u.arm ? 'Yakin cabut?' : 'Cabut link'}</button>` : ''}</div></td></tr>`;
  }).join('') || '<tr><td class="empty-row" colspan="6">Belum ada BA yang disubmit.</td></tr>';
  const mrows = ((A.usage && A.usage.models_today) || []).map(m => `<tr><td>${esc(m.engine)}</td><td>${esc(m.model)}</td><td>${m.calls || 0}</td><td>${m.n429 || 0}</td><td>${m.last_429 ? esc(fmtDT(m.last_429)) : '–'}</td><td>${m.cooldown_s ? (m.cooldown_s > 3600 ? 'sampai reset kuota' : m.cooldown_s + ' dtk') : '–'}</td></tr>`).join('') || '<tr><td class="empty-row" colspan="6">Belum ada panggilan hari ini.</td></tr>';
  return `<header class="top gut"><div class="wrap">
    <a class="brand" href="./"><span class="brand-mark">${ICON.doc}</span><span><span class="brand-name" style="display:block">Admin Portal BA</span><span class="brand-org">Dash Electric × IPP</span></span></a>
    ${themeCtl()}<button class="btn" id="logout">Keluar</button></div></header>
  <main class="gut"><div class="wrap adm">
    <div class="stats">
      <div class="stat"><b>${subsToday}</b><span>Submission hari ini, ${A.subs.length} total</span></div>
      <div class="stat"><b>${workersMonth}</b><span>Pekerja bulan ini</span></div>
      <div class="stat${ditunda ? ' w' : ''}"><b>${ditunda ?? '–'}</b><span>BA ditunda, antre otomatis</span></div>
      <div class="stat"><b>${calls}</b><span>Panggilan AI hari ini</span></div>
    </div>
    <section class="panel" aria-label="Kunci API"><div class="keys-intro"><h2>Kunci API</h2><p>Hanya key gratis. Dipakai berurutan dari atas; kalau satu kena limit, pindah ke berikutnya. Key terlihat penuh hanya saat diketik. Setelah disimpan, yang tampil hanya 4 karakter terakhir dan key tidak bisa dilihat lagi.</p></div>${A.keys.map(keyRow).join('')}</section>
    <section aria-label="Pemakaian per model">
      <div class="res-h"><div><h2 style="font-size:18px">Pemakaian per model</h2><p>Kuota gratis Google dihitung per model, bukan total. 429 = kuota model itu habis.</p></div></div>
      <div class="tbl-w"><table class="t" style="min-width:620px"><thead><tr><th>Layer</th><th>Model</th><th>Panggilan hari ini</th><th>429</th><th>429 terakhir</th><th>Jeda</th></tr></thead><tbody>${mrows}</tbody></table></div>
    </section>
    <section aria-label="Submission">
      <div class="res-h"><div><h2 style="font-size:18px">Submission</h2><p>Satu baris per BA, terbaru di atas. CSV berisi satu baris per pekerja.</p></div><div class="acts"><button class="btn" id="csv">${ICON.dl}Unduh CSV</button></div></div>
      <div class="tbl-w"><table class="t" style="min-width:820px"><thead><tr><th>Waktu</th><th>Pengirim</th><th>File</th><th>Pekerja</th><th>Link publik</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
    </section>
  </div></main>`;
}
function render(){
  const focus = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.draft : null;
  $('#app').innerHTML = A.token && A.loaded ? dash() : gate();
  if (focus) { const i = $(`[data-draft="${focus}"]`); if (i) { i.focus(); i.setSelectionRange(i.value.length, i.value.length); } }
  if (!A.token) $('#code')?.focus();
}

/* ---------- events ---------- */
document.addEventListener('submit', async e => {
  if (e.target.id !== 'gate') return;
  e.preventDefault();
  const v = $('#code').value.trim(); if (!v) return;
  A.token = v; A.err = ''; A.busy = true; render();
  try { await loadAll(); ssSet(v); } catch (err) { if (err.message !== 'auth') { A.token = null; A.err = 'Server tidak bisa dihubungi.'; } }
  A.busy = false; render();
});
document.addEventListener('input', e => { const p = e.target.dataset.draft; if (p) A.ui[p] = {...A.ui[p], draft: e.target.value, err: ''}; });
document.addEventListener('keydown', e => {
  const p = e.target.dataset && e.target.dataset.draft; if (!p) return;
  if (e.key === 'Enter') { e.preventDefault(); $(`[data-ksave="${p}"]`)?.click(); }
  if (e.key === 'Escape') { $(`[data-kcancel="${p}"]`)?.click(); }
});
document.addEventListener('click', async e => {
  const t = e.target.closest('button'); if (!t) return;
  const d = t.dataset;
  try {
    if (t.id === 'logout') { A.token = null; A.loaded = false; A.err = ''; ssSet(null); render(); return; }
    if (d.copy) { copyText(d.copy); return; }
    if (t.id === 'csv') {
      const r = await api('/api/admin/export.csv');
      if (!r.ok) { toast(await errText(r)); return; }
      download(`ba_submissions_${new Date().toISOString().slice(0, 10)}.csv`, await r.text()); return;
    }
    if (d.kedit) { A.ui[d.kedit] = {editing: true, draft: '', err: ''}; render(); $(`[data-draft="${d.kedit}"]`)?.focus(); return; }
    if (d.kcancel) { A.ui[d.kcancel] = {}; render(); return; }
    if (d.ksave) {
      const p = d.ksave, u = A.ui[p] || {}, key = (u.draft || '').trim();
      if (!/^[A-Za-z0-9_.:-]{16,300}$/.test(key)) { A.ui[p] = {...u, err: 'Key tidak valid: 16–300 karakter, hanya huruf, angka, atau _ - . :'}; render(); return; }
      A.ui[p] = {...u, busy: true}; render();
      const r = await api(`/api/admin/keys/${p}`, {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({key})});
      if (!r.ok) { A.ui[p] = {...u, busy: false, err: await errText(r)}; render(); return; }
      A.ui[p] = {}; toast(`Key ${PROV[p]?.[0] || p} disimpan`); await loadKeys(); return;
    }
    if (d.ktest) {
      const p = d.ktest; A.ui[p] = {testing: true}; render();
      const r = await api(`/api/admin/keys/${p}/test`, {method: 'POST'});
      const j = r.ok ? await r.json() : {result: 'error', http: r.status};
      const http = j.http ? ` (HTTP ${j.http})` : '';
      const msg = {ok: ['ok', `Berfungsi.${j.model ? ` Model: ${j.model}.` : ''}`], limited: ['w', `Kena limit${http}. Dipakai lagi setelah pulih.`],
        invalid: ['bad', `Key ditolak provider${http}. Ganti key.`], 'missing-key': ['bad', 'Belum ada key.']}[j.result] || ['bad', `Tes gagal${http}. Coba lagi nanti.`];
      A.ui[p] = {test: {c: msg[0], t: msg[1]}}; await loadKeys(); return;
    }
    if (d.kdel) {
      const p = d.kdel, u = A.ui[p] || {};
      if (!u.arm) { A.ui[p] = {arm: true}; render(); setTimeout(() => { if (A.ui[p]?.arm) { A.ui[p] = {}; render(); } }, 3000); return; }
      const r = await api(`/api/admin/keys/${p}`, {method: 'DELETE'});
      A.ui[p] = {}; toast(r.ok ? `Key ${PROV[p]?.[0] || p} dihapus` : await errText(r)); await loadKeys(); return;
    }
    if (d.rev) {
      const id = d.rev, k = 'rev:' + id, u = A.ui[k] || {};
      if (!u.arm) { A.ui[k] = {arm: true}; render(); setTimeout(() => { if (A.ui[k]?.arm) { A.ui[k] = {}; render(); } }, 3000); return; }
      const r = await api(`/api/admin/revoke/${id}`, {method: 'POST'});
      A.ui[k] = {};
      if (r.ok) { const s = A.subs.find(x => x.id === id); if (s) { s.public_url = null; s.revoked = true; } toast('Link dicabut'); } else toast(await errText(r));
      render(); return;
    }
  } catch (err) { if (err.message !== 'auth') toast('Server tidak bisa dihubungi.'); }
});

/* ---------- boot ---------- */
A.token = ssGet();
render();
if (A.token) loadAll().catch(() => {});
