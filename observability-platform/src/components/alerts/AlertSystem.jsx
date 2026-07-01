import { createContext, useContext, useReducer, useCallback, useRef } from 'react';

const AlertContext = createContext(null);

const initialState = {
  alerts: [],
  thresholds: {
    aws: { cpu: 80, memory: 85, latency: 500, errorRate: 5 },
    openai: { tokens: 100000, latency: 2000, errorRate: 3 },
    database: { connections: 90, latency: 100, storage: 85 }
  },
  history: [],
  incidentCount: 0
};

function alertReducer(state, action) {
  switch (action.type) {
    case 'ADD_ALERT': {
      const alert = {
        id: `alert-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`,
        severity: action.payload.severity,
        message: action.payload.message,
        resource: action.payload.resource,
        timestamp: new Date().toISOString(),
        active: true
      };
      return {
        ...state,
        alerts: [...state.alerts, alert],
        history: [{ ...alert, resolvedAt: null }, ...state.history].slice(0, 100),
        incidentCount: state.incidentCount + 1
      };
    }
    case 'CLEAR_ALERT': {
      const clearedAlert = state.alerts.find(a => a.id === action.payload.id);
      return {
        ...state,
        alerts: state.alerts.map(a => 
          a.id === action.payload.id ? { ...a, active: false, resolvedAt: new Date().toISOString() } : a
        ),
        history: state.history.map(h =>
          h.id === action.payload.id ? { ...h, resolvedAt: new Date().toISOString() } : h
        )
      };
    }
    case 'SET_THRESHOLD': {
      const { service, metric, value } = action.payload;
      return {
        ...state,
        thresholds: {
          ...state.thresholds,
          [service]: {
            ...state.thresholds[service],
            [metric]: value
          }
        }
      };
    }
    case 'CHECK_THRESHOLDS': {
      return state;
    }
    default:
      return state;
  }
}

export function AlertProvider({ children }) {
  const [state, dispatch] = useReducer(alertReducer, initialState);
  const audioRef = useRef(null);

  const addAlert = useCallback((severity, message, resource) => {
    dispatch({ type: 'ADD_ALERT', payload: { severity, message, resource } });
  }, []);

  const clearAlert = useCallback((id) => {
    dispatch({ type: 'CLEAR_ALERT', payload: { id } });
  }, []);

  const getActiveAlerts = useCallback(() => {
    return state.alerts.filter(a => a.active);
  }, [state.alerts]);

  const setThreshold = useCallback((service, metric, value) => {
    dispatch({ type: 'SET_THRESHOLD', payload: { service, metric, value } });
  }, []);

  const getAlertCounts = useCallback(() => {
    const active = state.alerts.filter(a => a.active);
    return {
      warning: active.filter(a => a.severity === 'warning').length,
      critical: active.filter(a => a.severity === 'critical').length,
      breach: active.filter(a => a.severity === 'breach').length,
      total: active.length
    };
  }, [state.alerts]);

  const checkThreshold = useCallback((service, metric, value) => {
    const threshold = state.thresholds[service]?.[metric];
    if (threshold === undefined) return null;
    
    const ratio = value / threshold;
    if (ratio >= 1.0) return 'breach';
    if (ratio >= 0.9) return 'critical';
    if (ratio >= 0.75) return 'warning';
    return null;
  }, [state.thresholds]);

  const value = {
    ...state,
    addAlert,
    clearAlert,
    getActiveAlerts,
    setThreshold,
    getAlertCounts,
    checkThreshold,
    audioRef
  };

  return (
    <AlertContext.Provider value={value}>
      {children}
    </AlertContext.Provider>
  );
}

export function useAlertContext() {
  const context = useContext(AlertContext);
  if (!context) {
    throw new Error('useAlertContext must be used within AlertProvider');
  }
  return context;
}

export default AlertContext;
