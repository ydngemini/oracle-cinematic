import { useCallback, useEffect, useState } from 'react';
import { crmGet, crmPost } from '../state/useCrmApi';
import styles from './ContractVaultTab.module.css';

/**
 * The tenant's contract template registry, and the approval gate on it.
 *
 * Four endpoints shipped with no caller: list, bootstrap, per-template decision,
 * and the policy statement. Without them a tenant had no way to see which
 * templates existed, no way to install the built-in candidates, and — the one
 * that matters — no way to record the attorney review that makes a template
 * usable for a real document.
 *
 * Bootstrap deliberately installs candidates as DRAFTS and never auto-approves.
 * Source approval only proves a template came from the version-controlled
 * registry; it is not a substitute for the attorney review, and the server
 * keeps those two facts separate. This screen shows them separately too, since
 * collapsing them would let "it came from our registry" read as "a lawyer
 * signed off on it".
 */

const STATUS_ORDER = { draft: 0, pending_review: 1, approved: 2, rejected: 3 };

export default function ContractTemplateRegistry() {
  const [templates, setTemplates] = useState(null);
  const [policy, setPolicy] = useState(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState('');
  const [reviewer, setReviewer] = useState('');
  const [reason, setReason] = useState('');
  const [openId, setOpenId] = useState('');

  const load = useCallback(() => {
    setError('');
    return Promise.allSettled([
      crmGet('/api/contracts/templates'),
      crmGet('/api/contracts/policy'),
    ]).then(([list, pol]) => {
      if (list.status === 'fulfilled') {
        const rows = list.value?.templates || list.value?.sources || [];
        setTemplates(Array.isArray(rows) ? rows : []);
      } else {
        setTemplates([]);
        setError('The template registry could not be read.');
      }
      setPolicy(pol.status === 'fulfilled' ? pol.value : null);
    });
  }, []);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => { void load(); });
    return () => window.cancelAnimationFrame(frame);
  }, [load]);

  const act = async (label, run) => {
    if (busy) return;
    setBusy(label);
    setError('');
    setNotice('');
    try {
      await run();
      await load();
    } catch (reason_) {
      setError(
        reason_?.status === 403
          ? 'Only a broker owner can install or approve templates.'
          : reason_?.message || 'The registry refused that change.',
      );
    } finally {
      setBusy('');
    }
  };

  const bootstrap = () => act('bootstrap', async () => {
    const result = await crmPost('/api/contracts/templates/bootstrap', {});
    const n = Array.isArray(result?.created) ? result.created.length : 0;
    setNotice(n === 0
      ? 'Nothing installed — the built-in candidates are already present.'
      : `Installed ${n} candidate${n === 1 ? '' : 's'} as drafts. None is approved yet.`);
  });

  const decide = (template, decision) => act(template.id, async () => {
    await crmPost(`/api/contracts/templates/${template.id}/decision`, {
      decision,
      attorney_reviewed_by: reviewer.trim(),
      reason: reason.trim(),
    });
    setReason('');
    setOpenId('');
    setNotice(decision === 'approved'
      ? 'Approved — this template may now be used to generate documents.'
      : 'Rejected.');
  });

  const canDecide = reviewer.trim().length >= 3 && reason.trim().length >= 8;
  const sorted = [...(templates || [])].sort(
    (a, b) => (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9),
  );

  return (
    <section className={styles.clientVault} aria-labelledby="template-registry-title">
      <div className={styles.clientVaultHeader}>
        <div>
          <span className={styles.kicker}>Registry</span>
          <h2 id="template-registry-title">Contract templates</h2>
        </div>
        <button type="button" onClick={bootstrap} disabled={busy !== ''}>
          {busy === 'bootstrap' ? 'Installing…' : 'Install built-in candidates'}
        </button>
      </div>

      {error ? <div className={styles.error} role="alert"><p>{error}</p></div> : null}
      {notice ? <p role="status">{notice}</p> : null}

      {policy ? (
        <p>
          Policy: approved templates only
          {policy.attorney_review_required ? ', attorney review required' : ''}
          {policy.storage ? ` · ${policy.storage}` : ''}.
          A template that came from the version-controlled registry is still not usable
          until someone has reviewed it — those are two different facts.
        </p>
      ) : null}

      {templates === null ? <div className={styles.skeleton} aria-hidden="true" /> : null}

      {templates && templates.length === 0 ? (
        <p>No templates registered. Install the built-in candidates to get started — they
          arrive as drafts and each still needs review before it can generate a document.</p>
      ) : null}

      {sorted.map((template) => (
        <div key={template.id}>
          <div className={styles.clientVaultHeader}>
            <div>
              <strong>{template.template_key || template.name || template.id}</strong>
              <span>
                {template.version ? `v${template.version}` : ''}
                {template.jurisdiction ? ` · ${template.jurisdiction}` : ''}
                {template.document_type ? ` · ${template.document_type}` : ''}
              </span>
            </div>
            <span>{template.status || 'unknown'}</span>
          </div>

          {template.status === 'approved' ? (
            <p>
              Reviewed by {template.attorney_reviewed_by || 'an unrecorded reviewer'}
              {template.approved_at ? ` on ${new Date(template.approved_at).toLocaleDateString()}` : ''}.
            </p>
          ) : (
            <>
              <button
                type="button"
                onClick={() => setOpenId(openId === template.id ? '' : template.id)}
                aria-expanded={openId === template.id}
              >
                {openId === template.id ? 'Cancel review' : 'Review this template'}
              </button>
              {openId === template.id ? (
                <>
                  <label>
                    <span>Attorney who reviewed it</span>
                    <input
                      value={reviewer}
                      onChange={(event) => setReviewer(event.target.value)}
                      maxLength={200}
                      placeholder="Named, because the record is the point"
                    />
                  </label>
                  <label>
                    <span>Reason (8+ characters)</span>
                    <input
                      value={reason}
                      onChange={(event) => setReason(event.target.value)}
                      maxLength={500}
                    />
                  </label>
                  <button
                    type="button"
                    onClick={() => decide(template, 'approved')}
                    disabled={!canDecide || busy !== ''}
                  >
                    Approve for use
                  </button>
                  <button
                    type="button"
                    onClick={() => decide(template, 'rejected')}
                    disabled={!canDecide || busy !== ''}
                  >
                    Reject
                  </button>
                </>
              ) : null}
            </>
          )}
        </div>
      ))}
    </section>
  );
}
