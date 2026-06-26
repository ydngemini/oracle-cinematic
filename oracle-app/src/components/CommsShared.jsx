/* CommsShared — glyphs, time formatters, and defensive shape readers shared by
   CommsTab and CommsComposer. The comms surface renders ONLY real API data; it
   never simulates a message. Every reader below is built to tolerate two server
   shapes without crashing:
     • legacy nested  — { client:{…}, last:{interaction_type,payload,…}, count }
     • flat contract  — { thread_id, client_id, client_name, last_snippet,
                          last_at, channel, count } / Message { direction,
                          channel, subject, body, actor_role, status }
   so the tab keeps working today and lights up new fields the moment the
   backend lands them. */

// Inline stroke glyphs — currentColor, zero icon deps (house rule).
export const GLYPHS = {
  email: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3.5" y="5.5" width="17" height="13" rx="2" />
      <path d="m4.5 7.5 7.5 5.5 7.5-5.5" />
    </svg>
  ),
  sms: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 5.5h16v11H9l-5 4v-15Z" />
      <path d="M8 9.5h8M8 12.5h5" />
    </svg>
  ),
  note: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4.5 19.5h4l9.5-9.5-4-4-9.5 9.5v4Z" />
      <path d="m14 6 4 4" />
    </svg>
  ),
  voice: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M5 4.5h3.2l1.5 4-2 1.6a12.5 12.5 0 0 0 6.2 6.2l1.6-2 4 1.5v3.2c0 .9-.7 1.6-1.6 1.5C10.7 19.9 4.1 13.3 3.5 6.1c-.1-.9.6-1.6 1.5-1.6Z" />
    </svg>
  ),
  portal: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3.5" y="4.5" width="17" height="15" rx="2" />
      <path d="M3.5 9h17" />
      <path d="M6.5 6.8h.01M9.2 6.8h.01" />
    </svg>
  ),
  doc: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M6.5 3.5h7L18.5 8v12.5h-12V3.5Z" />
      <path d="M13.5 3.5V8h5" />
      <path d="M9.5 12h5M9.5 15h5" />
    </svg>
  ),
  waveform: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 10v4M8 7v10M12 4.5v15M16 7v10M20 10v4" />
    </svg>
  ),
  chevron: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="m14.5 5.5-6.5 6.5 6.5 6.5" />
    </svg>
  ),
  send: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3.5 11.2 20.5 4l-6.8 16.5-2.7-7.1-7.5-2.2Z" />
      <path d="M20.5 4 11 13.4" />
    </svg>
  ),
  refresh: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M20.5 12a8.5 8.5 0 1 1-2.49-6.01" />
      <path d="M20.5 3.5V8H16" />
    </svg>
  ),
  search: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="11" cy="11" r="6.5" />
      <path d="m16 16 4.5 4.5" />
    </svg>
  ),
  sparkle: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 3.5 13.8 8.2 18.5 10l-4.7 1.8L12 16.5l-1.8-4.7L5.5 10l4.7-1.8L12 3.5Z" />
      <path d="m18.6 14.4.6 1.6 1.6.6-1.6.6-.6 1.6-.6-1.6-1.6-.6 1.6-.6.6-1.6Z" />
    </svg>
  ),
  template: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="4.5" y="4.5" width="15" height="15" rx="2" />
      <path d="M8 9h8M8 12h8M8 15h5" />
    </svg>
  ),
  plus: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 5v14M5 12h14" />
    </svg>
  ),
  close: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="m6 6 12 12M18 6 6 18" />
    </svg>
  ),
};

export const MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
export const SYSTEM_TYPES = new Set(['portal_view', 'document_signed', 'status_change']);
export const MAX_BODY_HEIGHT = 132;

// ── Defensive primitives — never trust shape, never crash ──────────────────
export const firstString = (...vals) =>
  vals.find((v) => typeof v === 'string' && v.trim() !== '') ?? null;

export function parsePayload(p) {
  if (p == null) return {};
  if (typeof p === 'string') {
    try {
      const o = JSON.parse(p);
      return o && typeof o === 'object' ? o : { body: p };
    } catch {
      return { body: p };
    }
  }
  return typeof p === 'object' ? p : {};
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

export function firstNameOf(name) {
  return String(name || '').trim().split(/\s+/)[0] || '';
}

export function ts(iso) {
  const t = new Date(iso ?? 0).getTime();
  return Number.isNaN(t) ? 0 : t;
}

// Compact mono relative time for thread rows / system lines: NOW · 5M · 2H ·
// 3D · MAY 28. Instrument-cluster terse, tabular by construction.
export function relTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const diff = Date.now() - d.getTime();
  if (diff < 60e3) return 'NOW';
  const m = Math.floor(diff / 60e3);
  if (m < 60) return `${m}M`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}H`;
  const days = Math.floor(h / 24);
  if (days < 7) return `${days}D`;
  return `${MONTHS[d.getMonth()]} ${d.getDate()}`;
}

// Bubble timestamp: clock for today, MON D · clock otherwise.
export function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  let h = d.getHours();
  const mins = String(d.getMinutes()).padStart(2, '0');
  const ap = h >= 12 ? 'P' : 'A';
  h = h % 12 || 12;
  const clock = `${h}:${mins}${ap}`;
  return d.toDateString() === new Date().toDateString()
    ? clock
    : `${MONTHS[d.getMonth()]} ${d.getDate()} · ${clock}`;
}

export function channelOf(type) {
  const t = String(type || '').toLowerCase();
  if (t.includes('email')) return 'email';
  if (t.includes('voice') || t.includes('call')) return 'voice';
  if (t.includes('portal')) return 'portal';
  if (t.includes('doc') || t.includes('sign') || t.includes('contract')) return 'doc';
  if (t.includes('note')) return 'note';
  if (t.includes('sms') || t.includes('text')) return 'sms';
  // legacy 'message' and unknown → speech bubble
  return 'sms';
}

export function isSystem(it) {
  return SYSTEM_TYPES.has(String(it?.interaction_type || '').toLowerCase());
}

export function isVoiceNote(it) {
  const t = String(it?.interaction_type || it?.channel || '').toLowerCase();
  return t.includes('voice') || t.includes('call');
}

// 'out' | 'in' | null — the single source of direction truth.
export function directionOf(it) {
  if (!it) return null;
  const d = String(it.direction || '').toLowerCase();
  if (d) {
    if (d.startsWith('out') || d === 'sent') return 'out';
    if (d.startsWith('in') || d === 'received') return 'in';
  }
  const role = String(it.actor_role || '').toLowerCase();
  if (role === 'agent' || role === 'ai') return 'out';
  if (role === 'client' || role === 'contact' || role === 'lead') return 'in';
  return null;
}

export function isOutbound(it) {
  return directionOf(it) === 'out';
}

export function bodyOf(it) {
  // Flat Message carries body directly; legacy carries a JSON payload.
  if (typeof it?.body === 'string' && it.body.trim() !== '') return it.body;
  const p = parsePayload(it?.payload);
  return firstString(p.body, p.text, p.message, p.content, p.snippet) ?? '';
}

export function transcriptOf(it) {
  if (typeof it?.body === 'string' && it.body.trim() !== '') return it.body;
  const p = parsePayload(it?.payload);
  return firstString(p.transcript, p.transcript_snippet, p.summary, p.text, p.body);
}

export function systemLabel(it) {
  const t = String(it?.interaction_type || '').toLowerCase();
  const p = parsePayload(it?.payload);
  if (t === 'portal_view') {
    const where = firstString(p.page, p.section, p.url);
    return where ? `Portal view — ${where}` : 'Portal viewed';
  }
  if (t === 'document_signed') {
    const doc = firstString(p.document_name, p.document, p.title, p.name);
    return doc ? `Signed — ${doc}` : 'Document signed';
  }
  if (t === 'status_change') {
    const to = firstString(p.to, p.new_status, p.status);
    const from = firstString(p.from, p.old_status);
    if (to) return from ? `Status ${from} → ${to}` : `Status → ${to}`;
    return 'Status changed';
  }
  return t.replace(/_/g, ' ') || 'Event';
}

export function roleLabel(it) {
  const role = String(it?.actor_role || '').toLowerCase();
  if (role === 'ai') return 'AI';
  if (role === 'agent') return 'AGENT';
  return null;
}

// Delivery status → terse badge {label, tone}. tone ∈ amber|good|red|neutral|ghost.
export function statusMeta(status) {
  const s = String(status || '').toLowerCase().trim();
  if (!s || s === 'logged' || s === 'none') return null;
  if (/(queue|pending|schedul|sending)/.test(s)) return { label: 'Queued', tone: 'amber' };
  if (/(fail|error|bounce|undeliver|reject)/.test(s)) return { label: 'Failed', tone: 'red' };
  if (/(deliver)/.test(s)) return { label: 'Delivered', tone: 'good' };
  if (/(read|open|seen)/.test(s)) return { label: 'Read', tone: 'good' };
  if (/(sent|complete|done)/.test(s)) return { label: 'Sent', tone: 'neutral' };
  if (/(draft)/.test(s)) return { label: 'Draft', tone: 'ghost' };
  return { label: s.slice(0, 12).toUpperCase(), tone: 'neutral' };
}

// ── Normalizers — one canonical object from either server shape ────────────
export function normalizeThread(raw) {
  const r = raw && typeof raw === 'object' ? raw : {};
  const client = r.client && typeof r.client === 'object' ? r.client : null;
  const last = r.last && typeof r.last === 'object' ? r.last : null;

  const clientId = firstString(client?.id, r.client_id, r.clientId);
  const threadId = firstString(r.thread_id, r.threadId);
  const clientName = firstString(client?.full_name, r.client_name, r.clientName) ?? 'Unknown client';
  const clientType = firstString(client?.client_type, r.client_type, r.clientType);

  let emailKnown = false;
  let email = null;
  if (client && Object.prototype.hasOwnProperty.call(client, 'email')) {
    emailKnown = true;
    email = client.email;
  } else if (Object.prototype.hasOwnProperty.call(r, 'email')) {
    emailKnown = true;
    email = r.email;
  } else if (Object.prototype.hasOwnProperty.call(r, 'client_email')) {
    emailKnown = true;
    email = r.client_email;
  }

  const channelRaw = firstString(r.channel, last?.channel, last?.interaction_type);
  const channel = channelOf(channelRaw);
  const subject = firstString(last?.subject, r.subject);
  const snippet = firstString(last?.snippet, last?.subject, r.last_snippet, r.snippet, r.subject) ?? 'No messages yet';
  const lastAt = firstString(last?.created_at, r.last_at, r.lastAt) ?? null;
  const direction = directionOf(last);

  const countRaw = r.count ?? r.message_count ?? r.count_total;
  const count = Number.isFinite(Number(countRaw)) && countRaw != null ? Number(countRaw) : null;

  const ucRaw = r.unread_count ?? r.unreadCount;
  const unreadCount = Number.isFinite(Number(ucRaw)) && ucRaw != null ? Number(ucRaw) : null;
  let unread = false;
  if (unreadCount && unreadCount > 0) unread = true;
  else if (r.unread === true || Number(r.unread) > 0) unread = true;
  else if (direction === 'in') unread = true; // last message inbound → needs reply

  return {
    threadId,
    clientId,
    clientName,
    clientType,
    emailKnown,
    email,
    channel,
    channelRaw,
    subject,
    snippet,
    lastAt,
    direction,
    count,
    unread,
    unreadCount,
  };
}

export function normalizeMessage(raw, i = 0) {
  const r = raw && typeof raw === 'object' ? raw : {};
  const id = firstString(r.id, r.message_id) ?? `${firstString(r.created_at) ?? 'x'}-${i}`;
  const system = isSystem(r);
  const voice = isVoiceNote(r);
  return {
    id,
    createdAt: firstString(r.created_at, r.createdAt),
    system,
    systemLabel: system ? systemLabel(r) : null,
    out: isOutbound(r),
    voice,
    channel: channelOf(firstString(r.channel, r.interaction_type)),
    subject: firstString(r.subject),
    text: voice ? transcriptOf(r) : bodyOf(r),
    status: firstString(r.status),
    role: roleLabel(r),
    queued: Boolean(r._queued),
  };
}
