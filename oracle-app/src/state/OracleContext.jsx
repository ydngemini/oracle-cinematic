import { createContext, useContext, useReducer, useRef } from 'react';
import { oracleReducer, initialState, ACTIONS } from './oracleReducer';

const OracleContext = createContext(null);
const OracleDispatchContext = createContext(null);

export function OracleProvider({ children }) {
  const [state, dispatch] = useReducer(oracleReducer, initialState);
  const wsRef = useRef(null);

  return (
    <OracleContext.Provider value={state}>
      <OracleDispatchContext.Provider value={{ dispatch, wsRef }}>
        {children}
      </OracleDispatchContext.Provider>
    </OracleContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useOracleState() {
  const state = useContext(OracleContext);
  if (state === null) {
    throw new Error('useOracleState must be used within OracleProvider');
  }
  return state;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useOracleDispatch() {
  const context = useContext(OracleDispatchContext);
  if (context === null) {
    throw new Error('useOracleDispatch must be used within OracleProvider');
  }
  return context;
}

export { ACTIONS };
