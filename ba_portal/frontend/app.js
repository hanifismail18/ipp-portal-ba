/* Portal BA: submitter page. Contract: SPEC_PORTAL.md + BACKEND_NOTES.md (v3). */
const CRIT_KEYS = ['1_identitas','2_periode','3_hk_diajukan','4_ttd','5_kehadiran','6_transaksi'];
const CRIT_LABEL = {'1_identitas':'Identitas pekerja','2_periode':'Periode kekurangan gaji','3_hk_diajukan':'HK diajukan',
  '4_ttd':'Tanda tangan','5_kehadiran':'Lampiran kehadiran DMS','6_transaksi':'Lampiran transaksi DMS'};
const CRIT_SHORT = {'1_identitas':'Identitas','2_periode':'Periode','3_hk_diajukan':'HK','4_ttd':'TTD','5_kehadiran':'Hadir','6_transaksi':'TRX'};
const FIX_LABEL = {'1_identitas':'Identitas','2_periode':'Periode','4_ttd':'TTD','5_kehadiran':'Kehadiran','6_transaksi':'Transaksi'};
const LABEL3 = {kurang_hk:'HK diajukan', kurang_nominal:'Nominal kekurangan', single_tanggal:'Tanggal yang diajukan'};
const JENIS = {kurang_hk:'Kekurangan HK', kurang_nominal:'Selisih nominal', single_tanggal:'Satu tanggal', lainnya:'Jenis lain'};
const PAGE_WHAT = {BA:'BA', kehadiran:'Kehadiran', transaksi:'Transaksi', lain:'Lain'};
const MAX_MB = 20, LS_FILES = 'ba.files.v3', LS_NAME = 'ba.submitter';

const S = {items: [], sel: null, page: 1, checked: new Set(), stageKey: '', clearArm: false};
let lidN = 0;

/* ---------- storage ---------- */
const ls = {get(k, d){ try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
            set(k, v){ try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }};
const save = () => ls.set(LS_FILES, S.items.filter(i => i.id).map(i => ({id: i.id, name: i.name, subAt: i.subAt || null})));
const submitter = () => $('#name').value.trim();

/* ---------- item helpers ---------- */
const cur = () => S.items.find(i => i.lid === S.sel);
const st = (it) => it.d ? it.d.status : (it.st === 'upload-error' ? it.st : 'uploading');          // uploading | upload-error | processing | ditunda | error | done
const label3 = (d) => LABEL3[d.jenis] || 'HK diajukan';
const critLabel = (d, k) => k === '3_hk_diajukan' ? label3(d) : CRIT_LABEL[k];
function workerCrit(w, d, k){                            // per-worker criterion status if the API provides it
  const c = w && w.criteria;
  if (Array.isArray(c)) { const x = c.find(e => e.key === k); if (x) return x.status; }
  else if (c && typeof c === 'object' && c[k]) return c[k];
  const b = (d.criteria || []).find(e => e.key === k); return b ? b.status : null;
}
function fixNote(c){                                   // shared issue once; per-worker issues point to the Pekerja list
  const n = (c.note || '').trim(); let m;
  if (c.status === 'KOSONG' && !/^\d+ dari/i.test(n)) return 'tidak ada';
  if ((m = n.match(/^Semua \d+ pekerja:\s*(.+)$/i))) return m[1];
  if ((m = n.match(/^(\d+) dari \d+ pekerja:/i))) return `${m[1]} pekerja, lihat Pekerja`;
  return n ? fmtPer(n) : (c.status === 'KOSONG' ? 'tidak ada di dokumen' : 'perlu diperbaiki');
}
function fixes(d){
  const out = [], seen = new Set();
  const add = (l, t) => { const k = t.toLowerCase(); if (!seen.has(k)) { seen.add(k); out.push([l, t]); } };
  if (d.quality === 'poor') add('Foto', 'kurang jelas, scan ulang');
  (d.criteria || []).forEach(c => { if (c.status !== 'VALID') add(c.key === '3_hk_diajukan' ? label3(d) : FIX_LABEL[c.key] || c.key, fixNote(c)); });
  return out;
}
function otherAlerts(d){                                 // drop alerts that only repeat criteria
  const crit = (d.criteria || []).map(c => `${c.label || ''} ${c.note || ''}`.toLowerCase()).join(' | ');
  return [...new Set(d.alerts || [])].filter(a => {
    if (/^\d+ dari \d+ pekerja bermasalah/i.test(a)) return false;
    const body = (a.includes(':') ? a.slice(a.indexOf(':') + 1) : a).trim().toLowerCase();
    return !(body && crit.includes(body));
  });
}
function ownProblems(w, ws){                            // problems not shared by every worker, one per criterion
  const all = Array.isArray(w.problems) ? w.problems : [];
  const own = ws.length > 1 ? all.filter(p => !ws.every(x => (x.problems || []).includes(p))) : all;
  const seen = new Set(), out = [];
  own.forEach(p => { const i = p.indexOf(':'), k = i > 0 && i < 20 ? p.slice(0, i) : p;
    if (!seen.has(k)) { seen.add(k); out.push(p); } });
  return out.filter(p => { const m = p.match(/\(([^)]{8,})\)/);                  // drop a warning that restates another problem
    return !(m && out.some(q => q !== p && q.includes(m[1]))); });
}
const clip = (txt, cls) => txt.length > 56
  ? `<div class="${cls} clip"><span>${esc(txt)}</span><button class="linkbtn more" data-more>lihat detail</button></div>`
  : `<div class="${cls}">${esc(txt)}</div>`;
function fileStatus(it){
  const s = st(it), d = it.d;
  if (s === 'uploading') return {m:'P', t:'Mengupload…', c:''};
  if (s === 'upload-error') return {m:'N', t:it.err || 'Gagal diupload', c:'i'};
  if (s === 'processing') return {m:'P', t:d.queue_position > 0 ? `Antrean ke-${d.queue_position}` : 'Memproses', c:''};
  if (s === 'ditunda') return {m:'W', t:d.retry_at ? `Ditunda, coba lagi ${fmtTime(d.retry_at)}` : 'Ditunda', c:'w'};
  if (s === 'error') return {m:'N', t:'Gagal diproses', c:''};
  if (d.submitted) return {m:'S', t:it.subAt ? `Disubmit ${fmtTime(it.subAt)}` : 'Sudah disubmit', c:''};
  if (d.verdict === 'VALID') return {m:'V', t:'VALID', c:'v'};
  const n = fixes(d).length;
  return {m:'I', t:d.quality === 'poor' ? 'INVALID, foto kurang jelas' : n ? `INVALID, ${n} perbaikan` : 'INVALID', c:'i'};
}

/* ---------- network ---------- */
async function fetchBA(it){
  try {
    const r = await fetch(`/api/ba/${it.id}`);
    if (r.status === 404) { S.items = S.items.filter(x => x !== it); save(); if (S.sel === it.lid) S.sel = S.items[0]?.lid ?? null; renderAll(); return; }
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    const changed = JSON.stringify(d) !== JSON.stringify(it.d);
    it.d = d; it.st = d.status;
    if (changed) renderAll();
  } catch (e) { /* network hiccup: keep polling */ }
  schedule(it);
}
function schedule(it){
  clearTimeout(it.t);
  const s = st(it);
  if (s === 'processing') it.t = setTimeout(() => fetchBA(it), 1500);
  else if (s === 'ditunda') it.t = setTimeout(() => fetchBA(it), 30000);
  else if (!it.d && it.id) it.t = setTimeout(() => fetchBA(it), 3000);
}
let upChain = Promise.resolve();
function addFiles(list){
  const errs = [];
  let first = null;
  [...list].forEach(f => {
    if (!/\.pdf$/i.test(f.name) && f.type !== 'application/pdf') { errs.push(`${f.name}: bukan PDF.`); return; }
    if (f.size > MAX_MB * 1048576) { errs.push(`${f.name}: lebih dari ${MAX_MB} MB.`); return; }
    const it = {lid: ++lidN, id: null, name: f.name, st: 'uploading', d: null};
    S.items.push(it); first = first || it;
    upChain = upChain.then(() => upload(it, f));
  });
  showMsgs(errs);
  if (first) { S.sel = first.lid; S.page = 1; }
  renderAll();
}
async function upload(it, f){
  try {
    const fd = new FormData(); fd.append('file', f);
    const r = await fetch('/api/upload', {method:'POST', body: fd});
    if (r.status !== 202 && r.status !== 200) { it.st = 'upload-error'; it.err = await errText(r); renderAll(); return; }
    const j = await r.json();
    const dup = S.items.find(x => x.id === j.id && x !== it);
    if (dup) { S.items = S.items.filter(x => x !== it); if (S.sel === it.lid) S.sel = dup.lid; toast(`${it.name} sudah ada di daftar.`); renderAll(); return; }
    it.id = j.id; save();
    await fetchBA(it);
  } catch (e) { it.st = 'upload-error'; it.err = 'Koneksi terputus. Coba lagi.'; renderAll(); }
}
async function submitOne(it){
  const name = submitter(); if (!name) { $('#name').focus(); toast('Isi nama pengirim dulu.'); return; }
  const btn = $('[data-submit]'); if (btn) { btn.disabled = true; btn.textContent = 'Mengirim…'; }
  const r = await fetch(`/api/ba/${it.id}/submit`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({submitter: name})});
  if (!r.ok) { toast(await errText(r)); renderAll(); return; }
  const j = await r.json();
  it.subAt = new Date().toISOString(); save();
  Object.assign(it.d, {submitted: true, can_submit: false, public_url: j.public_url || it.d.public_url});
  renderAll(); toast('BA disubmit. Link publik siap.');
  fetchBA(it);
}
async function submitBulk(){
  const name = submitter(); if (!name) { $('#name').focus(); toast('Isi nama pengirim dulu.'); return; }
  const ok = S.items.filter(i => i.d && i.d.can_submit);
  const pick = ok.filter(i => S.checked.has(i.id));
  const targets = pick.length ? pick : ok;
  if (!targets.length) return;
  const r = await fetch('/api/submit-bulk', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ids: targets.map(i => i.id), submitter: name})});
  if (!r.ok) { toast(await errText(r)); return; }
  const {results = []} = await r.json();
  let n = 0; const fail = [];
  results.forEach(x => {
    const it = S.items.find(i => i.id === x.id); if (!it) return;
    if (x.submitted) { n++; it.subAt = new Date().toISOString(); Object.assign(it.d, {submitted: true, can_submit: false, public_url: x.public_url}); S.checked.delete(it.id); }
    else fail.push(`${it.name}: ${x.reason || 'gagal'}`);
  });
  save(); showMsgs(fail); renderAll();
  toast(fail.length ? `${n} BA disubmit, ${fail.length} gagal.` : `${n} BA disubmit.`);
}

/* ---------- render ---------- */
function showMsgs(list){
  const el = $('#msgs'); el.hidden = !list.length;
  el.innerHTML = list.map(m => `<div class="msg">${esc(m)}</div>`).join('');
}
function renderAll(){
  const has = S.items.length > 0;
  $('#drop').classList.toggle('hero', !has);
  $('#drop .drop-s').textContent = has ? `Bisa banyak file sekaligus. Maks ${MAX_MB} MB per file.` : `Bisa banyak file sekaligus. Maks ${MAX_MB} MB per file. Hasil cek muncul otomatis.`;
  $('#desk').hidden = !has; $('#res').hidden = !has;
  if (!has) return;
  if (!cur()) S.sel = S.items[0].lid;
  renderRail(); renderBar(); renderStage(); renderVerdict(); renderResults();
}
function renderRail(){
  $('#railh').innerHTML = `<span style="color:var(--ink);font-weight:600;font-size:14px">File <span>${S.items.length}</span></span>
    <button class="linkbtn" id="clear">${S.clearArm ? 'Yakin kosongkan?' : 'Kosongkan daftar'}</button>`;
  $('#files').innerHTML = S.items.map(it => { const s = fileStatus(it);
    return `<li><button class="file" data-f="${it.lid}" aria-current="${it.lid === S.sel}">${mark(s.m)}<span class="file-b"><span class="file-n" title="${esc(it.name)}">${esc(it.name)}</span><span class="file-s ${s.c}">${esc(s.t)}</span></span></button></li>`; }).join('');
}
function pageCount(it){ return (it.d && (it.pdfPages || it.d.pages)) || it.pdfPages || 0; }
function renderBar(){
  const it = cur(), idx = S.items.indexOf(it), real = pageCount(it), ms = real ? missing(it.d) : [], n = real + ms.length;
  const notes = (it.d && it.d.page_notes) || [];
  const tabs = n > 1 && real <= 10 ? `<div class="ptabs">${Array.from({length: real}, (_, i) => {
      const w = notes.find(p => p.page === i + 1); return `<button class="ptab" data-p="${i + 1}" aria-pressed="${S.page === i + 1}"><b>${i + 1}</b>${w ? esc(PAGE_WHAT[w.what] || cap(w.what)) : ''}</button>`; }).join('')}${ms.map((m, j) =>
      `<button class="ptab miss" data-p="${real + j + 1}" aria-pressed="${S.page === real + j + 1}" title="Lampiran ${m} tidak ada"><b>${real + j + 1}</b>${cap(m)}</button>`).join('')}</div>` : '';
  $('#vbar').innerHTML = `<div class="nav"><button data-nav="-1" ${idx <= 0 ? 'disabled' : ''} aria-label="File sebelumnya">${ICON.l}</button><span>File ${idx + 1} dari ${S.items.length}</span><button data-nav="1" ${idx >= S.items.length - 1 ? 'disabled' : ''} aria-label="File berikutnya">${ICON.r}</button></div>
    ${n ? `<div class="nav"><button data-pg="-1" ${S.page <= 1 ? 'disabled' : ''} aria-label="Halaman sebelumnya">${ICON.l}</button><span>Hal ${S.page} / ${n}</span><button data-pg="1" ${S.page >= n ? 'disabled' : ''} aria-label="Halaman berikutnya">${ICON.r}</button></div>` : ''}
    ${tabs}`;
}
function sigSlots(d){                                 // unsigned slots from signature_slots (BA + per worker); [] until backend sends them
  if (!d) return [];
  const all = [...(Array.isArray(d.signature_slots) ? d.signature_slots : []),
    ...(d.workers || []).flatMap(w => Array.isArray(w.signature_slots) ? w.signature_slots : [])];
  const seen = new Set();
  return all.filter(x => x && x.signed === false && Array.isArray(x.bbox) && x.bbox.length === 4 && x.bbox.every(n => typeof n === 'number' && n >= 0 && n <= 1) && x.bbox[2] > x.bbox[0] && x.bbox[3] > x.bbox[1])
    .filter(x => { const k = `${x.page}|${x.bbox.map(n => n.toFixed(3))}`; if (seen.has(k)) return false; seen.add(k); return true; });
}
const okBox = (b) => Array.isArray(b) && b.length === 4 && b.every(n => typeof n === 'number' && n >= 0 && n <= 1) && b[2] > b[0] && b[3] > b[1];
function marks(d){                                    // red boxes: unsigned slots + v7 issue_marks; [] until backend sends them
  if (!d) return [];
  const okCrit = new Set((d.criteria || []).filter(c => c.status === 'VALID').map(c => c.key));   // never mark a criterion shown as Lengkap
  const im = [...(Array.isArray(d.issue_marks) ? d.issue_marks : []), ...(d.workers || []).flatMap(w => Array.isArray(w.issue_marks) ? w.issue_marks : [])]
    .filter(x => x && okBox(x.bbox) && x.label && !okCrit.has(x.criterion))
    .map(x => ({criterion: x.criterion, page: +x.page || 1, bbox: x.bbox, label: String(x.label), title: [x.worker, x.label].filter(Boolean).join(': ')}));
  const sg = sigSlots(d).map(x => ({criterion: '4_ttd', page: +x.page || 1, bbox: x.bbox, label: 'Belum TTD', title: x.role || ''}));
  const seen = new Set();
  return [...sg, ...im].filter(x => { const k = `${x.page}|${x.label}|${x.bbox.map(n => n.toFixed(3))}`; if (seen.has(k)) return false; seen.add(k); return true; });
}
const MISS = {transaksi: '6_transaksi', kehadiran: '5_kehadiran'};
const MISS_TXT = {transaksi: ['Lampiran transaksi DMS tidak ada', 'Lampirkan screenshot transaksi DMS untuk periode yang diajukan, lalu upload ulang.'],
  kehadiran: ['Lampiran kehadiran DMS tidak ada', 'Lampirkan screenshot kehadiran DMS untuk periode yang diajukan, lalu upload ulang.']};
const missing = (d) => [...new Set((d && Array.isArray(d.missing_attachments) ? d.missing_attachments : []).filter(m => MISS[m]))];
function jumpPage(it, key){                           // first page showing a problem for this criterion, or its placeholder page
  const m = marks(it.d).filter(x => x.criterion === key).sort((a, b) => a.page - b.page || a.bbox[1] - b.bbox[1])[0];
  if (m) return m.page;
  const j = missing(it.d).findIndex(x => MISS[x] === key), real = pageCount(it);
  return j >= 0 && real ? real + j + 1 : 0;
}
function drawSigs(sheet, it){
  sheet.querySelector('.sigov')?.remove();
  const sl = marks(it.d).filter(x => x.page === S.page);
  if (!sl.length || !sheet.querySelector('canvas')) return;
  const pct = (n) => `${(n * 100).toFixed(2)}%`;
  sheet.insertAdjacentHTML('beforeend', `<div class="sigov" aria-hidden="true">${sl.map(({bbox: [x0, y0, x1, y1], label, title}) =>
    `<div class="sigbox" style="left:${pct(x0)};top:${pct(y0)};width:${pct(x1 - x0)};height:${pct(y1 - y0)}" title="${esc(title)}"><span class="tag">${esc(label)}</span></div>`).join('')}</div>`);
}
let renderSeq = 0;
function renderStage(force){
  const it = cur(), s = st(it);
  const key = `${it.lid}|${it.id}|${S.page}|${s}|${Math.round($('#stage').clientWidth / 40)}`;
  if (!force && key === S.stageKey) { const sh = $('#stage .sheet'); if (sh) drawSigs(sh, it); return; }
  S.stageKey = key;
  const stage = $('#stage');
  if (!it.id) {
    stage.innerHTML = s === 'upload-error'
      ? `<div class="empty"><div class="ico">${ICON.bang}</div><b>File tidak terupload</b>${esc(it.err || '')}</div>`
      : `<div class="empty"><div class="ico"><span class="spin"></span></div><b>Mengupload</b>${esc(it.name)}</div>`;
    return;
  }
  const real = pageCount(it), ms = missing(it.d);
  if (real && S.page > real && ms[S.page - real - 1]) {
    const [h, t] = MISS_TXT[ms[S.page - real - 1]];
    stage.innerHTML = `<div class="sheet missing"><div class="miss-b"><div class="ico">${ICON.x}</div><b>${h}</b>${t}</div></div>`;
    return;
  }
  const url = `/api/ba/${it.id}/file`;
  const overlay = s === 'processing' ? '<div class="scan"></div>' : '';
  const dim = s === 'ditunda' ? ' dim' : '';
  if (!PDF.ready()) { stage.innerHTML = `<div class="sheet${dim}"><iframe src="${url}#page=${S.page}" title="Pratinjau ${esc(it.name)}"></iframe>${overlay}</div>`; return; }
  const seq = ++renderSeq;
  let sheet = stage.querySelector('.sheet');
  if (!sheet || sheet.dataset.lid != it.lid) {
    stage.innerHTML = `<div class="sheet loading" data-lid="${it.lid}"><div class="ld"><span class="spin"></span></div></div>`;
    sheet = stage.querySelector('.sheet');
  }
  sheet.className = `sheet${dim}${sheet.querySelector('canvas') ? '' : ' loading'}`;
  const width = Math.min(680, stage.clientWidth - 40);
  PDF.page(url, S.page, width).then(({canvas, pages}) => {
    if (seq !== renderSeq) return;
    if (it.pdfPages !== pages) { it.pdfPages = pages; renderBar(); }
    sheet.className = `sheet${dim}`;
    sheet.innerHTML = ''; sheet.appendChild(canvas); drawSigs(sheet, it);
    if (overlay) sheet.insertAdjacentHTML('beforeend', overlay);
  }).catch(() => {
    if (seq !== renderSeq) return;
    stage.innerHTML = `<div class="sheet${dim}"><iframe src="${url}#page=${S.page}" title="Pratinjau ${esc(it.name)}"></iframe>${overlay}</div>`;
  });
}
function workersSec(d){
  const ws = d.workers || []; if (!ws.length) return '';
  const nom = d.nominal ? `<p style="font-size:13px;margin-bottom:8px">Kurang <b>${rp(d.nominal.nominal_kurang)}</b> (seharusnya ${rp(d.nominal.nominal_seharusnya)}, diterima ${rp(d.nominal.nominal_diterima)}).</p>` : '';
  const own = ws.map(w => ({w, p: ownProblems(w, ws)}));
  const shown = ws.length > 1 ? own.filter(x => x.p.length) : own;
  const li = shown.map(({w, p}) => {
    const s = w.status && ST[w.status];
    const prob = p.length ? clip(p.map(cap).join('; '), 'wk-p') : '';
    return `<li>${s ? mark(s, w.status) : ''}<div class="wk-b"><div class="wk-n">${esc(w.nama || 'Tanpa nama')}<span class="nik">${esc(w.nik || '')}</span></div>${prob}</div></li>`;
  }).join('');
  const rest = ws.length - shown.length;
  const restLi = ws.length > 1 && rest ? `<li class="wk-rest">${rest === ws.length ? `Tidak ada masalah per pekerja` : `${rest} pekerja lain tidak ada masalah sendiri`}</li>` : '';
  return `<div class="sec"><div class="sec-h">Pekerja <span>${ws.length} orang</span></div>${nom}<ul class="wk">${li}${restLi}</ul></div>`;
}
function renderVerdict(){
  const it = cur(), s = st(it), d = it.d, el = $('#vcol');
  const meta = d ? [d.pages ? `${d.pages} halaman` : null, d.jenis ? JENIS[d.jenis] || d.jenis : null, d.workers && d.workers.length ? `${d.workers.length} pekerja` : null].filter(Boolean).join(', ') : '';
  const head = `<div class="vhead"><div class="n">${esc(it.name)}</div>${meta ? `<div class="m">${meta}</div>` : ''}</div>`;
  const reup = (t) => `<label class="btn${t === 'Upload ulang' ? ' btn-lg btn-primary' : ''}">${t}<input type="file" accept="application/pdf,.pdf" class="sr" data-reup></label>`;
  const skel = `<div class="sec"><div class="sec-h">Syarat BA <span>menunggu hasil</span></div><ul class="crit skel">${CRIT_KEYS.map(k => `<li>${mark('E')}<div class="crit-b"><div class="crit-l">${CRIT_LABEL[k]}</div></div></li>`).join('')}</ul></div>`;
  if (s === 'uploading') { el.innerHTML = head + `<div class="slab slab-neutral"><div class="slab-word sm"><span class="ic"><span class="spin" style="border-color:rgba(255,255,255,.3);border-top-color:#fff"></span></span>Mengupload</div><div class="slab-sub">Tunggu sebentar.</div></div>` + skel; return; }
  if (s === 'upload-error') { el.innerHTML = head + `<div class="slab slab-neutral"><div class="slab-word sm"><span class="ic">${ICON.bang}</span>Gagal diupload</div><div class="slab-sub">${esc(it.err || '')}</div></div><div class="act">${reup('Upload ulang')}</div>`; return; }
  if (s === 'processing') { el.innerHTML = head + `<div class="slab slab-neutral"><div class="slab-word sm"><span class="ic"><span class="spin" style="border-color:rgba(255,255,255,.3);border-top-color:#fff"></span></span>Memproses</div><div class="slab-sub">${d.queue_position > 0 ? `Antrean ke-${d.queue_position}. ` : ''}Hasil muncul otomatis.</div></div>` + skel + `<div class="act"><button class="btn btn-lg" disabled>Submit BA</button></div>`; return; }
  if (s === 'ditunda') { el.innerHTML = head + `<div class="slab slab-wait"><div class="slab-word sm"><span class="ic">${ICON.clock}</span>Ditunda</div><div class="slab-sub">${esc((d.alerts && d.alerts[0]) || 'Antrean, layanan sedang penuh, coba lagi otomatis.')}${d.retry_at ? ` Dicoba lagi pukul ${fmtTime(d.retry_at)}.` : ''}</div></div><div class="act"><button class="btn btn-lg" disabled>Submit BA</button></div>`; return; }
  if (s === 'error') { el.innerHTML = head + `<div class="slab slab-neutral"><div class="slab-word sm"><span class="ic">${ICON.bang}</span>Gagal diproses</div><div class="slab-sub">${esc(d.error || 'BA ini belum dicek.')}</div></div><div class="sec"><p>${esc((d.alerts && d.alerts[0]) || 'Upload ulang file ini.')}</p></div><div class="act">${reup('Upload ulang')}</div>`; return; }

  const V = d.verdict === 'VALID', fx = fixes(d), ok = (d.criteria || []).filter(c => c.status === 'VALID').length;
  const slab = V
    ? `<div class="slab slab-valid"><div class="slab-word"><span class="ic">${ICON.check}</span>VALID</div><div class="slab-sub">${d.submitted ? 'Sudah disubmit. Link publik aktif.' : 'Siap disubmit.'}</div></div>`
    : `<div class="slab slab-invalid"><div class="slab-word"><span class="ic">${ICON.x}</span>INVALID</div><div class="slab-sub">${fx.length > 1 ? `Perbaiki ${fx.length} hal, lalu upload ulang.` : fx.length ? 'Perbaiki, lalu upload ulang.' : 'Upload ulang versi perbaikan.'}</div></div>`;
  const fixSec = !V && fx.length ? `<div class="sec"><div class="sec-h">Yang harus diperbaiki</div><ul class="fix">${(fx.length > 5 ? [...fx.slice(0, 4), ['Lainnya', `${fx.length - 4} hal lagi, lihat Syarat BA`]] : fx).map(x => `<li>${mark('I')}<span title="${esc(x[0] + ': ' + x[1])}"><b>${esc(x[0])}:</b> ${esc(x[1])}</span></li>`).join('')}</ul></div>` : '';
  const oth = otherAlerts(d);
  const noteList = `<ul class="notes">${oth.map(a => `<li>${esc(cap(fmtPer(a)))}</li>`).join('')}</ul>`;
  const notes = oth.length ? `<div class="sec"><div class="sec-h">Catatan</div>${noteList}</div>` : '';
  const crit = `<div class="sec"><div class="sec-h">Syarat BA <span>${ok} dari ${(d.criteria || []).length} lengkap</span></div><ul class="crit">${(d.criteria || []).map(c => { const jp = c.status !== 'VALID' ? jumpPage(it, c.key) : 0; return `<li${jp ? ` class="jump" data-jump="${jp}" tabindex="0" role="button" title="Lihat di halaman ${jp}"` : ''}>${mark(ST[c.status] || 'I', WORD[c.status])}<div class="crit-b"><div class="crit-l"><span>${esc(critLabel(d, c.key))}</span><span class="crit-w ${esc(c.status)}">${WORD[c.status] || esc(c.status)}</span></div>${c.note && c.note.trim().toLowerCase() !== (WORD[c.status] || '').toLowerCase() ? clip(cap(fmtPer(c.note)), 'crit-n') : ''}${jp ? `<div class="crit-j">Lihat di hal ${jp}${ICON.r}</div>` : ''}</div></li>`; }).join('')}</ul></div>`;
  let act;
  if (d.submitted) {
    const url = d.public_url;
    act = url
      ? `<div class="act pub"><div class="ok">${mark('V')}${it.subAt ? `Disubmit ${fmtTime(it.subAt)}` : 'Sudah disubmit'}</div><div class="row"><input class="input" id="publink" readonly value="${esc(url)}" aria-label="Link publik"><button class="btn" data-copy="${esc(url)}">${ICON.copy}Salin link</button></div><a class="linkbtn" href="lihat.html#${esc(url.split('/f/')[1] || '')}" target="_blank" rel="noopener">Lihat halaman publik</a></div>`
      : `<div class="act"><div class="note">Sudah disubmit. Link publik dicabut admin.</div></div>`;
  } else if (d.can_submit) {
    const noName = !submitter();
    act = `<div class="act"><button class="btn btn-lg btn-primary" data-submit ${noName ? 'disabled' : ''}>Submit BA</button><div class="note${noName ? ' bad' : ''}">${noName ? 'Isi nama pengirim dulu.' : ''}</div></div>`;
  } else if (V) {
    act = `<div class="act"><button class="btn btn-lg" disabled>Tidak bisa disubmit</button><div class="note bad">Foto kurang jelas, scan ulang.</div>${reup('Upload versi baru')}</div>`;
  } else {
    act = `<div class="act"><button class="btn btn-lg" disabled>Tidak bisa disubmit</button>${reup('Upload versi perbaikan')}</div>`;
  }
  el.innerHTML = head + slab + fixSec + crit + workersSec(d) + notes + act;
}
function rowsAll(){
  const rows = [];
  S.items.forEach(it => { if (st(it) !== 'done') return;
    const ws = it.d.workers && it.d.workers.length ? it.d.workers : [{}];
    ws.forEach(w => rows.push({it, d: it.d, w})); });
  return rows;
}
function renderResults(){
  const c = {v:0, i:0, s:0, w:0};
  S.items.forEach(it => { const s = st(it); if (s !== 'done') c.w++; else if (it.d.submitted) c.s++; else if (it.d.verdict === 'VALID') c.v++; else c.i++; });
  const can = S.items.filter(i => i.d && i.d.can_submit);
  const nPick = can.filter(i => S.checked.has(i.id)).length;
  const rows = rowsAll().map(({it, d, w}) => {
    const sub = d.submitted, canS = d.can_submit, sel = canS && S.checked.has(it.id);
    const wv = w.status || d.verdict, bad = !sub && wv !== 'VALID';
    const hasil = sub ? `<span class="vd s">${mark('S')}Disubmit</span>`
      : wv === 'VALID' ? `<span class="vd v">${mark('V')}VALID</span>`
      : `<span class="vd i">${mark('I')}INVALID <span class="sub-t">perlu revisi</span></span>`;
    return `<tr class="${bad ? 'bad' : ''}${sel ? ' sel' : ''}">
      <td><input type="checkbox" class="cb" data-ck="${it.id}" ${sel ? 'checked' : ''} ${canS ? '' : 'disabled'} aria-label="Pilih ${esc(w.nama || it.name)}"></td>
      <td>${hasil}</td><td><b style="font-weight:600">${esc(w.nama || '–')}</b></td><td class="nik">${esc(w.nik || '–')}</td><td>${esc(w.jabatan || '–')}</td><td>${esc(w.lokasi || '–')}</td><td>${esc(fmtPer(w.periode))}</td><td>${esc(w.hk_diajukan ?? '–')}</td>
      ${CRIT_KEYS.map((k, i) => { const v = workerCrit(w, d, k); return `<td class="c${i === 0 ? ' grp' : ''}">${v ? mark(ST[v] || 'I', `${CRIT_SHORT[k]}: ${WORD[v] || v}`) : ''}</td>`; }).join('')}
      <td class="file-c grp"><button class="linkbtn" data-f="${it.lid}" style="color:var(--ink-2)" title="${esc(it.name)}">${esc(it.name)}</button></td></tr>`;
  }).join('') || `<tr><td class="empty-row" colspan="16">Belum ada hasil. Hasil muncul di sini setelah file selesai dicek.</td></tr>`;
  $('#res').innerHTML = `<div class="res-h"><div><h2>Rekap semua BA</h2><p>${S.items.length} file: ${c.v} VALID, ${c.i} INVALID, ${c.s} sudah disubmit${c.w ? `, ${c.w} belum selesai` : ''}.</p></div>
    <div class="acts"><button class="btn" id="csv" ${rowsAll().length ? '' : 'disabled'}>${ICON.dl}Unduh CSV</button><button class="btn btn-primary" id="bulk" ${can.length && submitter() ? '' : 'disabled'} title="${submitter() ? '' : 'Isi nama pengirim dulu'}">${nPick ? `Submit yang dipilih (${nPick})` : `Submit semua yang VALID (${can.length})`}</button></div></div>
    <div class="tbl-w"><table class="t"><thead><tr><th><span class="sr">Pilih</span></th><th>Hasil</th><th>Nama</th><th>NIK</th><th>Jabatan</th><th>Lokasi</th><th>Periode</th><th>HK</th>${CRIT_KEYS.map((k, i) => `<th class="c${i === 0 ? ' grp' : ''}" title="${CRIT_LABEL[k]}">${CRIT_SHORT[k]}</th>`).join('')}<th class="grp">File</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}
function csv(){
  const head = ['file','nik','nama','jabatan','lokasi','periode','hk_diajukan','jenis', ...CRIT_KEYS, 'status_pekerja','verdict','submitted','public_url','catatan'];
  const lines = [head.join(',')];
  rowsAll().forEach(({it, d, w}) => lines.push([it.name, w.nik, w.nama, w.jabatan, w.lokasi, w.periode, w.hk_diajukan, d.jenis,
    ...CRIT_KEYS.map(k => workerCrit(w, d, k)), w.status || d.verdict, d.verdict, d.submitted ? 'ya' : 'tidak', d.public_url || '',
    [...(Array.isArray(w.problems) ? w.problems : []), ...(d.alerts || [])].join(' | ')].map(csvCell).join(',')));
  download(`rekap_ba_${new Date().toISOString().slice(0, 10)}.csv`, '﻿' + lines.join('\r\n'));
}

/* ---------- events ---------- */
function goPage(p){ if (!p) return; S.page = p; renderBar(); renderStage(); const r = $('#vbar').getBoundingClientRect(); if (r.top < 0 || r.top > innerHeight * .5) $('#vbar').scrollIntoView({behavior: 'smooth', block: 'start'}); }
function select(lid){ if (S.sel !== lid) { S.sel = lid; S.page = 1; } renderAll(); }
document.addEventListener('click', e => {
  const t = e.target.closest('button,a');
  if (!t) { const j = e.target.closest('[data-jump]'); if (j) goPage(+j.dataset.jump); return; }
  const d = t.dataset;
  if (d.f) { select(+d.f); if (t.closest('.t')) window.scrollTo({top: 0, behavior: 'smooth'}); return; }
  if (d.nav) { const it = S.items[S.items.indexOf(cur()) + +d.nav]; if (it) select(it.lid); return; }
  if (d.pg) { S.page += +d.pg; renderBar(); renderStage(); return; }
  if (d.p) { S.page = +d.p; renderBar(); renderStage(); return; }
  if ('more' in d) { t.parentElement.classList.toggle('open'); t.textContent = t.parentElement.classList.contains('open') ? 'tutup' : 'lihat detail'; return; }
  if (d.copy) { copyText(d.copy, $('#publink')); return; }
  if ('submit' in d) { submitOne(cur()); return; }
  if (t.id === 'bulk') { submitBulk(); return; }
  if (t.id === 'csv') { csv(); return; }
  if (t.id === 'clear') {
    if (!S.clearArm) { S.clearArm = true; renderRail(); setTimeout(() => { S.clearArm = false; if (S.items.length) renderRail(); }, 3000); return; }
    S.items.forEach(i => clearTimeout(i.t)); S.items = []; S.checked.clear(); S.clearArm = false; S.stageKey = ''; save(); showMsgs([]); renderAll(); return;
  }
});
document.addEventListener('change', e => {
  const t = e.target;
  if (t.dataset.ck) { t.checked ? S.checked.add(t.dataset.ck) : S.checked.delete(t.dataset.ck); renderResults(); return; }
  if (t.type === 'file' && t.files.length) { addFiles(t.files); t.value = ''; }
});
$('#name').addEventListener('input', () => { ls.set(LS_NAME, $('#name').value); $('#name').classList.remove('err'); if (S.items.length) { renderVerdict(); renderResults(); } });
document.addEventListener('keydown', e => {
  if (!S.items.length || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) || e.altKey || e.ctrlKey || e.metaKey) return;
  if (e.key === 'Enter' && document.activeElement.dataset.jump) { goPage(+document.activeElement.dataset.jump); return; }
  if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
    const it = S.items[S.items.indexOf(cur()) + (e.key === 'ArrowRight' ? 1 : -1)];
    if (it) { e.preventDefault(); select(it.lid); }
  }
});
['dragenter','dragover'].forEach(ev => document.addEventListener(ev, e => { e.preventDefault(); $('#drop').classList.add('over'); }));
['dragleave','drop'].forEach(ev => document.addEventListener(ev, e => {
  e.preventDefault();
  if (ev === 'dragleave' && e.relatedTarget) return;
  $('#drop').classList.remove('over');
  if (ev === 'drop' && e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
}));
let rz; window.addEventListener('resize', () => { clearTimeout(rz); rz = setTimeout(() => { if (S.items.length) renderStage(); }, 200); });

/* ---------- boot ---------- */
$('#logo').innerHTML = ICON.doc; $('#dropico').innerHTML = ICON.up;
$('#name').value = ls.get(LS_NAME, '');
S.items = ls.get(LS_FILES, []).filter(x => x && x.id).map(x => ({lid: ++lidN, id: x.id, name: x.name, subAt: x.subAt, st: 'processing', d: null}));
S.items.forEach(it => { it.d = {status: 'processing', queue_position: null}; });
renderAll();
S.items.forEach(it => fetchBA(it));
