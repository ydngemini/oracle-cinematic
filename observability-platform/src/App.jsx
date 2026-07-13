import { useState, useEffect, useCallback, useMemo } from 'react';
import AWSWebGLCanvas from './components/AWSWebGLCanvas.jsx';
import ServicePanel from './components/ServicePanel.jsx';
import MetricsPanel from './components/MetricsPanel.jsx';
import BillingPanel from './components/BillingPanel.jsx';
import RetroDashboard from './components/RetroDashboard.jsx';
import AICopilot from './components/AICopilot.jsx';
import MobileNav, { SwipeContainer, PullToRefresh } from './components/MobileNav.jsx';
import Login from './components/Login.jsx';
import FeaturePanels from './components/panels/FeaturePanels.jsx';
import useAwsWebSocket from './hooks/useAwsWebSocket.jsx';
import { resolveToken, logout } from './lib/auth';
import './index.css';

// Auth gate: the dashboard's WebSocket is now server-side gated to
// platform_admin/broker_owner, so we must hold a JWT before mounting it. No
// token → show <Login>; a WS auth rejection drops us back here.
export default function App() {
  const [token, setToken] = useState(() => resolveToken());
  const handleLogout = useCallback(() => {
    logout();
    setToken('');
  }, []);
  if (!token) return <Login onAuth={setToken} />;
  return <Dashboard token={token} onLogout={handleLogout} />;
}

function Dashboard({ token, onLogout }) {
  const { connected, messages, acknowledgeMessages, requestSnapshot, authError } = useAwsWebSocket(token);
  const [infrastructure, setInfrastructure] = useState({
    ec2: { instances: [], count: 0 },
    rds: { instances: [], count: 0 },
    s3: { buckets: [], count: 0 },
    lambda: { functions: [], count: 0 },
    ebs: { volumes: [], count: 0 },
    elb: { load_balancers: [], count: 0 },
    cloudfront: { distributions: [], count: 0 },
    billing: { month_total: 0, by_service: {} },
    security: { findings: [], score: 100, findings_count: 0 },
    auto_scaling: { groups: [], count: 0 },
  });
  const [metrics, setMetrics] = useState({ ec2: {}, rds: {}, lambda: {}, elb: {} });
  const [selectedService, setSelectedService] = useState(null);
  const [viewMode, setViewMode] = useState('dashboard');
  const [alerts, setAlerts] = useState([]);
  const [isMobile, setIsMobile] = useState(false);

  useEffect(() => {
    const checkMobile = () => setIsMobile(window.innerWidth < 768);
    checkMobile();
    window.addEventListener('resize', checkMobile);
    return () => window.removeEventListener('resize', checkMobile);
  }, []);

  useEffect(() => {
    if (messages.length === 0) return;
    messages.forEach((message) => {
      if (message.type === 'AWS_INFRASTRUCTURE_SNAPSHOT') {
        const { type, ...rest } = message;
        setInfrastructure(prev => ({ ...prev, ...rest }));

        if (rest.security?.findings_count > 0) {
          setAlerts(prev => {
            const seenIds = new Set(prev.map(a => a.id));
            const newAlerts = rest.security.findings
              .filter(f => f.severity === 'CRITICAL' || f.severity === 'HIGH')
              .filter(f => !seenIds.has(f.id))
              .slice(0, 3)
              .map(f => ({
                id: f.id,
                severity: f.severity.toLowerCase(),
                message: f.title,
                resource: f.aws_account_id,
              }));
            return [...newAlerts, ...prev].slice(0, 10);
          });
        }
      } else if (message.type === 'AWS_METRICS_UPDATE') {
        setMetrics(prev => ({
          ...prev,
          ec2: { ...prev.ec2, ...(message.ec2 || {}) },
          rds: { ...prev.rds, ...(message.rds || {}) },
          lambda: { ...prev.lambda, ...(message.lambda || {}) },
          elb: { ...prev.elb, ...(message.elb || {}) },
        }));
      }
    });
    acknowledgeMessages(messages.length);
  }, [messages, acknowledgeMessages]);

  useEffect(() => {
    if (connected) {
      requestSnapshot();
    }
  }, [connected, requestSnapshot]);

  // Server rejected the token/role (4401/4403) — clear it and return to login.
  useEffect(() => {
    if (authError) onLogout();
  }, [authError, onLogout]);

  const handleServiceSelect = useCallback((type, id) => {
    setSelectedService({ type, id });
    if (isMobile) {
      setViewMode('metrics');
    }
  }, [isMobile]);

  const handleRefresh = useCallback(async () => {
    requestSnapshot();
    await new Promise(resolve => setTimeout(resolve, 500));
  }, [requestSnapshot]);

  const handleViewChange = useCallback((view) => {
    setViewMode(view);
  }, []);

  const selectedInstance = useMemo(() => {
    if (!selectedService) return null;
    const { type, id } = selectedService;
    if (type === 'ec2') return infrastructure.ec2.instances.find(i => i.id === id);
    if (type === 'rds') return infrastructure.rds.instances.find(i => i.id === id);
    if (type === 'lambda') return infrastructure.lambda.functions.find(f => f.name === id);
    return null;
  }, [selectedService, infrastructure]);

  const totalResources = 
    infrastructure.ec2.count + 
    infrastructure.rds.count + 
    infrastructure.s3.count + 
    infrastructure.lambda.count +
    infrastructure.ebs.count +
    infrastructure.elb.count;

  const runningCount = 
    infrastructure.ec2.instances.filter(i => i.state === 'running').length +
    infrastructure.rds.instances.filter(i => i.status === 'available').length;

  const mobileViews = [
    { id: 'dashboard', label: 'Dash', icon: '◉' },
    { id: 'metrics', label: 'Metrics', icon: '◐' },
    { id: 'topology', label: 'Map', icon: '◈' },
    { id: 'alerts', label: 'Alerts', icon: '⚡' },
    { id: 'more', label: 'More', icon: '☰' },
  ];

  const renderMobileContent = () => {
    switch (viewMode) {
      case 'dashboard':
        return (
          <RetroDashboard 
            ec2={infrastructure.ec2} 
            rds={infrastructure.rds} 
            lambda={infrastructure.lambda}
            metrics={metrics}
            billing={infrastructure.billing}
          />
        );
      case 'metrics':
        return (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            <ServicePanel
              ec2={infrastructure.ec2}
              rds={infrastructure.rds}
              lambda={infrastructure.lambda}
              onSelect={handleServiceSelect}
              selectedService={selectedService}
            />
            <MetricsPanel
              selectedService={selectedService}
              instance={selectedInstance}
              metrics={selectedService?.type === 'ec2' 
                ? metrics.ec2[selectedService?.id] 
                : selectedService?.type === 'rds' 
                  ? metrics.rds[selectedService?.id] 
                  : selectedService?.type === 'lambda'
                    ? metrics.lambda[selectedService?.id]
                    : null}
            />
          </div>
        );
      case 'topology':
        return (
          <div className="canvas-container" style={{ height: '60vh' }}>
            <AWSWebGLCanvas
              ec2={infrastructure.ec2.instances}
              rds={infrastructure.rds.instances}
              lambda={infrastructure.lambda.functions}
              metrics={metrics}
              selectedService={selectedService}
            />
          </div>
        );
      case 'alerts':
        return <AlertsPanel alerts={alerts} security={infrastructure.security} />;
      case 'more':
        return (
          <FeaturePanels
            infrastructure={infrastructure}
            selectedService={selectedService}
            onSelect={handleServiceSelect}
          />
        );
      default:
        return null;
    }
  };

  const renderDesktopContent = () => (
    <>
      <div className="dashboard-grid">
        <ServicePanel
          ec2={infrastructure.ec2}
          rds={infrastructure.rds}
          lambda={infrastructure.lambda}
          onSelect={handleServiceSelect}
          selectedService={selectedService}
        />

        {viewMode === 'dashboard' ? (
          <RetroDashboard
            ec2={infrastructure.ec2}
            rds={infrastructure.rds}
            metrics={metrics}
            billing={infrastructure.billing}
          />
        ) : (
          <div className="canvas-container">
            <AWSWebGLCanvas
              ec2={infrastructure.ec2.instances}
              rds={infrastructure.rds.instances}
              metrics={metrics}
              selectedService={selectedService}
            />
          </div>
        )}

        <div className="metrics-col">
          <MetricsPanel
            selectedService={selectedService}
            instance={selectedInstance}
            metrics={selectedService?.type === 'ec2'
              ? metrics.ec2[selectedService?.id]
              : selectedService?.type === 'rds'
                ? metrics.rds[selectedService?.id]
                : null}
          />
          <BillingPanel billing={infrastructure.billing} />
        </div>
      </div>

      {/* 20 real-data feature panels grouped by category (Compute / Storage /
          Network / Security / Cost & Ops), scrolling below the dashboard. */}
      <FeaturePanels
        infrastructure={infrastructure}
        selectedService={selectedService}
        onSelect={handleServiceSelect}
      />
    </>
  );

  return (
    <div className="overlay-ui">
      <header className="header-bar">
        <h1>
          AWS <span>Observability</span>
        </h1>
        <div className="header-controls">
          {!isMobile && (
            <div className="view-toggle">
              <button 
                type="button"
                className={viewMode === 'dashboard' ? 'active' : ''} 
                onClick={() => setViewMode('dashboard')}
                aria-pressed={viewMode === 'dashboard'}
              >
                Dashboard
              </button>
              <button 
                type="button"
                className={viewMode === 'topology' ? 'active' : ''} 
                onClick={() => setViewMode('topology')}
                aria-pressed={viewMode === 'topology'}
              >
                Topology
              </button>
            </div>
          )}
          <div className="status-chips">
            <div className="status-chip">
              <span className={`dot ${connected ? 'green' : 'red'}`} />
              <span className="hide-mobile">{connected ? 'LIVE' : 'OFFLINE'}</span>
            </div>
            <div className="status-chip hide-mobile">
              <span className="dot green" />
              <span>{runningCount} RUNNING</span>
            </div>
            <div className="status-chip">
              <span style={{ color: 'var(--accent-cyan)' }}>{totalResources}</span>
            </div>
          </div>
          <button type="button" className="logout-btn" onClick={onLogout} title="Sign out" aria-label="Sign out">⏻</button>
        </div>
      </header>

      <main className={`main-content ${viewMode}`}>
        <PullToRefresh onRefresh={handleRefresh}>
          {isMobile ? renderMobileContent() : renderDesktopContent()}
        </PullToRefresh>
      </main>

      <AICopilot 
        infrastructure={infrastructure} 
        metrics={metrics}
        alerts={alerts}
      />

      {isMobile && (
        <MobileNav 
          activeView={viewMode}
          onViewChange={handleViewChange}
          views={mobileViews}
        />
      )}
    </div>
  );
}

function AlertsPanel({ alerts, security }) {
  return (
    <div className="panel">
      <div className="panel-header">
        <h2>Alerts</h2>
        <span className="panel-count">{alerts.length}</span>
      </div>
      <div className="service-list">
        {alerts.length === 0 ? (
          <div style={{ textAlign: 'center', padding: '24px', color: 'var(--accent-green)' }}>
            ✓ All systems nominal
          </div>
        ) : (
          alerts.map(alert => (
            <div key={alert.id} className="service-item" style={{ borderColor: alert.severity === 'critical' ? 'var(--accent-red)' : 'var(--accent-orange)' }}>
              <div className="service-header">
                <span style={{ color: alert.severity === 'critical' ? 'var(--accent-red)' : 'var(--accent-orange)' }}>
                  {alert.severity.toUpperCase()}
                </span>
              </div>
              <div style={{ fontSize: '12px' }}>{alert.message}</div>
              {alert.resource && <div className="service-meta">{alert.resource}</div>}
            </div>
          ))
        )}
      </div>
      {security && (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '12px', padding: '16px' }}>
          <span style={{ fontSize: '32px', fontWeight: 'bold', color: security.score > 80 ? 'var(--accent-green)' : security.score > 60 ? 'var(--accent-orange)' : 'var(--accent-red)' }}>
            {security.score}
          </span>
          <span style={{ fontSize: '14px', color: 'var(--text-secondary)' }}>Security Score</span>
        </div>
      )}
    </div>
  );
}
