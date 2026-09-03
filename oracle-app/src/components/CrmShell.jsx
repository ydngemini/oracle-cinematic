import {
  Suspense,
  lazy,
  startTransition,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import {
  Bot,
  CircleHelp,
  House,
  Radar,
  Search,
  ShieldAlert,
  UserRound,
  X,
} from 'lucide-react';
import {
  DEFAULT_WORK_TYPE,
  SALES_ROUTES,
  VIEWS,
  VIEW_PATHS,
  href,
  parse,
  redirectFor,
  resolveLegacyId,
} from '../routes';
import { BillingOverlay } from './BillingOverlay';
import { OnboardingGate } from './OnboardingGate';
import { ErrorBoundary } from './ErrorBoundary';
import { TabBar } from './TabBar';
import { StateProvider } from '../state/StateContext';
import { StateSelector } from './StateSelector';
import { NeohBrandMark } from './NeohBrandMark';
import { NeohFooter } from './NeohFooter';
import { AssistantProvider } from './AssistantContext';
import { AssistantShell } from './AssistantShell';
import { BorderBeam } from './motion/BorderBeam';
import { AdaptiveViewTransition } from './motion/AdaptiveViewTransition';
import { ProductTour } from './ProductTour';
import styles from './CrmShell.module.css';

// Each tab is its own chunk — a field agent on LTE only pays for the tab
// they open. (Same code-split rationale the HUD used for its 3D canvas.)
const loadNeohHome = () => import('../neoh/NeohHome');
const loadPeopleTab = () => import('./PeopleTab');
const loadOurAITab = () => import('./OurAITab');
const loadPersonalAITab = () => import('./PersonalAITab');
const loadMyProfileTab = () => import('./MyProfileTab');
const loadAdminOpsTab = () => import('./AdminOpsTab');

const NeohHome = lazy(() =>
  import('../neoh/NeohHome').then((m) => ({ default: m.NeohHome })));
const OurAITab = lazy(loadOurAITab);
const UniversalWorkspace = lazy(() =>
  import('../neoh/UniversalWorkspace').then((m) => ({ default: m.UniversalWorkspace })));
const PersonalAITab = lazy(loadPersonalAITab);
const MyProfileTab = lazy(loadMyProfileTab);
const AdminOpsTab = lazy(loadAdminOpsTab);

// Three destinations. The six old tabs are not gone — People, Inbox, Deals
// and Property View are Work views chosen by ?type, and Our AI's workspaces
// live there too until they are re-homed — but they are no longer places the
// agent has to know about to find anything. Home says what matters, Work
// holds everything, Neoh is the conversation. `preload` warms the chunk each
// view renders first.
const TABS = [
  { id: VIEWS.home, label: 'Home', Icon: House, preload: loadNeohHome },
  { id: VIEWS.work, label: 'Work', Icon: Search, preload: loadPeopleTab },
  { id: VIEWS.neoh, label: 'Neoh', Icon: Radar, preload: loadOurAITab },
];

const TAB_KEY = 'oracle_crm_tab';

/** The view a session left off on. Old ids stored by the six-tab shell are
 *  resolved through the same alias table that always handled renames. */
function savedRoute() {
  const stored = sessionStorage.getItem(TAB_KEY) || '';
  const resolved = resolveLegacyId(stored);
  if (resolved.view !== stored) sessionStorage.setItem(TAB_KEY, resolved.view);
  return resolved;
}

function workParams(type, sales = null, q = '') {
  return { type: type || DEFAULT_WORK_TYPE, sales: sales || null, q: q || '' };
}

/**
 * The route on first mount. An old address is rewritten with replaceState so
 * a stale bookmark does not leave a dead URL in the back stack; the bare '/'
 * the app is usually entered at resumes the saved view — but ONLY on first
 * mount. On popstate it must not: `select` has already overwritten
 * sessionStorage with the view being navigated away from, so resuming it
 * would leave the screen unchanged while the address bar went back, and Back
 * would look broken. That is why popstate parses the address and nothing else.
 */
function initialRoute() {
  const redirect = redirectFor(window.location.pathname);
  if (redirect) window.history.replaceState({}, '', redirect);
  const url = new URL(redirect || (window.location.pathname + window.location.search), window.location.origin);
  const parsed = parse(url.pathname, url.search, { fallbackView: VIEWS.home });
  if (url.pathname === VIEW_PATHS.home && !parsed.entity) {
    const saved = savedRoute();
    if (saved.view === VIEWS.work) return { view: VIEWS.work, params: workParams(saved.type), entity: null };
    if (saved.view === VIEWS.neoh) return { view: VIEWS.neoh, params: {}, entity: null };
  }
  return parsed;
}

function ViewFallback() {
  return (
    <div className={styles.fallback} aria-hidden="true">
      <div className={styles.fallbackCard} />
      <div className={styles.fallbackCard} />
      <div className={styles.fallbackCard} />
    </div>
  );
}

/**
 * CrmShell — the agent CRM frame ("as soon as they open the app,
 * they should be here": Today is the landing tab). Replaces the desktop
 * the retired DashboardLayout; BillingOverlay and OnboardingGate stay as self-gating
 * overlays, exactly as before.
 */
export function CrmShell() {
  const [profileOpen, setProfileOpen] = useState(false);
  const [profileView, setProfileView] = useState('settings');
  const profileSheetRef = useRef(null);
  const profileButtonRef = useRef(null);
  const tourButtonRef = useRef(null);
  const reducedMotion = useReducedMotion();
  // First visit opens the walkthrough automatically; closeTour records the visit
  // either way, so it never reopens on its own after that. Read once at mount
  // rather than in an effect, which would cost an extra render.
  const [tourOpen, setTourOpen] = useState(() => {
    try {
      return !window.localStorage.getItem('oracle_product_tour_v1');
    } catch {
      // Private browsing / storage disabled — treat as already seen rather than
      // reopening the tour on every mount.
      return false;
    }
  });
  const [tourStep, setTourStep] = useState(0);

  useEffect(() => {
    const viewport = window.visualViewport;
    if (!viewport) return undefined;

    const updateKeyboardState = () => {
      const obscured = Math.max(
        0,
        window.innerHeight - viewport.height - viewport.offsetTop
      );
      document.documentElement.dataset.keyboardOpen = obscured > 120 ? 'true' : 'false';
      document.documentElement.style.setProperty(
        '--visual-viewport-height',
        `${Math.round(viewport.height)}px`
      );
      document.documentElement.style.setProperty(
        '--visual-viewport-offset-top',
        `${Math.round(viewport.offsetTop)}px`
      );
    };

    updateKeyboardState();
    viewport.addEventListener('resize', updateKeyboardState);
    viewport.addEventListener('scroll', updateKeyboardState);
    return () => {
      viewport.removeEventListener('resize', updateKeyboardState);
      viewport.removeEventListener('scroll', updateKeyboardState);
      delete document.documentElement.dataset.keyboardOpen;
      document.documentElement.style.removeProperty('--visual-viewport-height');
      document.documentElement.style.removeProperty('--visual-viewport-offset-top');
    };
  }, []);

  useEffect(() => {
    if (!profileOpen) return undefined;
    const profileButton = profileButtonRef.current;
    const onKey = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        setProfileOpen(false);
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = [...(profileSheetRef.current?.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ) ?? [])];
      if (focusable.length === 0) {
        event.preventDefault();
        profileSheetRef.current?.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    window.requestAnimationFrame(() => profileSheetRef.current?.focus());
    return () => {
      window.removeEventListener('keydown', onKey);
      profileButton?.focus({ preventScroll: true });
    };
  }, [profileOpen]);

  // LoginVault stamps oracle_role before this mounts (App's auth gate). Ops is
  // intentionally a profile-level destination, never a sixth CRM workspace.
  const isPlatformAdmin = useMemo(
    () => sessionStorage.getItem('oracle_role') === 'platform_admin',
    [],
  );
  const tabs = TABS;

  // `route.view`/`route.params` are ALWAYS the view beneath — when an entity
  // sheet is open they describe what it is open over, not the sheet. That is
  // what lets /p/:id opened from Work leave Work mounted beneath, and lets
  // Back close the sheet without a remount. Kept in state via functional
  // updates rather than a ref, so nothing is written during render.
  const [route, setRoute] = useState(initialRoute);

  const go = useCallback((next, replace = false) => {
    const address = next.entity
      ? window.location.pathname + window.location.search
      : href(next.view, next.params);
    startTransition(() => {
      setRoute((prev) => (next.entity
        ? { view: prev.view, params: prev.params, entity: next.entity }
        : next));
      if (!next.entity) sessionStorage.setItem(TAB_KEY, next.view);
      const current = window.location.pathname + window.location.search;
      if (current !== address) {
        window.history[replace ? 'replaceState' : 'pushState']({}, '', address);
      }
    });
  }, []);

  // Accepts a view id OR any of the old tab ids: every existing
  // onNavigate('deals') in the tab components keeps working through the alias
  // table, and the guided walkthrough's per-step navigation still uses
  // `replace` so it does not bury the entry page under sixteen history rows.
  const select = useCallback((id, replaceOrExtra = false, extra = {}) => {
    // Tab components call select(id) and the tour calls select(id, true);
    // the Work chips call select(kind, { q }) to switch kind and keep the
    // query. One signature serves all three.
    const replace = replaceOrExtra === true;
    const opts = replaceOrExtra && typeof replaceOrExtra === 'object' ? replaceOrExtra : extra;
    const resolved = resolveLegacyId(id);
    if (resolved.view === VIEWS.work) {
      go({ view: VIEWS.work, params: workParams(resolved.type, null, opts.q), entity: null }, replace);
    } else {
      go({ view: resolved.view, params: {}, entity: null }, replace);
    }
  }, [go]);

  // Typing replaces the current entry rather than pushing one per keystroke.
  const setQuery = useCallback((q) => {
    go({ view: VIEWS.work, params: workParams(route.params?.type, route.params?.sales, q), entity: null }, true);
  }, [go, route.params]);

  // An entity address over whatever is beneath. The sheet that renders it
  // arrives in the next commit; the route plumbing is already here.
  const openEntity = useCallback((address) => {
    const next = parse(address, '', { fallbackView: route.view });
    go({ view: route.view, params: route.params, entity: next.entity }, false);
    if (next.entity) window.history.pushState({}, '', address);
  }, [go, route.view, route.params]);

  const navigateSales = useCallback((path, replace = false) => {
    if (path === '/our-ai') {
      go({ view: VIEWS.work, params: workParams('ai'), entity: null }, replace);
      return;
    }
    if (!SALES_ROUTES.has(path)) return;
    go({ view: VIEWS.work, params: workParams('sales', path), entity: null }, replace);
  }, [go]);

  useEffect(() => {
    const onPopState = () => {
      const next = parse(window.location.pathname, window.location.search, {
        fallbackView: VIEWS.home,
      });
      startTransition(() => {
        // An entity address on Back/Forward keeps whatever view was beneath.
        setRoute((prev) => (next.entity
          ? { view: prev.view, params: prev.params, entity: next.entity }
          : next));
        if (!next.entity) sessionStorage.setItem(TAB_KEY, next.view);
      });
    };
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, []);

  const openProfile = useCallback(() => {
    startTransition(() => setProfileOpen(true));
  }, []);

  const closeProfile = useCallback(() => {
    startTransition(() => setProfileOpen(false));
  }, []);

  const closeTour = useCallback((completed = false) => {
    // Dismissing counts too — the tour now opens itself on a first visit, so a
    // close that recorded nothing would reopen it on the next mount.
    try {
      window.localStorage.setItem(
        'oracle_product_tour_v1',
        completed ? 'complete' : 'dismissed',
      );
    } catch {
      // Storage unavailable; the tour stays available from the header button.
    }
    setTourOpen(false);
    window.requestAnimationFrame(() => tourButtonRef.current?.focus({ preventScroll: true }));
  }, []);

  const tab = tabs.find((t) => t.id === route.view) ?? tabs[0];
  const viewKey = `${route.view}:${route.params?.type || ''}`;
  const profileViews = isPlatformAdmin
    ? [
        { id: 'settings', label: 'Settings', Icon: UserRound, Component: MyProfileTab, preload: loadMyProfileTab },
        { id: 'ai', label: 'AI controls', Icon: Bot, Component: PersonalAITab, preload: loadPersonalAITab },
        { id: 'ops', label: 'Admin', Icon: ShieldAlert, Component: AdminOpsTab, preload: loadAdminOpsTab },
      ]
    : [
        { id: 'settings', label: 'Settings', Icon: UserRound, Component: MyProfileTab, preload: loadMyProfileTab },
        { id: 'ai', label: 'AI controls', Icon: Bot, Component: PersonalAITab, preload: loadPersonalAITab },
      ];
  const activeProfileView = profileViews.find((view) => view.id === profileView) ?? profileViews[0];
  const ProfileComponent = activeProfileView.Component;

  return (
    <StateProvider>
    <AssistantProvider>
    <div className={styles.shellContainer}>
      <header
        className={`${styles.header} hud-glass-panel`}
        style={{ viewTransitionName: 'crm-header' }}
      >
        <span className={styles.headerReticles} aria-hidden="true" />
        <NeohBrandMark />
        <div className={styles.headerTools}>
          <StateSelector />
          <button
            ref={tourButtonRef}
            type="button"
            className={styles.profileButton}
            onClick={() => { setTourStep(0); setTourOpen(true); }}
            aria-label="Start CRM guided walkthrough"
            aria-expanded={tourOpen}
            aria-controls="crm-product-tour"
          >
            <CircleHelp aria-hidden="true" />
          </button>
          <button
            ref={profileButtonRef}
            type="button"
            className={styles.profileButton}
            onClick={openProfile}
            aria-label="Open agent profile and settings"
            aria-expanded={profileOpen}
            aria-controls="agent-profile-sheet"
          >
            <UserRound aria-hidden="true" />
          </button>
        </div>
      </header>

      <AdaptiveViewTransition
        key={viewKey}
        enter="fade-in"
        exit="fade-out"
        default="none"
      >
        <main
          className={styles.scrollableContent}
        >
          <div
            id={`view-${tab.id}`}
            role="tabpanel"
            aria-labelledby={`tab-${tab.id}`}
            className={styles.tabPanel}
          >
            <div className={styles.tabContent}>
              <ErrorBoundary label={`${tab.label} tab`}>
                <Suspense
                  fallback={(
                    <AdaptiveViewTransition exit="slide-down" default="none">
                      <ViewFallback />
                    </AdaptiveViewTransition>
                  )}
                >
                  <AdaptiveViewTransition enter="slide-up" default="none">
                    {route.view === VIEWS.work ? (
                      <UniversalWorkspace
                        type={route.params.type}
                        query={route.params.q || ''}
                        salesRoute={route.params.sales}
                        onNavigate={select}
                        onSalesNavigate={navigateSales}
                        onQueryChange={setQuery}
                        onOpenEntity={openEntity}
                      />
                    ) : route.view === VIEWS.neoh ? (
                      // The full-screen conversation lands in U7. Until then
                      // Neoh is the AI workspace it has always been.
                      <OurAITab
                        onNavigate={select}
                        salesRoute={null}
                        onSalesNavigate={navigateSales}
                        initialWorkspace="cowork"
                      />
                    ) : (
                      <NeohHome onNavigate={select} />
                    )}
                  </AdaptiveViewTransition>
                </Suspense>
              </ErrorBoundary>
            </div>
            <NeohFooter />
          </div>
        </main>
      </AdaptiveViewTransition>

      <TabBar tabs={tabs} active={tab.id} onSelect={select} />
      <AnimatePresence>
        {profileOpen && (
          <motion.div
            className={styles.profileLayer}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: reducedMotion ? 0 : 0.18 }}
          >
            <button
              type="button"
              className={styles.profileScrim}
              onClick={closeProfile}
              aria-label="Close profile"
              tabIndex={-1}
            />
            <motion.aside
              ref={profileSheetRef}
              id="agent-profile-sheet"
              className={`${styles.profileSheet} hud-glass-panel hud-reticle`}
              role="dialog"
              aria-modal="true"
              aria-labelledby="agent-profile-title"
              tabIndex={-1}
              initial={{ opacity: 0, y: reducedMotion ? 0 : 28 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: reducedMotion ? 0 : 20 }}
              transition={{ duration: reducedMotion ? 0 : 0.32, ease: [0.16, 1, 0.3, 1] }}
            >
              <BorderBeam duration={4} size={250} />
              <div className={styles.profileSheetHead}>
                <div>
                  <span>Agent settings</span>
                  <h2 id="agent-profile-title">{activeProfileView.label}</h2>
                </div>
                <button
                  type="button"
                  onClick={closeProfile}
                  aria-label="Close profile"
                >
                  <X aria-hidden="true" />
                </button>
              </div>
              <nav className={styles.profileNav} aria-label="Profile and administration">
                {profileViews.map((view) => {
                  const Icon = view.Icon;
                  return (
                    <button
                      key={view.id}
                      type="button"
                      aria-pressed={activeProfileView.id === view.id}
                      onClick={() => setProfileView(view.id)}
                      onPointerEnter={() => { void view.preload?.(); }}
                      onFocus={() => { void view.preload?.(); }}
                    >
                      <Icon aria-hidden="true" />
                      <span>{view.label}</span>
                    </button>
                  );
                })}
              </nav>
              <div className={styles.profileSheetBody}>
                <ErrorBoundary label={activeProfileView.label}>
                  <Suspense key={activeProfileView.id} fallback={<ViewFallback />}>
                    <ProfileComponent />
                  </Suspense>
                </ErrorBoundary>
              </div>
            </motion.aside>
          </motion.div>
        )}
      </AnimatePresence>
      <ErrorBoundary label="Personal AI">
        <AssistantShell />
      </ErrorBoundary>
      <ProductTour
        open={tourOpen}
        stepIndex={tourStep}
        onStepChange={setTourStep}
        onClose={closeTour}
        onNavigateTab={select}
        onNavigateSales={navigateSales}
      />

      <BillingOverlay />
      <OnboardingGate />
    </div>
    </AssistantProvider>
    </StateProvider>
  );
}
