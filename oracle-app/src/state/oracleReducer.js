export const initialState = {
  activeAgent: null,
  propertyData: {
    squareFootage: 0,
    novelty: 0,
    address: '',
    price: 0,
    bedrooms: 0,
    bathrooms: 0,
  },
  transcriptLog: [],
  isFurnished: false,
  legalPackage: null,
  gisBoundaryVisible: true,
  anchorsVisible: true,
  jarvisListening: false,
  jarvisTranscript: '',
  predictiveCache: [],
  cacheWarm: [],
  isAiAppraisalMode: true,
  manualComps: [],
  walkerThought: '',
  walkerAgent: 'SCOUT',
  walkerStreaming: false,
  memorySync: false,
  maoThreshold: 0.70,
  profileSummary: '',
  targetMarkets: [],
  liveFeed: [],          // Live Pulse — newest-first activity cards, capped at 50
  jobProgress: {},       // durable job id -> latest authenticated progress frame
  negotiationTelemetry: null,
  callConsents: {},
  aiChatMessages: [],
  aiChatRevision: 0,
  aiChatConnection: 'offline',
};

export const ACTIONS = {
  SET_ACTIVE_AGENT: 'SET_ACTIVE_AGENT',
  UPDATE_PROPERTY: 'UPDATE_PROPERTY',
  APPEND_TRANSCRIPT: 'APPEND_TRANSCRIPT',
  CLEAR_TRANSCRIPT: 'CLEAR_TRANSCRIPT',
  SET_FURNISHED: 'SET_FURNISHED',
  HYDRATE: 'HYDRATE',
  SET_LEGAL_PACKAGE: 'SET_LEGAL_PACKAGE',
  TOGGLE_GIS_BOUNDARY: 'TOGGLE_GIS_BOUNDARY',
  TOGGLE_ANCHORS: 'TOGGLE_ANCHORS',
  SET_JARVIS_LISTENING: 'SET_JARVIS_LISTENING',
  SET_JARVIS_TRANSCRIPT: 'SET_JARVIS_TRANSCRIPT',
  JARVIS_COMMAND: 'JARVIS_COMMAND',
  SET_PREDICTIVE_CACHE: 'SET_PREDICTIVE_CACHE',
  SET_CACHE_WARM: 'SET_CACHE_WARM',
  TOGGLE_APPRAISAL_MODE: 'TOGGLE_APPRAISAL_MODE',
  SET_MANUAL_COMPS: 'SET_MANUAL_COMPS',
  WALKER_THOUGHT_START: 'WALKER_THOUGHT_START',
  WALKER_THOUGHT_TOKEN: 'WALKER_THOUGHT_TOKEN',
  WALKER_THOUGHT_END: 'WALKER_THOUGHT_END',
  SESSION_RESTORED: 'SESSION_RESTORED',
  FEED_EVENT: 'FEED_EVENT',
  PROFILE_SAVED: 'PROFILE_SAVED',
  JOB_PROGRESS: 'JOB_PROGRESS',
  NEGOTIATION_TELEMETRY: 'NEGOTIATION_TELEMETRY',
  CALL_CONSENT: 'CALL_CONSENT',
  AI_CHAT_CONNECTION: 'AI_CHAT_CONNECTION',
  AI_CHAT_HYDRATE: 'AI_CHAT_HYDRATE',
  AI_CHAT_SEND_LOCAL: 'AI_CHAT_SEND_LOCAL',
  AI_CHAT_ACCEPTED: 'AI_CHAT_ACCEPTED',
  AI_CHAT_START: 'AI_CHAT_START',
  AI_CHAT_DELTA: 'AI_CHAT_DELTA',
  AI_CHAT_COMPLETE: 'AI_CHAT_COMPLETE',
  AI_CHAT_ERROR: 'AI_CHAT_ERROR',
  AI_CHAT_REJECTED: 'AI_CHAT_REJECTED',
  AI_CHAT_ACTION_UNDONE: 'AI_CHAT_ACTION_UNDONE',
};

export function oracleReducer(state, action) {
  switch (action.type) {
    case ACTIONS.AI_CHAT_CONNECTION:
      return { ...state, aiChatConnection: action.payload };

    case ACTIONS.AI_CHAT_HYDRATE: {
      const incoming = Array.isArray(action.payload) ? action.payload : [];
      const pending = state.aiChatMessages.filter((message) => {
        if (!message.local) return false;
        return !incoming.some((saved) => {
          if (saved.request_id !== message.request_id) return false;
          if (saved.role !== message.role) return false;
          if (message.id && saved.id && saved.id !== message.id) return false;
          return true;
        });
      });
      return { ...state, aiChatMessages: [...incoming, ...pending] };
    }

    case ACTIONS.AI_CHAT_SEND_LOCAL:
      return {
        ...state,
        aiChatMessages: [
          ...state.aiChatMessages,
          action.payload.user,
          action.payload.assistant,
        ],
      };

    case ACTIONS.AI_CHAT_ACCEPTED:
      return {
        ...state,
        aiChatRevision: state.aiChatRevision + 1,
        aiChatMessages: state.aiChatMessages.map((message) => {
          if (message.request_id !== action.payload.request_id) return message;
          if (message.role === 'assistant') {
            return { ...message, id: action.payload.message_id, status: action.payload.status || 'pending', local: false };
          }
          if (message.role === 'user' && action.payload.user_message_id) {
            return { ...message, id: action.payload.user_message_id, local: false };
          }
          return message;
        }),
      };

    case ACTIONS.AI_CHAT_START:
      return {
        ...state,
        aiChatMessages: upsertAssistant(state.aiChatMessages, action.payload, {
          status: 'streaming', content: '', local: false,
        }),
      };

    case ACTIONS.AI_CHAT_DELTA:
      return {
        ...state,
        aiChatMessages: upsertAssistant(state.aiChatMessages, action.payload, (message) => ({
          status: 'streaming', content: `${message.content || ''}${action.payload.delta || ''}`, local: false,
        })),
      };

    case ACTIONS.AI_CHAT_COMPLETE:
      return {
        ...state,
        aiChatRevision: state.aiChatRevision + 1,
        aiChatMessages: upsertAssistant(state.aiChatMessages, action.payload, {
          status: 'completed', actions: action.payload.actions || [], model_id: action.payload.model_id,
        }),
      };

    case ACTIONS.AI_CHAT_ERROR:
      return {
        ...state,
        aiChatRevision: state.aiChatRevision + 1,
        aiChatMessages: upsertAssistant(state.aiChatMessages, action.payload, {
          status: 'failed', content: action.payload.message || 'The assistant is unavailable.',
          error_code: action.payload.code,
        }),
      };

    case ACTIONS.AI_CHAT_REJECTED:
      return {
        ...state,
        aiChatMessages: state.aiChatMessages.map((message) =>
          message.request_id === action.payload.request_id && message.role === 'assistant'
            ? { ...message, status: 'failed', content: action.payload.message || 'Message was not accepted.' }
            : message
        ),
      };

    case ACTIONS.AI_CHAT_ACTION_UNDONE:
      return {
        ...state,
        aiChatMessages: state.aiChatMessages.map((message) => ({
          ...message,
          actions: (message.actions || []).map((receipt) =>
            receipt.action_id === action.payload.id ? { ...receipt, status: 'undone' } : receipt
          ),
        })),
      };

    case ACTIONS.JOB_PROGRESS: {
      const next = { ...state.jobProgress, [action.payload.job_id]: action.payload };
      const entries = Object.entries(next);
      return {
        ...state,
        jobProgress: entries.length > 50 ? Object.fromEntries(entries.slice(-50)) : next,
      };
    }

    case ACTIONS.NEGOTIATION_TELEMETRY:
      return { ...state, negotiationTelemetry: action.payload };

    case ACTIONS.CALL_CONSENT:
      return {
        ...state,
        callConsents: { ...state.callConsents, [action.payload.call_session_id]: action.payload },
      };

    case ACTIONS.FEED_EVENT:
      // Newest first; hard cap keeps an always-on dashboard from growing without bound.
      return { ...state, liveFeed: [action.payload, ...state.liveFeed].slice(0, 50) };

    case ACTIONS.SET_ACTIVE_AGENT:
      return { ...state, activeAgent: action.payload };

    case ACTIONS.UPDATE_PROPERTY:
      return {
        ...state,
        propertyData: { ...state.propertyData, ...action.payload },
      };

    case ACTIONS.APPEND_TRANSCRIPT:
      return {
        ...state,
        transcriptLog: [...state.transcriptLog, action.payload],
      };

    case ACTIONS.CLEAR_TRANSCRIPT:
      return { ...state, transcriptLog: [] };

    case ACTIONS.SET_FURNISHED:
      return { ...state, isFurnished: action.payload };

    case ACTIONS.HYDRATE:
      return { ...state, ...action.payload };

    case ACTIONS.SET_LEGAL_PACKAGE:
      return { ...state, legalPackage: action.payload };

    case ACTIONS.TOGGLE_GIS_BOUNDARY:
      return { ...state, gisBoundaryVisible: !state.gisBoundaryVisible };

    case ACTIONS.TOGGLE_ANCHORS:
      return { ...state, anchorsVisible: !state.anchorsVisible };

    case ACTIONS.SET_JARVIS_LISTENING:
      return { ...state, jarvisListening: action.payload };

    case ACTIONS.SET_JARVIS_TRANSCRIPT:
      return { ...state, jarvisTranscript: action.payload };

    case ACTIONS.JARVIS_COMMAND:
      return applyJarvisCommand(state, action.payload);

    case ACTIONS.SET_PREDICTIVE_CACHE:
      return { ...state, predictiveCache: action.payload };

    case ACTIONS.SET_CACHE_WARM:
      return { ...state, cacheWarm: action.payload };

    case ACTIONS.TOGGLE_APPRAISAL_MODE:
      return { ...state, isAiAppraisalMode: !state.isAiAppraisalMode, manualComps: [] };

    case ACTIONS.SET_MANUAL_COMPS:
      return { ...state, manualComps: action.payload };

    case ACTIONS.WALKER_THOUGHT_START:
      return {
        ...state,
        walkerAgent: action.payload.agent,
        walkerThought: action.payload.token,
        walkerStreaming: true,
      };

    case ACTIONS.WALKER_THOUGHT_TOKEN:
      return {
        ...state,
        walkerThought: state.walkerThought + action.payload.token,
      };

    case ACTIONS.WALKER_THOUGHT_END:
      return { ...state, walkerStreaming: false };

    case ACTIONS.SESSION_RESTORED:
      return {
        ...state,
        memorySync: action.payload.restored === true,
        maoThreshold:
          typeof action.payload.maoThreshold === 'number'
            ? action.payload.maoThreshold
            : state.maoThreshold,
        profileSummary: action.payload.summary || '',
        targetMarkets: action.payload.markets || [],
      };

    case ACTIONS.PROFILE_SAVED:
      // Onboarding gate completed — markets land immediately; the next
      // SESSION_RESTORED frame re-hydrates the same values from Postgres.
      return { ...state, targetMarkets: action.payload.markets || [] };

    default:
      return state;
  }
}

function upsertAssistant(messages, payload, patch) {
  const index = messages.findIndex((message) =>
    (payload.message_id && message.id === payload.message_id) ||
    (payload.request_id && message.request_id === payload.request_id && message.role === 'assistant')
  );
  if (index < 0) {
    return [...messages, {
      id: payload.message_id || `pending-${payload.request_id}`,
      request_id: payload.request_id,
      role: 'assistant',
      content: '',
      status: 'pending',
      created_at: new Date().toISOString(),
      ...(typeof patch === 'function' ? patch({}) : patch),
    }];
  }
  const current = messages[index];
  const next = [...messages];
  next[index] = { ...current, ...(typeof patch === 'function' ? patch(current) : patch) };
  return next;
}

function applyJarvisCommand(state, transcript) {
  const t = transcript.toLowerCase().trim();

  if (matches(t, ['show property lines', 'show boundaries', 'show gis', 'show parcel lines'])) {
    return { ...state, gisBoundaryVisible: true };
  }
  if (matches(t, ['hide property lines', 'hide boundaries', 'hide gis', 'hide parcel lines'])) {
    return { ...state, gisBoundaryVisible: false };
  }
  if (matches(t, ['toggle property lines', 'toggle boundaries', 'toggle gis'])) {
    return { ...state, gisBoundaryVisible: !state.gisBoundaryVisible };
  }

  if (matches(t, ['show anchors', 'show data', 'show cards', 'show labels', 'show specs'])) {
    return { ...state, anchorsVisible: true };
  }
  if (matches(t, ['hide anchors', 'hide data', 'hide cards', 'hide labels', 'hide specs'])) {
    return { ...state, anchorsVisible: false };
  }

  if (matches(t, ['furnish', 'stage the property', 'show furnished', 'stage it'])) {
    return { ...state, isFurnished: true };
  }
  if (matches(t, ['unfurnish', 'clear staging', 'remove furniture'])) {
    return { ...state, isFurnished: false };
  }

  if (matches(t, ['clear transcript', 'clear log', 'clear chat'])) {
    return { ...state, transcriptLog: [] };
  }

  if (matches(t, ['go inside', 'interior view', 'show interior', 'enter the house'])) {
    return { ...state, activeAgent: 'INTERIOR_ROOM' };
  }
  if (matches(t, ['exterior', 'outside view', 'show exterior', 'pull back'])) {
    return { ...state, activeAgent: null };
  }
  if (matches(t, ['isometric', 'bird eye', 'birds eye', 'overview', 'top down'])) {
    return { ...state, activeAgent: 'ISOMETRIC' };
  }

  return state;
}

function matches(input, patterns) {
  return patterns.some((p) => input.includes(p));
}
