import { createContext, useCallback, useContext, useEffect, useId, useMemo, useState } from 'react';

const AssistantContext = createContext(null);

let commandStatusSeq = 0;

export function AssistantProvider({ children }) {
  const [open, setOpen] = useState(false);
  const [record, setRecord] = useState(null);
  const [commandStatus, setRawCommandStatus] = useState({
    state: 'idle',
    message: 'Assistant ready',
    detail: '',
    at: 0,
    seq: 0,
  });
  // Every status is stamped with when it was set, so the mascot can tell a
  // receipt that just arrived from one left over from minutes ago (a success
  // must never replay on remount). `seq` orders two statuses set in the same
  // millisecond.
  const setCommandStatus = useCallback((next) => {
    const at = Date.now();
    commandStatusSeq += 1;
    const seq = commandStatusSeq;
    setRawCommandStatus((previous) => {
      const value = typeof next === 'function' ? next(previous) : next;
      return value ? { ...value, at, seq } : value;
    });
  }, []);
  const [commandRequest, setCommandRequest] = useState(null);

  const registerRecord = useCallback((next, owner = 'manual') => {
    setRecord(next ? { ...next, owner } : null);
  }, []);

  const clearRecord = useCallback((owner) => {
    setRecord((current) => (!owner || current?.owner === owner ? null : current));
  }, []);

  const requestCommand = useCallback((request) => {
    setCommandRequest({
      ...request,
      requestId: globalThis.crypto?.randomUUID?.() || `command-${Date.now()}`,
    });
  }, []);

  const clearCommandRequest = useCallback(() => setCommandRequest(null), []);

  const value = useMemo(() => ({
    open,
    setOpen,
    record: record ? { type: record.type, id: record.id, label: record.label, detail: record.detail } : null,
    registerRecord,
    clearRecord,
    commandStatus,
    setCommandStatus,
    commandRequest,
    requestCommand,
    clearCommandRequest,
  }), [
    clearCommandRequest,
    clearRecord,
    commandRequest,
    commandStatus,
    open,
    record,
    registerRecord,
    requestCommand,
    setCommandStatus,
  ]);

  return <AssistantContext.Provider value={value}>{children}</AssistantContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAssistant() {
  const value = useContext(AssistantContext);
  if (!value) throw new Error('useAssistant must be used inside AssistantProvider');
  return value;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useOptionalAssistant() {
  return useContext(AssistantContext);
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAssistantRecord(type, id, label, detail = '') {
  const owner = useId();
  const { registerRecord, clearRecord } = useAssistant();

  useEffect(() => {
    if (!type || !id) return undefined;
    registerRecord({ type, id: String(id), label: label || 'Selected record', detail }, owner);
    return () => clearRecord(owner);
  }, [clearRecord, detail, id, label, owner, registerRecord, type]);
}
