/* ClientShared — the shared vocabulary of the enterprise client book.
   Pure helpers, inline stroke glyphs (currentColor, zero icon deps — house
   rule), domain constants (stages / types / sorts) and the small presentational
   atoms (Avatar, StagePill, ScoreMeter, TagList) reused by the list, the
   detail drawer, and every sub-pane. Renders only what the API returns. */
import styles from './ClientShared.module.css';

export const fmtInt = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });

// ── Inline stroke glyphs ──────────────────────────────────────────────────
export const GLYPHS = {
  refresh: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M20.5 12a8.5 8.5 0 1 1-2.49-6.01" /><path d="M20.5 3.5V8H16" />
    </svg>
  ),
  plus: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 5.5v13M5.5 12h13" />
    </svg>
  ),
  close: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="m6 6 12 12M18 6 6 18" />
    </svg>
  ),
  search: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="11" cy="11" r="7" /><path d="m20 20-3.2-3.2" />
    </svg>
  ),
  clients: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="9" cy="8.5" r="3.2" /><path d="M3.5 19.5c0-3 2.5-5 5.5-5s5.5 2 5.5 5" />
      <circle cx="16.5" cy="9.5" r="2.4" /><path d="M16 14.7c2.6.3 4.5 2 4.5 4.3" />
    </svg>
  ),
  house: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 10.5 12 3l9 7.5" /><path d="M5.5 9.5V20h13V9.5" /><path d="M9.5 20v-6h5v6" />
    </svg>
  ),
  eye: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z" /><circle cx="12" cy="12" r="3" />
    </svg>
  ),
  sort: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M7 4v16M7 20l-3-3M7 4l3 3" /><path d="M14 7h6M14 12h4M14 17h2" />
    </svg>
  ),
  check: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="m4.5 12.5 5 5 10-11" />
    </svg>
  ),
  pin: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M9 4h6l-1 5 3 3v2H7v-2l3-3-1-5Z" /><path d="M12 14v6" />
    </svg>
  ),
  trash: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 7h16M9 7V4.5h6V7M6 7l1 13h10l1-13" /><path d="M10 11v5M14 11v5" />
    </svg>
  ),
  edit: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 20h4L19 9l-4-4L4 16v4Z" /><path d="M14 6l4 4" />
    </svg>
  ),
  clock: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" />
    </svg>
  ),
  tag: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3.5 11.5 11 4h6.5V10.5L10 18l-6.5-6.5Z" /><circle cx="14" cy="7.5" r="1.1" fill="currentColor" stroke="none" />
    </svg>
  ),
  archive: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3.5 6.5h17v3h-17z" /><path d="M5 9.5V20h14V9.5" /><path d="M10 13h4" />
    </svg>
  ),
  assign: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="10" cy="8" r="3.4" /><path d="M4 19c0-3 2.7-5 6-5 1 0 2 .2 2.8.6" /><path d="M17 14v6M14 17h6" />
    </svg>
  ),
  mail: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="5.5" width="18" height="13" rx="2" /><path d="m4 7 8 6 8-6" />
    </svg>
  ),
  phone: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M6 3.5h3l1.5 4-2 1.5a11 11 0 0 0 5 5l1.5-2 4 1.5v3c0 1-.8 1.9-1.9 1.8C12 19.5 4.5 12 4.2 5.4 4.1 4.3 5 3.5 6 3.5Z" />
    </svg>
  ),
  briefcase: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3.5" y="7.5" width="17" height="12" rx="2" /><path d="M9 7.5V6a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v1.5" /><path d="M3.5 12.5h17" />
    </svg>
  ),
  bolt: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M13 3 5 13.5h5L11 21l8-10.5h-5L13 3Z" />
    </svg>
  ),
  chevron: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="m9 6 6 6-6 6" />
    </svg>
  ),
  note: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M5 4h14v12l-4 4H5V4Z" /><path d="M15 20v-4h4" /><path d="M8.5 9h7M8.5 12.5h5" />
    </svg>
  ),
  task: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="4" y="4.5" width="16" height="16" rx="3" /><path d="m8 12 3 3 5-6" />
    </svg>
  ),
  minus: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
      <path d="M5.5 12h13" />
    </svg>
  ),
};

// ── Domain constants ──────────────────────────────────────────────────────
export const STAGES = [
  { id: 'lead', label: 'Lead' },
  { id: 'active', label: 'Active' },
  { id: 'nurture', label: 'Nurture' },
  { id: 'under_contract', label: 'Under Contract' },
  { id: 'closed', label: 'Closed' },
  { id: 'lost', label: 'Lost' },
];
const STAGE_LABELS = Object.fromEntries(STAGES.map((s) => [s.id, s.label]));
export const stageLabel = (id) => STAGE_LABELS[String(id || '').toLowerCase()] || 'Lead';
export const isStage = (id) => Object.prototype.hasOwnProperty.call(STAGE_LABELS, String(id || '').toLowerCase());
export const normStage = (id) => (isStage(id) ? String(id).toLowerCase() : 'lead');

export const CLIENT_TYPES = [
  { id: 'all', label: 'All' },
  { id: 'seller', label: 'Sellers' },
  { id: 'buyer', label: 'Buyers' },
  { id: 'both', label: 'Both' },
];

export const SORTS = [
  { id: 'recent', label: 'Recent' },
  { id: 'score', label: 'Score' },
  { id: 'name', label: 'Name' },
  { id: 'last_contacted', label: 'Last Contact' },
];

export const PORTAL_LINK_KINDS = [
  { id: 'seller', label: 'Seller' },
  { id: 'joint_venture', label: 'Joint venture' },
];

export const PORTAL_ASSET_SCOPES = [
  { id: 'summary', label: 'Property summary', defaultEnabled: true },
  { id: 'media', label: 'Media', defaultEnabled: true },
  { id: 'milestones', label: 'Milestones', defaultEnabled: true },
  { id: 'title_summary', label: 'Preliminary title summary', defaultEnabled: false },
  { id: 'zoning_summary', label: 'Zoning summary', defaultEnabled: false },
  { id: 'underwriting', label: 'Underwriting trace', defaultEnabled: false },
  { id: 'documents', label: 'Approved documents', defaultEnabled: false },
];

const PORTAL_KIND_LABELS = Object.fromEntries(PORTAL_LINK_KINDS.map((item) => [item.id, item.label]));
const PORTAL_SCOPE_LABELS = Object.fromEntries(PORTAL_ASSET_SCOPES.map((item) => [item.id, item.label]));
export const portalKindLabel = (id) => PORTAL_KIND_LABELS[id] || 'Dossier';
export const portalScopeLabel = (id) => PORTAL_SCOPE_LABELS[id] || id;
export const defaultPortalAssetScope = () => Object.fromEntries(
  PORTAL_ASSET_SCOPES.map((item) => [item.id, item.defaultEnabled]),
);

// ── Pure helpers ──────────────────────────────────────────────────────────
export function toNum(v) {
  if (v === null || v === undefined || v === '') return null;
  const n = typeof v === 'string' ? Number(v) : v;
  return Number.isFinite(n) ? n : null;
}

export function clampScore(v) {
  const n = toNum(v);
  if (n === null) return null;
  return Math.max(0, Math.min(100, Math.round(n)));
}

export function scoreTier(score) {
  if (score === null || score === undefined) return 'mid';
  if (score >= 67) return 'high';
  if (score >= 34) return 'mid';
  return 'low';
}

export function shortDollars(n) {
  const trim = (x) => {
    const s = x >= 100 ? String(Math.round(x)) : x.toFixed(1);
    return s.replace(/\.0$/, '');
  };
  const abs = Math.abs(n);
  if (abs >= 1e9) return `$${trim(n / 1e9)}B`;
  if (abs >= 1e6) return `$${trim(n / 1e6)}M`;
  if (abs >= 1e3) return `$${trim(n / 1e3)}K`;
  return `$${fmtInt.format(n)}`;
}

export function initialsOf(name) {
  return (
    String(name || '')
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((w) => w[0].toUpperCase())
      .join('') || '·'
  );
}

// "now / 12m / 3h / 2d / 5w / 4mo / 1y" — instrument-cluster relative time.
export function relTime(iso) {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return 'now';
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  const d = s / 86400;
  if (d < 7) return `${Math.floor(d)}d`;
  if (d < 30) return `${Math.floor(d / 7)}w`;
  if (d < 365) return `${Math.floor(d / 30)}mo`;
  return `${Math.floor(d / 365)}y`;
}

export function fmtDate(iso) {
  if (!iso) return null;
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return null;
  return t.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

export function normalizeType(t, fallback = 'buyer') {
  const v = String(t || '').toLowerCase();
  return v === 'seller' || v === 'buyer' || v === 'both' ? v : fallback;
}

// preferences is jsonb — object, JSON string, or junk. Never crash.
export function parsePrefs(p) {
  if (!p) return null;
  if (typeof p === 'string') {
    try { p = JSON.parse(p); } catch { return null; }
  }
  return typeof p === 'object' && !Array.isArray(p) ? p : null;
}

function budgetChip(b) {
  if (b === null || b === undefined) return null;
  if (typeof b === 'object' && !Array.isArray(b)) {
    const min = toNum(b.min);
    const max = toNum(b.max);
    if (min !== null && max !== null) return `${shortDollars(min)}–${shortDollars(max)}`;
    if (max !== null) return `≤ ${shortDollars(max)}`;
    if (min !== null) return `≥ ${shortDollars(min)}`;
    return null;
  }
  const n = toNum(b);
  if (n !== null) return shortDollars(n);
  return typeof b === 'string' && b.trim() ? b.trim() : null;
}

export function prefChipsOf(preferences) {
  const p = parsePrefs(preferences);
  if (!p) return [];
  const chips = [];
  const budget = budgetChip(p.budget ?? p.price ?? p.max_price);
  if (budget) chips.push(budget);
  const bedsRaw = p.beds ?? p.bedrooms;
  const beds = toNum(bedsRaw);
  if (beds !== null) chips.push(`${fmtInt.format(beds)} bd`);
  else if (typeof bedsRaw === 'string' && bedsRaw.trim()) chips.push(`${bedsRaw.trim()} bd`);
  const zipsRaw = p.zips ?? p.zip_codes ?? p.zip;
  const zips = Array.isArray(zipsRaw)
    ? zipsRaw.map((z) => String(z).trim()).filter(Boolean)
    : typeof zipsRaw === 'string'
      ? zipsRaw.split(/[\s,]+/).filter(Boolean)
      : [];
  chips.push(...zips.slice(0, 4));
  if (zips.length > 4) chips.push(`+${zips.length - 4}`);
  return chips;
}

// Friendly message from a thrown crmApi error (carries .status).
export function errMessage(err, noun = 'data') {
  if (!err) return `Couldn’t reach the ${noun}.`;
  if (err.status === 404) return `The ${noun} service isn’t online yet.`;
  if (err.status === 401 || err.status === 403) return 'Your session expired — sign in again.';
  return err.message || `Couldn’t reach the ${noun}.`;
}

// ── Presentational atoms ──────────────────────────────────────────────────
export function Avatar({ name, type, size = 'md' }) {
  return (
    <span className={styles.avatar} data-size={size} data-tint={normalizeType(type, 'both')} aria-hidden="true">
      {initialsOf(name)}
    </span>
  );
}

export function StagePill({ stage }) {
  const s = normStage(stage);
  return (
    <span className={styles.stagePill} data-stage={s}>
      <span className={styles.stageDot} aria-hidden="true" />
      {stageLabel(s)}
    </span>
  );
}

export function ScoreMeter({ score, showVal = true }) {
  const v = clampScore(score);
  if (v === null) return null;
  return (
    <span className={styles.meter} aria-label={`Lead score ${v} of 100`}>
      <span className={styles.meterTrack} data-tier={scoreTier(v)}>
        <span className={styles.meterFill} style={{ '--pct': `${v}%` }} />
      </span>
      {showVal && <span className={styles.meterVal}>{v}</span>}
    </span>
  );
}

export function TagList({ tags, max = 4 }) {
  const list = Array.isArray(tags) ? tags.filter(Boolean) : [];
  if (!list.length) return null;
  const shown = list.slice(0, max);
  return (
    <span className={styles.tagRow}>
      {shown.map((t) => (
        <span key={t} className={styles.tagChip}>
          <span className={styles.tagHash} aria-hidden="true">#</span>{t}
        </span>
      ))}
      {list.length > max && <span className={styles.tagMore}>+{list.length - max}</span>}
    </span>
  );
}
