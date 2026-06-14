import { createContext, useCallback, useContext, useMemo, useState } from 'react';

const StateCtx = createContext(null);

const STORAGE_KEY = 'oracle_active_states';

function loadStates() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed) && parsed.length > 0) return parsed;
    }
  } catch { /* corrupted — fall through */ }
  return [];
}

export function StateProvider({ children }) {
  const [activeStates, setActiveStatesRaw] = useState(loadStates);

  const setActiveStates = useCallback((states) => {
    const arr = Array.isArray(states) ? states : [states];
    setActiveStatesRaw(arr);
    localStorage.setItem(STORAGE_KEY, JSON.stringify(arr));
  }, []);

  const addState = useCallback((code) => {
    setActiveStatesRaw((prev) => {
      if (prev.includes(code)) return prev;
      const next = [...prev, code];
      localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      return next;
    });
  }, []);

  const removeState = useCallback((code) => {
    setActiveStatesRaw((prev) => {
      const next = prev.filter((s) => s !== code);
      localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      return next;
    });
  }, []);

  const value = useMemo(() => ({
    activeStates,
    primaryState: activeStates[0] || null,
    setActiveStates,
    addState,
    removeState,
  }), [activeStates, setActiveStates, addState, removeState]);

  return <StateCtx.Provider value={value}>{children}</StateCtx.Provider>;
}

export function useStateCtx() {
  const ctx = useContext(StateCtx);
  if (!ctx) throw new Error('useStateCtx must be used within StateProvider');
  return ctx;
}
