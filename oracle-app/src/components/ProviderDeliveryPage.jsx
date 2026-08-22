import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  CalendarDays,
  CheckCircle2,
  Cloud,
  KeyRound,
  Mail,
  MessageSquareText,
  PhoneCall,
  PlugZap,
  RefreshCw,
  Save,
  ShieldCheck,
  Trash2,
  XCircle,
} from 'lucide-react';
import { crmDelete, crmGet, crmPost, crmPut } from '../state/useCrmApi';
import styles from './SalesWorkspace.module.css';

const EMPTY_TWILIO = {
  account_label: 'default', account_sid: '', auth_token: '', api_key: '', api_secret: '',
  from_number: '', twiml_app_sid: '', sms_sender: '', sms_sender_type: '',
};
const EMPTY_ACS = { account_label: 'default', connection_string: '', from_number: '', sms_sender: '' };
const EMPTY_SES = {
  account_label: 'default', from_email: '', region: 'us-east-2',
  aws_access_key_id: '', aws_secret_access_key: '', aws_session_token: '',
};
const EMPTY_ROUTE = {
  inbound_did: '', twilio_account_sid: '', intake_mode: 'auto', forwarding_mode: 'none',
  forwarding_source_e164: '', sip_domain: '', voice_caller_id_e164: '',
  voice_caller_id_verified: false, sms_sender_e164: '', sms_sender_type: '', active: true,
  agent_forward_e164: '', forward_on_request: true, forward_when_ai_unavailable: true,
  forward_timeout_seconds: 25,
};

function errorText(error) {
  const detail = error?.payload?.detail;
  if (typeof detail === 'string') return detail;
  if (detail?.message) return detail.message;
  return error?.message || 'The provider action could not be completed.';
}

function compact(payload) {
  return Object.fromEntries(Object.entries(payload).filter(([, value]) => value !== ''));
}

function ProviderState({ provider }) {
  const valid = provider?.configured && provider?.validation_status === 'valid';
  const googleValid = provider?.provider === 'google' && provider?.configured;
  const tone = valid || googleValid ? 'good' : provider?.validation_status === 'invalid' ? 'bad' : 'warn';
  return <span className={styles.badge} data-tone={tone}>{valid || googleValid ? 'connected' : String(provider?.validation_status || 'setup required').replaceAll('_', ' ')}</span>;
}

export default function ProviderDeliveryPage() {
  const [providers, setProviders] = useState([]);
  const [channels, setChannels] = useState({});
  const [route, setRoute] = useState(null);
  const [twilio, setTwilio] = useState(EMPTY_TWILIO);
  const [acs, setAcs] = useState(EMPTY_ACS);
  const [ses, setSes] = useState(EMPTY_SES);
  const [routeForm, setRouteForm] = useState(EMPTY_ROUTE);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const response = await crmGet('/api/sales/providers', { retries: 0 });
      setProviders(response?.providers || []);
      setChannels(response?.channels || {});
      setRoute(response?.route || null);
      if (response?.route && Object.keys(response.route).length) {
        const current = response.route;
        setRouteForm((prior) => ({
          ...prior,
          inbound_did: current.inbound_did || prior.inbound_did,
          twilio_account_sid: current.twilio_account_sid || prior.twilio_account_sid,
          intake_mode: current.intake_mode || 'auto',
          forwarding_mode: current.forwarding_mode || 'none',
          forwarding_source_e164: current.forwarding_source_e164 || '',
          sip_domain: current.sip_domain || '',
          voice_caller_id_e164: current.voice_caller_id_e164 || '',
          voice_caller_id_verified: Boolean(current.voice_caller_id_verified),
          sms_sender_e164: current.sms_sender_e164 || '',
          sms_sender_type: current.sms_sender_type || '',
          active: current.active !== false,
          agent_forward_e164: current.agent_forward_e164 || '',
          forward_on_request: Boolean(current.agent_forward_e164) && current.forward_on_request !== false,
          forward_when_ai_unavailable:
            Boolean(current.agent_forward_e164) && current.forward_when_ai_unavailable !== false,
          forward_timeout_seconds: Number(current.forward_timeout_seconds) || 25,
        }));
      }
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

  const byName = useMemo(
    () => Object.fromEntries(providers.map((provider) => [provider.provider, provider])),
    [providers],
  );

  const configure = useCallback(async (provider, values, reset) => {
    setWorking(`configure:${provider}`); setError(''); setMessage('');
    try {
      await crmPut(`/api/sales/providers/${provider}`, compact(values));
      reset();
      setMessage(`${provider.toUpperCase()} credentials encrypted and stored. Validate them before delivery can be connected.`);
      await load();
    } catch (configureError) {
      setError(errorText(configureError));
    } finally {
      setWorking('');
    }
  }, [load]);

  const validate = useCallback(async (provider) => {
    const accountLabel = byName[provider]?.account_label || 'default';
    setWorking(`validate:${provider}`); setError(''); setMessage('');
    try {
      const result = await crmPost(`/api/sales/providers/${provider}/${encodeURIComponent(accountLabel)}/validate`, {});
      if (result.validation_status === 'valid') {
        setMessage(`${provider.toUpperCase()} validated. Enabled capabilities: ${Object.entries(result.capabilities || {}).filter(([, ready]) => ready).map(([name]) => name).join(', ') || 'none'}.`);
      } else {
        setError(result.error || `${provider.toUpperCase()} validation failed.`);
      }
      await load();
    } catch (validateError) {
      setError(errorText(validateError));
    } finally {
      setWorking('');
    }
  }, [byName, load]);

  const disconnect = useCallback(async (provider) => {
    const accountLabel = byName[provider]?.account_label || 'default';
    if (!window.confirm(`Disconnect ${provider.toUpperCase()} account “${accountLabel}”?`)) return;
    setWorking(`disconnect:${provider}`); setError(''); setMessage('');
    try {
      await crmDelete(`/api/sales/providers/${provider}/${encodeURIComponent(accountLabel)}`);
      setMessage(`${provider.toUpperCase()} disconnected. Stored credentials are disabled and cannot be used for delivery.`);
      await load();
    } catch (disconnectError) {
      setError(errorText(disconnectError));
    } finally {
      setWorking('');
    }
  }, [byName, load]);

  const connectGoogle = useCallback(async () => {
    setWorking('google'); setError(''); setMessage('');
    try {
      const returnPath = `${window.location.pathname}${window.location.search}${window.location.hash}`;
      const result = await crmPost('/api/commands/providers/google/oauth/start', { return_path: returnPath });
      window.location.assign(result.authorization_url);
    } catch (googleError) {
      setError(errorText(googleError));
      setWorking('');
    }
  }, []);

  const saveRoute = useCallback(async () => {
    setWorking('route'); setError(''); setMessage('');
    try {
      const payload = compact(routeForm);
      if (!payload.sms_sender_e164) delete payload.sms_sender_type;
      if (!payload.forwarding_source_e164) delete payload.forwarding_source_e164;
      if (!payload.sip_domain) delete payload.sip_domain;
      if (!payload.voice_caller_id_e164) payload.voice_caller_id_verified = false;
      // No number to hand off to means hand-off is off, not a validation error.
      if (!payload.agent_forward_e164) {
        payload.forward_on_request = false;
        payload.forward_when_ai_unavailable = false;
      }
      const response = await crmPut('/api/telephony/routes/me', payload);
      setRoute(response);
      setMessage('Telephony route saved. Browser calling is ready only when provider validation and verified caller-ID requirements both pass.');
      await load();
    } catch (routeError) {
      setError(errorText(routeError));
    } finally {
      setWorking('');
    }
  }, [load, routeForm]);

  const providerActions = (provider) => (
    <div className={styles.buttonRow}>
      <button type="button" className={styles.secondaryButton} onClick={() => validate(provider)} disabled={Boolean(working) || !byName[provider]?.account_label}><ShieldCheck aria-hidden="true" /> Validate</button>
      <button type="button" className={styles.dangerButton} onClick={() => disconnect(provider)} disabled={Boolean(working) || !byName[provider]?.account_label}><Trash2 aria-hidden="true" /> Disconnect</button>
    </div>
  );

  return (
    <div className={styles.page}>
      <div className={styles.pageIntro}>
        <div>
          <h3>Tenant-scoped delivery truth</h3>
          <p>Connect email, SMS, and voice with structured provider setup. A channel is shown as connected only after credentials validate and its required sender or route is present.</p>
        </div>
        <button type="button" className={styles.secondaryButton} onClick={load} disabled={loading || Boolean(working)}><RefreshCw aria-hidden="true" /> Refresh</button>
      </div>

      <div className={styles.notice}>
        <KeyRound aria-hidden="true" />
        <span>Secret fields are write-only: values are encrypted server-side, never returned, and never prefilled here. Provider validation is read-only and does not place a call or send a message.</span>
      </div>
      {error ? <div className={styles.error} role="alert"><XCircle aria-hidden="true" /> {error}</div> : null}
      {message ? <div className={styles.success} role="status"><CheckCircle2 aria-hidden="true" /> {message}</div> : null}

      <div className={styles.metricGrid}>
        <div className={styles.metricCard}><span>Email</span><strong>{channels.email ? 'Ready' : 'Setup'}</strong><small>Google or SES</small></div>
        <div className={styles.metricCard}><span>SMS</span><strong>{channels.sms ? 'Ready' : 'Setup'}</strong><small>registered sender</small></div>
        <div className={styles.metricCard}><span>AI voice</span><strong>{channels.ai_call ? 'Ready' : 'Setup'}</strong><small>approval delivery route</small></div>
        <div className={styles.metricCard}><span>Agent voice</span><strong>{channels.agent_call ? 'Ready' : 'Setup'}</strong><small>browser + verified caller ID</small></div>
      </div>

      <section className={styles.providerGrid} aria-label="Delivery providers">
        <article className={styles.panel}>
          <header className={styles.panelHeader}><div><h4>Google Workspace</h4><p>OAuth email and calendar</p></div><ProviderState provider={byName.google || { provider: 'google' }} /></header>
          <div className={styles.panelBody}>
            <div className={styles.providerIcon}><CalendarDays aria-hidden="true" /><span><strong>Google OAuth</strong><small>Oracle never receives your Google password.</small></span></div>
            <button type="button" className={styles.primaryButton} onClick={connectGoogle} disabled={Boolean(working)}><PlugZap aria-hidden="true" /> {byName.google?.configured ? 'Reconnect Google' : 'Connect Google'}</button>
            {byName.google?.configured ? <button type="button" className={styles.dangerButton} onClick={() => disconnect('google')} disabled={Boolean(working)}><Trash2 aria-hidden="true" /> Disconnect</button> : null}
          </div>
        </article>

        <article className={styles.panel}>
          <header className={styles.panelHeader}><div><h4>Twilio</h4><p>Browser voice, AI voice, and SMS</p></div><ProviderState provider={byName.twilio || { provider: 'twilio' }} /></header>
          <form className={styles.panelBody} onSubmit={(event) => { event.preventDefault(); void configure('twilio', twilio, () => setTwilio(EMPTY_TWILIO)); }} autoComplete="off">
            <div className={styles.fieldGrid}>
              <div className={styles.field}><label htmlFor="twilio-account-label">Account label</label><input id="twilio-account-label" value={twilio.account_label} onChange={(event) => setTwilio((current) => ({ ...current, account_label: event.target.value }))} required /></div>
              <div className={styles.field}><label htmlFor="twilio-account-sid">Account SID</label><input id="twilio-account-sid" value={twilio.account_sid} onChange={(event) => setTwilio((current) => ({ ...current, account_sid: event.target.value }))} placeholder="AC…" minLength={34} maxLength={34} required /></div>
              <div className={styles.field}><label htmlFor="twilio-auth-token">Auth token</label><input id="twilio-auth-token" type="password" value={twilio.auth_token} onChange={(event) => setTwilio((current) => ({ ...current, auth_token: event.target.value }))} autoComplete="new-password" required /></div>
              <div className={styles.field}><label htmlFor="twilio-api-key">API key SID</label><input id="twilio-api-key" value={twilio.api_key} onChange={(event) => setTwilio((current) => ({ ...current, api_key: event.target.value }))} placeholder="SK…" minLength={34} maxLength={34} required /></div>
              <div className={styles.field}><label htmlFor="twilio-api-secret">API key secret</label><input id="twilio-api-secret" type="password" value={twilio.api_secret} onChange={(event) => setTwilio((current) => ({ ...current, api_secret: event.target.value }))} autoComplete="new-password" required /></div>
              <div className={styles.field}><label htmlFor="twilio-app-sid">TwiML App SID</label><input id="twilio-app-sid" value={twilio.twiml_app_sid} onChange={(event) => setTwilio((current) => ({ ...current, twiml_app_sid: event.target.value }))} placeholder="AP…" minLength={34} maxLength={34} required /></div>
              <div className={styles.field}><label htmlFor="twilio-from">Voice caller ID</label><input id="twilio-from" type="tel" value={twilio.from_number} onChange={(event) => setTwilio((current) => ({ ...current, from_number: event.target.value }))} placeholder="+15551234567" required /></div>
              <div className={styles.field}><label htmlFor="twilio-sms">SMS sender (optional)</label><input id="twilio-sms" type="tel" value={twilio.sms_sender} onChange={(event) => setTwilio((current) => ({ ...current, sms_sender: event.target.value }))} placeholder="+15551234567" /></div>
              <div className={styles.field}><label htmlFor="twilio-sms-type">SMS registration type</label><select id="twilio-sms-type" value={twilio.sms_sender_type} onChange={(event) => setTwilio((current) => ({ ...current, sms_sender_type: event.target.value }))}><option value="">No SMS sender</option><option value="twilio_registered">Twilio registered</option><option value="ported">Ported</option><option value="toll_free_verified">Toll-free verified</option></select></div>
            </div>
            <button type="submit" className={styles.primaryButton} disabled={Boolean(working)}><Save aria-hidden="true" /> Encrypt and save</button>
            {providerActions('twilio')}
            {byName.twilio?.validation_error ? <p className={styles.inlineError}>{byName.twilio.validation_error}</p> : null}
          </form>
        </article>

        <article className={styles.panel}>
          <header className={styles.panelHeader}><div><h4>Azure Communication Services</h4><p>Alternative voice and SMS</p></div><ProviderState provider={byName.acs || { provider: 'acs' }} /></header>
          <form className={styles.panelBody} onSubmit={(event) => { event.preventDefault(); void configure('acs', acs, () => setAcs(EMPTY_ACS)); }} autoComplete="off">
            <div className={styles.field}><label htmlFor="acs-account-label">Account label</label><input id="acs-account-label" value={acs.account_label} onChange={(event) => setAcs((current) => ({ ...current, account_label: event.target.value }))} required /></div>
            <div className={styles.field}><label htmlFor="acs-connection">Connection string</label><input id="acs-connection" type="password" value={acs.connection_string} onChange={(event) => setAcs((current) => ({ ...current, connection_string: event.target.value }))} autoComplete="new-password" placeholder="endpoint=https://…;accesskey=…" required /></div>
            <div className={styles.fieldGrid}>
              <div className={styles.field}><label htmlFor="acs-from">Voice caller ID (optional)</label><input id="acs-from" type="tel" value={acs.from_number} onChange={(event) => setAcs((current) => ({ ...current, from_number: event.target.value }))} placeholder="+15551234567" /></div>
              <div className={styles.field}><label htmlFor="acs-sms">SMS sender (optional)</label><input id="acs-sms" type="tel" value={acs.sms_sender} onChange={(event) => setAcs((current) => ({ ...current, sms_sender: event.target.value }))} placeholder="+15551234567" /></div>
            </div>
            <button type="submit" className={styles.primaryButton} disabled={Boolean(working)}><Cloud aria-hidden="true" /> Encrypt and save</button>
            {providerActions('acs')}
            {byName.acs?.validation_error ? <p className={styles.inlineError}>{byName.acs.validation_error}</p> : null}
          </form>
        </article>

        <article className={styles.panel}>
          <header className={styles.panelHeader}><div><h4>Amazon SES</h4><p>Tenant sender email</p></div><ProviderState provider={byName.ses || { provider: 'ses' }} /></header>
          <form className={styles.panelBody} onSubmit={(event) => { event.preventDefault(); void configure('ses', ses, () => setSes(EMPTY_SES)); }} autoComplete="off">
            <div className={styles.field}><label htmlFor="ses-account-label">Account label</label><input id="ses-account-label" value={ses.account_label} onChange={(event) => setSes((current) => ({ ...current, account_label: event.target.value }))} required /></div>
            <div className={styles.fieldGrid}>
              <div className={styles.field}><label htmlFor="ses-from">Verified from email</label><input id="ses-from" type="email" value={ses.from_email} onChange={(event) => setSes((current) => ({ ...current, from_email: event.target.value }))} required /></div>
              <div className={styles.field}><label htmlFor="ses-region">AWS region</label><input id="ses-region" value={ses.region} onChange={(event) => setSes((current) => ({ ...current, region: event.target.value }))} required /></div>
            </div>
            <div className={styles.fieldGrid}>
              <div className={styles.field}><label htmlFor="ses-access-key">AWS access key ID (optional)</label><input id="ses-access-key" type="password" value={ses.aws_access_key_id} onChange={(event) => setSes((current) => ({ ...current, aws_access_key_id: event.target.value }))} autoComplete="new-password" /><small>Leave all AWS key fields blank to use the platform’s workload identity.</small></div>
              <div className={styles.field}><label htmlFor="ses-secret-key">AWS secret access key</label><input id="ses-secret-key" type="password" value={ses.aws_secret_access_key} onChange={(event) => setSes((current) => ({ ...current, aws_secret_access_key: event.target.value }))} autoComplete="new-password" /></div>
            </div>
            <div className={styles.field}><label htmlFor="ses-session-token">AWS session token (optional)</label><input id="ses-session-token" type="password" value={ses.aws_session_token} onChange={(event) => setSes((current) => ({ ...current, aws_session_token: event.target.value }))} autoComplete="new-password" /></div>
            <button type="submit" className={styles.primaryButton} disabled={Boolean(working)}><Mail aria-hidden="true" /> Encrypt and save</button>
            {providerActions('ses')}
            {byName.ses?.validation_error ? <p className={styles.inlineError}>{byName.ses.validation_error}</p> : null}
          </form>
        </article>
      </section>

      <section className={styles.panel} aria-labelledby="provider-route-title">
        <header className={styles.panelHeader}>
          <div><h4 id="provider-route-title">Verified telephony route</h4><p>Inbound number, browser caller ID, and registered SMS sender</p></div>
          <ProviderState provider={{ provider: 'route', configured: Boolean(route?.active && route?.voice_caller_id_verified), validation_status: route?.active ? 'valid' : 'unverified' }} />
        </header>
        <form className={styles.panelBody} onSubmit={(event) => { event.preventDefault(); void saveRoute(); }}>
          <div className={styles.fieldGridWide}>
            <div className={styles.field}><label htmlFor="route-inbound">Inbound Twilio DID</label><input id="route-inbound" type="tel" value={routeForm.inbound_did} onChange={(event) => setRouteForm((current) => ({ ...current, inbound_did: event.target.value }))} placeholder="+15551234567" required /></div>
            <div className={styles.field}><label htmlFor="route-account">Twilio Account SID</label><input id="route-account" value={routeForm.twilio_account_sid} onChange={(event) => setRouteForm((current) => ({ ...current, twilio_account_sid: event.target.value }))} placeholder="AC…" minLength={34} maxLength={34} required /></div>
            <div className={styles.field}><label htmlFor="route-caller-id">Outbound voice caller ID</label><input id="route-caller-id" type="tel" value={routeForm.voice_caller_id_e164} onChange={(event) => setRouteForm((current) => ({ ...current, voice_caller_id_e164: event.target.value }))} placeholder="+15551234567" /></div>
            <div className={styles.field}><label htmlFor="route-sms-sender">Registered SMS sender</label><input id="route-sms-sender" type="tel" value={routeForm.sms_sender_e164} onChange={(event) => setRouteForm((current) => ({ ...current, sms_sender_e164: event.target.value }))} placeholder="+15551234567" /></div>
            <div className={styles.field}><label htmlFor="route-sms-type">SMS sender type</label><select id="route-sms-type" value={routeForm.sms_sender_type} onChange={(event) => setRouteForm((current) => ({ ...current, sms_sender_type: event.target.value }))}><option value="">No SMS sender</option><option value="twilio_registered">Twilio registered</option><option value="ported">Ported</option><option value="toll_free_verified">Toll-free verified</option></select></div>
            <div className={styles.field}><label htmlFor="route-intake">Inbound intake mode</label><select id="route-intake" value={routeForm.intake_mode} onChange={(event) => setRouteForm((current) => ({ ...current, intake_mode: event.target.value }))}><option value="auto">Auto</option><option value="buyer">Buyer</option><option value="seller">Seller</option></select></div>
            <div className={styles.field}><label htmlFor="route-forwarding-mode">Keep your existing number</label><select id="route-forwarding-mode" value={routeForm.forwarding_mode} onChange={(event) => setRouteForm((current) => ({ ...current, forwarding_mode: event.target.value }))} aria-describedby="route-forwarding-mode-help"><option value="none">Off — clients call the Neoh number directly</option><option value="carrier_conditional">Carrier forwarding — clients keep calling my number</option><option value="sip">SIP trunk</option></select><small id="route-forwarding-mode-help">Carrier forwarding lets clients keep dialling the number on your signs and cards. You set the forward at your carrier; NEOH answers.</small></div>
            <div className={styles.field}><label htmlFor="route-forwarding-source">Your public number</label><input id="route-forwarding-source" type="tel" value={routeForm.forwarding_source_e164} onChange={(event) => setRouteForm((current) => ({ ...current, forwarding_source_e164: event.target.value }))} placeholder="+15551234567" disabled={routeForm.forwarding_mode !== 'carrier_conditional'} required={routeForm.forwarding_mode === 'carrier_conditional'} aria-describedby="route-forwarding-source-help" /><small id="route-forwarding-source-help">The number your clients already dial. Forward it to the Neoh DID above, then NEOH picks up.</small></div>
            <div className={styles.field}><label htmlFor="route-sip-domain">SIP domain</label><input id="route-sip-domain" value={routeForm.sip_domain} onChange={(event) => setRouteForm((current) => ({ ...current, sip_domain: event.target.value }))} placeholder="pbx.example.com" disabled={routeForm.forwarding_mode !== 'sip'} required={routeForm.forwarding_mode === 'sip'} aria-describedby="route-sip-domain-help" /><small id="route-sip-domain-help">Required for SIP trunking. Your PBX sends the call here instead of a carrier forward.</small></div>
            <div className={styles.field}><label htmlFor="route-forward">Your phone for live hand-off</label><input id="route-forward" type="tel" value={routeForm.agent_forward_e164} onChange={(event) => setRouteForm((current) => ({ ...current, agent_forward_e164: event.target.value }))} placeholder="+15551234567" aria-describedby="route-forward-help" /><small id="route-forward-help">Where NEOH transfers a live caller. Leave blank to disable hand-off.</small></div>
            <div className={styles.field}><label htmlFor="route-forward-timeout">Ring your phone for</label><input id="route-forward-timeout" type="number" min={5} max={120} step={1} value={routeForm.forward_timeout_seconds} onChange={(event) => setRouteForm((current) => ({ ...current, forward_timeout_seconds: Number(event.target.value) || 25 }))} disabled={!routeForm.agent_forward_e164} aria-describedby="route-forward-timeout-help" /><small id="route-forward-timeout-help">Seconds before the caller is told you will call back (5-120).</small></div>
          </div>
          <label className={styles.consentRow}><input type="checkbox" checked={routeForm.forward_on_request} disabled={!routeForm.agent_forward_e164} onChange={(event) => setRouteForm((current) => ({ ...current, forward_on_request: event.target.checked }))} /><span><strong>Transfer when the caller asks for a person</strong><small>NEOH says it is connecting them, then bridges the live call to your phone.</small></span></label>
          <label className={styles.consentRow}><input type="checkbox" checked={routeForm.forward_when_ai_unavailable} disabled={!routeForm.agent_forward_e164} onChange={(event) => setRouteForm((current) => ({ ...current, forward_when_ai_unavailable: event.target.checked }))} /><span><strong>Transfer when the assistant is unavailable</strong><small>Without this the caller is told to expect a callback and the call ends.</small></span></label>
          <label className={styles.consentRow}><input type="checkbox" checked={routeForm.voice_caller_id_verified} onChange={(event) => setRouteForm((current) => ({ ...current, voice_caller_id_verified: event.target.checked }))} /><span><strong>Caller ID is verified for this Twilio account</strong><small>I confirm the number is owned or verified in Twilio and may be used for outbound voice. Provider validation is still required.</small></span></label>
          <label className={styles.consentRow}><input type="checkbox" checked={routeForm.active} onChange={(event) => setRouteForm((current) => ({ ...current, active: event.target.checked }))} /><span><strong>Route active</strong><small>Inactive routes cannot receive calls or originate browser calls.</small></span></label>
          <button type="submit" className={styles.primaryButton} disabled={Boolean(working)}><Save aria-hidden="true" /> Save route</button>
          {route?.voice_webhook_url ? <div className={styles.endpointGrid}><div><PhoneCall aria-hidden="true" /><span><strong>Inbound voice webhook</strong><code>{route.voice_webhook_url}</code></span></div><div><MessageSquareText aria-hidden="true" /><span><strong>Status callback</strong><code>{route.status_callback_url}</code></span></div></div> : null}
        </form>
      </section>
    </div>
  );
}
