import { useCallback, useEffect, useRef, useState } from 'react';
import { crmGet, crmDelete } from '../state/useCrmApi';
import useProtectedMedia from '../state/useProtectedMedia';
import styles from './MediaUploader.module.css';

/**
 * MediaUploader — reusable drag-and-drop / file-picker photo uploader + gallery.
 *
 * Speaks the CRM media contract:
 *   POST /api/crm/leads/{lead_id}/media   (multipart `file`)   -> {id,url,kind,sort_order}
 *   POST /api/crm/listings/{listing_id}/media
 *   GET  /api/crm/media?lead_id=&listing_id=                   -> {media:[...]}
 *   DELETE /api/crm/media/{id}
 *   GET  /api/media/{id}  (JWT-protected bytes, rendered via an object URL)
 *
 * JSON calls go through crmGet/crmDelete; the multipart upload is a direct
 * XHR (gives real upload progress) carrying the Bearer token. A `token` prop
 * overrides sessionStorage — that's the seam for a homeowner client portal,
 * which doesn't exist as a view in this app yet (agent-side is wired today).
 *
 * Props:
 *   leadId / listingId {string|number}  one of them — the media owner
 *   token   {string}                    optional bearer override (portal auth)
 *   title   {string}                    section heading (default "Property photos")
 *   onChange {(media:[]) => void}       fired after the gallery list changes
 *   compact {boolean}                   tighter layout for inline use
 */

const API_BASE = import.meta.env.VITE_API_BASE
  || (import.meta.env.DEV ? 'http://localhost:8000' : '');
const MAX_BYTES = 25 * 1024 * 1024; // 25 MB per image — friendly client-side guard

// Media rows are resolved by useProtectedMedia. Never put the authenticated API
// route directly in <img src>, because browsers cannot attach the Bearer token.
export function mediaSrc(m) {
  if (!m) return '';
  if (m.display_url) return m.display_url;
  const u = m.url;
  return u && /^https?:\/\//i.test(u) && !u.startsWith(API_BASE) ? u : '';
}

function authToken(override) {
  return override || '';
}

// Multipart POST with progress. fetch can't surface upload progress, so XHR is
// the right tool — still a "direct" request with the Bearer token, no JSON body.
function uploadOne({ file, leadId, listingId, token, onProgress, signal }) {
  return new Promise((resolve, reject) => {
    const path = listingId != null
      ? `/api/crm/listings/${listingId}/media`
      : `/api/crm/leads/${leadId}/media`;
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${API_BASE}${path}`);
    const tok = authToken(token);
    if (tok) xhr.setRequestHeader('Authorization', `Bearer ${tok}`);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(Math.round((e.loaded / e.total) * 100));
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try { resolve(JSON.parse(xhr.responseText || '{}')); }
        catch { resolve({}); }
      } else {
        let detail = xhr.statusText;
        try { detail = JSON.parse(xhr.responseText).detail || detail; } catch { /* keep statusText */ }
        const err = new Error(detail || `Upload failed (${xhr.status})`);
        err.status = xhr.status;
        reject(err);
      }
    };
    xhr.onerror = () => reject(new Error('Network error during upload.'));
    xhr.onabort = () => reject(new Error('Upload cancelled.'));
    if (signal) signal.addEventListener('abort', () => xhr.abort(), { once: true });
    const fd = new FormData();
    fd.append('file', file);
    xhr.send(fd);
  });
}

const GLYPHS = {
  upload: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 16V5" /><path d="m7.5 9.5 4.5-4.5 4.5 4.5" />
      <path d="M5 19h14" />
    </svg>
  ),
  trash: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 7h16" /><path d="M9 7V5h6v2" /><path d="M6 7l1 13h10l1-13" />
    </svg>
  ),
  retry: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1" /><path d="M5 3v4h4" />
    </svg>
  ),
};

export default function MediaUploader({
  leadId,
  listingId,
  token,
  title = 'Property photos',
  onChange,
  compact = false,
}) {
  const targetKey = listingId != null ? `listing:${listingId}` : leadId != null ? `lead:${leadId}` : null;

  const [items, setItems] = useState([]);
  const [galleryState, setGalleryState] = useState(targetKey ? 'loading' : 'idle'); // idle|loading|ready|error
  const [galleryError, setGalleryError] = useState('');
  const [uploads, setUploads] = useState([]); // {id,name,preview,progress,status,error,file}
  const [dragOver, setDragOver] = useState(false);
  const [lightbox, setLightbox] = useState(null); // index into items, or null
  const displayItems = useProtectedMedia(items, { token });

  const inputRef = useRef(null);
  const uidRef = useRef(0);
  const urlsRef = useRef(new Set()); // object URLs to revoke on unmount
  const onChangeRef = useRef(onChange);

  useEffect(() => {
    onChangeRef.current = onChange;
  }, [onChange]);

  // ── Gallery load ───────────────────────────────────────────────────────────
  const refresh = useCallback(() => {
    if (!targetKey) return;
    const p = new URLSearchParams();
    if (leadId != null) p.set('lead_id', String(leadId));
    if (listingId != null) p.set('listing_id', String(listingId));
    crmGet(`/api/crm/media?${p.toString()}`).then(
      (data) => {
        const media = (Array.isArray(data?.media) ? data.media : [])
          .slice()
          .sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0));
        setItems(media);
        setGalleryState('ready');
        onChangeRef.current?.(media);
      },
      (err) => {
        setGalleryError(
          err.status === 404 ? 'The media service is not online yet.' : (err.message || 'Could not load photos.')
        );
        setGalleryState('error');
      }
    );
  }, [leadId, listingId, targetKey]);

  // Reset the gallery to its loading state when the target record changes —
  // render-phase, so the load effect carries no synchronous setState.
  const [prevTargetKey, setPrevTargetKey] = useState(targetKey);
  if (targetKey !== prevTargetKey) {
    setPrevTargetKey(targetKey);
    setGalleryState(targetKey ? 'loading' : 'idle');
    setGalleryError('');
  }

  useEffect(() => { refresh(); }, [refresh]);

  // Revoke any outstanding object URLs when the component unmounts.
  useEffect(() => () => {
    urlsRef.current.forEach((u) => { try { URL.revokeObjectURL(u); } catch { /* noop */ } });
    urlsRef.current.clear();
  }, []);

  const dropPreview = useCallback((url) => {
    if (urlsRef.current.has(url)) {
      try { URL.revokeObjectURL(url); } catch { /* noop */ }
      urlsRef.current.delete(url);
    }
  }, []);

  // ── Upload ─────────────────────────────────────────────────────────────────
  const startUpload = useCallback((file, entryId, preview) => {
    uploadOne({
      file,
      leadId,
      listingId,
      token,
      onProgress: (pct) => setUploads((u) => u.map((x) => (x.id === entryId ? { ...x, progress: pct } : x))),
    }).then(
      () => {
        setUploads((u) => u.map((x) => (x.id === entryId ? { ...x, progress: 100, status: 'done' } : x)));
        // Let the success tick linger, then drop the chip and pull the gallery.
        setTimeout(() => {
          setUploads((u) => u.filter((x) => x.id !== entryId));
          dropPreview(preview);
        }, 800);
        refresh();
      },
      (err) => {
        setUploads((u) => u.map((x) => (x.id === entryId ? { ...x, status: 'error', error: err.message || 'Upload failed.' } : x)));
      }
    );
  }, [leadId, listingId, token, refresh, dropPreview]);

  const handleFiles = useCallback((fileList) => {
    if (!targetKey) return;
    const files = Array.from(fileList || []).filter((f) => f.type?.startsWith('image/'));
    if (files.length === 0) return;
    files.forEach((file) => {
      const id = ++uidRef.current;
      const preview = URL.createObjectURL(file);
      urlsRef.current.add(preview);
      const tooBig = file.size > MAX_BYTES;
      setUploads((u) => [
        ...u,
        {
          id,
          name: file.name,
          preview,
          file,
          progress: 0,
          status: tooBig ? 'error' : 'uploading',
          error: tooBig ? 'Image exceeds 25 MB.' : '',
        },
      ]);
      if (!tooBig) startUpload(file, id, preview);
    });
  }, [targetKey, startUpload]);

  const retryUpload = useCallback((entry) => {
    setUploads((u) => u.map((x) => (x.id === entry.id ? { ...x, status: 'uploading', progress: 0, error: '' } : x)));
    startUpload(entry.file, entry.id, entry.preview);
  }, [startUpload]);

  const dismissUpload = useCallback((entry) => {
    setUploads((u) => u.filter((x) => x.id !== entry.id));
    dropPreview(entry.preview);
  }, [dropPreview]);

  // ── Delete ─────────────────────────────────────────────────────────────────
  const removeItem = useCallback(async (m) => {
    const prev = items;
    const next = items.filter((x) => x.id !== m.id);
    setItems(next);
    onChangeRef.current?.(next);
    setLightbox((idx) => (idx == null ? idx : null));
    try {
      await crmDelete(`/api/crm/media/${m.id}`);
    } catch {
      // Restore the truth from the server if the delete didn't take.
      setItems(prev);
      onChangeRef.current?.(prev);
      refresh();
    }
  }, [items, refresh]);

  // ── Drag-and-drop ──────────────────────────────────────────────────────────
  const onDrop = useCallback((e) => {
    e.preventDefault();
    setDragOver(false);
    handleFiles(e.dataTransfer?.files);
  }, [handleFiles]);

  const onDragOver = useCallback((e) => {
    e.preventDefault();
    if (!dragOver) setDragOver(true);
  }, [dragOver]);

  const onDragLeave = useCallback((e) => {
    if (e.currentTarget.contains(e.relatedTarget)) return;
    setDragOver(false);
  }, []);

  // ── Lightbox keyboard nav ──────────────────────────────────────────────────
  useEffect(() => {
    if (lightbox == null) return;
    const onKey = (e) => {
      if (e.key === 'Escape') setLightbox(null);
      else if (e.key === 'ArrowRight') setLightbox((i) => (i == null ? i : (i + 1) % items.length));
      else if (e.key === 'ArrowLeft') setLightbox((i) => (i == null ? i : (i - 1 + items.length) % items.length));
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [lightbox, items.length]);

  // ── Lightbox focus management ──────────────────────────────────────────────
  // Move focus into the dialog on open and restore it to the opener on close.
  // Keyed on open/closed (not the index) so prev/next nav keeps trapped focus.
  const lightboxOpen = lightbox != null;
  const lbDialogRef = useRef(null);
  const lbCloseRef = useRef(null);

  useEffect(() => {
    if (!lightboxOpen) return undefined;
    const previouslyFocused = document.activeElement;
    lbCloseRef.current?.focus();
    return () => previouslyFocused?.focus?.();
  }, [lightboxOpen]);

  // Trap Tab/Shift+Tab within the dialog's controls.
  const trapLightboxFocus = useCallback((e) => {
    if (e.key !== 'Tab') return;
    const focusable = [...(lbDialogRef.current?.querySelectorAll('button') ?? [])];
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last?.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first?.focus();
    }
  }, []);

  const count = items.length;
  const wrapClass = `${styles.wrap}${compact ? ` ${styles.compact}` : ''}`;

  if (!targetKey) {
    return (
      <section className={wrapClass} aria-label={title}>
        <header className={styles.head}>
          <span className={styles.title}>{title}</span>
        </header>
        <p className={styles.disabled}>Save the record first to attach photos.</p>
      </section>
    );
  }

  return (
    <section className={wrapClass} aria-label={title}>
      <header className={styles.head}>
        <span className={styles.title}>{title}</span>
        {count > 0 && <span className={styles.count}>{count}</span>}
      </header>

      {/* Drop zone — a <label> so the hidden input stays keyboard-reachable. */}
      <label
        className={`${styles.dropzone}${dragOver ? ` ${styles.dropzoneActive}` : ''}`}
        onDrop={onDrop}
        onDragOver={onDragOver}
        onDragEnter={onDragOver}
        onDragLeave={onDragLeave}
      >
        <input
          ref={inputRef}
          className={styles.fileInput}
          type="file"
          accept="image/*"
          multiple
          onChange={(e) => { handleFiles(e.target.files); e.target.value = ''; }}
        />
        <span className={styles.dropGlyph} aria-hidden="true">{GLYPHS.upload}</span>
        <span className={styles.dropTitle}>Drag photos here or tap to upload</span>
        <span className={styles.dropHint}>JPG · PNG · WebP · up to 25 MB each · multiple at once</span>
      </label>

      {/* In-flight uploads */}
      {uploads.length > 0 && (
        <ul className={styles.uploadList}>
          {uploads.map((u) => (
            <li key={u.id} className={styles.uploadRow} data-status={u.status}>
              <span className={styles.uploadThumb}>
                <img src={u.preview} alt="" />
              </span>
              <span className={styles.uploadBody}>
                <span className={styles.uploadName} title={u.name}>{u.name}</span>
                {u.status === 'error' ? (
                  <span className={styles.uploadError}>{u.error}</span>
                ) : (
                  <span className={styles.progressTrack} aria-hidden="true">
                    <span className={styles.progressBar} style={{ width: `${u.progress}%` }} />
                  </span>
                )}
              </span>
              {u.status === 'error' ? (
                <span className={styles.uploadActions}>
                  {u.file && u.file.size <= MAX_BYTES && (
                    <button type="button" className={styles.iconBtn} onClick={() => retryUpload(u)} aria-label="Retry upload">
                      {GLYPHS.retry}
                    </button>
                  )}
                  <button type="button" className={styles.iconBtn} onClick={() => dismissUpload(u)} aria-label="Dismiss">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg>
                  </button>
                </span>
              ) : u.status === 'done' ? (
                <span className={styles.uploadDone} aria-hidden="true">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12.5 10 17.5 19 7" /></svg>
                </span>
              ) : (
                <span className={styles.uploadPct}>{u.progress}%</span>
              )}
            </li>
          ))}
        </ul>
      )}

      {/* Gallery */}
      {galleryState === 'loading' && (
        <div className={styles.grid} aria-hidden="true">
          <span className={styles.skel} /><span className={styles.skel} /><span className={styles.skel} />
        </div>
      )}

      {galleryState === 'error' && (
        <div className={styles.errorBox}>
          <span className={styles.errorText}>{galleryError}</span>
          <button type="button" className={styles.retryBtn} onClick={refresh}>Retry</button>
        </div>
      )}

      {galleryState === 'ready' && count === 0 && uploads.length === 0 && (
        <p className={styles.empty}>No photos yet — uploads appear here instantly.</p>
      )}

      {count > 0 && (
        <ul className={styles.grid}>
          {displayItems.map((m, i) => (
            <li key={m.id} className={styles.cell}>
              <button
                type="button"
                className={styles.thumbBtn}
                onClick={() => setLightbox(i)}
                aria-label={`View photo ${i + 1}`}
              >
                <img
                  className={styles.thumbImg}
                  src={mediaSrc(m) || undefined}
                  alt={`Property photo ${i + 1}`}
                  loading="lazy"
                  decoding="async"
                  onError={(e) => { e.currentTarget.dataset.broken = 'true'; }}
                />
              </button>
              <button
                type="button"
                className={styles.deleteBtn}
                onClick={() => removeItem(m)}
                aria-label={`Delete photo ${i + 1}`}
              >
                {GLYPHS.trash}
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* Lightbox */}
      {lightbox != null && displayItems[lightbox] && (
        <div
          ref={lbDialogRef}
          className={styles.lightbox}
          role="dialog"
          aria-modal="true"
          aria-label={`Photo ${lightbox + 1} of ${count}`}
          onKeyDown={trapLightboxFocus}
          onClick={(e) => { if (e.target === e.currentTarget) setLightbox(null); }}
        >
          <img className={styles.lightboxImg} src={mediaSrc(displayItems[lightbox]) || undefined} alt={`Property photo ${lightbox + 1}`} />
          <button ref={lbCloseRef} type="button" className={styles.lbClose} onClick={() => setLightbox(null)} aria-label="Close">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg>
          </button>
          {count > 1 && (
            <>
              <button type="button" className={`${styles.lbNav} ${styles.lbPrev}`} onClick={() => setLightbox((i) => (i - 1 + count) % count)} aria-label="Previous photo">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6" /></svg>
              </button>
              <button type="button" className={`${styles.lbNav} ${styles.lbNext}`} onClick={() => setLightbox((i) => (i + 1) % count)} aria-label="Next photo">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M9 18l6-6-6-6" /></svg>
              </button>
              <span className={styles.lbCounter}>{lightbox + 1} / {count}</span>
            </>
          )}
        </div>
      )}
    </section>
  );
}
