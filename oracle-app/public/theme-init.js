// Stamp the chosen theme before any CSS paints (loaded blocking from <head>).
// A file rather than an inline <script> so the SPA's CSP needs no
// 'unsafe-inline' and no per-build hash in nginx.conf (security review WEB-3).
// Light is the default;
// a stored 'dark' must not flash white on load. Mirrors src/theme.js.
(function () {
  try {
    var t = localStorage.getItem('neoh.theme');
    if (t === 'dark' || t === 'light') document.documentElement.setAttribute('data-theme', t);
    else if (t !== 'system') document.documentElement.setAttribute('data-theme', 'light');
  } catch { document.documentElement.setAttribute('data-theme', 'light'); }
})();
