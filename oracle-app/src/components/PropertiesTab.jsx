import { lazy, Suspense, useState } from 'react';
import { House, MapPin } from 'lucide-react';
import { ErrorBoundary } from './ErrorBoundary';
import styles from './PropertiesTab.module.css';

// The tab restructure replaced the old Houses tab with Property View, but
// Property View is a single-address page — it has no lead browser, no map, and
// no route into HouseWorkspace's walkable tour. Both surfaces live here, in the
// same switcher pattern DealsTab uses for Transactions/Contracts.
const PropertyViewTab = lazy(() => import('./PropertyViewTab'));
const HouseSelection = lazy(() => import('./HouseSelection'));

const VIEWS = [
  { id: 'address', label: 'Address', Icon: MapPin },
  { id: 'houses', label: 'Houses', Icon: House },
];

function ViewFallback() {
  return <div className={styles.fallback} aria-hidden="true"><span /><span /><span /></div>;
}

export default function PropertiesTab() {
  const [view, setView] = useState('address');

  return (
    <section className={styles.wrap} aria-labelledby="properties-title">
      <header className={styles.hero}>
        <div>
          <span className={styles.kicker}>Property workspace</span>
          <h1 id="properties-title">Property View</h1>
          <p>Look up one address and its media, or browse the houses already in your pipeline.</p>
        </div>
        <nav className={styles.switcher} aria-label="Property workspace">
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
        <ErrorBoundary label={view === 'address' ? 'Property View' : 'Houses'}>
          <Suspense fallback={<ViewFallback />}>
            {view === 'address' ? <PropertyViewTab /> : <HouseSelection />}
          </Suspense>
        </ErrorBoundary>
      </div>
    </section>
  );
}
