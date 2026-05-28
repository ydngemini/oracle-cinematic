import { useEffect, useCallback, useRef } from 'react';
import { useOracleDispatch, ACTIONS } from './OracleContext';
import { registerSW } from 'virtual:pwa-register';

const WS_URL = import.meta.env.VITE_WS_URL || 'ws://localhost:8000/ws';
const BASE_DELAY = 2000;
const MAX_DELAY = 60000;
const MAX_RETRIES = 8;

export function useOracleWebSocket() {
  const { dispatch, wsRef } = useOracleDispatch();
  const retryCount = useRef(0);
  const retryTimer = useRef(null);
  const mountedRef = useRef(false);

  const connect = useCallback(() => {
    if (!mountedRef.current) return;
    if (wsRef.current?.readyState === WebSocket.OPEN) return;
    if (wsRef.current?.readyState === WebSocket.CONNECTING) return;

    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      retryCount.current = 0;
    };

    ws.onmessage = (event) => {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch {
        return;
      }

      switch (msg.type) {
        case 'STATUS_UPDATE':
          dispatch({
            type: ACTIONS.SET_ACTIVE_AGENT,
            payload: msg.agent,
          });
          break;

        case 'DATA_PULLED':
          dispatch({
            type: ACTIONS.UPDATE_PROPERTY,
            payload: msg.data,
          });
          break;

        case 'STAGE_PROPERTY':
          dispatch({
            type: ACTIONS.SET_FURNISHED,
            payload: true,
          });
          break;

        case 'SPLAT_READY':
          dispatch({
            type: ACTIONS.UPDATE_PROPERTY,
            payload: { splatUrl: msg.url },
          });
          break;

        case 'TRANSCRIPT_LINE':
          dispatch({
            type: ACTIONS.APPEND_TRANSCRIPT,
            payload: {
              id: crypto.randomUUID(),
              agent: msg.agent || 'SYSTEM',
              text: msg.text,
              timestamp: msg.timestamp || Date.now(),
            },
          });
          break;

        case 'HYDRATE':
          dispatch({
            type: ACTIONS.HYDRATE,
            payload: msg.state,
          });
          break;

        case 'PREDICTIVE_CACHE':
          dispatch({
            type: ACTIONS.SET_PREDICTIVE_CACHE,
            payload: msg.properties || [],
          });
          if (navigator.serviceWorker?.controller) {
            navigator.serviceWorker.controller.postMessage({
              type: 'PREDICTIVE_CACHE',
              payload: { properties: msg.properties || [] },
            });
          }
          break;

        case 'LEGAL_PACKAGE':
          dispatch({
            type: ACTIONS.SET_LEGAL_PACKAGE,
            payload: msg.data,
          });
          break;

        case 'MANUAL_COMPS':
          dispatch({
            type: ACTIONS.SET_MANUAL_COMPS,
            payload: msg.comps || [],
          });
          break;

        case 'AGENT_THOUGHT':
          if (msg.mode === 'start') {
            dispatch({
              type: ACTIONS.WALKER_THOUGHT_START,
              payload: { agent: msg.agent, token: msg.token },
            });
          } else if (msg.mode === 'stream') {
            dispatch({
              type: ACTIONS.WALKER_THOUGHT_TOKEN,
              payload: { token: msg.token },
            });
          } else if (msg.mode === 'end') {
            dispatch({ type: ACTIONS.WALKER_THOUGHT_END });
          }
          break;
      }
    };

    ws.onclose = () => {
      wsRef.current = null;
      if (!mountedRef.current) return;
      if (retryCount.current >= MAX_RETRIES) return;

      const delay = Math.min(
        BASE_DELAY * Math.pow(2, retryCount.current),
        MAX_DELAY
      );

      retryTimer.current = setTimeout(() => {
        retryCount.current += 1;
        connect();
      }, delay);
    };

    ws.onerror = () => {
      ws.close();
    };
  }, [dispatch, wsRef]);

  useEffect(() => {
    mountedRef.current = true;
    retryTimer.current = setTimeout(connect, 100);

    registerSW({ immediate: true });

    const swListener = (event) => {
      const { type, propertyIds } = event.data || {};
      if (type === 'CACHE_WARM') {
        dispatch({ type: ACTIONS.SET_CACHE_WARM, payload: propertyIds || [] });
      }
    };
    navigator.serviceWorker?.addEventListener('message', swListener);

    return () => {
      mountedRef.current = false;
      clearTimeout(retryTimer.current);
      navigator.serviceWorker?.removeEventListener('message', swListener);
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.onerror = null;
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [connect, dispatch]);

  const send = useCallback(
    (payload) => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify(payload));
      }
    },
    [wsRef]
  );

  const disconnect = useCallback(() => {
    mountedRef.current = false;
    clearTimeout(retryTimer.current);
    if (wsRef.current) {
      wsRef.current.onclose = null;
      wsRef.current.close();
      wsRef.current = null;
    }
  }, [wsRef]);

  return { send, disconnect, reconnect: connect };
}
