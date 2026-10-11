/* Theme bootstrap: runs before first paint so the stored mode never flashes.
   html.theme-dark (default) keeps the Last.fm-style dark reports and dark
   chrome; html.theme-light switches the whole app, reports included. */
(() => {
  'use strict';
  let stored = null;
  try { stored = window.localStorage.getItem('music-journal.theme'); } catch { /* storage may be blocked */ }
  const theme = stored === 'light' ? 'light' : 'dark';
  document.documentElement.classList.add('theme-' + theme);
  document.documentElement.dataset.theme = theme;
})();
