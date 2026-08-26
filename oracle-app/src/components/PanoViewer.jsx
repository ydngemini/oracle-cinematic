import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  bearingTo,
  exitsFrom,
  groupByFloor,
  initialYaw,
  nextScene,
  sceneById,
  walkOrder,
} from '../lib/tour/panoGraph';
import styles from './PanoViewer.module.css';

/**
 * PanoViewer — walk a property through linked 360° photos (tour tier 2).
 *
 * Raw WebGL, no Three.js: an equirectangular photo is one inverted UV sphere
 * with one texture, which is a shader and about a hundred lines of buffer
 * setup. Pulling in a scene graph for that would add a megabyte to the bundle
 * for geometry we can write out longhand — and CLAUDE.md forbids Three.js in
 * this app regardless.
 *
 * Movement hotspots are DOM buttons overlaid on the canvas rather than picked
 * geometry inside it. They are positioned by projected bearing when the scenes
 * carry positions, and simply listed when they do not — which means they are
 * focusable, labelled and keyboard-reachable without a parallel accessibility
 * implementation. A 360 tour that only responds to dragging is unusable for
 * anyone navigating by keyboard.
 *
 * Props: { scenes, disclosure?, address?, title?, floors?, onClose }
 */

const API_BASE = import.meta.env.VITE_API_BASE
  || (import.meta.env.DEV ? 'http://localhost:8000' : '');
const absUrl = (u) => (!u ? '' : /^https?:\/\//i.test(u) ? u : `${API_BASE}${u.startsWith('/') ? '' : '/'}${u}`);

const DISCLOSURE_FALLBACK =
  '360° photography of the property. Fixed viewpoints — not a measured survey or a substitute for an in-person showing.';

const FOV_Y = (75 * Math.PI) / 180;
const PITCH_LIMIT = Math.PI / 2 - 0.01;
const LOOK_SPEED = 0.005;
const KEY_LOOK_STEP = 0.12;

const VERT = `
attribute vec3 aPos;
attribute vec2 aUv;
uniform mat4 uProj;
uniform mat4 uView;
varying vec2 vUv;
void main() {
  vUv = aUv;
  gl_Position = uProj * uView * vec4(aPos, 1.0);
}`;

const FRAG = `
precision mediump float;
uniform sampler2D uTex;
varying vec2 vUv;
void main() {
  gl_FragColor = texture2D(uTex, vUv);
}`;

/** Inverted UV sphere — we render from inside, so winding faces inward. */
function buildSphere(segments = 48, rings = 32) {
  const positions = [];
  const uvs = [];
  const indices = [];
  for (let ring = 0; ring <= rings; ring += 1) {
    const v = ring / rings;
    const phi = v * Math.PI;
    for (let segment = 0; segment <= segments; segment += 1) {
      const u = segment / segments;
      const theta = u * Math.PI * 2;
      positions.push(
        Math.sin(phi) * Math.cos(theta),
        Math.cos(phi),
        Math.sin(phi) * Math.sin(theta),
      );
      // Flip u so the photo reads the right way round from the inside.
      uvs.push(1 - u, v);
    }
  }
  for (let ring = 0; ring < rings; ring += 1) {
    for (let segment = 0; segment < segments; segment += 1) {
      const a = ring * (segments + 1) + segment;
      const b = a + segments + 1;
      indices.push(a, b, a + 1, b, b + 1, a + 1);
    }
  }
  return {
    positions: new Float32Array(positions),
    uvs: new Float32Array(uvs),
    indices: new Uint16Array(indices),
  };
}

function perspective(fovY, aspect, near, far) {
  const f = 1 / Math.tan(fovY / 2);
  const nf = 1 / (near - far);
  return new Float32Array([
    f / aspect, 0, 0, 0,
    0, f, 0, 0,
    0, 0, (far + near) * nf, -1,
    0, 0, 2 * far * near * nf, 0,
  ]);
}

/** View matrix for a camera at the origin looking along yaw/pitch. */
function lookAt(yaw, pitch) {
  const cy = Math.cos(yaw); const sy = Math.sin(yaw);
  const cp = Math.cos(pitch); const sp = Math.sin(pitch);
  // Forward, then right and up derived from a fixed world up.
  const fx = sy * cp; const fy = sp; const fz = cy * cp;
  const rx = cy; const ry = 0; const rz = -sy;
  const ux = -sy * sp; const uy = cp; const uz = -cy * sp;
  return new Float32Array([
    rx, ux, -fx, 0,
    ry, uy, -fy, 0,
    rz, uz, -fz, 0,
    0, 0, 0, 1,
  ]);
}

function compile(gl, type, source) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(shader);
    gl.deleteShader(shader);
    throw new Error(`shader compile failed: ${log}`);
  }
  return shader;
}

export default function PanoViewer({
  scenes = [], disclosure, address, title, onClose, focusSceneId = null,
}) {
  const canvasRef = useRef(null);
  const stateRef = useRef({ yaw: 0, pitch: 0, drag: false, lastX: 0, lastY: 0 });
  const [status, setStatus] = useState('loading'); // loading | ready | error | nowebgl
  const [currentId, setCurrentId] = useState(() => walkOrder(scenes)[0]?.scene_id ?? null);

  // A guided route drives the viewer from outside; free roam still drives it
  // from within. Only an actual CHANGE of focus moves the camera, so a visitor
  // who wanders off the route is not yanked back on every re-render.
  //
  // Adjusted during render rather than in an effect: an effect would paint the
  // old scene first and correct it on the next frame, and it re-fired whenever
  // the `scenes` array identity changed even though focus had not moved. This
  // is the same render-phase reset lib/propertyImagery.js uses.
  const [lastFocus, setLastFocus] = useState(focusSceneId);
  if (focusSceneId !== lastFocus) {
    setLastFocus(focusSceneId);
    if (focusSceneId && scenes.some((sc) => sc.scene_id === focusSceneId)) {
      setCurrentId(focusSceneId);
    }
  }
  // Look direction lives in a ref so the 60fps draw loop never touches React.
  // Hotspot placement does need it at render time though, so interactions
  // mirror the ref into state — once per gesture event, not once per frame.
  // Reading the ref during render instead would be a torn read: the value
  // could change between React deciding to render and actually rendering.
  const [viewYaw, setViewYaw] = useState(0);

  const current = useMemo(() => sceneById(scenes, currentId), [scenes, currentId]);
  const exits = useMemo(() => (current ? exitsFrom(scenes, current.scene_id) : []), [scenes, current]);
  const floors = useMemo(() => groupByFloor(scenes), [scenes]);

  const go = useCallback((sceneId) => {
    if (!sceneId) return;
    setCurrentId(sceneId);
    setStatus('loading');
  }, []);

  // One GL context for the whole tour; only the texture changes between scenes.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !current) return undefined;

    let gl;
    try {
      gl = canvas.getContext('webgl2') || canvas.getContext('webgl');
    } catch {
      gl = null;
    }
    if (!gl) {
      // Same degradation as WalkableSplatViewer: no WebGL → a readable fallback
      // rather than a blank canvas.
       
      setStatus('nowebgl');
      return undefined;
    }

    let raf = 0;
    let disposed = false;
    const state = stateRef.current;
    state.yaw = initialYaw(current);
    state.pitch = 0;
     
    setViewYaw(state.yaw);

    let program;
    let texture;
    const buffers = {};
    try {
      program = gl.createProgram();
      gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, VERT));
      gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, FRAG));
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
        throw new Error(gl.getProgramInfoLog(program) || 'link failed');
      }
    } catch {
       
      setStatus('nowebgl');
      return undefined;
    }

    const sphere = buildSphere();
    buffers.pos = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffers.pos);
    gl.bufferData(gl.ARRAY_BUFFER, sphere.positions, gl.STATIC_DRAW);
    buffers.uv = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffers.uv);
    gl.bufferData(gl.ARRAY_BUFFER, sphere.uvs, gl.STATIC_DRAW);
    buffers.index = gl.createBuffer();
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, buffers.index);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, sphere.indices, gl.STATIC_DRAW);

    const aPos = gl.getAttribLocation(program, 'aPos');
    const aUv = gl.getAttribLocation(program, 'aUv');
    const uProj = gl.getUniformLocation(program, 'uProj');
    const uView = gl.getUniformLocation(program, 'uView');
    const uTex = gl.getUniformLocation(program, 'uTex');

    texture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, texture);
    // One opaque pixel so the first frames draw something rather than flashing
    // whatever was in the buffer.
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE,
      new Uint8Array([20, 22, 28, 255]));
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);

    const image = new Image();
    image.crossOrigin = 'use-credentials';
    image.onload = () => {
      if (disposed) return;
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
      setStatus('ready');
    };
    image.onerror = () => { if (!disposed) setStatus('error'); };
    image.src = absUrl(current.url);

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const width = Math.max(1, Math.floor(canvas.clientWidth * dpr));
      const height = Math.max(1, Math.floor(canvas.clientHeight * dpr));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
    };

    const draw = () => {
      if (disposed) return;
      resize();
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.clearColor(0.08, 0.09, 0.11, 1);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.useProgram(program);

      gl.bindBuffer(gl.ARRAY_BUFFER, buffers.pos);
      gl.enableVertexAttribArray(aPos);
      gl.vertexAttribPointer(aPos, 3, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, buffers.uv);
      gl.enableVertexAttribArray(aUv);
      gl.vertexAttribPointer(aUv, 2, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, buffers.index);

      const aspect = canvas.width / Math.max(1, canvas.height);
      gl.uniformMatrix4fv(uProj, false, perspective(FOV_Y, aspect, 0.01, 10));
      gl.uniformMatrix4fv(uView, false, lookAt(state.yaw, state.pitch));
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.uniform1i(uTex, 0);
      gl.drawElements(gl.TRIANGLES, sphere.indices.length, gl.UNSIGNED_SHORT, 0);
      raf = window.requestAnimationFrame(draw);
    };
    raf = window.requestAnimationFrame(draw);

    return () => {
      disposed = true;
      window.cancelAnimationFrame(raf);
      image.onload = null;
      image.onerror = null;
      try {
        gl.deleteTexture(texture);
        gl.deleteBuffer(buffers.pos);
        gl.deleteBuffer(buffers.uv);
        gl.deleteBuffer(buffers.index);
        gl.deleteProgram(program);
        // Free the GPU context deterministically — a modal that can be opened
        // repeatedly will otherwise exhaust the browser's context limit.
        gl.getExtension('WEBGL_lose_context')?.loseContext();
      } catch {
        // Teardown is best-effort; the context may already be gone.
      }
    };
  }, [current]);

  // Drag-look. Pointer events cover mouse and touch with one path.
  const onPointerDown = (event) => {
    const state = stateRef.current;
    state.drag = true;
    state.lastX = event.clientX;
    state.lastY = event.clientY;
    event.currentTarget.setPointerCapture?.(event.pointerId);
  };
  const onPointerMove = (event) => {
    const state = stateRef.current;
    if (!state.drag) return;
    state.yaw -= (event.clientX - state.lastX) * LOOK_SPEED;
    state.pitch = Math.max(-PITCH_LIMIT, Math.min(PITCH_LIMIT,
      state.pitch + (event.clientY - state.lastY) * LOOK_SPEED));
    state.lastX = event.clientX;
    state.lastY = event.clientY;
    setViewYaw(state.yaw);
  };
  const endDrag = (event) => {
    stateRef.current.drag = false;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  };

  const onKeyDown = (event) => {
    const state = stateRef.current;
    if (event.key === 'ArrowLeft') { state.yaw += KEY_LOOK_STEP; setViewYaw(state.yaw); }
    else if (event.key === 'ArrowRight') { state.yaw -= KEY_LOOK_STEP; setViewYaw(state.yaw); }
    else if (event.key === 'ArrowUp') {
      state.pitch = Math.min(PITCH_LIMIT, state.pitch + KEY_LOOK_STEP);
    } else if (event.key === 'ArrowDown') {
      state.pitch = Math.max(-PITCH_LIMIT, state.pitch - KEY_LOOK_STEP);
    } else if (event.key === 'n' || event.key === 'N') {
      go(nextScene(scenes, currentId)?.scene_id);
    } else {
      return;
    }
    event.preventDefault();
  };

  // Horizontal offset of an exit relative to where the viewer is looking, as a
  // fraction of the viewport. Null when the bearing is unknown — those exits
  // render in the room list instead of floating on the sphere.
  const hotspotOffset = (target) => {
    if (!current) return null;
    const bearing = bearingTo(current, target);
    if (bearing == null) return null;
    const yawDeg = ((-viewYaw * 180) / Math.PI) % 360;
    let delta = ((bearing - yawDeg + 540) % 360) - 180;
    const halfFov = 40;
    if (Math.abs(delta) > halfFov) return null; // behind the viewer
    return 50 + (delta / halfFov) * 45;
  };

  const total = scenes.length;

  return (
    <div className={styles.overlay} role="dialog" aria-modal="true" aria-label={`360 tour of ${address || title || 'this property'}`}>
      <div className={styles.bar}>
        <div>
          <span className={styles.kicker}>360° walkthrough</span>
          {address ? <span className={styles.addr}>{address}</span> : null}
        </div>
        <button type="button" className={styles.closeBtn} onClick={onClose} aria-label="Close 360 tour">
          Close
        </button>
      </div>

      {status === 'nowebgl' ? (
        <div className={styles.center}>
          <p className={styles.errText}>
            This browser cannot display the 360° view — WebGL is unavailable.
          </p>
          <p className={styles.hint}>
            The photographs themselves are still available from the property&apos;s photo list.
          </p>
        </div>
      ) : (
        <>
          <canvas
            ref={canvasRef}
            className={styles.canvas}
            tabIndex={0}
            aria-label={
              current
                ? `360 view: ${current.label || 'scene'}. Arrow keys look around, N moves to the next room.`
                : '360 view'
            }
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
            onKeyDown={onKeyDown}
          />

          {status === 'loading' ? (
            <div className={styles.center}><p className={styles.loadingText}>Loading 360° scene…</p></div>
          ) : null}
          {status === 'error' ? (
            <div className={styles.center}>
              <p className={styles.errText}>This 360° scene could not be loaded.</p>
            </div>
          ) : null}

          {/* Hotspots for exits we can place. Real buttons, so they focus and
              activate from the keyboard like anything else. */}
          {status === 'ready' && exits.map((exit) => {
            const offset = hotspotOffset(exit);
            if (offset == null) return null;
            return (
              <button
                key={exit.scene_id}
                type="button"
                className={styles.hotspot}
                style={{ left: `${offset}%` }}
                onClick={() => go(exit.scene_id)}
              >
                {exit.label || 'Next room'}
              </button>
            );
          })}
        </>
      )}

      <div className={styles.rooms}>
        {[...floors.entries()].map(([floorIndex, floorScenes]) => (
          <div key={floorIndex} className={styles.floorGroup}>
            <span className={styles.floorLabel}>
              {floorIndex === 0 ? 'Ground floor' : floorIndex > 0 ? `Floor ${floorIndex + 1}` : `Basement ${-floorIndex}`}
            </span>
            {floorScenes.map((scene, index) => (
              <button
                key={scene.scene_id}
                type="button"
                className={scene.scene_id === currentId ? styles.roomActive : styles.room}
                aria-current={scene.scene_id === currentId ? 'true' : undefined}
                onClick={() => go(scene.scene_id)}
              >
                {scene.label || `Scene ${index + 1}`}
              </button>
            ))}
          </div>
        ))}
      </div>

      <p className={styles.disclosure}>
        {disclosure || DISCLOSURE_FALLBACK}
        {total > 0 ? ` ${total} fixed viewpoint${total === 1 ? '' : 's'}.` : ''}
      </p>
    </div>
  );
}
