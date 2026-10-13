/**
 * Serve a prefetched splat from the service worker's IndexedDB copy only after
 * the server confirms this viewer may still see it (prompt 16 T16.14).
 *
 * The copy used to be served with no check until logout, so access removed any
 * other way (session revoked, media deleted, a future per-agent rule) left the
 * space viewable. The check is a conditional GET on the media id (a strong
 * validator): the server answers 304 only after reading the row under RLS, so
 * no bytes move. 401/403/404/410 purge the copy and pass the refusal on; any
 * other non-304 answer is returned as-is; a network failure (offline) still
 * serves the copy, which is what prefetching is for.
 */
export const REVOKED_STATUSES = new Set([401, 403, 404, 410]);

export async function serveCachedSplat(request, { get, remove, fetch }) {
  const mediaId = new URL(request.url).pathname.split('/').pop();
  const cached = await get(mediaId);
  if (!cached?.blob) return fetch(request);

  let check;
  try {
    check = await fetch(request.url, {
      method: 'GET',
      headers: { 'If-None-Match': `"${mediaId}"` },
      credentials: 'same-origin',
      cache: 'no-store',
    });
  } catch {
    check = null;
  }
  if (check && REVOKED_STATUSES.has(check.status)) {
    await remove(mediaId);
    return check;
  }
  if (check && check.status !== 304) return check;
  return new Response(cached.blob, {
    status: 200,
    headers: {
      'Content-Type': 'application/octet-stream',
      'Content-Length': cached.blob.size.toString(),
      'X-Neoh-Cache': check ? 'predictive-hit' : 'predictive-hit-offline',
    },
  });
}
