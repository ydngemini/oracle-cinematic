import { Suspense, lazy, useCallback, useMemo, useState } from 'react';
import { BillingOverlay } from './BillingOverlay';
import { OnboardingGate } from './OnboardingGate';
import { SubscriptionBadge } from './SubscriptionBadge';
import { ErrorBoundary } from './ErrorBoundary';
import { TabBar } from './TabBar';
import { StateProvider } from '../state/StateContext';
import { StateSelector } from './StateSelector';
import styles from './CrmShell.module.css';

// Each tab is its own chunk — a field agent on LTE only pays for the tab
// they open. (Same code-split rationale as PropertyCanvas in the HUD era.)
const MarketplaceTab = lazy(() => import('./MarketplaceTab'));
const HouseProfileTab = lazy(() => import('./HouseProfileTab'));
const ClientCrmTab = lazy(() => import('./ClientCrmTab'));
const CommsTab = lazy(() => import('./CommsTab'));
const MyProfileTab = lazy(() => import('./MyProfileTab'));
const AdminOpsTab = lazy(() => import('./AdminOpsTab'));

const TABS = [
  { id: 'marketplace', label: 'Listings', glyph: 'marketplace', Component: MarketplaceTab },
  { id: 'house', label: 'House', glyph: 'house', Component: HouseProfileTab },
  { id: 'clients', label: 'Clients', glyph: 'clients', Component: ClientCrmTab },
  { id: 'comms', label: 'Comms', glyph: 'comms', Component: CommsTab },
  { id: 'profile', label: 'Me', glyph: 'profile', Component: MyProfileTab },
];

// The sixth key only exists for the platform admin — everyone else never
// downloads the OPS chunk (lazy) or sees the tab. The backend enforces the
// real gate (403 on /api/admin/*); this is purely presentational.
const OPS_TAB = { id: 'ops', label: 'Ops', glyph: 'ops', Component: AdminOpsTab };

const TAB_KEY = 'oracle_crm_tab';

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
 * CrmShell — the 5-tab agent CRM frame ("as soon as they open the app,
 * they should be here": Listings is the landing tab). Replaces the desktop
 * DashboardLayout; BillingOverlay and OnboardingGate stay as self-gating
 * overlays, exactly as before.
 */
export function CrmShell() {
  // LoginVault stamps oracle_role before this mounts (App's auth gate).
  const tabs = useMemo(
    () =>
      sessionStorage.getItem('oracle_role') === 'platform_admin'
        ? [...TABS, OPS_TAB]
        : TABS,
    []
  );

  const [active, setActive] = useState(
    () => sessionStorage.getItem(TAB_KEY) || 'marketplace'
  );

  const select = useCallback((id) => {
    setActive(id);
    sessionStorage.setItem(TAB_KEY, id);
  }, []);

  const tab = tabs.find((t) => t.id === active) ?? tabs[0];
  const { Component } = tab;

  return (
    <StateProvider>
    <div className={styles.shell}>
      <header className={styles.header}>
        <span className={styles.wordmark}>
          <span className={styles.tick} aria-hidden="true" />
          NEOH
        </span>
        <StateSelector />
        <SubscriptionBadge />
      </header>

      <main
        key={tab.id}
        id={`view-${tab.id}`}
        role="tabpanel"
        className={styles.view}
      >
        <ErrorBoundary label={`${tab.label} tab`}>
          <Suspense fallback={<ViewFallback />}>
            <Component />
          </Suspense>
        </ErrorBoundary>
      </main>

      <TabBar tabs={tabs} active={active} onSelect={select} />

      <BillingOverlay />
      <OnboardingGate />
    </div>
    </StateProvider>
  );
}
