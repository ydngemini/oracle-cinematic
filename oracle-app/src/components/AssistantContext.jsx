import { createContext, useCallback, useContext, useEffect, useId, useMemo, useState } from 'react';

const AssistantContext = createContext(null);

export function AssistantProvider({ children }) {
  const [open, setOpen] = useState(false);
  const [record, setRecord] = useState(null);
  const [commandStatus, setCommandStatus] = useState({
    state: 'idle',
    message: 'Assistant ready',
    detail: '',
  });
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
    clearCommandRequest: () => setCommandRequest(null),
  }), [
    clearRecord,
    commandRequest,
    commandStatus,
    open,
    record,
    registerRecord,
    requestCommand,
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
