import { useCallback, useEffect, useState } from 'react';
import { crmGet, crmPost } from '../state/useCrmApi';
import {
  GLYPHS, CLIENT_TYPES, STAGES, SORTS, fmtInt, normalizeType,
  relTime, errMessage, toNum, Avatar, StagePill, ScoreMeter,
} from './ClientShared';
import ClientDetailDrawer from './ClientDetailDrawer';
import styles from './ClientCrmTab.module.css';

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const EMPTY_FORM = {
  full_name: '', email: '', phone: '', client_type: 'seller', stage: 'lead',
  company: '', budget: '', beds: '', zips: '',
};
const DEFAULT_FILTERS = { q: '', type: 'all', stage: '', sort: 'recent' };

function buildQuery(f) {
  const p = new URLSearchParams();
  p.set('type', f.type || 'all');
  p.set('sort', f.sort || 'recent');
  if (f.q) p.set('q', f.q);
  if (f.stage) p.set('stage', f.stage);
  return `/api/crm/clients?${p.toString()}`;
}

const BULK_ACTIONS = [
  { id: 'stage', label: 'Stage', glyph: GLYPHS.bolt, needs: 'stage' },
  { id: 'assign', label: 'Assign', glyph: GLYPHS.assign, needs: 'text', placeholder: 'agent id' },
  { id: 'archive', label: 'Archive', glyph: GLYPHS.archive, needs: null },
];

/**
 * ClientCrmTab — the enterprise client book. A unified, filterable, sortable
 * roster (search + type/stage/tag chips + saved segments), bulk operations,
 * a quick-add sheet, and the full ClientDetailDrawer. Renders only what
 * /api/crm/clients returns; every other state (loading/empty/error) is graceful.
 */
export default function ClientCrmTab({ embedded = false }) {
  const [clients, setClients] = useState(null); // null = first load
  const [error, setError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);

  const [qInput, setQInput] = useState('');
  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  const [activeSeg, setActiveSeg] = useState(null);

  const [segments, setSegments] = useState(null); // null = unknown
  const [segmentsOnline, setSegmentsOnline] = useState(false);
  const [savingView, setSavingView] = useState(false);
  const [viewName, setViewName] = useState('');

  const [selected, setSelected] = useState(() => new Set());
  const [bulkPanel, setBulkPanel] = useState(null); // {action, value}
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkError, setBulkError] = useState('');

  const [sheetOpen, setSheetOpen] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [formError, setFormError] = useState('');
  const [saving, setSaving] = useState(false);

  const [drawerCard, setDrawerCard] = useState(null);

  const filterKey = JSON.stringify(filters);

  // Debounce the search box into filters.q (clears any active segment).
  useEffect(() => {
    const id = setTimeout(() => {
      setFilters((f) => (f.q === qInput ? f : { ...f, q: qInput }));
    }, 280);
    return () => clearTimeout(id);
  }, [qInput]);

  // ── Fetch roster whenever filters change ─────────────────────────────────
  const fetchClients = useCallback((f) => {
    setRefreshing(true);
    return crmGet(buildQuery(f)).then(
      (data) => {
        const rows = Array.isArray(data?.clients) ? data.clients : [];
        setClients(rows);
        setError(null);
        setRefreshing(false);
        // Prune selection to ids still present.
        setSelected((sel) => {
          if (sel.size === 0) return sel;
          const ids = new Set(rows.map((r) => r.id));
          const next = new Set([...sel].filter((id) => ids.has(id)));
          return next.size === sel.size ? sel : next;
        });
      },
      (err) => {
        setError(err);
        setRefreshing(false);
        setClients((c) => (c === null ? null : c)); // keep stale list on soft fail
      }
    );
  }, []);

  useEffect(() => {
    // fetchClients sets `refreshing` synchronously and is shared by 3 event
    // handlers (manual refresh + two post-mutation reloads). Refetch-on-filter-
    // change is a legitimate data-fetch effect, so the loading flag stays in the
    // loader rather than being hoisted to render-phase across every caller.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    fetchClients(JSON.parse(filterKey));
  }, [filterKey, fetchClients]);

  // Segments — fetched once; the whole bar stays hidden if the route is offline.
  useEffect(() => {
    crmGet('/api/crm/clients/segments').then(
      (data) => { setSegments(Array.isArray(data?.segments) ? data.segments : []); setSegmentsOnline(true); },
      () => setSegmentsOnline(false)
    );
  }, []);

  // Esc closes the quick-add sheet.
  useEffect(() => {
    if (!sheetOpen) return undefined;
    const onKey = (e) => { if (e.key === 'Escape') setSheetOpen(false); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [sheetOpen]);

  const refresh = () => fetchClients(filters);

  const patchFilter = (patch, { clearSeg = true } = {}) => {
    if (clearSeg) setActiveSeg(null);
    setFilters((f) => ({ ...f, ...patch }));
  };

  const toggleType = (id) => patchFilter({ type: filters.type === id ? 'all' : id });
  const toggleStage = (id) => patchFilter({ stage: filters.stage === id ? '' : id });
  const setSort = (id) => patchFilter({ sort: id }, { clearSeg: false });

  const applySegment = (seg) => {
    const d = seg?.definition && typeof seg.definition === 'object' ? seg.definition : {};
    setActiveSeg(seg.id);
    setQInput(d.q || '');
    setFilters({
      q: d.q || '',
      type: d.type || 'all',
      stage: d.stage || '',
      sort: d.sort || 'recent',
    });
  };

  const saveSegment = () => {
    const name = viewName.trim();
    if (!name) return;
    setSavingView(false);
    setViewName('');
    crmPost('/api/crm/clients/segments', { name, definition: { ...filters } }).then(
      (data) => {
        const seg = data?.segment ?? data;
        if (seg?.id) { setSegments((s) => [...(s || []), seg]); setActiveSeg(seg.id); }
        else crmGet('/api/crm/clients/segments').then((d) => setSegments(Array.isArray(d?.segments) ? d.segments : []), () => {});
      },
      () => {}
    );
  };

  // ── Selection / bulk ─────────────────────────────────────────────────────
  const toggleSelect = (id) => setSelected((sel) => {
    const next = new Set(sel);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const clearSelection = () => { setSelected(new Set()); setBulkPanel(null); setBulkError(''); };
  const allVisibleSelected = clients && clients.length > 0 && selected.size === clients.length;
  const toggleSelectAll = () => {
    if (allVisibleSelected) setSelected(new Set());
    else setSelected(new Set((clients || []).map((c) => c.id)));
  };

  const openBulk = (action) => {
    if (action.needs === null) { runBulk(action.id, true); return; }
    setBulkError('');
    setBulkPanel({ action: action.id, value: action.needs === 'stage' ? 'active' : '' });
  };

  const runBulk = (action, value) => {
    const ids = [...selected];
    if (ids.length === 0) return;
    setBulkBusy(true);
    setBulkError('');
    crmPost('/api/crm/clients/bulk', { ids, action, value }).then(
      () => {
        setBulkBusy(false);
        clearSelection();
        fetchClients(filters);
      },
      (err) => { setBulkBusy(false); setBulkError(errMessage(err, 'bulk action')); }
    );
  };

  // Drawer pushes header edits back into the row in place.
  const onClientChanged = useCallback((card) => {
    if (!card?.id) return;
    setDrawerCard((d) => (d && d.id === card.id ? { ...d, ...card } : d));
    setClients((prev) => {
      if (!Array.isArray(prev)) return prev;
      if (card.archived) return prev.filter((c) => c.id !== card.id);
      return prev.map((c) => (c.id === card.id ? { ...c, ...card } : c));
    });
  }, []);

  // ── Quick-add ────────────────────────────────────────────────────────────
  const setField = (key, value) => setForm((f) => ({ ...f, [key]: value }));
  const openSheet = () => { setForm({ ...EMPTY_FORM, client_type: filters.type === 'buyer' ? 'buyer' : 'seller' }); setFormError(''); setSheetOpen(true); };
  const closeSheet = () => { if (!saving) setSheetOpen(false); };

  const submitProfile = () => {
    const name = form.full_name.trim();
    if (!name) { setFormError('A name is required.'); return; }
    const email = form.email.trim();
    if (email && !EMAIL_RE.test(email)) { setFormError('That email doesn’t look right.'); return; }
    const body = { full_name: name, client_type: form.client_type, stage: form.stage };
    if (email) body.email = email;
    if (form.phone.trim()) body.phone = form.phone.trim();
    if (form.company.trim()) body.company = form.company.trim();
    const prefs = {};
    const budget = toNum(form.budget.trim());
    if (budget !== null) prefs.budget = budget;
    const beds = toNum(form.beds.trim());
    if (beds !== null) prefs.beds = beds;
    const zips = form.zips.split(/[\s,]+/).map((z) => z.trim()).filter(Boolean);
    if (zips.length) prefs.zips = zips;
    if (Object.keys(prefs).length) body.preferences = prefs;

    setSaving(true);
    setFormError('');
    crmPost('/api/crm/clients', body).then(
      () => {
        setSaving(false);
        setSheetOpen(false);
        setForm(EMPTY_FORM);
        fetchClients(filters); // pull the server-computed card back
      },
      (err) => { setSaving(false); setFormError(errMessage(err, 'client')); }
    );
  };

  // ── Render ────────────────────────────────────────────────────────────────
  const isLoading = clients === null && !error;
  const hasList = Array.isArray(clients);
  const total = hasList ? clients.length : null;

  return (
    <section className={styles.wrap} aria-labelledby="people-title" aria-busy={isLoading || refreshing}>
      <header className={styles.topRow} data-embedded={embedded}>
        {embedded ? (
          <span className={styles.kicker}>Opportunity book{total !== null ? ` · ${fmtInt.format(total)}` : ''}</span>
        ) : (
          <div className={styles.destinationTitle}>
            <span className={styles.kicker}>Relationship intelligence</span>
            <h1 id="people-title">People</h1>
            {total !== null && <small>{fmtInt.format(total)} visible profiles</small>}
          </div>
        )}
        <div className={styles.topActions}>
          <button type="button" className={styles.newProfileBtn} onClick={openSheet}>
            {GLYPHS.plus} New profile
          </button>
          <button
            type="button"
            className={`${styles.refreshBtn} ${refreshing ? styles.refreshing : ''}`}
            onClick={refresh}
            disabled={refreshing || isLoading}
            aria-label="Refresh people"
          >
            {GLYPHS.refresh}
          </button>
        </div>
      </header>

      {/* Search + sort */}
      <div className={styles.toolbar}>
        <div className={styles.searchBar}>
          <span className={styles.searchIcon} aria-hidden="true">{GLYPHS.search}</span>
          <input
            className={styles.searchInput}
            value={qInput}
            onChange={(e) => setQInput(e.target.value)}
            placeholder="Search name, email, company…"
            aria-label="Search clients"
            type="search"
          />
          {qInput && (
            <button type="button" className={styles.searchClear} onClick={() => setQInput('')} aria-label="Clear search">{GLYPHS.close}</button>
          )}
        </div>
        <label className={styles.sortWrap}>
          <span className={styles.sortGlyph} aria-hidden="true">{GLYPHS.sort}</span>
          <select className={styles.sortSelect} value={filters.sort} onChange={(e) => setSort(e.target.value)} aria-label="Sort clients">
            {SORTS.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
          </select>
        </label>
      </div>

      {/* Type filter chips */}
      <div className={styles.chipRow} role="group" aria-label="Filter by type">
        {CLIENT_TYPES.map((t) => (
          <button key={t.id} type="button" className={styles.chip} aria-pressed={filters.type === t.id} onClick={() => toggleType(t.id)}>
            {t.label}
          </button>
        ))}
      </div>

      {/* Stage filter chips */}
      <div className={styles.chipScroll} role="group" aria-label="Filter by stage">
        {STAGES.map((s) => (
          <button key={s.id} type="button" className={styles.chip} data-stage={s.id} aria-pressed={filters.stage === s.id} onClick={() => toggleStage(s.id)}>
            {s.label}
          </button>
        ))}
      </div>

      {/* Saved segments */}
      {segmentsOnline && (
        <div className={styles.segmentBar}>
          {(segments || []).map((seg) => (
            <button key={seg.id} type="button" className={styles.segChip} aria-pressed={activeSeg === seg.id} onClick={() => applySegment(seg)}>
              {seg.name || 'View'}
            </button>
          ))}
          {savingView ? (
            <span className={styles.saveView}>
              <input
                className={styles.saveInput}
                value={viewName}
                onChange={(e) => setViewName(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') saveSegment(); if (e.key === 'Escape') { setSavingView(false); setViewName(''); } }}
                placeholder="View name"
                aria-label="Segment name"
                autoFocus
              />
              <button type="button" className={styles.saveGo} onClick={saveSegment} aria-label="Save segment">{GLYPHS.check}</button>
            </span>
          ) : (
            <button type="button" className={styles.segAdd} onClick={() => setSavingView(true)} aria-label="Save current filters as a segment">
              {GLYPHS.plus} Save view
            </button>
          )}
        </div>
      )}

      {/* Bulk toolbar */}
      {selected.size > 0 && (
        <div className={styles.bulkBar} role="region" aria-label="Bulk actions">
          <div className={styles.bulkTop}>
            <button type="button" className={styles.bulkAll} onClick={toggleSelectAll} aria-pressed={allVisibleSelected}>
              <span className={styles.bulkCheck} data-on={allVisibleSelected} aria-hidden="true">{GLYPHS.check}</span>
              {fmtInt.format(selected.size)} selected
            </button>
            <div className={styles.bulkActions}>
              {BULK_ACTIONS.map((a) => (
                <button key={a.id} type="button" className={styles.bulkBtn} data-danger={a.id === 'archive'} disabled={bulkBusy} onClick={() => openBulk(a)}>
                  <span className={styles.bulkGlyph} aria-hidden="true">{a.glyph}</span>{a.label}
                </button>
              ))}
            </div>
            <button type="button" className={styles.bulkClose} onClick={clearSelection} aria-label="Clear selection">{GLYPHS.close}</button>
          </div>

          {bulkPanel && (
            <div className={styles.bulkPanel}>
              {bulkPanel.action === 'stage' ? (
                <select className={styles.bulkInput} value={bulkPanel.value} onChange={(e) => setBulkPanel((p) => ({ ...p, value: e.target.value }))} aria-label="Stage">
                  {STAGES.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
                </select>
              ) : (
                <input
                  className={styles.bulkInput}
                  value={bulkPanel.value}
                  onChange={(e) => setBulkPanel((p) => ({ ...p, value: e.target.value }))}
                  onKeyDown={(e) => { if (e.key === 'Enter' && bulkPanel.value.trim()) runBulk(bulkPanel.action, bulkPanel.value.trim()); }}
                  placeholder={BULK_ACTIONS.find((a) => a.id === bulkPanel.action)?.placeholder}
                  aria-label={`${bulkPanel.action} value`}
                  autoFocus
                />
              )}
              <button
                type="button"
                className={styles.bulkApply}
                disabled={bulkBusy || (bulkPanel.action !== 'stage' && !bulkPanel.value.trim())}
                onClick={() => runBulk(bulkPanel.action, bulkPanel.action === 'stage' ? bulkPanel.value : bulkPanel.value.trim())}
              >
                {bulkBusy ? 'Applying…' : 'Apply'}
              </button>
            </div>
          )}
          {bulkError && <span className={styles.err} role="alert">{bulkError}</span>}
        </div>
      )}

      {/* List body */}
      {isLoading ? (
        <div className={styles.skeleton} aria-hidden="true">
          <div className={styles.skelCard} /><div className={styles.skelCard} /><div className={styles.skelCard} />
        </div>
      ) : error && clients === null ? (
        <div className={styles.errorBox} role="alert">
          <span className={styles.errorTick} aria-hidden="true" />
          <p className={styles.errorText}>{errMessage(error, 'client book')}</p>
          <button type="button" className={styles.retryBtn} onClick={refresh}>Retry</button>
        </div>
      ) : (
        <>
          {error && (
            <div className={styles.errorStrip} role="alert">
              <span className={styles.errorTick} aria-hidden="true" />
              <p className={styles.errorText}>{errMessage(error, 'client book')}</p>
              <button type="button" className={styles.retrySm} onClick={refresh}>Retry</button>
            </div>
          )}

          {hasList && clients.length === 0 ? (
            <div className={styles.empty}>
              <span className={styles.emptyGlyph} aria-hidden="true">{GLYPHS.clients}</span>
              <p className={styles.emptyTitle}>
                {filters.q || filters.stage || filters.type !== 'all' ? 'No matches' : 'No clients yet'}
              </p>
              <p className={styles.emptyHint}>
                {filters.q || filters.stage || filters.type !== 'all'
                  ? 'Loosen the filters, or tap + to add a client.'
                  : 'Tap + to create your first client profile — the roster fills the pipeline.'}
              </p>
            </div>
          ) : (
            <ul className={styles.list} role="list">
              {(clients || []).map((c, i) => {
                const tint = normalizeType(c.client_type, 'both');
                const checked = selected.has(c.id);
                const last = c.last_activity || (c.last_touch ? { kind: c.last_touch.interaction_type, summary: c.last_touch.interaction_type, created_at: c.last_touch.created_at } : null);
                const openCount = toNum(c.open_tasks);
                return (
                  <li
                    key={c.id ?? `${c.full_name}-${i}`}
                    className={styles.clientRow}
                    data-selected={checked}
                    style={{ animationDelay: `${Math.min(i, 8) * 50}ms` }}
                  >
                    <button
                      type="button"
                      className={styles.rowCheck}
                      data-on={checked}
                      aria-pressed={checked}
                      aria-label={checked ? `Deselect ${c.full_name}` : `Select ${c.full_name}`}
                      onClick={() => toggleSelect(c.id)}
                    >
                      <span aria-hidden="true">{GLYPHS.check}</span>
                    </button>

                    <button type="button" className={styles.rowOpen} onClick={() => setDrawerCard(c)} aria-label={`Open ${c.full_name || 'client'}`}>
                      <Avatar name={c.full_name} type={tint} size="md" />
                      <div className={styles.rowMain}>
                        <span className={styles.rowNameLine}>
                          <span className={styles.rowName}>{c.full_name || 'Unnamed'}</span>
                          <StagePill stage={c.stage} />
                        </span>
                        {(c.company || c.email) && (
                          <span className={styles.rowSub}>{c.company || c.email}</span>
                        )}
                        <span className={styles.rowFoot}>
                          {toNum(c.lead_score) !== null && <span className={styles.rowMeterWrap}><ScoreMeter score={c.lead_score} /></span>}
                          {openCount !== null && openCount > 0 && (
                            <span className={styles.footTasks}>{GLYPHS.task}{fmtInt.format(openCount)} task{openCount === 1 ? '' : 's'}</span>
                          )}
                          {last && (
                            <span className={styles.footActivity}>
                              <span className={styles.footTime}>{relTime(last.created_at) || ''}</span>
                              {last.summary && <span className={styles.footKind}>{last.summary}</span>}
                            </span>
                          )}
                        </span>
                      </div>
                      <span className={styles.rowChevron} aria-hidden="true">{GLYPHS.chevron}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </>
      )}

      {/* FAB → quick-add */}
      <button type="button" className={styles.fab} aria-label="Add client" onClick={openSheet}>{GLYPHS.plus}</button>

      {sheetOpen && (
        <div className={styles.sheetLayer}>
          <button type="button" className={styles.scrim} aria-label="Close" onClick={closeSheet} />
          <div className={styles.sheet} role="dialog" aria-modal="true" aria-label="Add client">
            <span className={styles.sheetGrip} aria-hidden="true" />
            <header className={styles.sheetHead}>
              <div className={styles.sheetHeading}>
                <span className={styles.sheetKicker}>New Client</span>
                <h3 className={styles.sheetTitle}>Create Profile</h3>
              </div>
              <button type="button" className={styles.sheetClose} onClick={closeSheet} disabled={saving} aria-label="Close">{GLYPHS.close}</button>
            </header>

            {/* Type */}
            <div className={`${styles.segments} ${styles.typeToggle}`}>
              {[{ id: 'seller', label: 'Seller' }, { id: 'buyer', label: 'Buyer' }, { id: 'both', label: 'Both' }].map((s) => (
                <button key={s.id} type="button" data-seg={s.id === 'buyer' ? 'buyer' : 'seller'} aria-pressed={form.client_type === s.id} className={`${styles.segKey}${form.client_type === s.id ? ` ${styles.segKeyActive}` : ''}`} onClick={() => setField('client_type', s.id)}>
                  <span className={styles.segLabel}>{s.label}</span>
                </button>
              ))}
            </div>

            <label className={styles.field}>
              <span className={styles.microLabel}>Full Name · Required</span>
              <input className={styles.input} value={form.full_name} onChange={(e) => { setField('full_name', e.target.value); if (formError) setFormError(''); }} autoComplete="name" placeholder="Client name" />
            </label>

            <div className={styles.fieldGrid}>
              <label className={styles.field}>
                <span className={styles.microLabel}>Stage</span>
                <select className={styles.input} value={form.stage} onChange={(e) => setField('stage', e.target.value)}>
                  {STAGES.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
                </select>
              </label>
              <label className={styles.field}>
                <span className={styles.microLabel}>Company</span>
                <input className={styles.input} value={form.company} onChange={(e) => setField('company', e.target.value)} placeholder="Optional" />
              </label>
            </div>

            <div className={styles.formSection}>
              <span className={styles.sectionLabel}>Contact Info</span>
              <label className={styles.field}>
                <span className={styles.microLabel}>Email</span>
                <input className={styles.input} value={form.email} onChange={(e) => setField('email', e.target.value)} type="email" inputMode="email" autoComplete="email" placeholder="client@email.com" />
              </label>
              <label className={styles.field}>
                <span className={styles.microLabel}>Phone</span>
                <input className={`${styles.input} ${styles.inputMono}`} value={form.phone} onChange={(e) => setField('phone', e.target.value)} type="tel" inputMode="tel" autoComplete="tel" placeholder="555 000 0000" />
              </label>
            </div>

            <div className={styles.formSection}>
              <span className={styles.sectionLabel}>Preferences</span>
              <div className={styles.fieldGrid}>
                <label className={styles.field}>
                  <span className={styles.microLabel}>Budget · USD</span>
                  <input className={`${styles.input} ${styles.inputMono}`} value={form.budget} onChange={(e) => setField('budget', e.target.value.replace(/[^\d.]/g, ''))} inputMode="decimal" placeholder="0" />
                </label>
                <label className={styles.field}>
                  <span className={styles.microLabel}>Beds</span>
                  <input className={`${styles.input} ${styles.inputMono}`} value={form.beds} onChange={(e) => setField('beds', e.target.value.replace(/[^\d]/g, ''))} inputMode="numeric" placeholder="0" />
                </label>
              </div>
              <label className={styles.field}>
                <span className={styles.microLabel}>Zip Codes · Comma separated</span>
                <input className={`${styles.input} ${styles.inputMono}`} value={form.zips} onChange={(e) => setField('zips', e.target.value)} inputMode="numeric" placeholder="33101, 33139" />
              </label>
            </div>

            {formError && <span className={styles.err} role="alert">{formError}</span>}

            <button type="button" className={styles.primaryBtn} onClick={submitProfile} disabled={saving}>
              {saving ? 'Saving…' : 'Create Profile'}
            </button>
          </div>
        </div>
      )}

      {drawerCard && (
        <ClientDetailDrawer
          card={drawerCard}
          onClose={() => setDrawerCard(null)}
          onClientChanged={onClientChanged}
        />
      )}
    </section>
  );
}
