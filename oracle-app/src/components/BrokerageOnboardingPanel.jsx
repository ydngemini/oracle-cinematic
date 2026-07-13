import { useCallback, useEffect, useState } from 'react';
import { crmGet, crmPost, crmPut } from '../state/useCrmApi';
import { getUserId } from '../state/identity';
import styles from './BrokerageOnboardingPanel.module.css';

const EMPTY_LICENSE = { state_code: '', license_number: '', license_type: 'salesperson', expires_on: '' };

function human(value) {
  return String(value || 'not started').replaceAll('_', ' ');
}

export function BrokerageOnboardingPanel() {
  const [status, setStatus] = useState(null);
  const [team, setTeam] = useState([]);
  const [approvals, setApprovals] = useState([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [reason, setReason] = useState('Reviewed brokerage membership, license details, and AI settings.');
  const [form, setForm] = useState({
    team_name: '', title: '', licenses: [{ ...EMPTY_LICENSE }], approved_tone: 'neutral',
    autonomous_research: true, autonomous_drafting: true, style_training_opt_in: false,
  });
  const role = sessionStorage.getItem('oracle_role') || 'agent';

  const load = useCallback(() => {
    const requests = [crmGet('/api/agents/me/onboarding')];
    if (role === 'broker_owner' || role === 'platform_admin') {
      requests.push(crmGet('/api/agents/team'), crmGet('/api/commands/approvals?status=pending&limit=100'));
    }
    return Promise.all(requests).then((values) => {
      const mine = values[0];
      setStatus(mine);
      const membership = mine?.membership;
      const settings = mine?.ai_settings;
      const licenses = Array.isArray(mine?.licenses) && mine.licenses.length
        ? mine.licenses.map((license) => ({
          state_code: license.state_code || '', license_number: license.license_number || '',
          license_type: license.license_type || 'salesperson', expires_on: license.expires_on || '',
        }))
        : [{ ...EMPTY_LICENSE }];
      setForm((current) => ({
        ...current,
        team_name: membership?.team_name || current.team_name,
        title: membership?.title || current.title,
        licenses,
        approved_tone: settings?.approved_tone || current.approved_tone,
        autonomous_research: settings?.autonomous_research ?? current.autonomous_research,
        autonomous_drafting: settings?.autonomous_drafting ?? current.autonomous_drafting,
        style_training_opt_in: settings?.style_training_opt_in ?? current.style_training_opt_in,
      }));
      if (values[1]) setTeam(Array.isArray(values[1]?.members) ? values[1].members : []);
      if (values[2]) {
        const rows = Array.isArray(values[2]?.approvals) ? values[2].approvals : [];
        setApprovals(rows.filter((approval) => approval.action_type === 'brokerage.onboarding'));
      }
      setError('');
    }).catch((reasonValue) => setError(reasonValue.message || 'Brokerage onboarding is unavailable.'));
  }, [role]);

  useEffect(() => { load(); }, [load]);

  const updateLicense = (index, key, value) => setForm((current) => ({
    ...current,
    licenses: current.licenses.map((license, itemIndex) => itemIndex === index ? { ...license, [key]: value } : license),
  }));

  const submit = (event) => {
    event.preventDefault();
    setBusy(true); setNotice(''); setError('');
    crmPut('/api/agents/brokerage-onboarding', {
      team_name: form.team_name,
      title: form.title || null,
      licenses: form.licenses.map((license) => ({ ...license, state_code: license.state_code.toUpperCase(), expires_on: license.expires_on || null })),
      ai_settings: {
        approved_tone: form.approved_tone,
        autonomous_research: form.autonomous_research,
        autonomous_drafting: form.autonomous_drafting,
        style_training_opt_in: form.style_training_opt_in,
        preferences: {},
      },
    }).then(() => { setNotice('Submitted for a different broker’s approval.'); return load(); })
      .catch((reasonValue) => setError(reasonValue.message || 'Submission failed.'))
      .finally(() => setBusy(false));
  };

  const decide = (approval, decision) => {
    setBusy(true); setNotice('');
    crmPost(`/api/agents/brokerage-onboarding/${approval.id}/decision`, { decision, reason: reason.trim() })
      .then(() => { setNotice(`Onboarding ${decision}.`); return load(); })
      .catch((reasonValue) => setError(reasonValue.message || 'Decision failed.'))
      .finally(() => setBusy(false));
  };

  return (
    <section className={styles.wrap} aria-labelledby="brokerage-onboarding-title" aria-busy={!status || busy}>
      <header className={styles.heading}>
        <div><span>Brokerage controls</span><h2 id="brokerage-onboarding-title">Team onboarding</h2></div>
        <button type="button" onClick={load}>Refresh</button>
      </header>

      {error && <p className={styles.error} role="alert">{error}</p>}
      {notice && <p className={styles.notice} role="status">{notice}</p>}

      {status && (
        <div className={styles.statusGrid}>
          <div><span>Membership</span><strong>{human(status.membership?.status)}</strong></div>
          <div><span>License</span><strong>{human(status.licenses?.[0]?.verification_status)}</strong></div>
          <div><span>Google</span><strong>{status.google_connected ? 'connected' : 'not connected'}</strong></div>
          <div><span>Style model</span><strong>{human(status.membership?.training_status)}</strong></div>
        </div>
      )}

      <form onSubmit={submit} className={styles.form}>
        <div className={styles.twoCol}>
          <label><span>Team name</span><input value={form.team_name} onChange={(event) => setForm((current) => ({ ...current, team_name: event.target.value }))} minLength={2} maxLength={160} required /></label>
          <label><span>Title</span><input value={form.title} onChange={(event) => setForm((current) => ({ ...current, title: event.target.value }))} maxLength={120} /></label>
        </div>

        <fieldset className={styles.licenses}>
          <legend>Licenses</legend>
          {form.licenses.map((license, index) => (
            <div key={index} className={styles.licenseRow}>
              <label><span>State</span><input value={license.state_code} onChange={(event) => updateLicense(index, 'state_code', event.target.value.toUpperCase().slice(0, 2))} pattern="[A-Za-z]{2}" required /></label>
              <label><span>License number</span><input value={license.license_number} onChange={(event) => updateLicense(index, 'license_number', event.target.value)} minLength={2} required /></label>
              <label><span>Type</span><input value={license.license_type} onChange={(event) => updateLicense(index, 'license_type', event.target.value)} minLength={2} required /></label>
              <label><span>Expires</span><input type="date" value={license.expires_on} onChange={(event) => updateLicense(index, 'expires_on', event.target.value)} /></label>
              {form.licenses.length > 1 && <button type="button" onClick={() => setForm((current) => ({ ...current, licenses: current.licenses.filter((_, itemIndex) => itemIndex !== index) }))}>Remove</button>}
            </div>
          ))}
          <button type="button" className={styles.add} onClick={() => setForm((current) => ({ ...current, licenses: [...current.licenses, { ...EMPTY_LICENSE }] }))}>Add license</button>
        </fieldset>

        <fieldset className={styles.aiSettings}>
          <legend>AI settings</legend>
          <label className={styles.tone}><span>Approved tone</span><select value={form.approved_tone} onChange={(event) => setForm((current) => ({ ...current, approved_tone: event.target.value }))}><option value="neutral">Neutral</option><option value="concise">Concise</option><option value="warm">Warm</option><option value="formal">Formal</option><option value="direct">Direct</option></select></label>
          <label><input type="checkbox" checked={form.autonomous_research} onChange={(event) => setForm((current) => ({ ...current, autonomous_research: event.target.checked }))} /><span>Allow autonomous public-record research</span></label>
          <label><input type="checkbox" checked={form.autonomous_drafting} onChange={(event) => setForm((current) => ({ ...current, autonomous_drafting: event.target.checked }))} /><span>Allow autonomous draft preparation</span></label>
          <label><input type="checkbox" checked={form.style_training_opt_in} onChange={(event) => setForm((current) => ({ ...current, style_training_opt_in: event.target.checked }))} /><span>Opt in to consented, PII-redacted style training</span></label>
          <p>Outreach, calls, legal work, bidding messages, and financial actions always retain approval gates.</p>
        </fieldset>

        <button type="submit" className={styles.primary} disabled={busy}>{busy ? 'Submitting…' : 'Submit for broker approval'}</button>
      </form>

      {(role === 'broker_owner' || role === 'platform_admin') && (
        <>
          <section className={styles.review} aria-labelledby="onboarding-review-title">
            <header><h3 id="onboarding-review-title">Approval queue</h3><span>{approvals.length}</span></header>
            {approvals.length === 0 ? <p>No onboarding requests awaiting another broker.</p> : (
              <>
                <label className={styles.reason}><span>Decision reason</span><textarea value={reason} onChange={(event) => setReason(event.target.value)} minLength={8} maxLength={500} rows={3} /></label>
                <ul>{approvals.map((approval) => (
                  <li key={approval.id}>
                    <div><strong>{approval.draft_payload?.team_name || 'Team membership'}</strong><small>Requested by {approval.requested_by}{approval.requested_by === getUserId() ? ' · another broker required' : ''}</small></div>
                    <div><button type="button" onClick={() => decide(approval, 'rejected')} disabled={busy || reason.trim().length < 8}>Reject</button><button type="button" onClick={() => decide(approval, 'approved')} disabled={busy || reason.trim().length < 8 || approval.requested_by === getUserId()}>Approve</button></div>
                  </li>
                ))}</ul>
              </>
            )}
          </section>

          <section className={styles.review} aria-labelledby="team-roster-title">
            <header><h3 id="team-roster-title">Team roster</h3><span>{team.length}</span></header>
            {team.length === 0 ? <p>No team memberships recorded.</p> : <ul>{team.map((member) => (
              <li key={member.id}><div><strong>{member.agent_id}</strong><small>{member.team_name} · {human(member.member_role)}</small></div><span>{human(member.status)} · {member.verified_licenses}/{member.license_count} licenses</span></li>
            ))}</ul>}
          </section>
        </>
      )}
    </section>
  );
}
