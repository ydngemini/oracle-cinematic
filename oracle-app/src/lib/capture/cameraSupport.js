/**
 * What this browser can do for Capture Studio, feature-detected (CLAUDE.md
 * rule 9: no unsupported sensor assumptions), and what a camera error means
 * for the person holding the phone. Nothing here requires a camera: every
 * state other than `ready` comes with the file-picker fallback.
 */

/** Recording formats in preference order; the first the browser supports wins. */
const RECORDING_TYPES = ['video/mp4', 'video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm'];

export function cameraSupport(env = globalThis) {
  const nav = env.navigator;
  const secure = env.isSecureContext !== false;
  const camera = Boolean(nav?.mediaDevices?.getUserMedia) && secure;
  const Recorder = env.MediaRecorder;
  let recordingType = null;
  if (Recorder) {
    const probe = typeof Recorder.isTypeSupported === 'function' ? Recorder.isTypeSupported.bind(Recorder) : () => false;
    recordingType = RECORDING_TYPES.find((t) => probe(t)) || null;
  }
  return {
    camera,
    insecure: !secure,
    recording: camera && Boolean(recordingType),
    recordingType,
    worker: typeof env.Worker === 'function',
  };
}

/** A getUserMedia rejection, as a state and a sentence. Never the raw error text. */
export function cameraErrorState(error) {
  switch (error?.name) {
    case 'NotAllowedError':
    case 'PermissionDeniedError':
      return { state: 'denied', message: 'Camera access was turned down. Allow the camera for this site in your browser settings, or choose files instead.' };
    case 'NotFoundError':
    case 'DevicesNotFoundError':
    case 'OverconstrainedError':
      return { state: 'no_camera', message: 'No usable camera was found on this device. Choose photos or a video instead.' };
    case 'NotReadableError':
    case 'TrackStartError':
    case 'AbortError':
      return { state: 'in_use', message: 'The camera is busy in another app. Close it and try again, or choose files instead.' };
    case 'SecurityError':
      return { state: 'insecure', message: 'The camera only works over a secure connection. Choose files instead.' };
    default:
      return { state: 'error', message: 'The camera could not start. Choose photos or a video instead.' };
  }
}

/** Rear camera, the analysis resolution the gate wants, audio off. */
export const CAMERA_CONSTRAINTS = Object.freeze({
  audio: false,
  video: { facingMode: { ideal: 'environment' }, width: { ideal: 1920 }, height: { ideal: 1080 } },
});
