/* Shared helpers for Portal BA pages (portal, admin, public viewer). */
const ICON = {
  check:'<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.4l2.9 2.9 6.1-6.6" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  x:'<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.6 4.6l6.8 6.8M11.4 4.6l-6.8 6.8" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round"/></svg>',
  dash:'<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 8h7" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round"/></svg>',
  clock:'<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="5.6" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M8 5v3.2l2 1.3" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  bang:'<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 4v5" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/><circle cx="8" cy="12" r="1.2" fill="currentColor"/></svg>',
  up:'<svg viewBox="0 0 20 20" width="20" height="20" aria-hidden="true"><path d="M10 13V3.5M6 7.5l4-4 4 4M3.5 13v2.5a1 1 0 001 1h11a1 1 0 001-1V13" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  l:'<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M10 3.5L5.5 8l4.5 4.5" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  r:'<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6 3.5L10.5 8 6 12.5" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  dl:'<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2.5v8M4.5 7.2L8 10.7l3.5-3.5M3 13.5h10" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  copy:'<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.6" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M10.5 3.5v-.4A1.1 1.1 0 009.4 2H3.6A1.1 1.1 0 002.5 3.1v5.8A1.1 1.1 0 003.6 10h.4" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>',
  doc:'<svg viewBox="0 0 20 20" width="20" height="20" aria-hidden="true"><path d="M5 2.5h6.5L15.5 6.5v10a1 1 0 01-1 1h-9.5a1 1 0 01-1-1v-13a1 1 0 011-1z M11.5 2.5v4h4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M7 11l2 2 4-4.5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  lock:'<svg viewBox="0 0 20 20" width="20" height="20" aria-hidden="true"><rect x="4" y="8.5" width="12" height="8.5" rx="1.8" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M6.8 8.5V6.3a3.2 3.2 0 016.4 0v2.2" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>',
  file:'<svg viewBox="0 0 20 20" width="20" height="20" aria-hidden="true"><path d="M5 2.5h6.5L15.5 6.5v10a1 1 0 01-1 1h-9.5a1 1 0 01-1-1v-13a1 1 0 011-1z M11.5 2.5v4h4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>',
};
const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const cap = (s) => { s = String(s ?? '').trim(); return s ? s[0].toUpperCase() + s.slice(1) : s; };
const MON = ['Jan','Feb','Mar','Apr','Mei','Jun','Jul','Agu','Sep','Okt','Nov','Des'];
/* "2026-08-16 s/d 2026-08-31" -> "16–31 Agu 2026" */
function fmtPer(s){
  if (s == null || s === '') return '–';
  let t = String(s).replace(/(\d{4})-(\d{2})-(\d{2})/g, (_, y, m, d) => `${+d} ${MON[+m - 1] || m} ${y}`)
    .replace(/\s*(s\/d|sampai)\s*(?=\d{1,2} )/gi, ' – ');
  return t.replace(/(\d{1,2}) (\w{3}) (\d{4}) – (\d{1,2}) \2 \3/, '$1–$4 $2 $3');
}
function fmtTime(iso){ const d = new Date(iso); return isNaN(d) ? '' : d.toLocaleTimeString('id-ID', {hour:'2-digit', minute:'2-digit'}).replace('.', ':'); }
function fmtDT(iso){ const d = new Date(iso); return isNaN(d) ? (iso || '') : `${d.getDate()} ${MON[d.getMonth()]}, ${fmtTime(iso)}`; }
const rp = (n) => n == null || n === '' ? '–' : 'Rp ' + Number(n).toLocaleString('id-ID');

/* status mark: V valid, I invalid, K kosong, W wait, N neutral/error, S submitted, P processing */
function mark(c, label = ''){
  if (c === 'P') return '<span class="mk mk-p" aria-hidden="true"><span class="spin"></span></span>';
  const m = {V:['mk-v',ICON.check], I:['mk-i',ICON.x], K:['mk-k',ICON.dash], W:['mk-w',ICON.clock], N:['mk-n',ICON.bang], S:['mk-s',ICON.check], E:['mk-n','']}[c] || ['mk-n',''];
  return `<span class="mk ${m[0]}"${label ? ` role="img" aria-label="${esc(label)}"` : ' aria-hidden="true"'}>${m[1]}</span>`;
}
const ST = {VALID:'V', INVALID:'I', KOSONG:'K'};
const WORD = {VALID:'Lengkap', INVALID:'Salah', KOSONG:'Tidak ada'};

function toast(t){ const el = $('#toast'); if (!el) return; el.textContent = t; el.classList.add('on'); clearTimeout(toast.h); toast.h = setTimeout(() => el.classList.remove('on'), 2600); }
function copyText(text, input){
  const fallback = () => { if (input){ input.focus(); input.select(); } toast('Tekan Ctrl+C untuk menyalin'); };
  try { navigator.clipboard.writeText(text).then(() => toast('Link disalin'), fallback); } catch (e) { fallback(); }
}
function download(name, text, type = 'text/csv;charset=utf-8'){
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], {type}));
  a.download = name; document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}
const csvCell = (v) => { const s = String(v ?? ''); return /[",\n;]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
async function errText(r){ try { const j = await r.json(); return typeof j.detail === 'string' ? j.detail : `Gagal (${r.status}).`; } catch (e) { return `Gagal (${r.status}).`; } }

/* PDF rendering with pdf.js (cdnjs, pinned); callers fall back to an <iframe> if it is unavailable. */
const PDFJS_WORKER = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
const PDF = {
  docs: new Map(),
  ready(){ if (!window.pdfjsLib) return false; pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS_WORKER; return true; },
  doc(url){
    if (!this.docs.has(url)) {
      const p = pdfjsLib.getDocument({url, isEvalSupported:false}).promise;
      p.catch(() => this.docs.delete(url));
      this.docs.set(url, p);
    }
    return this.docs.get(url);
  },
  /* renders page n into a fresh canvas sized to cssWidth; resolves {canvas, pages} */
  async page(url, n, cssWidth){
    const doc = await this.doc(url);
    const pg = await doc.getPage(Math.min(Math.max(1, n), doc.numPages));
    const base = pg.getViewport({scale:1});
    const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
    const vp = pg.getViewport({scale: (cssWidth / base.width) * dpr});
    const c = document.createElement('canvas');
    c.width = Math.round(vp.width); c.height = Math.round(vp.height);
    c.setAttribute('role', 'img'); c.setAttribute('aria-label', `Halaman ${n} dari ${doc.numPages}`);
    await pg.render({canvasContext: c.getContext('2d'), viewport: vp}).promise;
    return {canvas: c, pages: doc.numPages};
  },
};
