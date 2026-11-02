/* Theme: Sistem / Terang / Gelap. Loaded in <head> so the saved choice applies before paint. */
(function(){
  const KEY = 'ba.theme', OPTS = [['system','Sistem'],['light','Terang'],['dark','Gelap']];
  const get = () => { try { return localStorage.getItem(KEY) || 'system'; } catch (e) { return 'system'; } };
  const apply = (v) => { const r = document.documentElement; if (v === 'light' || v === 'dark') r.dataset.theme = v; else r.removeAttribute('data-theme'); };
  apply(get());
  window.themeCtl = () => `<div class="themesw" role="group" aria-label="Tema">${OPTS.map(([v, l]) =>
    `<button type="button" data-theme-set="${v}" aria-pressed="${get() === v}">${l}</button>`).join('')}</div>`;
  const fill = () => document.querySelectorAll('[data-theme-ctl]').forEach(el => { el.outerHTML = themeCtl(); });
  document.addEventListener('DOMContentLoaded', fill);
  document.addEventListener('click', e => {
    const b = e.target.closest('[data-theme-set]'); if (!b) return;
    const v = b.dataset.themeSet;
    try { localStorage.setItem(KEY, v); } catch (err) {}
    apply(v);
    document.querySelectorAll('[data-theme-set]').forEach(x => x.setAttribute('aria-pressed', x.dataset.themeSet === v));
  });
})();
