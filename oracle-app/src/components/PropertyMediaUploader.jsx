import { useCallback, useEffect, useRef, useState } from 'react';
import { crmDelete, crmGet, crmUpload } from '../state/useCrmApi';
// /api/media/{id} is authenticated, so a bare <img src> gets a 401. This hook
// fetches the bytes with the JWT and hands back a revocable object URL — the
// same path the video studio and property tour already use.
import useProtectedMedia from '../state/useProtectedMedia';
import styles from './PropertyMediaUploader.module.css';

/**
 * Attach and remove photos on a lead or a listing.
 *
 * `POST /api/crm/leads/{id}/media`, `POST /api/crm/listings/{id}/media` and
 * `DELETE /api/crm/media/{id}` all shipped with no caller. The GET half was
 * wired — so the product could DISPLAY a photo filmstrip it had no way to add
 * to, and a wrong photo could never be removed.
 *
 * One component serves both subjects because the endpoints differ only in their
 * path segment; the lead route is the one that matters most in practice, since
 * leads are the primary property record (hundreds of thousands of them) and
 * listings are comparatively few.
 */

const MAX_MB = 25;

export default function PropertyMediaUploader({ leadId, listingId, onChanged }) {
  const [media, setMedia] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const inputRef = useRef(null);

  const subject = leadId ? { key: 'lead_id', id: leadId, path: `leads/${leadId}` }
    : listingId ? { key: 'listing_id', id: listingId, path: `listings/${listingId}` }
      : null;

  const load = useCallback(() => {
    if (!subject) return undefined;
    setError('');
    return crmGet(`/api/crm/media?${subject.key}=${subject.id}&kind=photo`).then(
      (payload) => setMedia(Array.isArray(payload?.media) ? payload.media : []),
      (reason) => {
        setMedia([]);
        setError(reason?.message || 'Existing photos could not be listed.');
      },
    );
    // subject is derived from the props below; listing it directly would rebuild
    // the callback every render.
  }, [subject?.key, subject?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => { void load(); });
    return () => window.cancelAnimationFrame(frame);
  }, [load]);

  const upload = async (fileList) => {
    const files = Array.from(fileList || []);
    if (files.length === 0 || busy || !subject) return;

    // Refuse oversized files here rather than letting the request start and
    // fail at the edge — a 25 MB upload that dies at the gateway looks like a
    // broken app, not a rejected file.
    const tooBig = files.filter((f) => f.size > MAX_MB * 1024 * 1024);
    if (tooBig.length > 0) {
      setError(`${tooBig.map((f) => f.name).join(', ')} exceeds ${MAX_MB} MB.`);
      return;
    }

    setBusy('upload');
    setError('');
    const form = new FormData();
    files.forEach((file) => form.append('files', file));
    try {
      await crmUpload(`/api/crm/${subject.path}/media`, form);
      if (inputRef.current) inputRef.current.value = '';
      await load();
      await onChanged?.();
    } catch (reason) {
      setError(
        reason?.status === 404
          ? 'That record no longer exists.'
          : reason?.message || 'The upload was refused.',
      );
    } finally {
      setBusy('');
    }
  };

  const remove = async (item) => {
    if (busy) return;
    setBusy(item.id);
    setError('');
    try {
      await crmDelete(`/api/crm/media/${item.id}`);
      await load();
      await onChanged?.();
    } catch (reason) {
      setError(reason?.message || 'The photo could not be removed.');
    } finally {
      setBusy('');
    }
  };

  // Called before the early return below: hooks cannot sit behind a condition,
  // so the "no subject" guard has to come after every hook in this component.
  const photos = useProtectedMedia(media || [], {});
  if (!subject) return null;

  return (
    <div className={styles.wrap}>
      <div className={styles.head}>
        <strong>Photos</strong>
        <span>{media === null ? 'loading…' : `${photos.length} attached`}</span>
      </div>

      {error ? <p className={styles.error} role="alert">{error}</p> : null}

      <label className={styles.picker}>
        <span>{busy === 'upload' ? 'Uploading…' : 'Add photos'}</span>
        <input
          ref={inputRef}
          type="file"
          accept="image/*"
          multiple
          disabled={busy !== ''}
          onChange={(event) => upload(event.target.files)}
        />
      </label>

      {photos.length > 0 ? (
        <ul className={styles.strip}>
          {photos.map((item) => (
            <li key={item.id}>
              {item.display_url
                ? <img src={item.display_url} alt="" loading="lazy" />
                : <span className={styles.pending} aria-hidden="true" />}
              <button
                type="button"
                onClick={() => remove(item)}
                disabled={busy !== ''}
                aria-label="Remove this photo"
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      ) : media !== null ? (
        <p className={styles.empty}>No photos attached to this property.</p>
      ) : null}
    </div>
  );
}
