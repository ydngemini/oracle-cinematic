import { useCallback, useEffect, useState } from 'react';
import { crmDelete, crmGet, crmPost, crmPut } from '../state/useCrmApi';
import styles from './StudioTab.module.css';
import { friendlyError } from '../lib/errorMessages';

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
 *
 * Once live (T14.04): the live version is named, and someone who may publish
 * can roll back to the version that was live before it. A custom hostname is
 * a claim proven by one DNS TXT record; "Check DNS" asks the server, which
 * moves the claim only on what DNS answered. A domain is never called live
 * here: ownership being verified is not the site being served at that name.
 */

// Plain words for a claim's state. None of them says "live".
const DOMAIN_STATUS = {
  pending: 'Waiting for the DNS record',
  verified: 'Ownership verified',
  active: 'Ownership verified · points at the platform',
  failed: 'Check failed — record missing or wrong',
  retired: 'Retired',
};

const DNS_UNREACHABLE = 'DNS could not be reached. Nothing changed — try again in a moment.';

export default function SitePublishPanel({ siteId }) {
  const [revisions, setRevisions] = useState([]);
  const [revisionId, setRevisionId] = useState('');
  const [hostname, setHostname] = useState('');
  const [approvalId, setApprovalId] = useState('');
  const [funnel, setFunnel] = useState(null);
  const [collaborators, setCollaborators] = useState([]);
  const [newAgent, setNewAgent] = useState('');
  const [canPublish, setCanPublish] = useState(false);
  const [mayPublish, setMayPublish] = useState(false);
  const [publication, setPublication] = useState(null);
  const [domains, setDomains] = useState([]);
  const [domainNotes, setDomainNotes] = useState({});
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const load = useCallback(() => {
    setError('');
    return Promise.allSettled([
      crmGet(`/api/sites/${siteId}`),
      crmGet(`/api/sites/${siteId}/funnel?days=90`),
      crmGet(`/api/sites/${siteId}/collaborators`),
      crmGet(`/api/sites/${siteId}/domains`),
    ]).then(([detail, funnelResult, collabResult, domainResult]) => {
      if (detail.status === 'fulfilled') {
        const rows = Array.isArray(detail.value?.revisions) ? detail.value.revisions : [];
        setRevisions(rows);
        setRevisionId((current) => current || rows[0]?.id || '');
        setMayPublish(detail.value?.can_publish === true);
        setPublication(detail.value?.publication || null);
      } else {
        setError('This site could not be read.');
      }
      // Only someone who may publish can read the claims (404 otherwise).
      setDomains(
        domainResult.status === 'fulfilled' && Array.isArray(domainResult.value?.domains)
          ? domainResult.value.domains
          : [],
      );
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
      setError(friendlyError(reason, { fallback: 'The request was refused.' }));
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

  // The detail lists the latest 25 revisions; an older one is named, not numbered.
  const versionLabel = (id, fallback = 'the previous version') => {
    const row = revisions.find((item) => item.id === id);
    return row ? `v${row.revision}` : fallback;
  };
  const liveId = publication?.status === 'published' ? publication?.live_revision_id || '' : '';
  const rollbackTo = mayPublish && liveId ? publication?.rollback_to_revision_id || '' : '';

  const rollBack = () => act('rollback', async () => {
    const label = versionLabel(rollbackTo);
    // Visitors see the change on their next page view, so ask first.
    if (!window.confirm(`Make ${label} live again? Visitors will see it on their next visit.`)) return;
    const result = await crmPost(`/api/sites/${siteId}/rollback`, {
      expected_live_revision_id: liveId,
      to_revision_id: rollbackTo,
    });
    setNotice(`${versionLabel(result?.live_revision_id || rollbackTo)} is live again.`);
    await load();
  });

  const checkDns = (domainId) => act(`dns:${domainId}`, async () => {
    const result = await crmPost(`/api/sites/${siteId}/domains/${domainId}/verify`, {});
    if (result?.domain) {
      setDomains((rows) => rows.map((row) => (row.id === domainId ? result.domain : row)));
    }
    const note = result?.outcome === 'dns_unavailable'
      ? (result?.message || DNS_UNREACHABLE)
      : (result?.message || '');
    setDomainNotes((notes) => ({ ...notes, [domainId]: note }));
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

      {liveId ? <p>{`Live: ${versionLabel(liveId, 'an older version')}`}</p> : null}
      {rollbackTo ? (
        <div className={styles.builderActions}>
          <button type="button" className={styles.secondary} onClick={rollBack} disabled={busy !== ''}>
            {busy === 'rollback' ? 'Rolling back…' : `Roll back to ${versionLabel(rollbackTo)}`}
          </button>
        </div>
      ) : null}

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

      {mayPublish && domains.length > 0 ? (
        <>
          <h4>Domains</h4>
          <p className={styles.disclosure}>
            To prove you control a domain, add the TXT record shown at your DNS provider, then
            check DNS. Changes can take a while to appear.
          </p>
          <ul className={`${styles.siteList} ${styles.domainList}`} aria-label="Domains">
            {domains.map((row) => (
              <li key={row.id}>
                <div>
                  <strong>{row.hostname}</strong>
                  <small>{DOMAIN_STATUS[row.status] || row.status}</small>
                  {row.challenge ? (
                    <dl className={styles.dnsRecord}>
                      <dt>TXT record name</dt>
                      <dd><code>{row.challenge.name}</code></dd>
                      <dt>Value</dt>
                      <dd><code>{row.challenge.value}</code></dd>
                    </dl>
                  ) : null}
                  {domainNotes[row.id] ? <p role="status">{domainNotes[row.id]}</p> : null}
                </div>
                {row.status !== 'retired' ? (
                  <button type="button" onClick={() => checkDns(row.id)} disabled={busy !== ''}>
                    {busy === `dns:${row.id}` ? 'Checking…' : 'Check DNS'}
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        </>
      ) : null}

      <h4>Audience (90 days)</h4>
      {breakdown.length === 0 ? (
        <p className={styles.empty}>No visits or leads from your site in this period.</p>
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
