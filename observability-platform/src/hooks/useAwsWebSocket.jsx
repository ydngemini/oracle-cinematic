import { useState, useEffect, useRef, useCallback } from 'react';
import { resolveToken } from '../lib/auth';

// The /api/aws/ws stream is auth-gated (platform_admin/broker_owner) — it exposes
// the full AWS infra inventory + billing. The browser WebSocket API can't set an
// Authorization header, so the JWT is sent as a WebSocket subprotocol value.
// Unlike a query parameter, it does not land in browser history or access URLs.
export default function useAwsWebSocket(token) {
  const [connected, setConnected] = useState(false);
  const [messages, setMessages] = useState([]);
  const [authError, setAuthError] = useState(false);
  const wsRef = useRef(null);
  const reconnectAttempts = useRef(0);
  const reconnectTimerRef = useRef(null);
  const heartbeatRef = useRef(null);
  const lastActivityRef = useRef(0);

  const HEARTBEAT_MS = 25000; // send a PING this often
  const STALE_MS = 60000;     // no traffic for this long → consider the socket dead

  const stopHeartbeat = useCallback(() => {
    if (heartbeatRef.current) {
      clearInterval(heartbeatRef.current);
      heartbeatRef.current = null;
    }
  }, []);

  useEffect(() => {
    let disposed = false;

    const connect = () => {
      if (disposed) return;
      const configuredWsUrl = (import.meta.env.VITE_WS_URL || '').replace(/\/+$/, '');
      const wsUrl = configuredWsUrl || `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}`;
      const tok = token || resolveToken();
      if (!tok) {
        setAuthError(true);
        return;
      }
      const ws = new WebSocket(`${wsUrl}/api/aws/ws`, ['oracle.jwt', tok]);
      wsRef.current = ws;

      ws.onopen = () => {
        if (disposed || wsRef.current !== ws) {
          ws.close();
          return;
        }
        setConnected(true);
        setAuthError(false);
        reconnectAttempts.current = 0;
        lastActivityRef.current = Date.now();
        // Heartbeat: PING the server; if no traffic (incl. PONG) within STALE_MS,
        // force-close so onclose triggers a reconnect and catches half-open sockets.
        stopHeartbeat();
        heartbeatRef.current = setInterval(() => {
          if (wsRef.current !== ws || ws.readyState !== WebSocket.OPEN) return;
          if (Date.now() - lastActivityRef.current > STALE_MS) {
            console.warn('AWS WS stale - forcing reconnect');
            ws.close();
            return;
          }
          try { ws.send(JSON.stringify({ type: 'PING' })); } catch { /* closing */ }
        }, HEARTBEAT_MS);
      };

      ws.onmessage = (event) => {
        if (disposed || wsRef.current !== ws) return;
        lastActivityRef.current = Date.now();
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === 'PONG') return; // heartbeat ack - don't churn app state
          setMessages(current => [...current, msg].slice(-100));
        } catch (e) {
          console.error('Failed to parse WS message:', e);
        }
      };

      ws.onclose = (event) => {
        if (disposed || wsRef.current !== ws) return;
        wsRef.current = null;
        setConnected(false);
        stopHeartbeat();
        // 4401/4403 = server rejected the token/role. Don't hammer reconnects on
        // an auth failure - surface it so the operator can supply a valid token.
        if (event.code === 4401 || event.code === 4403) {
          setAuthError(true);
          console.error('AWS observability WS unauthorized - provide a platform_admin token.');
          return;
        }
        const delay = Math.min(1000 * 2 ** reconnectAttempts.current, 30000);
        reconnectAttempts.current++;
        reconnectTimerRef.current = setTimeout(() => {
          reconnectTimerRef.current = null;
          connect();
        }, delay);
      };

      ws.onerror = (event) => {
        if (!disposed && wsRef.current === ws) {
          console.error('WebSocket error:', event);
        }
      };
    };

    connect();
    return () => {
      disposed = true;
      stopHeartbeat();
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      const ws = wsRef.current;
      wsRef.current = null;
      if (ws) {
        ws.close();
      }
    };
  }, [token, stopHeartbeat]);

  const requestSnapshot = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'REQUEST_SNAPSHOT' }));
    }
  }, []);

  const acknowledgeMessages = useCallback((count) => {
    setMessages(current => current.slice(count));
  }, []);

  return { connected, messages, acknowledgeMessages, requestSnapshot, authError };
}
