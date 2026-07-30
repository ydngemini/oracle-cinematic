import { useEffect, useMemo, useRef, useState } from 'react';
import { Bot, Check, ShieldCheck, X } from 'lucide-react';
import { crmPost } from '../state/useCrmApi';
import { useAssistant } from './AssistantContext';
import { AgentStatusBar } from './AgentStatusBar';
import { BorderBeam } from './motion/BorderBeam';
import styles from './PersonalCommandComposer.module.css';

const PLACEHOLDER =
  "Tell your assistant what to do (e.g., 'Email John and offer $185k' or 'Schedule call with Sarah')...";

function requestId() {
  return globalThis.crypto?.randomUUID?.() || `command-${Date.now()}`;
}

function errorText(error) {
  const detail = error?.detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => item.msg).join(', ');
  }
  if (detail && typeof detail === 'object') {
    if (Array.isArray(detail.missing_variables)) {
      return `Missing required record data: ${detail.missing_variables.join(', ')}`;
    }
    return detail.message || detail.code || 'The assistant could not prepare that action.';
  }
  return error?.message || detail || 'The assistant could not prepare that action.';
}

export function PersonalCommandComposer({
  clientId = null,
  propertyId = null,
  initialText = '',
  compact = false,
}) {
  const {
    commandRequest,
    clearCommandRequest,
    setCommandStatus,
  } = useAssistant();
  const [rawText, setRawText] = useState(initialText);
  const [proposal, setProposal] = useState(null);
  const [draftText, setDraftText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const authorizeRef = useRef(null);
  const inputRef = useRef(null);
  const modalRef = useRef(null);

  const resolvedClientId = commandRequest?.clientId || clientId;
  const resolvedPropertyId = commandRequest?.propertyId || propertyId;

  useEffect(() => {
    if (!commandRequest) return undefined;
    let active = true;
    queueMicrotask(() => {
      if (!active) return;
      setRawText(commandRequest.rawText || '');
      inputRef.current?.focus();
      clearCommandRequest();
    });
    return () => { active = false; };
  }, [clearCommandRequest, commandRequest]);

  useEffect(() => {
    if (!proposal) return undefined;
    const previous = document.activeElement;
    window.requestAnimationFrame(() => authorizeRef.current?.focus());
    const onKey = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        setProposal(null);
        setCommandStatus({ state: 'cancelled', message: 'Draft cancelled', detail: '' });
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = [...(modalRef.current?.querySelectorAll(
        'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ) ?? [])];
      if (focusable.length === 0) {
        event.preventDefault();
        modalRef.current?.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      if (previous instanceof HTMLElement) previous.focus();
    };
  }, [proposal, setCommandStatus]);

  const analyze = async () => {
    const command = rawText.trim();
    if (!command || busy) return;
    const idempotencyKey = `assistant:${requestId()}`;
    setBusy(true);
    setError('');
    setCommandStatus({
      state: 'analyzing',
      message: 'Assistant is analyzing intent and preparing draft…',
      detail: '',
    });
    try {
      const result = await crmPost('/api/commands/parse', {
        raw_text: command,
        client_id: resolvedClientId || undefined,
        property_id: resolvedPropertyId || undefined,
        idempotency_key: idempotencyKey,
      });
      if (result.requires_approval) {
        setProposal({ ...result, idempotency_key: idempotencyKey });
        setDraftText(JSON.stringify(result.draft_payload || {}, null, 2));
        setCommandStatus({
          state: 'awaiting_approval',
          message: 'Draft ready for your review',
          detail: result.intent,
        });
      } else {
        setProposal({ ...result, readOnly: true });
        setDraftText(JSON.stringify(result.draft_payload || {}, null, 2));
        setCommandStatus({
          state: 'completed',
          message: 'Assistant analysis complete',
          detail: result.intent,
        });
      }
    } catch (reason) {
      const message = errorText(reason);
      setError(message);
      setCommandStatus({ state: 'failed', message: 'Assistant could not prepare the draft', detail: message });
    } finally {
      setBusy(false);
    }
  };

  const cancel = () => {
    setProposal(null);
    setDraftText('');
    setCommandStatus({ state: 'cancelled', message: 'Draft cancelled', detail: 'Nothing was dispatched' });
  };

  const authorize = async () => {
    if (!proposal || busy || proposal.readOnly) {
      setProposal(null);
      return;
    }
    let draftPayload;
    try {
      draftPayload = JSON.parse(draftText);
    } catch {
      setError('Draft payload must be valid JSON before authorization.');
      return;
    }
    setBusy(true);
    setError('');
    setCommandStatus({ state: 'authorizing', message: 'Authorizing staged action…', detail: proposal.intent });
    try {
      const result = await crmPost('/api/commands/execute', {
        command_id: proposal.command_id || undefined,
        intent: proposal.intent,
        target: proposal.target,
        draft_payload: draftPayload,
        context: {
          client_id: proposal.target_client_id,
          property_id: proposal.target_property_id,
        },
        idempotency_key: proposal.idempotency_key,
        reason: 'Authorized after reviewing and editing the staged Personal AI draft.',
      });
      setProposal(null);
      setDraftText('');
      setRawText('');
      const completed = result?.status === 'ENCRYPTED_IN_VAULT';
      setCommandStatus({
        state: completed ? 'completed' : 'queued',
        message: completed ? 'Authorized document secured in the vault' : 'Authorized action queued',
        detail: result?.command?.command_type || proposal.intent,
      });
    } catch (reason) {
      const message = errorText(reason);
      setError(message);
      setCommandStatus({ state: 'failed', message: 'Authorization failed', detail: message });
    } finally {
      setBusy(false);
    }
  };

  const targetLabel = useMemo(() => {
    if (!proposal) return '';
    return proposal.extracted_name
      || proposal.target?.email
      || proposal.target?.phone
      || proposal.target_property_id
      || 'No recipient resolved';
  }, [proposal]);

  return (
    <section className={`${styles.wrap} ${compact ? styles.compact : ''}`} aria-label="Personal AI command">
      <AgentStatusBar />
      <div className={`${styles.commandBox} hud-glass-panel hud-reticle`}>
        {busy && <BorderBeam duration={4} size={250} />}
        <div className={styles.heading}>
          <span className={styles.botMark}><Bot aria-hidden="true" /></span>
          <div>
            <h2>Personal AI Command</h2>
            <p>Draft first. Nothing leaves NEOH without your authorization.</p>
          </div>
        </div>
        <label className={styles.inputLabel}>
          <span>Command</span>
          <textarea
            ref={inputRef}
            value={rawText}
            onChange={(event) => {
              setRawText(event.target.value.slice(0, 4_000));
              if (error) setError('');
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                analyze();
              }
            }}
            placeholder={PLACEHOLDER}
            rows={compact ? 3 : 4}
            maxLength={4_000}
          />
        </label>
        <div className={styles.commandActions}>
          <span>{rawText.length.toLocaleString()} / 4,000</span>
          <button type="button" onClick={analyze} disabled={busy || !rawText.trim()}>
            <Bot aria-hidden="true" />
            {busy ? 'Preparing…' : 'Prepare Draft'}
          </button>
        </div>
        {error && <p className={styles.error} role="alert">{error}</p>}
      </div>

      {proposal && (
        <div className={styles.modalLayer}>
          <button type="button" className={styles.scrim} onClick={cancel} tabIndex={-1} aria-label="Cancel staged action" />
          <div
            ref={modalRef}
            className={`${styles.modal} hud-glass-panel hud-reticle`}
            role="dialog"
            aria-modal="true"
            aria-labelledby="hitl-title"
            tabIndex={-1}
          >
            <BorderBeam duration={4} size={250} />
            <header>
              <div>
                <span className={styles.intentBadge}>{proposal.intent}</span>
                <h2 id="hitl-title">{proposal.readOnly ? 'Calculation result' : 'Review staged action'}</h2>
              </div>
              <button type="button" className={styles.close} onClick={cancel} aria-label="Close review">
                <X aria-hidden="true" />
              </button>
            </header>
            <dl className={styles.target}>
              <div><dt>Target</dt><dd>{targetLabel}</dd></div>
              {proposal.target_property_id && <div><dt>Property</dt><dd>{proposal.target_property_id}</dd></div>}
              <div><dt>Confidence</dt><dd>{Math.round(Number(proposal.confidence || 0) * 100)}%</dd></div>
            </dl>
            {proposal.missing_fields?.length > 0 && (
              <p className={styles.warning} role="alert">
                Complete these fields before dispatch: {proposal.missing_fields.join(', ')}
              </p>
            )}
            <label className={styles.draftLabel}>
              <span>{proposal.readOnly ? 'Structured result' : 'Editable draft payload'}</span>
              <textarea
                value={draftText}
                onChange={(event) => setDraftText(event.target.value)}
                readOnly={proposal.readOnly}
                rows={12}
                spellCheck={false}
              />
            </label>
            <footer>
              <button type="button" className={styles.cancel} onClick={cancel}>
                {proposal.readOnly ? 'Close' : 'Cancel'}
              </button>
              {!proposal.readOnly && (
                <button
                  ref={authorizeRef}
                  type="button"
                  className={styles.authorize}
                  onClick={authorize}
                  disabled={busy || proposal.missing_fields?.length > 0}
                >
                  <ShieldCheck aria-hidden="true" />
                  {busy ? 'Authorizing…' : 'Authorize & Dispatch'}
                </button>
              )}
              {proposal.readOnly && <Check aria-hidden="true" className={styles.completeIcon} />}
            </footer>
          </div>
        </div>
      )}
    </section>
  );
}
