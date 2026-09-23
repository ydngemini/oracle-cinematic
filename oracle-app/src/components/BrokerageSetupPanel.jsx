import { useCallback, useEffect, useState } from 'react';

import { crmDelete, crmGet, crmPatch, crmPost } from '../state/useCrmApi';
import styles from './BrokerageSetupPanel.module.css';

/**
 * Set up Neoh for your business.
 *
 * Deliberately calm. The backend decides what every capability's state is and
 * what to do next; this file renders that and nothing else. It computes no
 * readiness of its own, because a second opinion about whether the phone works
 * is a second opinion that can be wrong.
 *
 * It is also not a tab. Neoh's top level stays Home / Work / Neoh — setup
 * lives in settings, where you go once and then stop going.
 */

const CAPABILITY_LABELS = {
  brokerage_profile: 'Business profile',
  agent_invites: 'Team',
  contact_import: 'Contacts',
  email_calendar: 'Email & calendar',
  phone: 'Phone',
  mls: 'MLS',
  billing: 'Billing',
  readiness: 'Ready to work',
};

// What each state means to a person, rather than what it is called in the API.
const STATE_LABELS = {
  NOT_STARTED: 'Not set up',
  NEEDS_ACTION: 'Needs you',
  IN_PROGRESS: 'In progress',
  READY: 'Ready',
  BLOCKED: 'Waiting on setup',
  ERROR: 'Something went wrong',
  OPTIONAL: 'Optional',
};

const ORG_TYPE_LABELS = {
  brokerage: 'Brokerage',
  team: 'Team',
  independent_agent: 'Independent agent',
};

const ORG_TYPES = [
  ['brokerage', 'Brokerage'],
  ['team', 'Team'],
  ['independent_agent', 'Independent agent'],
];

function CapabilityRow({ name, state, optional }) {
  return (
    <li className={styles.capability} data-state={state}>
      <span className={styles.capabilityName}>{CAPABILITY_LABELS[name] || name}</span>
      <span className={styles.capabilityState}>
        {STATE_LABELS[state] || state}
        {optional && state !== 'READY' ? <em className={styles.optional}> · optional</em> : null}
      </span>
    </li>
  );
}

export function BrokerageSetupPanel() {
  const [setup, setSetup] = useState(null);
  const [team, setTeam] = useState(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [inviteText, setInviteText] = useState('');
  const [editingProfile, setEditingProfile] = useState(false);
  const [profileForm, setProfileForm] = useState({ name: '', org_type: 'brokerage', primary_state: '', website: '' });

  // Not an async function, deliberately: React 19's lint rejects setState
  // called synchronously from an effect, and a promise chain keeps every
  // setState inside a callback. Same shape BrokerageOnboardingPanel uses.
  const load = useCallback(() => (
    Promise.all([crmGet('/api/brokerage/setup'), crmGet('/api/brokerage/team')])
      .then(([state, roster]) => {
        setSetup(state);
        setTeam(roster);
        setProfileForm({
          name: state.brokerage.name || '',
          org_type: state.brokerage.org_type || 'brokerage',
          primary_state: state.brokerage.primary_state || '',
          website: state.brokerage.website || '',
        });
        setError('');
      })
      .catch((err) => setError(err?.message || 'Could not load your brokerage setup.'))
  ), []);

  useEffect(() => { load(); }, [load]);

  // One address per line or comma-separated — people paste both.
  const parseEmails = (raw) =>
    raw.split(/[\s,;]+/).map((s) => s.trim()).filter(Boolean);

  const pendingEmails = parseEmails(inviteText);

  const sendInvites = async () => {
    if (!pendingEmails.length) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const result = await crmPost('/api/brokerage/invitations', { emails: pendingEmails, role: 'agent' });
      const sent = result.created?.length || 0;
      const skipped = result.skipped?.length || 0;
      setNotice(
        `${sent} invitation${sent === 1 ? '' : 's'} sent`
        + (skipped ? ` · ${skipped} already on your team` : ''),
      );
      // Dev only: with no mail server configured the backend hands the link
      // back so the flow can be finished locally. It is never returned in prod.
      if (result.dev_links) {
        setNotice((n) => `${n} · dev: ${Object.values(result.dev_links).join(' ')}`);
      }
      setInviteText('');
      await load();
    } catch (err) {
      setError(err?.message || 'Could not send those invitations.');
    } finally {
      setBusy(false);
    }
  };

  const act = async (fn, successText) => {
    setBusy(true);
    setError('');
    try {
      await fn();
      setNotice(successText);
      await load();
    } catch (err) {
      setError(err?.message || 'That did not work.');
    } finally {
      setBusy(false);
    }
  };

  const saveProfile = () => act(async () => {
    const payload = { name: profileForm.name, org_type: profileForm.org_type };
    if (profileForm.primary_state) payload.primary_state = profileForm.primary_state;
    if (profileForm.website) payload.website = profileForm.website;
    const next = await crmPatch('/api/brokerage/profile', payload);
    setSetup(next);
    setEditingProfile(false);
  }, 'Business profile saved.');

  if (error && !setup) return <section className={styles.panel}><p className={styles.error}>{error}</p></section>;
  if (!setup) return <section className={styles.panel}><p className={styles.muted}>Loading your setup…</p></section>;

  const { brokerage, capabilities, recommended_next: next, team: teamCounts, optional } = setup;
  const optionalSet = new Set(optional || []);

  return (
    <section className={styles.panel} aria-labelledby="brokerage-setup-heading">
      <header className={styles.header}>
        <h2 id="brokerage-setup-heading" className={styles.title}>Set up Neoh for your business</h2>
      </header>

      {error ? <p className={styles.error} role="alert">{error}</p> : null}
      {notice ? <p className={styles.notice} role="status">{notice}</p> : null}

      {/* ── Business ─────────────────────────────────────────────────── */}
      {editingProfile ? (
        <div className={styles.block}>
          <label className={styles.field}>
            <span>Business name</span>
            <input
              value={profileForm.name}
              onChange={(e) => setProfileForm((f) => ({ ...f, name: e.target.value }))}
              maxLength={160}
            />
          </label>
          <label className={styles.field}>
            <span>Type</span>
            <select
              value={profileForm.org_type}
              onChange={(e) => setProfileForm((f) => ({ ...f, org_type: e.target.value }))}
            >
              {ORG_TYPES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          <label className={styles.field}>
            <span>Primary state</span>
            <input
              value={profileForm.primary_state}
              onChange={(e) => setProfileForm((f) => ({ ...f, primary_state: e.target.value.toUpperCase().slice(0, 2) }))}
              placeholder="DE"
              maxLength={2}
            />
          </label>
          <label className={styles.field}>
            <span>Website</span>
            <input
              value={profileForm.website}
              onChange={(e) => setProfileForm((f) => ({ ...f, website: e.target.value }))}
              placeholder="https://"
              maxLength={300}
            />
          </label>
          <div className={styles.actions}>
            <button type="button" className={styles.primary} onClick={saveProfile} disabled={busy || !profileForm.name.trim()}>
              Save
            </button>
            <button type="button" className={styles.ghost} onClick={() => setEditingProfile(false)} disabled={busy}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className={styles.identity}>
          <p className={styles.brokerageName}>{brokerage.name}</p>
          <p className={styles.muted}>
            {ORG_TYPE_LABELS[brokerage.org_type] || brokerage.org_type}
            {brokerage.primary_state ? ` · ${brokerage.primary_state}` : ''}
          </p>
          <button type="button" className={styles.ghost} onClick={() => setEditingProfile(true)}>
            {brokerage.profile_completed_at ? 'Edit business details' : 'Add business details'}
          </button>
        </div>
      )}

      {/* ── Capabilities ─────────────────────────────────────────────── */}
      <ul className={styles.capabilities}>
        {Object.entries(capabilities).map(([name, state]) => (
          <CapabilityRow key={name} name={name} state={state} optional={optionalSet.has(name)} />
        ))}
      </ul>

      {next ? (
        <p className={styles.next}>
          Next: <strong>{CAPABILITY_LABELS[next] || next}</strong>
        </p>
      ) : (
        <p className={styles.next}>Everything Neoh needs is set up.</p>
      )}

      {/* ── Team ─────────────────────────────────────────────────────── */}
      <div className={styles.block}>
        <h3 className={styles.subtitle}>
          Team
          <span className={styles.muted}>
            {' '}{teamCounts.active_members} active
            {teamCounts.pending_invitations ? ` · ${teamCounts.pending_invitations} pending` : ''}
          </span>
        </h3>

        <ul className={styles.roster}>
          {(team?.members || []).map((m) => (
            <li key={m.id} className={styles.member}>
              <span className={styles.memberName}>{m.full_name || m.email}</span>
              <span className={styles.memberRole}>{m.role === 'broker_owner' ? 'Owner' : 'Agent'}</span>
              <span className={styles.memberState} data-state={m.status}>{m.status === 'active' ? 'Active' : 'Suspended'}</span>
            </li>
          ))}
          {(team?.pending_invitations || []).map((inv) => (
            <li key={inv.id} className={styles.member} data-pending="true">
              <span className={styles.memberName}>{inv.email}</span>
              <span className={styles.memberRole}>{inv.role === 'broker_owner' ? 'Owner' : 'Agent'}</span>
              <span className={styles.memberState} data-state="pending">Pending</span>
              <span className={styles.rowActions}>
                <button
                  type="button" className={styles.link} disabled={busy}
                  onClick={() => act(() => crmPost(`/api/brokerage/invitations/${inv.id}/resend`, {}), 'Invitation resent.')}
                >
                  Resend
                </button>
                <button
                  type="button" className={styles.link} disabled={busy}
                  onClick={() => act(() => crmDelete(`/api/brokerage/invitations/${inv.id}`), 'Invitation revoked.')}
                >
                  Revoke
                </button>
              </span>
            </li>
          ))}
        </ul>

        <label className={styles.field}>
          <span>Invite your team</span>
          <textarea
            className={styles.invites}
            rows={3}
            value={inviteText}
            onChange={(e) => setInviteText(e.target.value)}
            placeholder={'john@example.com\nsarah@example.com\nmike@example.com'}
            aria-label="Email addresses to invite"
          />
        </label>
        <div className={styles.actions}>
          <button type="button" className={styles.primary} onClick={sendInvites} disabled={busy || !pendingEmails.length}>
            {pendingEmails.length > 1
              ? `Send ${pendingEmails.length} invitations`
              : 'Send invitation'}
          </button>
        </div>
      </div>
    </section>
  );
}

export default BrokerageSetupPanel;
