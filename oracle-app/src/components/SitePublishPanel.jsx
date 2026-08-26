import { useCallback, useEffect, useState } from 'react';
import { crmDelete, crmGet, crmPost, crmPut } from '../state/useCrmApi';
import styles from './StudioTab.module.css';

/**
 * Publish approval, audience attribution, and collaborators for one site.
 *
 * `sites_api` carried nine routes that Studio never called. It could create a
 * draft and preview it, and then stopped — so a site could not actually be
 * published from the product, nobody could be given edit rights, and the
 * visit → lead → appointment → contract → closing funnel it records was
 * written and never read.
 *
 * Publishing is deliberately two-step on the server: `publish-approval` raises
 * an approval and returns its id, and `publish` will not accept a revision
 * without one. That gate is the point — a site is public-facing marketing under
 * a brokerage's name — so this panel walks both steps rather than hiding them
 * behind one button.
 */

export default function SitePublishPanel({ siteId }) {
  const [revisions, setRevisions] = useState([]);
  const [revisionId, setRevisionId] = useState('');
  const [hostname, setHostname] = useState('');
  const [approvalId, setApprovalId] = useState('');
  const [funnel, setFunnel] = useState(null);
  const [collaborators, setCollaborators] = useState([]);
  const [newAgent, setNewAgent] = useState('');
  const [canPublish, setCanPublish] = useState(false);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const load = useCallback(() => {
    setError('');
    return Promise.allSettled([
      crmGet(`/api/sites/${siteId}`),
      crmGet(`/api/sites/${siteId}/funnel?days=90`),
      crmGet(`/api/sites/${siteId}/collaborators`),
    ]).then(([detail, funnelResult, collabResult]) => {
      if (detail.status === 'fulfilled') {
        const rows = Array.isArray(detail.value?.revisions) ? detail.value.revisions : [];
        setRevisions(rows);
        setRevisionId((current) => current || rows[0]?.id || '');
      } else {
        setError('This site could not be read.');
      }
      // Funnel and collaborators resolve independently: an agent with edit but
      // not publish rights gets 403 on collaborators, and that must not blank
      // the publish controls they are entitled to use.
      setFunnel(funnelResult.status === 'fulfilled' ? funnelResult.value : null);
      setCollaborators(
        collabResult.status === 'fulfilled' && Array.isArray(collabResult.value?.collaborators)
          ? collabResult.value.collaborators
          : [],
      );
    });
  }, [siteId]);

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
    } catch (reason) {
      setError(reason?.message || 'The request was refused.');
    } finally {
      setBusy('');
    }
  };

  const requestApproval = () => act('approval', async () => {
    const payload = { revision_id: revisionId };
    if (hostname.trim()) payload.hostname = hostname.trim();
    const result = await crmPost(`/api/sites/${siteId}/publish-approval`, payload);
    const id = result?.approval?.id || result?.approval_id || '';
    setApprovalId(id);
    setNotice(id
      ? 'Approval raised. It must be approved before the site can go live.'
      : 'Approval raised.');
  });

  const publish = () => act('publish', async () => {
    const payload = { revision_id: revisionId, approval_id: approvalId };
    if (hostname.trim()) payload.hostname = hostname.trim();
    await crmPost(`/api/sites/${siteId}/publish`, payload);
    setNotice('Published.');
    await load();
  });

  const addCollaborator = () => act('collab', async () => {
    const agentId = newAgent.trim();
    if (!agentId) return;
    await crmPut(`/api/sites/${siteId}/collaborators/${encodeURIComponent(agentId)}`, {
      agent_id: agentId,
      can_edit: true,
      can_publish: canPublish,
    });
    setNewAgent('');
    setCanPublish(false);
    await load();
  });

  const removeCollaborator = (agentId) => act(`remove:${agentId}`, async () => {
    await crmDelete(`/api/sites/${siteId}/collaborators/${encodeURIComponent(agentId)}`);
    await load();
  });

  const breakdown = Array.isArray(funnel?.breakdown) ? funnel.breakdown : [];

  return (
    <div className={styles.builderBody}>
      {error ? <p className={styles.saveError} role="alert">{error}</p> : null}
      {notice ? <p role="status">{notice}</p> : null}

      <div className={styles.form}>
        <label>
          <span>Revision</span>
          <select value={revisionId} onChange={(event) => setRevisionId(event.target.value)}>
            {revisions.length === 0 ? <option value="">No revisions yet</option> : null}
            {revisions.map((row) => (
              <option key={row.id} value={row.id}>
                v{row.revision}{row.created_at ? ` · ${new Date(row.created_at).toLocaleDateString()}` : ''}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Hostname</span>
          <input
            value={hostname}
            onChange={(event) => setHostname(event.target.value)}
            placeholder="Leave blank to keep the preview domain"
            maxLength={253}
          />
        </label>
      </div>

      <div className={styles.builderActions}>
        <button
          type="button"
          className={styles.secondary}
          onClick={requestApproval}
          disabled={!revisionId || busy !== ''}
        >
          {busy === 'approval' ? 'Requesting…' : '1 · Request approval'}
        </button>
        <button
          type="button"
          className={styles.primary}
          onClick={publish}
          disabled={!revisionId || !approvalId || busy !== ''}
        >
          {busy === 'publish' ? 'Publishing…' : '2 · Publish'}
        </button>
      </div>
      {!approvalId ? (
        <p className={styles.disclosure}>
          Publishing needs an approved request. Raise one first — the server refuses a publish
          that does not carry an approval id.
        </p>
      ) : null}

      <h4>Audience (90 days)</h4>
      {breakdown.length === 0 ? (
        <p className={styles.empty}>No attributed events recorded in this window.</p>
      ) : (
        <ul className={styles.siteList}>
          {breakdown.map((row) => (
            <li key={`${row.event_type}:${row.source}:${row.medium}`}>
              <div>
                <strong>{row.event_type}</strong>
                <small>{[row.source, row.medium].filter(Boolean).join(' · ') || 'direct'}</small>
              </div>
              <span>{row.event_count}</span>
            </li>
          ))}
        </ul>
      )}
      {funnel?.note ? <p className={styles.disclosure}>{funnel.note}</p> : null}

      <h4>Collaborators</h4>
      <div className={styles.form}>
        <label>
          <span>Agent id</span>
          <input
            value={newAgent}
            onChange={(event) => setNewAgent(event.target.value)}
            maxLength={128}
          />
        </label>
        <label className={styles.checkRow}>
          <input
            type="checkbox"
            checked={canPublish}
            onChange={(event) => setCanPublish(event.target.checked)}
          />
          <span>May publish, not just edit</span>
        </label>
      </div>
      <div className={styles.builderActions}>
        <button
          type="button"
          className={styles.secondary}
          onClick={addCollaborator}
          disabled={!newAgent.trim() || busy !== ''}
        >
          {busy === 'collab' ? 'Saving…' : 'Grant access'}
        </button>
      </div>
      {collaborators.length === 0 ? (
        <p className={styles.empty}>Only you can edit this site.</p>
      ) : (
        <ul className={styles.siteList}>
          {collaborators.map((row) => (
            <li key={row.agent_id}>
              <div>
                <strong>{row.agent_id}</strong>
                <small>{row.can_publish ? 'edit + publish' : 'edit only'}</small>
              </div>
              <button
                type="button"
                onClick={() => removeCollaborator(row.agent_id)}
                disabled={busy !== ''}
              >
                Revoke
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
