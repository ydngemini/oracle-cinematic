import { useState, useEffect, useRef, useCallback } from 'react';
import { resolveToken } from '../lib/auth';

// The /api/aws/ws stream is auth-gated (platform_admin/broker_owner) — it exposes
// the full AWS infra inventory + billing. The browser WebSocket API can't set an
// Authorization header, so the JWT rides as a ?token= query param. The caller
// passes the token it authenticated with; we fall back to resolveToken() for the
// URL/env/stored paths.
export default function useAwsWebSocket(token) {
  const [connected, setConnected] = useState(false);
  const [lastMessage, setLastMessage] = useState(null);
  const [authError, setAuthError] = useState(false);
  const wsRef = useRef(null);
  const reconnectAttempts = useRef(0);
  const heartbeatRef = useRef(null);
  const lastActivityRef = useRef(0);
  const closedByUnmount = useRef(false);

  const HEARTBEAT_MS = 25000; // send a PING this often
  const STALE_MS = 60000;     // no traffic for this long → consider the socket dead

  const stopHeartbeat = () => {
    if (heartbeatRef.current) {
      clearInterval(heartbeatRef.current);
      heartbeatRef.current = null;
    }
  };

  const connect = useCallback(() => {
    closedByUnmount.current = false; // fresh (re)connect; re-enable auto-reconnect
    const wsUrl = import.meta.env.VITE_WS_URL || 'ws://localhost:8000';
    const tok = token || resolveToken();
    const suffix = tok ? `?token=${encodeURIComponent(tok)}` : '';
    const ws = new WebSocket(`${wsUrl}/api/aws/ws${suffix}`);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      setAuthError(false);
      reconnectAttempts.current = 0;
      lastActivityRef.current = Date.now();
      // Heartbeat: PING the server; if no traffic (incl. PONG) within STALE_MS,
      // force-close so onclose triggers a reconnect — catches half-open sockets
      // (laptop sleep, NAT drop) that never fire onclose on their own.
      stopHeartbeat();
      heartbeatRef.current = setInterval(() => {
        if (ws.readyState !== WebSocket.OPEN) return;
        if (Date.now() - lastActivityRef.current > STALE_MS) {
          console.warn('AWS WS stale — forcing reconnect');
          ws.close();
          return;
        }
        try { ws.send(JSON.stringify({ type: 'PING' })); } catch { /* closing */ }
      }, HEARTBEAT_MS);
    };

    ws.onmessage = (event) => {
      lastActivityRef.current = Date.now();
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === 'PONG') return; // heartbeat ack — don't churn app state
        setLastMessage(msg);
      } catch (e) {
        console.error('Failed to parse WS message:', e);
      }
    };

    ws.onclose = (event) => {
      setConnected(false);
      stopHeartbeat();
      if (closedByUnmount.current) return; // unmount/token-swap — don't reconnect
      // 4401/4403 = server rejected the token/role. Don't hammer reconnects on
      // an auth failure — surface it so the operator can supply a valid token.
      if (event.code === 4401 || event.code === 4403) {
        setAuthError(true);
        console.error('AWS observability WS unauthorized — provide a platform_admin token.');
        return;
      }
      const delay = Math.min(1000 * 2 ** reconnectAttempts.current, 30000);
      reconnectAttempts.current++;
      setTimeout(connect, delay);
    };

    ws.onerror = (e) => {
      console.error('WebSocket error:', e);
    };
  }, [token]);

  useEffect(() => {
    connect();
    return () => {
      closedByUnmount.current = true;
      stopHeartbeat();
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, [connect]);

  const requestSnapshot = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'REQUEST_SNAPSHOT' }));
    }
  }, []);

  return { connected, lastMessage, requestSnapshot, authError };
}
