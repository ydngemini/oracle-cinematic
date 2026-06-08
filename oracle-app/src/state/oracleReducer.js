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
  dealPipeline: [],      // [{ state, count, leads: [...] }] from the 4-State firehose
  dealPipelineTotal: 0,
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
  SET_DEAL_PIPELINE: 'SET_DEAL_PIPELINE',
};

export function oracleReducer(state, action) {
  switch (action.type) {
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

    case ACTIONS.SET_DEAL_PIPELINE:
      return {
        ...state,
        dealPipeline: action.payload.states || [],
        dealPipelineTotal: action.payload.total || 0,
      };

    default:
      return state;
  }
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
