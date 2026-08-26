import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Bot,
  CheckCircle2,
  Headphones,
  History,
  Mic,
  MicOff,
  PhoneCall,
  PhoneOff,
  RefreshCw,
  Search,
  ShieldCheck,
} from 'lucide-react';
import { crmGet, crmPost } from '../state/useCrmApi';
// Live voice telemetry — the running transcript, listening state and
// negotiation signal for a call in progress. It was exported from the component
// barrel but rendered nowhere, so /api/voice/telemetry had no consumer and a
// dialer operator could place a call without ever seeing it transcribed.
import { LiveTranscript } from './LiveTranscript';
// The TCPA consent ledger. The send-time gate always enforced consent, but
// nothing could WRITE the consent an agent obtained on a call or honour a
// verbal opt-out — compliance you cannot record is compliance you cannot prove.
import OutreachConsentPanel from './OutreachConsentPanel';
import styles from './SalesWorkspace.module.css';

function errorText(error) {
  const detail = error?.payload?.detail;
  if (typeof detail === 'string') return detail;
  if (detail?.message) return detail.message;
  return error?.message || 'The dialer action could not be completed.';
}

function callLabel(call) {
  return String(call?.state || 'unknown').replaceAll('_', ' ');
}

export default function PowerDialerPage() {
  const [mode, setMode] = useState('agent');
  const [contacts, setContacts] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [query, setQuery] = useState('');
  const [history, setHistory] = useState([]);
  const [channels, setChannels] = useState({});
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [callState, setCallState] = useState('idle');
  const [muted, setMuted] = useState(false);
  const [script, setScript] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const deviceRef = useRef(null);
  const callRef = useRef(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [contactResponse, capabilityResponse, callResponse] = await Promise.all([
        crmGet('/api/crm/contacts?limit=200', { retries: 0 }),
        crmGet('/api/sales/capabilities', { retries: 0 }),
        crmGet('/api/telephony/agent/calls?limit=50', { retries: 0 }),
      ]);
      const nextContacts = contactResponse?.contacts || [];
      setContacts(nextContacts);
      setSelectedId((current) => (
        nextContacts.some((contact) => contact.id === current)
          ? current
          : nextContacts.find((contact) => contact.phone)?.id || nextContacts[0]?.id || null
      ));
      setChannels(capabilityResponse?.channels || {});
      setHistory(callResponse?.calls || []);
    } catch (loadError) {
      setError(errorText(loadError));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const initial = Promise.resolve().then(load);
    return () => { void initial; };
  }, [load]);

  useEffect(() => () => {
    try { callRef.current?.disconnect(); } catch { /* already disconnected */ }
    try { deviceRef.current?.destroy(); } catch { /* already destroyed */ }
    callRef.current = null;
    deviceRef.current = null;
  }, []);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return contacts;
    return contacts.filter((contact) => (
      [contact.full_name, contact.email, contact.phone]
        .some((value) => String(value || '').toLowerCase().includes(needle))
    ));
  }, [contacts, query]);

  const selected = useMemo(
    () => contacts.find((contact) => contact.id === selectedId) || null,
    [contacts, selectedId],
  );

  const endCall = useCallback(() => {
    const call = callRef.current;
    callRef.current = null;
    try { call?.disconnect(); } catch { /* connection already closed */ }
    setCallState('idle');
    setMuted(false);
    setMessage('Browser call ended.');
    void load();
  }, [load]);

  const startAgentCall = useCallback(async () => {
    if (!selected) return;
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      setError('Microphone calling requires a secure browser context (HTTPS or localhost).');
      return;
    }
    setWorking(true);
    setError('');
    setMessage('');
    setCallState('checking microphone');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((track) => track.stop());

      setCallState('checking permission');
      const prepared = await crmPost('/api/telephony/agent/calls/prepare', {
        contact_id: selected.id,
      });
      const access = await crmGet('/api/telephony/agent/token', { retries: 0 });
      const { Call, Device } = await import('@twilio/voice-sdk');

      try { deviceRef.current?.destroy(); } catch { /* replaced below */ }
      const device = new Device(access.token, {
        closeProtection: 'A browser call is active. Leave this page?',
        codecPreferences: [Call.Codec.Opus, Call.Codec.PCMU],
        enableImprovedSignalingErrorPrecision: true,
      });
      deviceRef.current = device;
      device.on('error', (deviceError) => {
        setError(deviceError?.message || 'The browser calling device reported an error.');
        setCallState('failed');
      });

      setCallState('connecting');
      const call = await device.connect({ params: { intent_id: prepared.intent_id } });
      callRef.current = call;
      const finishCall = (nextState, nextMessage = '', nextError = '') => {
        if (callRef.current !== call) return;
        callRef.current = null;
        setCallState(nextState);
        setMuted(false);
        if (nextMessage) setMessage(nextMessage);
        if (nextError) setError(nextError);
        void load();
      };
      call.on('ringing', () => setCallState('ringing'));
      call.on('accept', () => {
        setCallState('connected');
        setMessage(`Connected to ${selected.full_name}. Recording is off.`);
      });
      call.on('disconnect', () => finishCall('idle', 'Browser call ended.'));
      call.on('cancel', () => finishCall('idle', 'Browser call was cancelled.'));
      call.on('reject', () => finishCall('rejected', '', 'The call was rejected.'));
      call.on('error', (callError) => finishCall(
        'failed',
        '',
        callError?.message || 'The browser call failed.',
      ));
      if (prepared.warnings?.length) setMessage(prepared.warnings.join(' '));
    } catch (callError) {
      setCallState('idle');
      setError(errorText(callError));
    } finally {
      setWorking(false);
    }
  }, [load, selected]);

  const toggleMute = useCallback(() => {
    if (!callRef.current) return;
    const next = !muted;
    callRef.current.mute(next);
    setMuted(next);
  }, [muted]);

  const stageAiCall = useCallback(async () => {
    if (!selected || !script.trim()) return;
    setWorking(true);
    setError('');
    setMessage('');
    try {
      const response = await crmPost('/api/sales/agent/actions', {
        action: 'draft_call_script',
        contact_id: selected.id,
        body: script.trim(),
        stage_for_approval: true,
      });
      setMessage(response?.approval?.id
        ? `AI call staged as approval ${response.approval.id}. It has not been placed.`
        : 'AI call script staged for human approval. It has not been placed.');
    } catch (stageError) {
      setError(errorText(stageError));
    } finally {
      setWorking(false);
    }
  }, [script, selected]);

  const activeCall = ['connecting', 'ringing', 'connected'].includes(callState);

  return (
    <div className={styles.page}>
      <div className={styles.pageIntro}>
        <div>
          <h3>Human browser calling + approval-bound AI voice</h3>
          <p>Select a canonical CRM contact. Human calls use the browser microphone and a single-use server intent; AI calls remain drafts until an authorized person approves them.</p>
        </div>
        <button type="button" className={styles.secondaryButton} onClick={load} disabled={loading || working}>
          <RefreshCw aria-hidden="true" /> Refresh
        </button>
      </div>

      <div className={styles.notice}>
        <ShieldCheck aria-hidden="true" />
        <span>No destination number is sent from this dialer to Twilio. The server resolves the selected contact again, rechecks consent and calling hours, and keeps browser calls unrecorded.</span>
      </div>
      {error ? <div className={styles.error} role="alert"><PhoneOff aria-hidden="true" /> {error}</div> : null}
      {message ? <div className={styles.success} role="status"><CheckCircle2 aria-hidden="true" /> {message}</div> : null}

      <div className={styles.metricGrid}>
        <div className={styles.metricCard}><span>Agent calling</span><strong>{channels.agent_call ? 'Ready' : 'Setup'}</strong><small>verified Twilio route</small></div>
        <div className={styles.metricCard}><span>AI calling</span><strong>{channels.ai_call ? 'Ready' : 'Setup'}</strong><small>approval required</small></div>
        <div className={styles.metricCard}><span>Call state</span><strong className={styles.metricText}>{callState}</strong><small>recording disabled</small></div>
        <div className={styles.metricCard}><span>History</span><strong>{history.length}</strong><small>browser call intents</small></div>
      </div>

      <div className={styles.twoColumn}>
        <section className={styles.panel} aria-labelledby="dialer-contact-title">
          <header className={styles.panelHeader}>
            <div><h4 id="dialer-contact-title">Choose a contact</h4><p>Canonical contacts only</p></div>
            <Headphones aria-hidden="true" />
          </header>
          <div className={styles.panelBody}>
            <div className={styles.field}>
              <label htmlFor="dialer-search">Search contacts</label>
              <div className={styles.searchRow}>
                <input id="dialer-search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Name, email, or phone" />
                <button type="button" className={styles.iconButton} aria-label="Clear search" onClick={() => setQuery('')}><Search aria-hidden="true" /></button>
              </div>
            </div>
          </div>
          <div className={styles.scrollList}>
            {loading ? <div className={styles.empty}>Loading contacts…</div> : null}
            {!loading && filtered.length === 0 ? <div className={styles.empty}>No contacts match this search.</div> : null}
            <ul className={styles.itemList}>
              {filtered.map((contact) => (
                <li key={contact.id}>
                  <button type="button" className={styles.itemButton} aria-pressed={selectedId === contact.id} onClick={() => setSelectedId(contact.id)} disabled={activeCall}>
                    <span><strong>{contact.full_name}</strong><small>{contact.phone || 'Phone missing'} · {contact.state_code || 'State missing'}</small></span>
                    <span className={styles.itemMeta}>{contact.preferred_channel || 'no preference'}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </section>

        <div className={styles.stack}>
          <section className={styles.panel} aria-labelledby="dialer-console-title">
            <header className={styles.panelHeader}>
              <div><h4 id="dialer-console-title">{selected?.full_name || 'Dialer console'}</h4><p>{selected ? `${selected.phone || 'phone missing'} · ${selected.timezone || 'timezone missing'}` : 'Select a contact'}</p></div>
              <div className={styles.toggle} aria-label="Calling mode">
                <button type="button" aria-pressed={mode === 'agent'} onClick={() => setMode('agent')} disabled={activeCall}>Agent</button>
                <button type="button" aria-pressed={mode === 'ai'} onClick={() => setMode('ai')} disabled={activeCall}>AI</button>
              </div>
            </header>
            <div className={styles.panelBody}>
              {mode === 'agent' ? (
                <>
                  <div className={styles.resultCard}>
                    <h5>Human browser call</h5>
                    <p>Your microphone carries your voice. This mode does not use an AI voice or recording. Contact consent and local calling hours are checked before Twilio receives permission to connect.</p>
                  </div>
                  <div className={styles.callControls}>
                    {!activeCall ? (
                      <button type="button" className={styles.primaryButton} onClick={startAgentCall} disabled={working || !selected?.phone || !channels.agent_call}>
                        <Mic aria-hidden="true" /> Check and call
                      </button>
                    ) : (
                      <>
                        <button type="button" className={styles.secondaryButton} onClick={toggleMute} disabled={callState !== 'connected'}>
                          {muted ? <MicOff aria-hidden="true" /> : <Mic aria-hidden="true" />} {muted ? 'Unmute' : 'Mute'}
                        </button>
                        <button type="button" className={styles.dangerButton} onClick={endCall}><PhoneOff aria-hidden="true" /> End call</button>
                      </>
                    )}
                  </div>
                </>
              ) : (
                <>
                  <div className={styles.resultCard}>
                    <h5>AI voice approval</h5>
                    <p>Write the script the approved AI call may use. The system will check written AI-voice consent, disclosure, recording, calling hours, state requirements, and provider readiness before delivery.</p>
                  </div>
                  <div className={styles.field}>
                    <label htmlFor="dialer-ai-script">Call script</label>
                    <textarea id="dialer-ai-script" value={script} onChange={(event) => setScript(event.target.value)} maxLength={20000} placeholder={`Draft an approved call script for ${selected?.full_name || 'the selected contact'}…`} />
                  </div>
                  <button type="button" className={styles.primaryButton} onClick={stageAiCall} disabled={working || !selected?.phone || !script.trim() || !channels.ai_call}>
                    <Bot aria-hidden="true" /> Stage for approval
                  </button>
                </>
              )}
            </div>
          </section>

          <section className={styles.panel} aria-labelledby="dialer-history-title">
            <header className={styles.panelHeader}>
              <div><h4 id="dialer-history-title">Recent browser calls</h4><p>Intent and provider state</p></div>
              <History aria-hidden="true" />
            </header>
            {history.length ? (
              <div className={styles.tableWrap}>
                <table className={styles.dataTable}>
                  <thead><tr><th>Contact</th><th>State</th><th>Created</th></tr></thead>
                  <tbody>
                    {history.map((call) => (
                      <tr key={call.id}>
                        <td>{contacts.find((contact) => contact.id === call.contact_id)?.full_name || 'Contact'}</td>
                        <td><span className={styles.badge} data-tone={call.state === 'completed' ? 'good' : call.state === 'failed' ? 'bad' : 'warn'}>{callLabel(call)}</span></td>
                        <td>{call.created_at ? new Date(call.created_at).toLocaleString() : '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <div className={styles.empty}><PhoneCall aria-hidden="true" /> No browser call history yet.</div>}
          </section>

          <OutreachConsentPanel />

          <section className={styles.panel} aria-labelledby="dialer-transcript-title">
            <header className={styles.panelHeader}>
              <h4 id="dialer-transcript-title">Live transcript</h4>
            </header>
            <div className={styles.panelBody}>
              <LiveTranscript />
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
