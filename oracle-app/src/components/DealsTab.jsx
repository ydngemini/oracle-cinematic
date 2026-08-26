import { lazy, Suspense, useState } from 'react';
import { BriefcaseBusiness, FileCheck2, Store, Workflow, PieChart } from 'lucide-react';
import { ErrorBoundary } from './ErrorBoundary';
import styles from './DealsTab.module.css';

const DealBook = lazy(() => import('./DealBook'));
const ContractVaultTab = lazy(() => import('./ContractVaultTab'));
// Disposition lives here rather than in its own top-level tab: a publication
// only exists once a contract is signed, so it is the tail of this workflow.
const MarketplaceBrowse = lazy(() => import('./MarketplaceBrowse'));
// Pipeline and Portfolio were both written, styled and tested, and then never
// imported by anything — so no user could reach either. Pipeline is where a
// deal STARTS (it holds the leads a transaction must be anchored to) and
// Portfolio is where the book is read back, so both belong beside Transactions
// rather than in tabs of their own.
const DealPipeline = lazy(() =>
  import('./DealPipeline').then((module) => ({ default: module.DealPipeline })),
);
const PortfolioTab = lazy(() => import('./PortfolioTab'));

const VIEWS = [
  { id: 'pipeline', label: 'Pipeline', Icon: Workflow },
  { id: 'transactions', label: 'Transactions', Icon: BriefcaseBusiness },
  { id: 'contracts', label: 'Contracts', Icon: FileCheck2 },
  { id: 'portfolio', label: 'Portfolio', Icon: PieChart },
  { id: 'marketplace', label: 'Marketplace', Icon: Store },
];

function ViewFallback() {
  return <div className={styles.fallback} aria-hidden="true"><span /><span /><span /></div>;
}

export default function DealsTab() {
  const [view, setView] = useState('transactions');

  return (
    <section className={styles.wrap} aria-labelledby="deals-title">
      <header className={styles.hero}>
        <div>
          <span className={styles.kicker}>Transaction workspace</span>
          <h1 id="deals-title">Deals</h1>
          <p>Work a property from pipeline to signed contract, and read the book back.</p>
        </div>
        <nav className={styles.switcher} aria-label="Deal workspace">
          {VIEWS.map(({ id, label, Icon }) => (
            <button
              key={id}
              type="button"
              aria-pressed={view === id}
              onClick={() => setView(id)}
            >
              <Icon aria-hidden="true" />
              <span>{label}</span>
            </button>
          ))}
        </nav>
      </header>

      <div className={styles.workspace}>
        <ErrorBoundary label={VIEWS.find((v) => v.id === view)?.label || 'Deals'}>
          <Suspense fallback={<ViewFallback />}>
            {view === 'pipeline' ? <DealPipeline /> : null}
            {view === 'transactions' ? <DealBook /> : null}
            {view === 'contracts' ? <ContractVaultTab embedded /> : null}
            {view === 'portfolio' ? <PortfolioTab /> : null}
            {view === 'marketplace' ? <MarketplaceBrowse /> : null}
          </Suspense>
        </ErrorBoundary>
      </div>
    </section>
  );
}

