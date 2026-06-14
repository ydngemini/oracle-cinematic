import { useEffect, useLayoutEffect, useCallback, useRef } from 'react';
import { useOracleDispatch, ACTIONS } from './OracleContext';
import { getUserId, getTenantId } from './identity';
import { registerSW } from 'virtual:pwa-register';

const WS_URL = import.meta.env.VITE_WS_URL || 'ws://localhost:8000/ws';
const BASE_DELAY = 2000;

// Identity for JIT memory hydration AND tenant-scoped lead delivery. Sending
// tenant_id here keeps the DealPipeline (RLS-scoped leads) on the same tenant
// as the billing gate — see state/identity.js.
function buildWsUrl() {
  try {
    const url = new URL(WS_URL);
    url.searchParams.set('user_id', getUserId());
    url.searchParams.set('tenant_id', getTenantId());
    // Verified JWT wins server-side over the (spoofable) tenant_id param —
    // and is the ONLY way into the platform-admin firehose group.
    const token = sessionStorage.getItem('oracle_token');
    if (token) url.searchParams.set('token', token);
    return url.toString();
  } catch {
    return WS_URL;
  }
}
const MAX_DELAY = 60000;
const MAX_RETRIES = 8;

export function useOracleWebSocket() {
  const { dispatch, wsRef } = useOracleDispatch();
  const retryCount = useRef(0);
  const retryTimer = useRef(null);
  const mountedRef = useRef(false);
  // Always points to the latest `connect` so onclose callbacks never hold a
  // stale closure when `connect` is recreated (e.g. if dispatch identity changes).
  const connectRef = useRef(null);

  const connect = useCallback(() => {
    if (!mountedRef.current) return;
    if (wsRef.current?.readyState === WebSocket.OPEN) return;
    if (wsRef.current?.readyState === WebSocket.CONNECTING) return;

    const ws = new WebSocket(buildWsUrl());
    wsRef.current = ws;

    ws.onopen = () => {
      retryCount.current = 0;
      window.__oracleRequestReconstruction = (address) => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'REQUEST_RECONSTRUCTION', address }));
        }
      };
    };

    ws.onmessage = (event) => {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch {
        return;
      }

      // Live Pulse feed entry — derived from real frames, never simulated.
      const feed = (entry) =>
        dispatch({
          type: ACTIONS.FEED_EVENT,
          payload: { id: crypto.randomUUID(), timestamp: Date.now(), ...entry },
        });

      switch (msg.type) {
        case 'STATUS_UPDATE':
          dispatch({
            type: ACTIONS.SET_ACTIVE_AGENT,
            payload: msg.agent,
          });
          if (msg.agent) {
            feed({ actor: 'ai', actorName: msg.agent, text: 'took point on the active operation', tag: 'AGENT LIVE' });
          }
          break;

        case 'DATA_PULLED':
          dispatch({
            type: ACTIONS.UPDATE_PROPERTY,
            payload: msg.data,
          });
          feed({
            actor: 'ai',
            actorName: 'Harvester',
            text: `pulled municipal data for ${msg.data?.address || 'the target property'}`,
            tag: 'DATA PULLED',
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
            payload: { splatUrl: msg.splatUrl || msg.url, propertyId: msg.propertyId },
          });
          feed({
            actor: 'ai',
            actorName: 'Spatial Agent',
            text: `3D reconstruction is live${msg.propertyId ? ` for ${msg.propertyId}` : ''}`,
            tag: 'SCAN LIVE',
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
          feed({
            actor: 'ai',
            actorName: 'AI Legal Agent',
            text: 'compiled the legal package for the active parcel',
            tag: 'CONTRACT READY',
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

        case 'SESSION_RESTORED': {
          const maoPct =
            typeof msg.mao_threshold === 'number' ? msg.mao_threshold : 0.70;
          // Hydrate the memory-sync state (drives AgentStatusBar indicator).
          dispatch({
            type: ACTIONS.SESSION_RESTORED,
            payload: {
              restored: msg.restored === true,
              maoThreshold: maoPct,
              summary: msg.summary || '',
              markets: msg.markets || [],
            },
          });
          // Load the operator's context into the LiveTranscript. Degrades to a
          // neutral line when the backend couldn't restore (no DB / unknown user).
          const restoredText = msg.restored
            ? `Memory Sync active — MAO threshold ${Math.round(maoPct * 100)}%` +
              (msg.summary ? ` · ${msg.summary}` : '') +
              (msg.markets?.length ? ` · Markets: ${msg.markets.join(', ')}` : '')
            : 'Memory Sync unavailable — running with default underwriting profile.';
          dispatch({
            type: ACTIONS.APPEND_TRANSCRIPT,
            payload: {
              id: crypto.randomUUID(),
              agent: 'MEMORY',
              text: restoredText,
              timestamp: Date.now(),
            },
          });
          break;
        }

        case 'DEAL_PIPELINE':
          dispatch({
            type: ACTIONS.SET_DEAL_PIPELINE,
            payload: {
              states: msg.states || [],
              total: msg.total || 0,
            },
          });
          if (msg.total > 0) {
            feed({
              actor: 'ai',
              actorName: 'Deal Pipeline',
              text: `firehose sync across ${(msg.states || []).length} states`,
              tag: 'PIPELINE',
              metric: `${msg.total} leads`,
            });
          }
          break;

        case 'VOICE_NOTE_LOGGED': {
          // Field walkthrough processed by the voice-intel worker. First real
          // producer for the 'agent' (amber) actor in LivePulse.
          const adj = Number(msg.price_adjustment);
          feed({
            actor: 'agent',
            actorName: 'Field Agent',
            text: `logged a voice walkthrough — ${msg.summary || 'note captured'}`,
            tag: (msg.sentiment || 'VOICE NOTE').toUpperCase(),
            ...(Number.isFinite(adj) && adj !== 0
              ? { metric: `${adj > 0 ? '+' : '−'}$${Math.abs(adj).toLocaleString()}` }
              : {}),
          });
          break;
        }

        case 'DOSSIER_MOVED': {
          // A teammate (or this session) dragged a deal on the PipelineBoard.
          const label = (msg.status || '').replace(/_/g, ' ');
          feed({
            actor: 'agent',
            actorName: 'Field Agent',
            text: `moved ${msg.address || msg.parcel_id} to ${label}`,
            tag: label.toUpperCase(),
          });
          // Re-sync the pipeline so every open dashboard converges.
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'REQUEST_DEAL_PIPELINE' }));
          }
          break;
        }

        case 'CONTRACT_DANGER':
          // Disposition Enforcer: assignment window in the danger zone.
          feed({
            actor: 'ai',
            actorName: 'Disposition Engine',
            text: msg.assets_ready
              ? `contract window critical for ${msg.address || msg.parcel_id} — fire-sale marketing assets generated`
              : `contract window critical for ${msg.address || msg.parcel_id} — asset generation queued`,
            tag: 'URGENT DISPOSITION',
            metric: `${msg.days_remaining}d left`,
          });
          break;

        case 'CONTRACT_EXPIRED':
          feed({
            actor: 'ai',
            actorName: 'Disposition Engine',
            text: `assignment window expired for ${msg.address || msg.parcel_id} — dossier moved to expired`,
            tag: 'EXPIRED',
          });
          break;

        case 'PING':
          // Keepalive: the backend idle-watchdog closes the socket after
          // ORACLE_WS_IDLE_TIMEOUT (default 300s) unless it hears from us.
          // Without this reply a passive viewer drops every ~5 minutes.
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'PONG' }));
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
        connectRef.current();
      }, delay);
    };

    ws.onerror = () => {
      ws.close();
    };
  }, [dispatch, wsRef]);

  // Keep ref in sync with the latest memoized `connect` so onclose never calls
  // a stale version.  useLayoutEffect runs synchronously after DOM mutations but
  // before paint, guaranteeing the ref is fresh before any retry fires.
  useLayoutEffect(() => {
    connectRef.current = connect;
  });

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
  }, [connect, dispatch, wsRef]);

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
