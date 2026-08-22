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
  BriefcaseBusiness,
  Building2,
  CalendarCheck2,
  CircleHelp,
  MessageSquare,
  ShieldAlert,
  UserRound,
  Users,
  X,
} from 'lucide-react';
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
const loadTodayTab = () => import('./TodayTab');
const loadPeopleTab = () => import('./PeopleTab');
const loadCommsTab = () => import('./CommsTab');
const loadDealsTab = () => import('./DealsTab');
const loadOurAITab = () => import('./OurAITab');
const loadPropertiesTab = () => import('./PropertiesTab');
const loadPersonalAITab = () => import('./PersonalAITab');
const loadMyProfileTab = () => import('./MyProfileTab');
const loadAdminOpsTab = () => import('./AdminOpsTab');

const TodayTab = lazy(loadTodayTab);
const PeopleTab = lazy(loadPeopleTab);
const CommsTab = lazy(loadCommsTab);
const DealsTab = lazy(loadDealsTab);
const OurAITab = lazy(loadOurAITab);
const PropertiesTab = lazy(loadPropertiesTab);
const PersonalAITab = lazy(loadPersonalAITab);
const MyProfileTab = lazy(loadMyProfileTab);
const AdminOpsTab = lazy(loadAdminOpsTab);

const TABS = [
  { id: 'today', label: 'Today', Icon: CalendarCheck2, Component: TodayTab, preload: loadTodayTab },
  { id: 'people', label: 'People', Icon: Users, Component: PeopleTab, preload: loadPeopleTab },
  { id: 'inbox', label: 'Inbox', Icon: MessageSquare, Component: CommsTab, preload: loadCommsTab },
  { id: 'deals', label: 'Deals', Icon: BriefcaseBusiness, Component: DealsTab, preload: loadDealsTab },
  { id: 'property-view', label: 'Property View', Icon: Building2, Component: PropertiesTab, preload: loadPropertiesTab },
  { id: 'studio', label: 'Our AI', Icon: Bot, Component: OurAITab, preload: loadOurAITab },
];

const TAB_KEY = 'oracle_crm_tab';
const TAB_PATHS = {
  today: '/today',
  people: '/people',
  inbox: '/inbox',
  deals: '/deals',
  // Distinct from the unauthenticated /property-upload/:token client page
  // routed in App.jsx — different prefix, no collision.
  'property-view': '/property-view',
  studio: '/our-ai',
};
const SALES_PATHS = new Set([
  '/our-ai/sales',
  '/our-ai/sales/agent',
  '/our-ai/sales/dialer',
  '/our-ai/sales/plans',
  '/our-ai/sales/providers',
  '/our-ai/sales/routing',
]);
const LEGACY_TAB_IDS = {
  portfolio: 'today',
  // The houses/marketplace surface lives under Property View, not People —
  // People is the contact book and has no lead browser or property workspace.
  houses: 'property-view',
  clients: 'people',
  pipeline: 'deals',
  marketplace: 'property-view',
  house: 'property-view',
  'house-profile': 'property-view',
  comms: 'inbox',
  ai: 'studio',
  'personal-ai': 'studio',
  docs: 'deals',
  contracts: 'deals',
  ops: 'today',
  profile: 'today',
};

function savedTabId() {
  const stored = sessionStorage.getItem(TAB_KEY) || 'today';
  const resolved = LEGACY_TAB_IDS[stored] || stored;
  if (resolved !== stored) sessionStorage.setItem(TAB_KEY, resolved);
  return resolved;
}

function routeForPath(pathname = window.location.pathname, { restoreSaved = false } = {}) {
  const path = pathname.length > 1 ? pathname.replace(/\/+$/, '') : pathname;
  if (SALES_PATHS.has(path)) return { tab: 'studio', salesRoute: path };
  const entry = Object.entries(TAB_PATHS).find(([, value]) => value === path);
  if (entry) return { tab: entry[0], salesRoute: null };
  // On first mount an unrecognised path — including the bare '/' the app is
  // usually entered at — resumes the tab the session left off on. On popstate
  // it must not: `select` has already overwritten sessionStorage with the tab
  // being navigated away from, so resuming it would leave the view unchanged
  // while the address bar went back, and Back would look broken.
  return { tab: restoreSaved ? savedTabId() : 'today', salesRoute: null };
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

  const [active, setActive] = useState(() => routeForPath(undefined, { restoreSaved: true }).tab);
  const [salesRoute, setSalesRoute] = useState(
    () => routeForPath(undefined, { restoreSaved: true }).salesRoute,
  );

  // `replace` is used by the guided walkthrough: it drives navigation on every
  // step, and pushing an entry per step would bury the page the user entered
  // from under ~16 history entries, so Back stops being an escape hatch.
  const select = useCallback((id, replace = false) => {
    const path = TAB_PATHS[id] || TAB_PATHS.today;
    startTransition(() => {
      setActive(id);
      setSalesRoute(null);
      sessionStorage.setItem(TAB_KEY, id);
      if (window.location.pathname !== path) {
        window.history[replace ? 'replaceState' : 'pushState']({}, '', path);
      }
    });
  }, []);

  const navigateSales = useCallback((path, replace = false) => {
    if (path !== '/our-ai' && !SALES_PATHS.has(path)) return;
    startTransition(() => {
      setActive('studio');
      setSalesRoute(path === '/our-ai' ? null : path);
      sessionStorage.setItem(TAB_KEY, 'studio');
      if (window.location.pathname !== path) {
        window.history[replace ? 'replaceState' : 'pushState']({}, '', path);
      }
    });
  }, []);

  useEffect(() => {
    const onPopState = () => {
      const next = routeForPath();
      startTransition(() => {
        setActive(next.tab);
        setSalesRoute(next.salesRoute);
        sessionStorage.setItem(TAB_KEY, next.tab);
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

  // Older sessions may still point to a tab removed from the deck.
  // Fall back cleanly to Today until the user selects their next destination.
  const tab = tabs.find((t) => t.id === active) ?? tabs[0];
  const { Component } = tab;
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
        key={tab.id}
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
                    <Component
                      onNavigate={select}
                      salesRoute={salesRoute}
                      onSalesNavigate={navigateSales}
                    />
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
