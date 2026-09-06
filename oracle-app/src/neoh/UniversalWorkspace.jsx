import { Suspense, lazy, useCallback, useEffect, useRef, useState } from 'react';
import { Search, X } from 'lucide-react';

import { crmGet } from '../state/useCrmApi';
import { entityHref } from '../routes';
import {
  MIN_QUERY,
  SEARCH_KINDS,
  degradedMessage,
  emptyMessage,
  groupHits,
  isSearchKind,
} from './searchModel';
import styles from './UniversalWorkspace.module.css';

/**
 * Work — everything in the business, one box.
 *
 * Type a name and get the person; type an address and get the house; type
 * "appraisal" and get the deal. The four chips are FILTERS on one search,
 * not four destinations — the thing the six-tab shell got backwards. With
 * the box empty, the selected kind's full view renders beneath, so nothing
 * the old tabs could show is lost; with a query, results replace it.
 *
 * `q` and `type` live in the URL. A search is linkable, Back restores it,
 * and typing uses replaceState so a query typed one letter at a time does
 * not leave twenty history entries behind.
 *
 * A failed search leg is shown, not hidden. The API names it in `degraded`,
 * and that survives all the way to a sentence above the results — because a
 * partial answer that looks complete is worse than an empty one.
 */

const PeopleTab = lazy(() => import('../components/PeopleTab'));
const CommsTab = lazy(() => import('../components/CommsTab'));
const DealsTab = lazy(() => import('../components/DealsTab'));
const PropertiesTab = lazy(() => import('../components/PropertiesTab'));
const OurAITab = lazy(() => import('../components/OurAITab'));
const MissionBuilder = lazy(() =>
  import('./MissionBuilder').then((m) => ({ default: m.MissionBuilder })));
const IntelligenceFeed = lazy(() =>
  import('../components/IntelligenceFeed').then((m) => ({ default: m.IntelligenceFeed })));

const AI_WORKSPACES = new Set(['ai', 'sales', 'social', 'homeowners', 'automations', 'sites']);

/** Typing pauses this long before a request goes out. */
const DEBOUNCE_MS = 220;

function Fallback() {
  return (
    <div className={styles.fallback} aria-hidden="true">
      <div className={styles.fallbackCard} />
      <div className={styles.fallbackCard} />
    </div>
  );
}

function hrefForHit(hit) {
  // Entity kinds open a sheet; everything else already carries a usable href.
  if (hit.kind === 'people' || hit.kind === 'conversations') {
    return hit.href?.startsWith('/p/') ? hit.href : hit.href;
  }
  if (hit.kind === 'properties' || hit.kind === 'records') return entityHref('property', hit.id);
  if (hit.kind === 'deals') return entityHref('deal', hit.id);
  return hit.href;
}

function Results({ query, response, loading, selectedKind, onOpen }) {
  if (loading && !response) {
    return <Fallback />;
  }
  const groups = groupHits(response?.results, selectedKind);
  const degraded = degradedMessage(response);
  const empty = groups.length === 0 ? emptyMessage(query, response) : null;

  return (
    <div className={styles.results} aria-live="polite" aria-busy={loading}>
      {degraded && <p className={styles.degraded} role="status">{degraded}</p>}
      {empty && <p className={styles.empty}>{empty}</p>}
      {groups.map((group) => (
        <section className={styles.group} key={group.kind} aria-labelledby={`search-${group.kind}`}>
          <h2 className={styles.groupLabel} id={`search-${group.kind}`}>
            {group.label}
            <span className={styles.groupCount}>{group.hits.length}</span>
          </h2>
          <ul className={styles.hits}>
            {group.hits.map((hit) => (
              <li key={`${hit.kind}-${hit.id}`}>
                <button
                  type="button"
                  className={styles.hit}
                  onClick={() => onOpen(hit)}
                >
                  <span className={styles.hitLabel}>{hit.label}</span>
                  {hit.sublabel && <span className={styles.hitSub}>{hit.sublabel}</span>}
                </button>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

export function UniversalWorkspace({
  type,
  query = '',
  salesRoute,
  onNavigate,
  onSalesNavigate,
  onQueryChange,
  onOpenEntity,
}) {
  const [draft, setDraft] = useState(query);
  const [response, setResponse] = useState(null);
  const [loading, setLoading] = useState(false);
  const inputRef = useRef(null);
  const requestId = useRef(0);

  // The URL is the source of truth for `query`; the input holds the draft
  // between keystrokes. When the URL changes underneath (Back, a chip, a
  // link), the draft follows it — but never the other way round mid-type.
  const [seenQuery, setSeenQuery] = useState(query);
  if (query !== seenQuery) { setSeenQuery(query); setDraft(query); }

  useEffect(() => {
    const q = (query || '').trim();
    let cancelled = false;
    const id = ++requestId.current;
    if (q.length < MIN_QUERY) {
      // Deferred a frame, matching the rest of the app: no state is set
      // synchronously from the effect body (react-hooks/set-state-in-effect).
      const frame = window.requestAnimationFrame(() => {
        if (cancelled || id !== requestId.current) return;
        setResponse(null);
        setLoading(false);
      });
      return () => { cancelled = true; window.cancelAnimationFrame(frame); };
    }
    const start = window.requestAnimationFrame(() => {
      if (!cancelled && id === requestId.current) setLoading(true);
    });
    const kinds = isSearchKind(type) ? type : '';
    const timer = window.setTimeout(() => {
      crmGet(`/api/search?q=${encodeURIComponent(q)}${kinds ? `&types=${kinds}` : ''}`)
        .then((data) => {
          // A slower, older response must not overwrite a newer one.
          if (cancelled || id !== requestId.current) return;
          setResponse(data);
          setLoading(false);
        })
        .catch(() => {
          if (cancelled || id !== requestId.current) return;
          setResponse({ results: [], counts: {}, degraded: SEARCH_KINDS.map((k) => k.id) });
          setLoading(false);
        });
    }, DEBOUNCE_MS);
    return () => {
      cancelled = true;
      window.cancelAnimationFrame(start);
      window.clearTimeout(timer);
    };
  }, [query, type]);

  const submit = useCallback((value) => {
    setDraft(value);
    onQueryChange?.(value);
  }, [onQueryChange]);

  const clear = useCallback(() => {
    submit('');
    inputRef.current?.focus();
  }, [submit]);

  const open = useCallback((hit) => {
    const href = hrefForHit(hit);
    if (href.startsWith('/p/') || href.startsWith('/property/') || href.startsWith('/deal/')) {
      onOpenEntity?.(href);
      return;
    }
    // A non-entity href is a Work address; hand it to the shell as a type.
    const params = new URLSearchParams(href.split('?')[1] || '');
    onNavigate?.(params.get('type') || type);
  }, [onOpenEntity, onNavigate, type]);

  const searching = (query || '').trim().length >= MIN_QUERY;
  const selectedKind = isSearchKind(type) ? type : null;

  let view;
  if (type === 'conversations') view = <CommsTab onNavigate={onNavigate} />;
  else if (type === 'deals') view = <DealsTab onNavigate={onNavigate} />;
  else if (type === 'properties') view = <PropertiesTab onNavigate={onNavigate} />;
  else if (type === 'opportunities') view = <IntelligenceFeed />;
  else if (type === 'missions') view = <MissionBuilder onOpenEntity={onOpenEntity} />;
  else if (AI_WORKSPACES.has(type)) {
    view = (
      <OurAITab
        onNavigate={onNavigate}
        salesRoute={salesRoute}
        onSalesNavigate={onSalesNavigate}
        initialWorkspace={type === 'ai' ? undefined : type}
      />
    );
  } else view = <PeopleTab onNavigate={onNavigate} />;

  return (
    <div className={styles.work} data-work-type={type}>
      <div className={styles.bar}>
        <label className={styles.box}>
          <Search aria-hidden="true" size={18} className={styles.boxIcon} />
          <input
            ref={inputRef}
            type="search"
            className={styles.input}
            value={draft}
            placeholder="Search people, properties, deals, conversations"
            aria-label="Search everything"
            autoComplete="off"
            onChange={(event) => submit(event.target.value)}
          />
          {draft && (
            <button type="button" className={styles.clear} onClick={clear} aria-label="Clear search">
              <X aria-hidden="true" size={16} />
            </button>
          )}
        </label>
        <div className={styles.chips} role="tablist" aria-label="Kind">
          {SEARCH_KINDS.map((kind) => (
            <button
              key={kind.id}
              type="button"
              role="tab"
              aria-selected={type === kind.id}
              className={`${styles.chip} ${type === kind.id ? styles.chipActive : ''}`}
              onClick={() => onNavigate?.(kind.id, { q: draft })}
            >
              {kind.label}
              {searching && response?.counts?.[kind.id] > 0 && (
                <span className={styles.chipCount}>{response.counts[kind.id]}</span>
              )}
            </button>
          ))}
        </div>
      </div>

      {searching ? (
        <Results
          query={query}
          response={response}
          loading={loading}
          selectedKind={selectedKind}
          onOpen={open}
        />
      ) : (
        <Suspense fallback={<Fallback />}>{view}</Suspense>
      )}
    </div>
  );
}

export default UniversalWorkspace;
