/* Public read-only viewer: lihat.html#<token>. The PDF is served by GET /f/{token}; title comes from /f/{token}?view=1. */
$('#logo').innerHTML = ICON.doc;
const token = decodeURIComponent(location.hash.slice(1));
const body = $('#body');
const gone = (t) => { body.innerHTML = `<div class="empty" style="margin:60px auto"><div class="ico">${ICON.lock}</div><b>${t}</b>Minta link baru ke pengirim BA.</div>`; };

async function main(){
  if (!/^[A-Za-z0-9_-]{22,64}$/.test(token)) return gone('Link tidak valid');
  const pdfUrl = `/f/${token}`;
  let title = '', sub = '', file = '';
  try {
    const r = await fetch(`${pdfUrl}?view=1`);
    if (r.status === 404) return gone('Link tidak aktif atau sudah dicabut');
    const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
    title = (doc.title || '').replace(/^BA\s*/, '');
    const [nama, per] = title.split(' - ');
    sub = [nama, per && fmtPer(per)].filter(Boolean).join(', ');
    file = (doc.querySelector('header span')?.firstChild?.textContent || '').replace(/[·\s]+$/, '').trim();
  } catch (e) { /* show the PDF anyway */ }
  document.title = sub ? `BA ${sub}` : 'Berita Acara';
  body.innerHTML = `<div class="pubv-h"><div><h1>Berita Acara</h1>${sub ? `<p>${esc(sub)}</p>` : ''}</div><a class="btn btn-primary" href="${pdfUrl}?dl=1">${ICON.dl}Unduh PDF</a></div>
    <div class="seal">${mark('V')}<div><b>VALID</b><br><span>Sudah dicek dan disubmit lewat Portal BA.${file ? ` File: ${esc(file)}.` : ''}</span></div></div>
    <div class="pages" id="pages"><div class="sheet loading"><div class="ld"><span class="spin"></span></div></div></div>`;
  const box = $('#pages');
  if (!PDF.ready()) { box.innerHTML = `<div class="sheet"><iframe src="${pdfUrl}" title="Dokumen BA"></iframe></div>`; return; }
  try {
    const w = Math.min(780, box.clientWidth);
    const first = await PDF.page(pdfUrl, 1, w);
    box.innerHTML = '';
    const add = (c) => { const s = document.createElement('div'); s.className = 'sheet'; s.appendChild(c); box.appendChild(s); };
    add(first.canvas);
    for (let i = 2; i <= first.pages; i++) add((await PDF.page(pdfUrl, i, w)).canvas);
  } catch (e) {
    box.innerHTML = `<div class="sheet"><iframe src="${pdfUrl}" title="Dokumen BA"></iframe></div>`;
  }
}
main();
window.addEventListener('hashchange', () => location.reload());
