import { lazy, Suspense, useState } from 'react';
import { House, MapPin, Store, LineChart, FileText, Building2 } from 'lucide-react';
import { ErrorBoundary } from './ErrorBoundary';
import styles from './PropertiesTab.module.css';

// The tab restructure replaced the old Houses tab with Property View, but
// Property View is a single-address page — it has no lead browser, no map, and
// no route into HouseWorkspace's walkable tour. Both surfaces live here, in the
// same switcher pattern DealsTab uses for Transactions/Contracts.
const PropertyViewTab = lazy(() => import('./PropertyViewTab'));
const HouseSelection = lazy(() => import('./HouseSelection'));
// GET/POST /api/crm/listings had no caller anywhere, so a listing could not be
// created through the product — while the assistant could already anchor to one
// and update_listing is an allowlisted tool. Distinct from Houses, which
// browses public parcel records the workspace does not own.
const ListingsInventory = lazy(() => import('./ListingsInventory'));
// Both written, styled, and imported by nothing. Market covers
// /api/market/{state}/overview; Forms covers /api/states and its per-state
// document library. Neither had any route into the product.
const MarketDataPanels = lazy(() =>
  import('./MarketDataPanels').then((module) => ({ default: module.MarketDataPanels })),
);
const StateDocumentLibrary = lazy(() =>
  import('./StateDocumentLibrary').then((module) => ({ default: module.StateDocumentLibrary })),
);
// Retail MLS browse. /api/mls/search and both detail endpoints had no caller,
// so an authorized listing could never be searched or opened here.
const MlsSearch = lazy(() => import('./MlsSearch'));

const VIEWS = [
  { id: 'address', label: 'Address', Icon: MapPin },
  { id: 'houses', label: 'Houses', Icon: House },
  { id: 'listings', label: 'Listings', Icon: Store },
  { id: 'mls', label: 'MLS', Icon: Building2 },
  { id: 'market', label: 'Market', Icon: LineChart },
  { id: 'forms', label: 'Forms', Icon: FileText },
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
          <p>Look up one address and its media, browse the houses in your pipeline, or read the market and state forms behind them.</p>
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
        <ErrorBoundary label={VIEWS.find((entry) => entry.id === view)?.label ?? 'Property View'}>
          <Suspense fallback={<ViewFallback />}>
            {view === 'address' && <PropertyViewTab />}
            {view === 'houses' && <HouseSelection />}
            {view === 'listings' && <ListingsInventory />}
            {view === 'mls' && <MlsSearch />}
            {view === 'market' && <MarketDataPanels />}
            {view === 'forms' && <StateDocumentLibrary />}
          </Suspense>
        </ErrorBoundary>
      </div>
    </section>
  );
}
