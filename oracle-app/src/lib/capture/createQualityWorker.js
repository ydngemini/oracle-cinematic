/**
 * The quality worker, spelled the way Vite recognises (`new Worker(new URL(...,
 * import.meta.url), { type: 'module' })`) so the build emits it as its own
 * same-origin chunk with its imports bundled. Any other spelling — e.g. a
 * Worker constructor read from a variable — is inlined as a data: URL with its
 * imports unresolved, which CSP (worker-src 'self' blob:) also blocks.
 */
export function createQualityWorker() {
  return new Worker(new URL('./qualityWorker.js', import.meta.url), { type: 'module' });
}
