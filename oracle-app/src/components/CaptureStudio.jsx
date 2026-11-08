import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { CAMERA_CONSTRAINTS, cameraErrorState, cameraSupport } from '../lib/capture/cameraSupport';
import { STORAGE_REFUSED_MESSAGE, StorageRefused, createCaptureStore, newCaptureId } from '../lib/capture/captureStore';
import { createQualityWorker } from '../lib/capture/createQualityWorker';
import { analysisSize, hintMessage } from '../lib/capture/frameQuality';
import { HintScheduler, MIN_INTERVAL_MS } from '../lib/capture/hintScheduler';
import { CAPTURE_STEPS } from '../lib/tour/captureGuide';
import styles from './CaptureStudio.module.css';

/**
 * Capture Studio, first slice (prompt 12, T12.03 + T12.05; ledger L12-03).
 *
 * Capture a property with the phone's camera from inside Property View, keep
 * every original on the device until the server has acknowledged it, and get
 * live hints about dark, blurred or repeated views while walking.
 *
 * Reliability first (CLAUDE.md rule 4): the preview and the recording run on
 * the camera stream alone. The hints come from a sampled, downsized frame
 * analysed in a Web Worker, one frame at a time, at an interval that adapts to
 * how long the phone actually took (HintScheduler); a busy phone gets fewer
 * hints, never a stalled preview. Hints are hints: the server's capture gate
 * decides, and nothing here says "verified" or shows a percentage.
 *
 * No camera is never a dead end: unsupported browsers, a refused permission,
 * a busy or missing camera and an insecure page all offer "Choose files
 * instead", which opens the existing picker.
 *
 * Originals are staged in IndexedDB (captureStore) before upload and removed
 * only after the upload endpoint answered success for that file. A reload
 * shows what is still waiting. When the device cannot keep a copy, the studio
 * says the capture lives only in this page.
 *
 * Props:
 *   subject       stable key for the property (the Property View query string)
 *   onUpload      async (File[]) => { uploaded: string[], failed: string[] } (file names)
 *   onChooseFiles opens the existing file picker (the no-camera fallback)
 *   onClose       leave the studio
 *   store, env, createWorker  injectable for tests (captureStore, globalThis-like, worker factory)
 */
export default function CaptureStudio({
  subject, onUpload, onChooseFiles, onClose, store: storeProp, env = globalThis, createWorker = createQualityWorker,
}) {
  const support = useMemo(() => cameraSupport(env), [env]);
  const store = useMemo(() => storeProp || createCaptureStore(), [storeProp]);

  const [phase, setPhase] = useState(support.camera ? 'idle' : 'unsupported');
  const [problem, setProblem] = useState(support.insecure ? cameraErrorState({ name: 'SecurityError' }).message : null);
  const [recording, setRecording] = useState(false);
  const [staged, setStaged] = useState([]);
  const [hint, setHint] = useState(null);
  const [note, setNote] = useState(null);
  const [uploading, setUploading] = useState(false);

  const videoRef = useRef(null);
  const streamRef = useRef(null);
  const recorderRef = useRef(null);
  const workerRef = useRef(null);
  const timerRef = useRef(null);
  const canvasRef = useRef(null);
  const schedulerRef = useRef(null);

  // What an earlier visit left on this device for this property.
  useEffect(() => {
    let live = true;
    store.pending(subject).then(
      (rows) => { if (live && rows.length) setStaged((cur) => [...rows.map((r) => ({ ...r, earlier: true })), ...cur]); },
      () => {},
    );
    return () => { live = false; };
  }, [store, subject]);

  const stopCamera = useCallback(() => {
    if (timerRef.current) env.clearInterval?.(timerRef.current);
    timerRef.current = null;
    workerRef.current?.terminate?.();
    workerRef.current = null;
    if (recorderRef.current && recorderRef.current.state !== 'inactive') recorderRef.current.stop();
    streamRef.current?.getTracks?.().forEach((t) => t.stop());
    streamRef.current = null;
  }, [env]);

  useEffect(() => stopCamera, [stopCamera]);

  const keep = useCallback(async (blob, name, kind) => {
    try {
      const row = await store.stage(blob, { subject, name, kind });
      setStaged((cur) => [...cur, row]);
    } catch (err) {
      const reason = err instanceof StorageRefused ? err.reason : 'write_failed';
      setNote(STORAGE_REFUSED_MESSAGE[reason]);
      setStaged((cur) => [...cur, { id: newCaptureId(), subject, name, kind, type: blob.type, size: blob.size, blob, memoryOnly: true }]);
    }
  }, [store, subject]);

  const sampleFrame = useCallback(() => {
    const video = videoRef.current;
    const scheduler = schedulerRef.current;
    const worker = workerRef.current;
    if (!video || !scheduler || !worker || !video.videoWidth) return;
    if (!scheduler.shouldSample()) return;
    const { width, height } = analysisSize(video.videoWidth, video.videoHeight);
    const canvas = canvasRef.current || env.document.createElement('canvas');
    canvasRef.current = canvas;
    canvas.width = width;
    canvas.height = height;
    const g = canvas.getContext('2d', { willReadFrequently: true });
    if (!g) return;
    g.drawImage(video, 0, 0, width, height);
    const { data } = g.getImageData(0, 0, width, height);
    scheduler.started();
    worker.postMessage({ width, height, buffer: data.buffer }, [data.buffer]);
  }, [env]);

  const start = useCallback(async () => {
    setPhase('starting');
    setProblem(null);
    try {
      const stream = await env.navigator.mediaDevices.getUserMedia(CAMERA_CONSTRAINTS);
      streamRef.current = stream;
      if (videoRef.current) {
        videoRef.current.srcObject = stream;
        try { await videoRef.current.play?.(); } catch { /* autoplay policies: the preview starts on interaction */ }
      }
      if (support.worker) {
        try {
          const worker = createWorker();
          schedulerRef.current = new HintScheduler();
          worker.onmessage = (event) => {
            schedulerRef.current?.finished();
            setHint(hintMessage(event.data?.hints));
          };
          worker.onerror = () => { schedulerRef.current?.finished(); };
          workerRef.current = worker;
          timerRef.current = env.setInterval(sampleFrame, MIN_INTERVAL_MS);
        } catch {
          workerRef.current = null; // no hints on this device; capture still works
        }
      }
      setPhase('live');
    } catch (err) {
      const { state, message } = cameraErrorState(err);
      setPhase(state);
      setProblem(message);
    }
  }, [createWorker, env, sampleFrame, support.worker]);

  const toggleRecording = useCallback(() => {
    const stream = streamRef.current;
    if (!stream) return;
    if (recorderRef.current && recorderRef.current.state === 'recording') {
      recorderRef.current.stop();
      return;
    }
    const chunks = [];
    const recorder = new env.MediaRecorder(stream, { mimeType: support.recordingType });
    recorder.ondataavailable = (event) => { if (event.data && event.data.size) chunks.push(event.data); };
    recorder.onstop = () => {
      setRecording(false);
      const type = support.recordingType.split(';')[0];
      const blob = new Blob(chunks, { type });
      if (blob.size) keep(blob, `walkthrough-${new Date().toISOString().replace(/[:.]/g, '-')}.${type === 'video/mp4' ? 'mp4' : 'webm'}`, 'video');
    };
    recorderRef.current = recorder;
    recorder.start(1000); // a chunk a second: a crash mid-walk keeps what was flushed
    setRecording(true);
  }, [env, keep, support.recordingType]);

  const takePhoto = useCallback(() => {
    const video = videoRef.current;
    if (!video || !video.videoWidth) return;
    const canvas = env.document.createElement('canvas');
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext('2d')?.drawImage(video, 0, 0);
    canvas.toBlob((blob) => {
      if (blob) keep(blob, `photo-${new Date().toISOString().replace(/[:.]/g, '-')}.jpg`, 'photo');
    }, 'image/jpeg', 0.92);
  }, [env, keep]);

  const uploadStaged = useCallback(async () => {
    if (!staged.length) return;
    setUploading(true);
    setNote(null);
    const files = staged.map((r) => new File([r.blob], r.name, { type: r.type }));
    let result;
    try {
      result = await onUpload(files);
    } catch {
      result = { uploaded: [], failed: files.map((f) => f.name) };
    }
    const acknowledged = new Set(result?.uploaded || []);
    for (const row of staged) {
      if (acknowledged.has(row.name)) await store.remove(row.id);
    }
    setStaged((cur) => cur.filter((r) => !acknowledged.has(r.name)));
    const failed = staged.length - acknowledged.size;
    setNote(failed
      ? `${acknowledged.size} of ${staged.length} acknowledged by the server. ${failed} still saved here — try again when the connection is back.`
      : `${acknowledged.size} acknowledged by the server.`);
    setUploading(false);
  }, [onUpload, staged, store]);

  const discard = useCallback(async (row) => {
    await store.remove(row.id);
    setStaged((cur) => cur.filter((r) => r.id !== row.id));
  }, [store]);

  const fallback = phase !== 'idle' && phase !== 'starting' && phase !== 'live';
  const totalMb = staged.reduce((n, r) => n + (r.size || 0), 0) / (1024 * 1024);

  return (
    <section className={styles.studio} aria-label="Capture with camera">
      <div className={styles.head}>
        <h3>Capture with camera</h3>
        <button type="button" className={styles.link} onClick={() => { stopCamera(); onClose?.(); }}>Close</button>
      </div>

      {fallback ? (
        <div className={styles.fallback} role="alert">
          <p>{problem || 'This browser cannot use the camera here. Choose photos or a video instead.'}</p>
          <button type="button" className={styles.primary} onClick={onChooseFiles}>Choose files instead</button>
        </div>
      ) : (
        <>
          <ol className={styles.tips}>
            {CAPTURE_STEPS.slice(0, 3).map((s) => <li key={s.title}><strong>{s.title}.</strong> {s.text}</li>)}
          </ol>
          <div className={styles.preview}>
            <video ref={videoRef} className={styles.video} muted playsInline aria-label="Camera preview" />
            {phase === 'live' && hint ? <p className={styles.hint}>{hint}</p> : null}
          </div>
          <p className={styles.srOnly} role="status" aria-live="polite">{phase === 'live' ? hint || '' : ''}</p>
          <p className={styles.small}>
            Live hints are a quick check on this phone only. Neoh checks every frame again after upload.
          </p>
          <div className={styles.actions}>
            {phase !== 'live' ? (
              <button type="button" className={styles.primary} onClick={start} disabled={phase === 'starting'}>
                {phase === 'starting' ? 'Starting camera…' : 'Start camera'}
              </button>
            ) : (
              <>
                {support.recording ? (
                  <button type="button" className={styles.primary} onClick={toggleRecording} aria-pressed={recording}>
                    {recording ? 'Stop recording' : 'Record walkthrough'}
                  </button>
                ) : null}
                <button type="button" className={styles.secondary} onClick={takePhoto} disabled={recording}>Take photo</button>
              </>
            )}
            <button type="button" className={styles.link} onClick={onChooseFiles}>Choose files instead</button>
          </div>
        </>
      )}

      {staged.length ? (
        <div className={styles.staged}>
          <h4>Waiting to upload ({staged.length}, {totalMb.toFixed(1)} MB)</h4>
          <p className={styles.small}>Signing out discards anything not uploaded.</p>
          <ul>
            {staged.map((r) => (
              <li key={r.id}>
                <span>{r.name}</span>
                <span className={styles.small}>
                  {r.earlier ? 'saved on this device earlier' : r.memoryOnly ? 'in this page only' : 'saved on this device'}
                </span>
                <button type="button" className={styles.link} onClick={() => discard(r)} disabled={uploading}>Discard</button>
              </li>
            ))}
          </ul>
          <button type="button" className={styles.primary} onClick={uploadStaged} disabled={uploading || recording}>
            {uploading ? 'Uploading…' : `Upload ${staged.length}`}
          </button>
        </div>
      ) : null}

      {note ? <p className={styles.note} role="status">{note}</p> : null}
    </section>
  );
}
