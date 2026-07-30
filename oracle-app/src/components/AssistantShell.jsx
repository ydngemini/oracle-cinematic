import { useCallback, useEffect, useRef, useState } from 'react';
import { ACTIONS, useOracleDispatch, useOracleState } from '../state';
import { crmDelete, crmDownload, crmGet, crmPost, crmUpload } from '../state/useCrmApi';
import { useAssistant } from './AssistantContext';
import { AssistantMessages } from './AssistantMessages';
import { AssistantRecordPicker } from './AssistantRecordPicker';
import styles from './AssistantShell.module.css';

const ACCEPT = '.pdf,.jpg,.jpeg,.png,.webp,application/pdf,image/jpeg,image/png,image/webp';

function Glyph({ name }) {
  const paths = {
    close: <><path d="M6 6l12 12M18 6 6 18" /></>,
    clip: <path d="m9 17 7.7-7.7a3 3 0 0 0-4.2-4.2l-8 8a5 5 0 0 0 7.1 7.1l7.6-7.6" />,
    mic: <><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3M9 21h6"/></>,
    send: <><path d="m4 4 17 8-17 8 3-8-3-8Z"/><path d="M7 12h14"/></>,
    record: <><circle cx="10" cy="8" r="3"/><path d="M4.5 19c.8-3.3 2.7-5 5.5-5s4.7 1.7 5.5 5M17 7h4M19 5v4"/></>,
    trash: <><path d="M5 7h14M9 7V4h6v3M7 7l1 13h8l1-13"/></>,
    download: <><path d="M12 3v12M7 10l5 5 5-5M5 20h14"/></>,
  };
  return <svg viewBox="0 0 24 24" aria-hidden="true">{paths[name]}</svg>;
}

function usePressToTalk(setDraft) {
  const [listening, setListening] = useState(false);
  const recognitionRef = useRef(null);
  const supported = typeof window !== 'undefined' && Boolean(window.SpeechRecognition || window.webkitSpeechRecognition);

  const stop = useCallback(() => {
    try { recognitionRef.current?.stop(); } catch { /* already stopped */ }
    setListening(false);
  }, []);

  const start = useCallback(() => {
    if (!supported || listening) return;
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    const recognition = new Recognition();
    recognition.continuous = true;
    recognition.interimResults = false;
    recognition.lang = 'en-US';
    recognition.onresult = (event) => {
      const words = Array.from(event.results)
        .slice(event.resultIndex)
        .filter((result) => result.isFinal)
        .map((result) => result[0].transcript.trim())
        .filter(Boolean)
        .join(' ');
      if (words) setDraft((current) => `${current}${current ? ' ' : ''}${words}`.slice(0, 8_000));
    };
    recognition.onerror = () => setListening(false);
    recognition.onend = () => setListening(false);
    recognitionRef.current = recognition;
    setListening(true);
    try { recognition.start(); } catch { setListening(false); }
  }, [listening, setDraft, supported]);

  useEffect(() => () => {
    try { recognitionRef.current?.abort(); } catch { /* no-op */ }
  }, []);
  return { listening, start, stop, supported };
}

export function AssistantShell() {
  const { open, setOpen, record, registerRecord, clearRecord } = useAssistant();
  const { aiChatMessages, aiChatRevision, aiChatConnection } = useOracleState();
  const { dispatch, wsRef } = useOracleDispatch();
  const [draft, setDraft] = useState('');
  const [pickerOpen, setPickerOpen] = useState(false);
  const [filesOpen, setFilesOpen] = useState(false);
  const [recordFiles, setRecordFiles] = useState([]);
  const [selectedIds, setSelectedIds] = useState([]);
  const [uploading, setUploading] = useState(false);
  const [notice, setNotice] = useState('');
  const [undoing, setUndoing] = useState('');
  const [featureAvailable, setFeatureAvailable] = useState(null);
  const launcherRef = useRef(null);
  const panelRef = useRef(null);
  const composerRef = useRef(null);
  const messagesRef = useRef(null);
  const inputRef = useRef(null);
  const pickerOpenRef = useRef(pickerOpen);
  const filesOpenRef = useRef(filesOpen);
  const voice = usePressToTalk(setDraft);
  const recordKey = record ? `${record.type}:${record.id}` : '';
  const [previousRecordKey, setPreviousRecordKey] = useState(recordKey);
  if (recordKey !== previousRecordKey) {
    setPreviousRecordKey(recordKey);
    setSelectedIds([]);
    setFilesOpen(false);
    setRecordFiles([]);
  }

  useEffect(() => { pickerOpenRef.current = pickerOpen; }, [pickerOpen]);
  useEffect(() => { filesOpenRef.current = filesOpen; }, [filesOpen]);

  useEffect(() => {
    let active = true;
    crmGet('/api/ai/chat/status').then(
      (data) => { if (active) setFeatureAvailable(data?.enabled === true); },
      () => { if (active) setFeatureAvailable(false); },
    );
    return () => { active = false; };
  }, []);

  useEffect(() => {
    let active = true;
    const timer = window.setTimeout(() => {
      if (featureAvailable !== true) return;
      crmGet('/api/ai/chat/messages?limit=80').then(
        (data) => {
          if (active) dispatch({ type: ACTIONS.AI_CHAT_HYDRATE, payload: data?.messages || [] });
        },
        () => { if (active && open) setNotice('Conversation history is temporarily unavailable.'); },
      );
    }, aiChatRevision ? 120 : 0);
    return () => { active = false; window.clearTimeout(timer); };
  }, [aiChatRevision, dispatch, featureAvailable, open]);

  useEffect(() => {
    if (!record) return undefined;
    let active = true;
    crmGet(`/api/ai/chat/attachments?record_type=${record.type}&record_id=${encodeURIComponent(record.id)}`).then(
      (data) => { if (active) setRecordFiles(data?.attachments || []); },
      () => { if (active) setRecordFiles([]); },
    );
    return () => { active = false; };
  }, [record]);

  useEffect(() => {
    if (!open) return undefined;
    const previous = document.activeElement;
    window.requestAnimationFrame(() => composerRef.current?.focus());
    const onKeyDown = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopImmediatePropagation();
        if (pickerOpenRef.current) setPickerOpen(false);
        else if (filesOpenRef.current) setFilesOpen(false);
        else setOpen(false);
        return;
      }
      if (event.key !== 'Tab' || !panelRef.current) return;
      const focusable = [...panelRef.current.querySelectorAll(
        'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), a[href]'
      )].filter((element) => element.offsetParent !== null);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    window.addEventListener('keydown', onKeyDown, true);
    return () => {
      window.removeEventListener('keydown', onKeyDown, true);
      if (previous instanceof HTMLElement) previous.focus();
    };
  }, [open, setOpen]);

  useEffect(() => {
    if (!open || !messagesRef.current) return;
    messagesRef.current.scrollTop = messagesRef.current.scrollHeight;
  }, [aiChatMessages, open]);

  const chooseRecord = (next) => {
    registerRecord(next, 'manual');
    setPickerOpen(false);
    setNotice('');
  };

  const toggleSelected = (id) => {
    setSelectedIds((current) => current.includes(id)
      ? current.filter((value) => value !== id)
      : current.length < 5 ? [...current, id] : current);
  };

  const upload = async (event) => {
    const files = [...(event.target.files || [])].slice(0, 5);
    event.target.value = '';
    if (!record) { setPickerOpen(true); return; }
    if (!files.length) return;
    setUploading(true);
    setNotice('');
    const form = new FormData();
    form.append('record_type', record.type);
    form.append('record_id', record.id);
    files.forEach((file) => form.append('files', file));
    try {
      const result = await crmUpload('/api/ai/chat/attachments', form);
      const saved = result?.attachments || [];
      setRecordFiles((current) => [...saved, ...current]);
      setSelectedIds((current) => [...new Set([...current, ...saved.map((file) => file.id)])].slice(0, 5));
      setFilesOpen(true);
    } catch (error) {
      setNotice(error.message || 'The files could not be attached.');
    } finally {
      setUploading(false);
    }
  };

  const removeFile = async (file) => {
    try {
      await crmDelete(`/api/ai/chat/attachments/${encodeURIComponent(file.id)}`);
      setRecordFiles((current) => current.filter((item) => item.id !== file.id));
      setSelectedIds((current) => current.filter((id) => id !== file.id));
    } catch (error) {
      setNotice(error.message || 'The attachment could not be removed.');
    }
  };

  const send = () => {
    const content = draft.trim();
    if (!content && selectedIds.length === 0) return;
    if (wsRef.current?.readyState !== WebSocket.OPEN) {
      setNotice('Reconnecting to the private channel. Try again in a moment.');
      return;
    }
    const requestId = crypto.randomUUID();
    const now = new Date().toISOString();
    const selectedFiles = recordFiles.filter((file) => selectedIds.includes(file.id));
    dispatch({
      type: ACTIONS.AI_CHAT_SEND_LOCAL,
      payload: {
        user: {
          id: `local-user-${requestId}`, request_id: requestId, role: 'user', content,
          status: 'completed', context: record, attachments: selectedFiles, created_at: now, local: true,
        },
        assistant: {
          id: `local-assistant-${requestId}`, request_id: requestId, role: 'assistant',
          content: '', status: 'pending', context: record, actions: [], created_at: now, local: true,
        },
      },
    });
    wsRef.current.send(JSON.stringify({
      type: 'AI_CHAT_SEND', version: 1, request_id: requestId, content,
      context: record ? { type: record.type, id: record.id } : null,
      attachment_ids: selectedIds,
    }));
    setDraft('');
    setSelectedIds([]);
    setFilesOpen(false);
    setNotice('');
  };

  const undo = async (action) => {
    setUndoing(action.action_id);
    try {
      const result = await crmPost(`/api/ai/chat/actions/${encodeURIComponent(action.action_id)}/undo`, {});
      dispatch({ type: ACTIONS.AI_CHAT_ACTION_UNDONE, payload: result });
    } catch (error) {
      setNotice(error.message || 'This change could not be undone.');
    } finally {
      setUndoing('');
    }
  };

  const onComposerKeyDown = (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      send();
    }
  };

  if (featureAvailable !== true) return null;

  return (
    <>
      <button
        ref={launcherRef}
        type="button"
        className={styles.launcher}
        onClick={() => setOpen(true)}
        aria-label="Open NEOH personal AI"
        aria-expanded={open}
        aria-controls="neoh-assistant-panel"
      >
        <span className={styles.launcherOrb} aria-hidden="true"><i /><i /></span>
        <span>Ask NEOH</span>
        {aiChatMessages.some((message) => ['pending', 'streaming'].includes(message.status)) && <i className={styles.busyPin} aria-label="Response in progress" />}
      </button>

      {open && (
        <div className={styles.layer}>
          <button type="button" className={styles.scrim} onClick={() => setOpen(false)} tabIndex={-1} aria-label="Close assistant" />
          <aside
            id="neoh-assistant-panel"
            className={styles.panel}
            role="dialog"
            aria-modal="true"
            aria-label="NEOH Personal AI"
            ref={panelRef}
          >
            <header className={styles.header}>
              <div className={styles.identity}>
                <span className={styles.neohSigil} aria-hidden="true"><i /><i /><i /></span>
                <div>
                  <span className={styles.eyebrow}>Private operating channel</span>
                  <h1 id="neoh-assistant-title">NEOH <small>Personal AI</small></h1>
                </div>
              </div>
              <div className={styles.headerTools}>
                <span className={styles.connection} data-state={aiChatConnection}><i />{aiChatConnection}</span>
                <button type="button" className={styles.iconButton} onClick={() => setOpen(false)} aria-label="Close assistant"><Glyph name="close" /></button>
              </div>
            </header>

            <div className={styles.contextBar}>
              <button type="button" className={styles.contextButton} onClick={() => setPickerOpen(true)}>
                <Glyph name="record" />
                <span>
                  <small>{record ? record.type : 'Conversation context'}</small>
                  <strong>{record?.label || 'Select a record'}</strong>
                </span>
                <b aria-hidden="true">⌄</b>
              </button>
              {record && <button type="button" className={styles.clearContext} onClick={() => clearRecord()} aria-label="Clear selected record">×</button>}
            </div>

            {pickerOpen && <AssistantRecordPicker onSelect={chooseRecord} onClose={() => setPickerOpen(false)} />}

            <div className={styles.messages} ref={messagesRef}>
              <AssistantMessages messages={aiChatMessages} onUndo={undo} undoing={undoing} />
            </div>

            {filesOpen && (
              <section className={styles.fileTray} aria-label="Files on selected record">
                <div className={styles.trayHeading}>
                  <span><strong>Record files</strong><small>Select up to five for this message</small></span>
                  <button type="button" onClick={() => inputRef.current?.click()} disabled={uploading}>{uploading ? 'Scanning…' : 'Add files'}</button>
                </div>
                {recordFiles.length === 0 ? <p>No files have been saved to this record.</p> : (
                  <div className={styles.fileList}>
                    {recordFiles.map((file) => (
                      <div className={styles.fileRow} key={file.id}>
                        <label>
                          <input type="checkbox" checked={selectedIds.includes(file.id)} onChange={() => toggleSelected(file.id)} />
                          <span><strong>{file.filename}</strong><small>{Math.ceil(file.byte_size / 1024)} KB · {file.media_type.replace('application/', '').replace('image/', '')}</small></span>
                        </label>
                        <button type="button" onClick={() => crmDownload(`/api/ai/chat/attachments/${file.id}/download`, file.filename)} aria-label={`Download ${file.filename}`}><Glyph name="download" /></button>
                        <button type="button" onClick={() => removeFile(file)} aria-label={`Remove ${file.filename}`}><Glyph name="trash" /></button>
                      </div>
                    ))}
                  </div>
                )}
              </section>
            )}

            <footer className={styles.composerZone}>
              {notice && <p className={styles.notice} role="alert">{notice}</p>}
              {selectedIds.length > 0 && <div className={styles.selectedSummary}>{selectedIds.length} record file{selectedIds.length === 1 ? '' : 's'} ready</div>}
              <div className={styles.composer}>
                <button
                  type="button"
                  className={styles.composerButton}
                  onClick={() => record ? setFilesOpen((value) => !value) : setPickerOpen(true)}
                  aria-label={record ? 'Attach record files' : 'Select a record before attaching files'}
                  aria-expanded={filesOpen}
                ><Glyph name="clip" /></button>
                <textarea
                  ref={composerRef}
                  value={draft}
                  onChange={(event) => setDraft(event.target.value.slice(0, 8_000))}
                  onKeyDown={onComposerKeyDown}
                  placeholder={record ? `Ask about ${record.label}` : 'Ask NEOH anything about your work'}
                  rows={1}
                  maxLength={8_000}
                  aria-label="Message NEOH"
                />
                <button
                  type="button"
                  className={styles.composerButton}
                  data-listening={voice.listening}
                  disabled={!voice.supported}
                  onPointerDown={(event) => { event.preventDefault(); voice.start(); }}
                  onPointerUp={voice.stop}
                  onPointerCancel={voice.stop}
                  onKeyDown={(event) => { if ([' ', 'Enter'].includes(event.key)) { event.preventDefault(); voice.start(); } }}
                  onKeyUp={(event) => { if ([' ', 'Enter'].includes(event.key)) voice.stop(); }}
                  aria-label={voice.supported ? 'Hold to talk' : 'Voice input is not supported by this browser'}
                ><Glyph name="mic" /></button>
                <button
                  type="button"
                  className={styles.sendButton}
                  onClick={send}
                  disabled={!draft.trim() && selectedIds.length === 0}
                  aria-label="Send message"
                ><Glyph name="send" /></button>
              </div>
              <div className={styles.composerFoot}>
                <span>{voice.listening ? 'Listening — release to stop' : 'Enter to send · Shift + Enter for a new line'}</span>
                <span>{draft.length.toLocaleString()} / 8,000</span>
              </div>
              <input ref={inputRef} className={styles.hiddenInput} type="file" accept={ACCEPT} multiple onChange={upload} />
            </footer>
          </aside>
        </div>
      )}
    </>
  );
}
