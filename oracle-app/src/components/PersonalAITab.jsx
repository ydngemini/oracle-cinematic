import { useCallback, useEffect, useMemo, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import { BrokerageOnboardingPanel } from './BrokerageOnboardingPanel';
import { CommandApprovalPanel } from './CommandApprovalPanel';
import { PersonalCommandComposer } from './PersonalCommandComposer';
import styles from './PersonalAITab.module.css';

const LOCAL_MODELS_ENABLED = import.meta.env.VITE_LOCAL_MODELS_ENABLED === 'true';

function human(value, fallback = 'not configured') {
  return String(value || fallback).replaceAll('_', ' ');
}

function toneFor(value) {
  if (['active', 'connected', 'approved', 'validated', 'completed', 'executed'].includes(value)) return 'good';
  if (['failed', 'rejected', 'disabled', 'reconciliation_required'].includes(value)) return 'danger';
  return 'neutral';
}

function formatTime(value) {
  if (!value) return 'No timestamp';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 'Unknown time' : date.toLocaleString();
}

export default function PersonalAITab() {
  const [data, setData] = useState({
    onboarding: null, models: [], commands: [], providers: [], contractDocuments: [],
  });
  const [errors, setErrors] = useState([]);
  const [loading, setLoading] = useState(true);
  const [documentError, setDocumentError] = useState('');

  const load = useCallback(() => {
    const requests = [
      ['onboarding', crmGet('/api/agents/me/onboarding')],
      ['commands', crmGet('/api/commands?limit=20')],
      ['providers', crmGet('/api/commands/providers')],
      ['contractDocuments', crmGet('/api/contracts/documents?limit=20')],
    ];
    if (LOCAL_MODELS_ENABLED) {
      requests.push(['models', crmGet('/api/models?limit=20')]);
    }
    return Promise.allSettled(requests.map(([, request]) => request)).then((settled) => {
      const next = {
        onboarding: null, models: [], commands: [], providers: [], contractDocuments: [],
      };
      const nextErrors = [];
      settled.forEach((result, index) => {
        const key = requests[index][0];
        if (result.status === 'rejected') {
          nextErrors.push(`${human(key)}: ${result.reason?.message || 'unavailable'}`);
          return;
        }
        if (key === 'models') next.models = Array.isArray(result.value?.models) ? result.value.models : [];
        else if (key === 'commands') next.commands = Array.isArray(result.value?.commands) ? result.value.commands : [];
        else if (key === 'providers') next.providers = Array.isArray(result.value?.providers) ? result.value.providers : [];
        else if (key === 'contractDocuments') next.contractDocuments = Array.isArray(result.value?.documents) ? result.value.documents : [];
        else next.onboarding = result.value;
      });
      setData(next);
      setErrors(nextErrors);
      setLoading(false);
    });
  }, []);

  useEffect(() => { load(); }, [load]);

  const refresh = () => {
    setLoading(true);
    load();
  };

  const openSecureDocument = (documentId) => {
    setDocumentError('');
    crmGet(`/api/contracts/documents/${encodeURIComponent(documentId)}/download`).then(
      (result) => {
        if (!result?.url) throw new Error('No secure document link returned.');
        const link = window.document.createElement('a');
        link.href = result.url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.click();
      },
      () => setDocumentError('The secure document link is unavailable. Try again after review approval.'),
    ).catch(() => setDocumentError('The secure document link is unavailable. Try again after review approval.'));
  };

  const metrics = useMemo(() => {
    const settings = data.onboarding?.ai_settings;
    const autonomy = [settings?.autonomous_research, settings?.autonomous_drafting].filter(Boolean).length;
    return {
      autonomy: `${autonomy}/2`,
      activeModels: data.models.filter((model) => model.status === 'active').length,
      pendingCommands: data.commands.filter((command) => command.state === 'awaiting_approval').length,
      connectedProviders: data.providers.filter((provider) => !provider.disabled_at).length,
    };
  }, [data]);

  const settings = data.onboarding?.ai_settings;
  const visibleModels = data.models.slice(0, 5);
  const visibleCommands = data.commands.slice(0, 5);
  const visibleDocuments = data.contractDocuments.slice(0, 8);
  return (
    <section className={styles.wrap} aria-labelledby="personal-ai-title" aria-busy={loading}>
      <header className={styles.hero}>
        <div>
          <span className={styles.kicker}>Agent intelligence</span>
          <h1 id="personal-ai-title">Personal AI</h1>
          <p>Your approved autonomy, private style model, provider links, live web search, and review-gated actions in one place.</p>
        </div>
        <button type="button" className={styles.refresh} onClick={refresh} disabled={loading} aria-label="Refresh Personal AI status">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/></svg>
        </button>
      </header>

      <PersonalCommandComposer />

      <div className={styles.guardrail} role="note">
        <strong>Approval boundary</strong>
        <p>Research, drafting, and web search can run autonomously. Outreach, calls, calendar writes, legal documents, bidding messages, financial actions, and model promotion require review.</p>
      </div>

      {errors.length > 0 && (
        <div className={styles.partial} role="status">
          <strong>Some AI services are unavailable</strong>
          <ul>{errors.map((error) => <li key={error}>{error}</li>)}</ul>
        </div>
      )}

      {loading ? (
        <div className={styles.skeletons} aria-label="Loading Personal AI status">
          <div /><div /><div />
        </div>
      ) : (
        <>
          <dl className={styles.metrics}>
            <div><dt>Autonomy enabled</dt><dd>{metrics.autonomy}</dd></div>
            <div><dt>Active models</dt><dd>{metrics.activeModels}</dd></div>
            <div><dt>Awaiting review</dt><dd>{metrics.pendingCommands}</dd></div>
            <div><dt>Providers linked</dt><dd>{metrics.connectedProviders}</dd></div>
          </dl>

          <section className={styles.panel} aria-labelledby="ai-contract-holdings-title">
            <header><h2 id="ai-contract-holdings-title">AI contract holdings</h2><span>{data.contractDocuments.length}</span></header>
            <p className={styles.contractNote}>Final vault records stay encrypted and tenant-scoped. Approved and signed records can be opened as PDFs; signature status remains a separate final workflow.</p>
            {documentError && <p className={styles.documentError} role="alert">{documentError}</p>}
            {visibleDocuments.length === 0 ? <p className={styles.empty}>No AI contract documents are held for this tenant yet.</p> : (
              <ul className={styles.rows}>{visibleDocuments.map((contract) => {
                const downloadable = ['approved', 'signed'].includes(contract.status);
                return (
                  <li key={contract.id} className={styles.documentRow}>
                    <div>
                      <strong>{human(contract.document_type, 'contract')}</strong>
                      <small>{contract.template_key || 'template pending'} · {formatTime(contract.created_at)}</small>
                    </div>
                    <div className={styles.documentAction}>
                      <span data-tone={toneFor(contract.status)}>{human(contract.status)}</span>
                      <button type="button" onClick={() => openSecureDocument(contract.id)} disabled={!downloadable}>
                        {downloadable ? 'Open PDF' : 'Vault review'}
                      </button>
                    </div>
                  </li>
                );
              })}</ul>
            )}
          </section>

          <section className={styles.panel} aria-labelledby="ai-boundary-title">
            <header><h2 id="ai-boundary-title">Current AI boundary</h2><span>{human(settings?.autonomy_mode, 'policy autopilot')}</span></header>
            <ul className={styles.boundaries}>
              <li><span>Operating mode</span><strong data-enabled>{human(settings?.autonomy_mode, 'policy autopilot')}</strong></li>
              <li><span>Approved tone</span><strong>{human(settings?.approved_tone, 'neutral')}</strong></li>
              <li><span>Public-record research</span><strong data-enabled={Boolean(settings?.autonomous_research)}>{settings?.autonomous_research ? 'Enabled' : 'Review first'}</strong></li>
              <li><span>Draft preparation</span><strong data-enabled={Boolean(settings?.autonomous_drafting)}>{settings?.autonomous_drafting ? 'Enabled' : 'Review first'}</strong></li>
              <li><span>Style training consent</span><strong data-enabled={Boolean(settings?.style_training_opt_in)}>{settings?.style_training_opt_in ? 'Opted in' : 'Not opted in'}</strong></li>
              <li><span>Consented examples</span><strong>{data.onboarding?.style_training_examples ?? 0}</strong></li>
            </ul>
          </section>

          <section className={styles.panel} aria-labelledby="personal-models-title">
            <header><h2 id="personal-models-title">Model registry</h2><span>{data.models.length}</span></header>
            {visibleModels.length === 0 ? <p className={styles.empty}>No model versions are registered for this tenant yet.</p> : (
              <ul className={styles.rows}>{visibleModels.map((model) => (
                <li key={model.id}>
                  <div><strong>{model.name}</strong><small>{human(model.model_kind)} · v{model.version}</small></div>
                  <span data-tone={toneFor(model.status)}>{human(model.status)}</span>
                </li>
              ))}</ul>
            )}
          </section>

          <section className={styles.panel} aria-labelledby="personal-commands-title">
            <header><h2 id="personal-commands-title">Recent AI actions</h2><span>{data.commands.length}</span></header>
            {visibleCommands.length === 0 ? <p className={styles.empty}>No email, call, or calendar commands have been drafted.</p> : (
              <ul className={styles.rows}>{visibleCommands.map((command) => (
                <li key={command.id}>
                  <div><strong>{command.command_type}</strong><small>{formatTime(command.created_at)}</small></div>
                  <span data-tone={toneFor(command.state)}>{human(command.state)}</span>
                </li>
              ))}</ul>
            )}
          </section>
        </>
      )}

      <CommandApprovalPanel />
      <BrokerageOnboardingPanel />
    </section>
  );
}
