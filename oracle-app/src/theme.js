/**
 * theme — light by default, dark when asked, the OS only when asked for that.
 *
 * The first light build followed the OS, and on a dark desktop that meant the
 * white the product was designed around was never seen. So the default is
 * now light, a person can choose dark, and "system" is a third, explicit
 * choice rather than the silent one. The choice is stamped on <html> as
 * data-theme, which index.css already honours in both directions, and it is
 * stamped once more by an inline script in index.html before any CSS paints
 * so a dark choice does not flash white on load.
 */

export const THEMES = Object.freeze(['light', 'dark', 'system']);
export const DEFAULT_THEME = 'light';
export const STORAGE_KEY = 'neoh.theme';

/** The theme-color the browser chrome should take, per resolved theme. */
export const CHROME_COLOR = Object.freeze({ light: '#fbfaf8', dark: '#080808' });

export function isTheme(value) {
  return THEMES.includes(value);
}

/** The stored choice, or the default. Storage can be absent or throw. */
export function readTheme(storage = safeStorage()) {
  try {
    const stored = storage?.getItem(STORAGE_KEY);
    return isTheme(stored) ? stored : DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}

/** What actually renders: 'system' resolves through the OS preference. */
export function resolveTheme(theme, prefersDark = osPrefersDark()) {
  if (theme === 'dark') return 'dark';
  if (theme === 'light') return 'light';
  return prefersDark ? 'dark' : 'light';
}

/**
 * Stamp the choice on the document. 'system' removes the attribute so the
 * media query decides — the same three-state contract index.css was built
 * for. Also keeps the browser chrome colour honest.
 */
export function applyTheme(theme, doc = globalThis.document) {
  if (!doc?.documentElement) return;
  const root = doc.documentElement;
  if (theme === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', theme);
  const meta = doc.querySelector?.('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', CHROME_COLOR[resolveTheme(theme)]);
}

export function writeTheme(theme, storage = safeStorage()) {
  try { storage?.setItem(STORAGE_KEY, theme); } catch { /* private mode, quota */ }
}

/** The next theme on a toggle: light ⇄ dark. 'system' goes to the opposite
 *  of what it currently shows, so the button always visibly does something. */
export function nextTheme(current, prefersDark = osPrefersDark()) {
  return resolveTheme(current, prefersDark) === 'dark' ? 'light' : 'dark';
}

export function osPrefersDark() {
  try {
    return Boolean(globalThis.matchMedia?.('(prefers-color-scheme: dark)').matches);
  } catch {
    return false;
  }
}

function safeStorage() {
  try { return globalThis.localStorage; } catch { return null; }
}
