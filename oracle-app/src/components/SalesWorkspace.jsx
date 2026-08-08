import { lazy, Suspense } from 'react';
import {
  ArrowLeft,
  Bot,
  PhoneCall,
  PlugZap,
  Route,
  Workflow,
} from 'lucide-react';
import styles from './SalesWorkspace.module.css';

const SalesAgentPage = lazy(() => import('./SalesAgentPage'));
const PowerDialerPage = lazy(() => import('./PowerDialerPage'));
const SmartPlansPage = lazy(() => import('./SmartPlansPage'));
const ProviderDeliveryPage = lazy(() => import('./ProviderDeliveryPage'));
const LeadRoutingPage = lazy(() => import('./LeadRoutingPage'));

const DESTINATIONS = [
  { path: '/our-ai/sales/agent', label: 'Sales Agent', Icon: Bot, Component: SalesAgentPage },
  { path: '/our-ai/sales/dialer', label: 'Power Dialer', Icon: PhoneCall, Component: PowerDialerPage },
  { path: '/our-ai/sales/plans', label: 'Smart Plans', Icon: Workflow, Component: SmartPlansPage },
  { path: '/our-ai/sales/providers', label: 'Providers', Icon: PlugZap, Component: ProviderDeliveryPage },
  { path: '/our-ai/sales/routing', label: 'Lead Routing', Icon: Route, Component: LeadRoutingPage },
];

function PageFallback() {
  return (
    <div className={styles.pageFallback} aria-label="Loading sales workspace">
      <span /><span /><span />
    </div>
  );
}

export default function SalesWorkspace({ route, onNavigate }) {
  const destination = DESTINATIONS.find((item) => item.path === route) || DESTINATIONS[0];
  const Page = destination.Component;
  return (
    <section className={styles.salesShell} aria-labelledby="sales-workspace-title">
      <header className={styles.salesHero}>
        <button type="button" className={styles.backButton} onClick={() => onNavigate('/our-ai/sales')}>
          <ArrowLeft aria-hidden="true" /> Sales overview
        </button>
        <div>
          <span>Our AI / Sales</span>
          <h2 id="sales-workspace-title">{destination.label}</h2>
          <p>CRM-grounded sales work with explicit provider readiness, compliance checks, and human approval before outbound delivery.</p>
        </div>
      </header>

      <nav className={styles.salesNav} aria-label="Sales AI destinations">
        {DESTINATIONS.map((item) => {
          const Icon = item.Icon;
          const current = item.path === destination.path;
          return (
            <button
              key={item.path}
              type="button"
              // Stable hook for the guided walkthrough spotlight (ProductTour).
              data-tour-anchor={item.path}
              aria-current={current ? 'page' : undefined}
              onClick={() => onNavigate(item.path)}
            >
              <Icon aria-hidden="true" />
              <span>{item.label}</span>
            </button>
          );
        })}
      </nav>

      <Suspense fallback={<PageFallback />}>
        <Page onNavigate={onNavigate} />
      </Suspense>
    </section>
  );
}
