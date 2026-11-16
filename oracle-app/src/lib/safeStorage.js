// Browser storage that never throws (external audit WEB-002).
//
// Reading `window.sessionStorage` itself throws a SecurityError when a browser
// blocks site data (sandboxed embeds, enterprise policy), and older Safari
// private windows throw a quota error on setItem. An unguarded call in a
// sign-out or expiry handler skipped clearing the private caches and leaving
// the CRM on screen; one in login turned a successful sign-in into an error.

function store(kind) {
  try {
    return window[kind] || null;
  } catch {
    return null;
  }
}

/** Write a value; returns false when storage is unavailable. */
export function safeSet(kind, key, value) {
  try {
    const s = store(kind);
    if (!s) return false;
    s.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

/** Remove a value; returns false when storage is unavailable. */
export function safeRemove(kind, key) {
  try {
    const s = store(kind);
    if (!s) return false;
    s.removeItem(key);
    return true;
  } catch {
    return false;
  }
}
