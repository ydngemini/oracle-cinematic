import { Suspense, lazy } from 'react';

import styles from './UniversalWorkspace.module.css';

/**
 * Work — everything in the business, one place, chosen by ?type.
 *
 * This first cut is deliberately only the switcher. The four old tabs and the
 * old AI workspaces become views here, selected by the `type` query param, so
 * nothing the six-tab shell could reach becomes unreachable the day the tabs
 * go. The search box and the cross-kind results arrive with the search API
 * (U4/U5); shipping the switcher first means the navigation change and the
 * search change can be judged separately.
 */

const PeopleTab = lazy(() => import('../components/PeopleTab'));
const CommsTab = lazy(() => import('../components/CommsTab'));
const DealsTab = lazy(() => import('../components/DealsTab'));
const PropertiesTab = lazy(() => import('../components/PropertiesTab'));
const OurAITab = lazy(() => import('../components/OurAITab'));
const IntelligenceFeed = lazy(() =>
  import('../components/IntelligenceFeed').then((m) => ({ default: m.IntelligenceFeed })));

/** Which component a Work type renders. Types the old Our-AI tab owned all
 *  render it with the matching workspace preselected, until U9 re-homes them. */
const AI_WORKSPACES = new Set(['ai', 'sales', 'social', 'homeowners', 'automations', 'sites', 'missions']);

function Fallback() {
  return (
    <div className={styles.fallback} aria-hidden="true">
      <div className={styles.fallbackCard} />
      <div className={styles.fallbackCard} />
    </div>
  );
}

export function UniversalWorkspace({ type, salesRoute, onNavigate, onSalesNavigate }) {
  let content;
  if (type === 'conversations') {
    content = <CommsTab onNavigate={onNavigate} />;
  } else if (type === 'deals') {
    content = <DealsTab onNavigate={onNavigate} />;
  } else if (type === 'properties') {
    content = <PropertiesTab onNavigate={onNavigate} />;
  } else if (type === 'opportunities') {
    content = <IntelligenceFeed />;
  } else if (AI_WORKSPACES.has(type)) {
    content = (
      <OurAITab
        onNavigate={onNavigate}
        salesRoute={salesRoute}
        onSalesNavigate={onSalesNavigate}
        initialWorkspace={type === 'ai' ? undefined : type}
      />
    );
  } else {
    content = <PeopleTab onNavigate={onNavigate} />;
  }

  return (
    <div className={styles.work} data-work-type={type}>
      <Suspense fallback={<Fallback />}>{content}</Suspense>
    </div>
  );
}

export default UniversalWorkspace;
