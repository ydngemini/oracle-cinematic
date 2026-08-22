import { lazy, Suspense, useState } from 'react';
import { BriefcaseBusiness, FileCheck2, Store } from 'lucide-react';
import { ErrorBoundary } from './ErrorBoundary';
import styles from './DealsTab.module.css';

const DealBook = lazy(() => import('./DealBook'));
const ContractVaultTab = lazy(() => import('./ContractVaultTab'));
// Disposition lives here rather than in its own top-level tab: a publication
// only exists once a contract is signed, so it is the tail of this workflow.
const MarketplaceBrowse = lazy(() => import('./MarketplaceBrowse'));

const VIEWS = [
  { id: 'transactions', label: 'Transactions', Icon: BriefcaseBusiness },
  { id: 'contracts', label: 'Contracts', Icon: FileCheck2 },
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
          <p>Move active transactions forward and keep every approved document with the deal.</p>
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
            {view === 'transactions' ? <DealBook /> : null}
            {view === 'contracts' ? <ContractVaultTab embedded /> : null}
            {view === 'marketplace' ? <MarketplaceBrowse /> : null}
          </Suspense>
        </ErrorBoundary>
      </div>
    </section>
  );
}

