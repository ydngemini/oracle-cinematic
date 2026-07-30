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
  FileText,
  House,
  MessageSquare,
  PieChart,
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
import styles from './CrmShell.module.css';

// Each tab is its own chunk — a field agent on LTE only pays for the tab
// they open. (Same code-split rationale as PropertyCanvas in the HUD era.)
const loadHouseSelection = () => import('./HouseSelection');
const loadPortfolioTab = () => import('./PortfolioTab');
const loadClientCrmTab = () => import('./ClientCrmTab');
const loadCommsTab = () => import('./CommsTab');
const loadPersonalAITab = () => import('./PersonalAITab');
const loadContractVaultTab = () => import('./ContractVaultTab');
const loadMyProfileTab = () => import('./MyProfileTab');
const loadAdminOpsTab = () => import('./AdminOpsTab');

const HouseSelection = lazy(loadHouseSelection);
const PortfolioTab = lazy(loadPortfolioTab);
const ClientCrmTab = lazy(loadClientCrmTab);
const CommsTab = lazy(loadCommsTab);
const PersonalAITab = lazy(loadPersonalAITab);
const ContractVaultTab = lazy(loadContractVaultTab);
const MyProfileTab = lazy(loadMyProfileTab);
const AdminOpsTab = lazy(loadAdminOpsTab);

const TABS = [
  { id: 'houses', label: 'Houses', Icon: House, Component: HouseSelection, preload: loadHouseSelection },
  { id: 'portfolio', label: 'Portfolio', Icon: PieChart, Component: PortfolioTab, preload: loadPortfolioTab },
  { id: 'clients', label: 'Clients', Icon: Users, Component: ClientCrmTab, preload: loadClientCrmTab },
  { id: 'comms', label: 'Comms', Icon: MessageSquare, Component: CommsTab, preload: loadCommsTab },
  { id: 'ai', label: 'AI Agent', shortLabel: 'AI', Icon: Bot, Component: PersonalAITab, preload: loadPersonalAITab },
  { id: 'docs', label: 'Docs', Icon: FileText, Component: ContractVaultTab, preload: loadContractVaultTab },
];

// The sixth key only exists for the platform admin — everyone else never
// downloads the OPS chunk (lazy) or sees the tab. The backend enforces the
// real gate (403 on /api/admin/*); this is purely presentational.
const OPS_TAB = {
  id: 'ops',
  label: 'Ops',
  Icon: ShieldAlert,
  Component: AdminOpsTab,
  preload: loadAdminOpsTab,
};

const TAB_KEY = 'oracle_crm_tab';
const LEGACY_TAB_IDS = {
  pipeline: 'houses',
  marketplace: 'houses',
  house: 'houses',
  'house-profile': 'houses',
  'personal-ai': 'ai',
  contracts: 'docs',
  profile: 'houses',
};

function savedTabId() {
  const stored = sessionStorage.getItem(TAB_KEY) || 'houses';
  const resolved = LEGACY_TAB_IDS[stored] || stored;
  if (resolved !== stored) sessionStorage.setItem(TAB_KEY, resolved);
  return resolved;
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
 * they should be here": Houses is the landing tab). Replaces the desktop
 * DashboardLayout; BillingOverlay and OnboardingGate stay as self-gating
 * overlays, exactly as before.
 */
export function CrmShell() {
  const [profileOpen, setProfileOpen] = useState(false);
  const profileSheetRef = useRef(null);
  const profileButtonRef = useRef(null);
  const reducedMotion = useReducedMotion();

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

  // LoginVault stamps oracle_role before this mounts (App's auth gate).
  const tabs = useMemo(
    () =>
      sessionStorage.getItem('oracle_role') === 'platform_admin'
        ? [...TABS, OPS_TAB]
        : TABS,
    []
  );

  const [active, setActive] = useState(savedTabId);

  const select = useCallback((id) => {
    startTransition(() => {
      setActive(id);
      sessionStorage.setItem(TAB_KEY, id);
    });
  }, []);

  const openProfile = useCallback(() => {
    startTransition(() => setProfileOpen(true));
  }, []);

  const closeProfile = useCallback(() => {
    startTransition(() => setProfileOpen(false));
  }, []);

  // Older sessions may still point to a tab removed from the deck.
  // Fall back cleanly to Houses until the user selects their next tab.
  const tab = tabs.find((t) => t.id === active) ?? tabs[0];
  const { Component } = tab;

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
                    <Component />
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
                  <h1 id="agent-profile-title">Profile</h1>
                </div>
                <button
                  type="button"
                  onClick={closeProfile}
                  aria-label="Close profile"
                >
                  <X aria-hidden="true" />
                </button>
              </div>
              <div className={styles.profileSheetBody}>
                <ErrorBoundary label="Agent profile">
                  <Suspense fallback={<ViewFallback />}>
                    <MyProfileTab />
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

      <BillingOverlay />
      <OnboardingGate />
    </div>
    </AssistantProvider>
    </StateProvider>
  );
}
