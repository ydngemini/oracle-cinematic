import { lazy, Suspense } from 'react';
import { useOracleState } from '../state';
import { AgentStatusBar } from './AgentStatusBar';
import { PropertySpecs } from './PropertySpecs';
import { LiveTranscript } from './LiveTranscript';
import { WalkerBubble } from './WalkerBubble';
import { DealPipeline } from './DealPipeline';
import { LegalMatrix } from './LegalMatrix';
import { BillingOverlay } from './BillingOverlay';
import { SubscriptionBadge } from './SubscriptionBadge';
import { ErrorBoundary } from './ErrorBoundary';
import styles from './DashboardLayout.module.css';

// gsplat (the heavy 3D dep) lives entirely inside PropertyCanvas. Code-split it
// so the HUD/gate paint without waiting on the WebGL bundle to parse.
const PropertyCanvas = lazy(() =>
  import('./PropertyCanvas').then((m) => ({ default: m.PropertyCanvas }))
);

export function DashboardLayout() {
  const { legalPackage } = useOracleState();

  return (
    <div className={styles.viewport}>
      {/* Scoped boundary: a spatial-viewer crash must not take down the C2 HUD.
          Quiet fallback (null) keeps the rest of the dashboard fully live. */}
      <ErrorBoundary label="Spatial Viewer" fallback={() => null}>
        <Suspense fallback={null}>
          <PropertyCanvas />
        </Suspense>
      </ErrorBoundary>

      <div className={styles.hud}>
        <div className={styles.topBar}>
          <AgentStatusBar />
          <SubscriptionBadge />
        </div>

        <div className={styles.leftPanel}>
          <PropertySpecs />
        </div>

        <div className={styles.rightPanel}>
          <DealPipeline />
        </div>

        <div className={styles.bottomPanel}>
          <LiveTranscript />
        </div>
      </div>

      <WalkerBubble />
      {/* Slide-in panel; stays off-screen until a LEGAL_PACKAGE frame arrives. */}
      <LegalMatrix legalPackage={legalPackage} visible={!!legalPackage} />
      <BillingOverlay />
    </div>
  );
}
