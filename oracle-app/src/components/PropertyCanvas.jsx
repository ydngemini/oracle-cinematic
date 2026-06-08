import { useRef, useEffect, useState, useMemo, useCallback } from 'react';
import { useOracleState } from '../state';
import * as SPLAT from 'gsplat';
import styles from './PropertyCanvas.module.css';

// ─── Camera Presets ──────────────────────────────────────────────────────────

const VIEWS = {
  ORBIT_EXTERIOR: { eye: [4.5, 3.0, 5.5], center: [0, 0.8, 0], fov: 55 },
  INTERIOR_WALK: { eye: [0.0, 1.5, 0.2], center: [-1.2, 1.2, -1.5], fov: 70 },
  BIRDS_EYE: { eye: [0.0, 12.0, 0.2], center: [0, 0, 0], fov: 40 },
  CINEMATIC_ENTRY: { eye: [8.0, 4.0, 8.0], center: [0, 0.5, 0], fov: 45 },
  STREET_LEVEL: { eye: [6.0, 1.6, 0.0], center: [0, 1.2, 0], fov: 60 },
};

const SPRING_DAMPING = 0.92;
const SPRING_STIFFNESS = 0.04;
const AUTO_ORBIT_SPEED = 0.0003;
const PARTICLE_COUNT = 120;

// ─── Math Utilities ──────────────────────────────────────────────────────────

function lerp(a, b, t) { return a + (b - a) * t; }
function clamp(v, min, max) { return Math.max(min, Math.min(max, v)); }

function springToward(current, target, velocity, stiffness, damping) {
  const force = (target - current) * stiffness;
  velocity = (velocity + force) * damping;
  return [current + velocity, velocity];
}

function buildViewMatrix(eye, center) {
  const fx = center[0] - eye[0];
  const fy = center[1] - eye[1];
  const fz = center[2] - eye[2];
  const fLen = Math.sqrt(fx * fx + fy * fy + fz * fz) || 1;
  const f = [fx / fLen, fy / fLen, fz / fLen];
  const up = [0, 1, 0];
  const s = [
    f[1] * up[2] - f[2] * up[1],
    f[2] * up[0] - f[0] * up[2],
    f[0] * up[1] - f[1] * up[0],
  ];
  const sLen = Math.sqrt(s[0] * s[0] + s[1] * s[1] + s[2] * s[2]) || 1;
  s[0] /= sLen; s[1] /= sLen; s[2] /= sLen;
  const u = [
    s[1] * f[2] - s[2] * f[1],
    s[2] * f[0] - s[0] * f[2],
    s[0] * f[1] - s[1] * f[0],
  ];
  return new Float32Array([
    s[0], u[0], -f[0], 0,
    s[1], u[1], -f[1], 0,
    s[2], u[2], -f[2], 0,
    -(s[0] * eye[0] + s[1] * eye[1] + s[2] * eye[2]),
    -(u[0] * eye[0] + u[1] * eye[1] + u[2] * eye[2]),
    (f[0] * eye[0] + f[1] * eye[1] + f[2] * eye[2]),
    1,
  ]);
}

function buildPerspective(fovDeg, aspect, near, far) {
  const f = 1.0 / Math.tan((fovDeg * Math.PI / 180) / 2);
  const nf = 1 / (near - far);
  return new Float32Array([
    f / aspect, 0, 0, 0,
    0, f, 0, 0,
    0, 0, (far + near) * nf, -1,
    0, 0, 2 * far * near * nf, 0,
  ]);
}

function multiplyMat4(out, a, b) {
  for (let i = 0; i < 4; i++) {
    for (let j = 0; j < 4; j++) {
      out[i * 4 + j] =
        a[0 * 4 + j] * b[i * 4 + 0] +
        a[1 * 4 + j] * b[i * 4 + 1] +
        a[2 * 4 + j] * b[i * 4 + 2] +
        a[3 * 4 + j] * b[i * 4 + 3];
    }
  }
  return out;
}

function projectToScreen(worldPos, viewMat, projMat, w, h) {
  const [x, y, z] = worldPos;
  const vx = viewMat[0]*x + viewMat[4]*y + viewMat[8]*z + viewMat[12];
  const vy = viewMat[1]*x + viewMat[5]*y + viewMat[9]*z + viewMat[13];
  const vz = viewMat[2]*x + viewMat[6]*y + viewMat[10]*z + viewMat[14];
  const vw = viewMat[3]*x + viewMat[7]*y + viewMat[11]*z + viewMat[15];
  const px = projMat[0]*vx + projMat[4]*vy + projMat[8]*vz + projMat[12]*vw;
  const py = projMat[1]*vx + projMat[5]*vy + projMat[9]*vz + projMat[13]*vw;
  const pz = projMat[2]*vx + projMat[6]*vy + projMat[10]*vz + projMat[14]*vw;
  const pw = projMat[3]*vx + projMat[7]*vy + projMat[11]*vz + projMat[15]*vw;
  if (pw <= 0) return null;
  const ndcX = px / pw, ndcY = py / pw, ndcZ = pz / pw;
  if (ndcZ < -1 || ndcZ > 1) return null;
  return { x: ((ndcX + 1) / 2) * w, y: ((1 - ndcY) / 2) * h, depth: ndcZ };
}

// ─── Particle System (GPU-accelerated ambient dust) ──────────────────────────

const PARTICLE_VS = `
  attribute vec3 a_pos;
  attribute float a_life;
  uniform float u_time;
  uniform mat4 u_vp;
  uniform vec2 u_mouse;
  varying float v_alpha;
  varying float v_size;

  void main() {
    float t = fract(a_life + u_time * 0.05);
    vec3 p = a_pos;
    p.y += t * 6.0 - 3.0;
    p.x += sin(t * 6.28 + a_pos.z * 2.0) * 0.3;
    p.z += cos(t * 4.71 + a_pos.x * 3.0) * 0.2;

    // Mouse repulsion
    vec4 clip = u_vp * vec4(p, 1.0);
    vec2 ndc = clip.xy / clip.w;
    vec2 mouseNdc = u_mouse * 2.0 - 1.0;
    mouseNdc.y = -mouseNdc.y;
    float mouseDist = length(ndc - mouseNdc);
    float repel = smoothstep(0.3, 0.0, mouseDist) * 0.4;
    p.xz += normalize(ndc - mouseNdc).xy * repel;

    gl_Position = u_vp * vec4(p, 1.0);
    float fade = 1.0 - abs(t * 2.0 - 1.0);
    v_alpha = fade * fade * 0.4;
    v_size = mix(1.5, 4.0, fade);
    gl_PointSize = v_size * (2.0 / max(gl_Position.w, 0.5));
  }
`;

const PARTICLE_FS = `
  precision mediump float;
  varying float v_alpha;
  void main() {
    float r = length(gl_PointCoord - 0.5) * 2.0;
    if (r > 1.0) discard;
    float soft = 1.0 - r * r;
    gl_FragColor = vec4(0.75, 0.65, 0.5, v_alpha * soft * soft);
  }
`;

// ─── Grid Floor Shader ───────────────────────────────────────────────────────

const GRID_VS = `
  attribute vec2 a_pos;
  uniform mat4 u_vp;
  uniform float u_time;
  varying vec3 v_worldPos;
  void main() {
    vec3 world = vec3(a_pos.x * 20.0, 0.0, a_pos.y * 20.0);
    v_worldPos = world;
    gl_Position = u_vp * vec4(world, 1.0);
  }
`;

const GRID_FS = `
  precision highp float;
  varying vec3 v_worldPos;
  uniform float u_time;
  uniform vec2 u_mouse;

  float grid(vec2 p, float spacing) {
    vec2 g = abs(fract(p / spacing - 0.5) - 0.5) / fwidth(p / spacing);
    return 1.0 - min(min(g.x, g.y), 1.0);
  }

  void main() {
    float dist = length(v_worldPos.xz);
    float fade = 1.0 - smoothstep(4.0, 16.0, dist);

    float g1 = grid(v_worldPos.xz, 1.0) * 0.15;
    float g2 = grid(v_worldPos.xz, 5.0) * 0.25;

    float pulse = 0.5 + 0.5 * sin(u_time * 0.5 + dist * 0.3);
    vec3 color = mix(vec3(0.2, 0.18, 0.15), vec3(0.4, 0.32, 0.2), pulse * 0.3);
    float alpha = (g1 + g2) * fade * fade;

    gl_FragColor = vec4(color, alpha * 0.6);
  }
`;

// ─── GIS Boundary Shader ─────────────────────────────────────────────────────

const BOUNDARY_VS = `
  attribute vec3 a_position;
  uniform mat4 u_vp;
  uniform float u_time;
  varying float v_dist;
  void main() {
    gl_Position = u_vp * vec4(a_position, 1.0);
    v_dist = length(a_position.xz) + u_time * 0.5;
  }
`;

const BOUNDARY_FS = `
  precision mediump float;
  uniform float u_time;
  varying float v_dist;
  void main() {
    float pulse = 0.5 + 0.5 * sin(v_dist * 5.0 - u_time * 3.0);
    vec3 amber = vec3(0.82, 0.58, 0.12);
    float alpha = pulse * 0.7;
    gl_FragColor = vec4(amber * (0.6 + pulse * 0.4), alpha);
  }
`;

// ─── AR Data Anchors ─────────────────────────────────────────────────────────

const ANCHORS = [
  { id: 'roof', pos: [0.0, 3.2, 0.0], label: 'ROOF', key: 'roofAge', fmt: v => `Age: ${v || 12}yr` },
  { id: 'hvac', pos: [-1.8, 0.6, 1.2], label: 'HVAC', key: 'hvacAge', fmt: v => `Age: ${v || 8}yr` },
  { id: 'foundation', pos: [0.0, -0.1, 0.0], label: 'FOUNDATION', key: 'foundationType', fmt: v => v || 'Poured Concrete' },
  { id: 'garage', pos: [2.5, 1.0, -0.5], label: 'GARAGE', key: 'garageType', fmt: v => v || '2-Car Attached' },
  { id: 'lot', pos: [3.0, 0.05, 3.0], label: 'LOT', key: 'lotAcres', fmt: v => `${v || 0.34} ac` },
  { id: 'kitchen', pos: [-0.5, 1.2, -1.0], label: 'KITCHEN', key: 'kitchenGrade', fmt: v => v || 'Grade B+' },
  { id: 'arv', pos: [0.0, 4.0, 0.0], label: 'ARV', key: 'arv', fmt: v => v ? `$${(v/1000).toFixed(0)}K` : '$285K' },
];

// ─── Main Component ──────────────────────────────────────────────────────────

export function PropertyCanvas() {
  const canvasRef = useRef(null);
  const { isFurnished, activeAgent, propertyData, gisBoundaryVisible, anchorsVisible } = useOracleState();

  const rendererRef = useRef(null);
  const sceneRef = useRef(null);
  const cameraRef = useRef(null);
  const animRef = useRef(null);
  const currentSplatUrl = useRef(null);

  const [loading, setLoading] = useState(false);
  const [loadProgress, setLoadProgress] = useState(0);
  const [splatLoaded, setSplatLoaded] = useState(false);
  const [currentView, setCurrentView] = useState('CINEMATIC_ENTRY');
  const [anchorPositions, setAnchorPositions] = useState({});
  const [hoverAnchor, setHoverAnchor] = useState(null);
  const [webglSupported, setWebglSupported] = useState(true);

  // Camera spring state
  const camState = useRef({
    eye: [...VIEWS.CINEMATIC_ENTRY.eye],
    center: [...VIEWS.CINEMATIC_ENTRY.center],
    fov: VIEWS.CINEMATIC_ENTRY.fov,
    targetEye: [...VIEWS.CINEMATIC_ENTRY.eye],
    targetCenter: [...VIEWS.CINEMATIC_ENTRY.center],
    targetFov: VIEWS.CINEMATIC_ENTRY.fov,
    velEye: [0, 0, 0],
    velCenter: [0, 0, 0],
    velFov: 0,
    orbitAngle: 0,
    autoOrbit: true,
    dragging: false,
    lastMouse: [0, 0],
    orbitRadius: 6.5,
    orbitHeight: 3.0,
    orbitTarget: [0, 0.8, 0],
  });

  const mouse = useRef({ x: 0.5, y: 0.5, nx: 0.5, ny: 0.5 });
  const timeRef = useRef(0);
  const frameRef = useRef(0);

  // WebGL overlay state
  const glState = useRef({
    gl: null,
    particleProgram: null,
    particleVAO: null,
    particleCount: PARTICLE_COUNT,
    gridProgram: null,
    gridVBO: null,
    boundaryProgram: null,
    boundaryGeo: null,
  });

  // Mirror visibility flags into refs so the rAF loop reads the latest value
  // without re-subscribing. Assigned in an effect (not during render) per the
  // rules-of-refs lint.
  const gisBoundaryVisibleRef = useRef(gisBoundaryVisible);
  const anchorsVisibleRef = useRef(anchorsVisible);
  useEffect(() => {
    gisBoundaryVisibleRef.current = gisBoundaryVisible;
    anchorsVisibleRef.current = anchorsVisible;
  }, [gisBoundaryVisible, anchorsVisible]);

  const assessorData = useMemo(() => ({
    roofAge: propertyData?.roofAge,
    hvacAge: propertyData?.hvacAge,
    foundationType: propertyData?.foundationType,
    garageType: propertyData?.garageType,
    lotAcres: propertyData?.lotAcres,
    kitchenGrade: propertyData?.kitchenGrade,
    arv: propertyData?.arv,
  }), [propertyData]);

  // ─── Resolve view from agent/state ─────────────────────────────────────

  const resolveView = useCallback((agent, furnished) => {
    if (agent?.includes('INTERIOR')) return VIEWS.INTERIOR_WALK;
    if (furnished) return VIEWS.INTERIOR_WALK;
    if (agent?.includes('BIRDS_EYE')) return VIEWS.BIRDS_EYE;
    if (agent?.includes('STREET')) return VIEWS.STREET_LEVEL;
    return null;
  }, []);

  // ─── Shader compilation ────────────────────────────────────────────────

  const compileShader = useCallback((gl, type, src) => {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, src);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      console.warn('[Spatial]', gl.getShaderInfoLog(shader));
      gl.deleteShader(shader);
      return null;
    }
    return shader;
  }, []);

  const buildProgram = useCallback((gl, vs, fs) => {
    const v = compileShader(gl, gl.VERTEX_SHADER, vs);
    const f = compileShader(gl, gl.FRAGMENT_SHADER, fs);
    if (!v || !f) return null;
    const p = gl.createProgram();
    gl.attachShader(p, v);
    gl.attachShader(p, f);
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
      console.warn('[Spatial] Link:', gl.getProgramInfoLog(p));
      return null;
    }
    return p;
  }, [compileShader]);

  // ─── GIS Boundary ──────────────────────────────────────────────────────

  const loadGISBoundary = useCallback((coords, originLat, originLon) => {
    const gl = glState.current.gl;
    if (!gl) return;

    if (!glState.current.boundaryProgram) {
      glState.current.boundaryProgram = buildProgram(gl, BOUNDARY_VS, BOUNDARY_FS);
    }
    if (!glState.current.boundaryProgram) return;

    const DEG_LAT = 111320;
    const DEG_LON = 111320 * Math.cos((originLat * Math.PI) / 180);
    const scale = 0.08;
    const local = coords.map(([lon, lat]) => [
      (lon - originLon) * DEG_LON * scale,
      0,
      -(lat - originLat) * DEG_LAT * scale,
    ]);

    const verts = [];
    const wallH = 0.15;
    for (let i = 0; i < local.length; i++) {
      const c = local[i], n = local[(i + 1) % local.length];
      verts.push(c[0], 0, c[2], n[0], 0, n[2]);
      verts.push(c[0], 0, c[2], c[0], wallH, c[2]);
      verts.push(n[0], 0, n[2], n[0], wallH, n[2]);
      verts.push(c[0], wallH, c[2], n[0], wallH, n[2]);
    }

    const vbo = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(verts), gl.STATIC_DRAW);
    glState.current.boundaryGeo = { vbo, count: verts.length / 3 };
  }, [buildProgram]);

  const ingestGISBoundary = useCallback((geojson) => {
    if (!geojson?.geometry?.coordinates) return;
    const coords = geojson.geometry.type === 'Polygon'
      ? geojson.geometry.coordinates[0] : geojson.geometry.coordinates;
    const lat = coords.reduce((s, c) => s + c[1], 0) / coords.length;
    const lon = coords.reduce((s, c) => s + c[0], 0) / coords.length;
    loadGISBoundary(coords, lat, lon);
  }, [loadGISBoundary]);

  useEffect(() => {
    window.__oracleIngestGIS = ingestGISBoundary;
    return () => { delete window.__oracleIngestGIS; };
  }, [ingestGISBoundary]);

  useEffect(() => {
    if (propertyData?.parcelBoundary) ingestGISBoundary(propertyData.parcelBoundary);
  }, [propertyData?.parcelBoundary, ingestGISBoundary]);

  // ─── Init WebGL + Render Loop ──────────────────────────────────────────

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    // gsplat's WebGLRenderer creates its own GL context and throws if WebGL is
    // unavailable (GPU blocklisted, disabled, headless, some VMs). Without this
    // guard the throw is uncaught inside the effect and React tears down the
    // entire app to a blank screen. Degrade to a CSS fallback instead.
    let renderer;
    try {
      renderer = new SPLAT.WebGLRenderer(canvas);
    } catch (err) {
      console.error('[Spatial] WebGL unavailable — spatial viewer disabled:', err);
      // One-time capability flag flip on init failure (renders the fallback).
      // Not a cascading-render risk — the effect returns immediately after.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setWebglSupported(false);
      return;
    }
    const scene = new SPLAT.Scene();
    const camera = new SPLAT.Camera();
    camera.data.setSize(canvas.clientWidth, canvas.clientHeight);
    camera.position = new SPLAT.Vector3(...VIEWS.CINEMATIC_ENTRY.eye);
    camera.target = new SPLAT.Vector3(...VIEWS.CINEMATIC_ENTRY.center);

    rendererRef.current = renderer;
    sceneRef.current = scene;
    cameraRef.current = camera;

    // Overlay WebGL context for particles/grid/boundary
    const overlayCanvas = document.createElement('canvas');
    overlayCanvas.className = styles.overlayCanvas;
    overlayCanvas.width = canvas.clientWidth * window.devicePixelRatio;
    overlayCanvas.height = canvas.clientHeight * window.devicePixelRatio;
    overlayCanvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:2;';
    canvas.parentElement.appendChild(overlayCanvas);

    const gl = overlayCanvas.getContext('webgl', { alpha: true, premultipliedAlpha: false, antialias: true });
    glState.current.gl = gl;

    if (gl) {
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      gl.clearColor(0, 0, 0, 0);

      // Particles
      const pp = buildProgram(gl, PARTICLE_VS, PARTICLE_FS);
      if (pp) {
        glState.current.particleProgram = pp;
        const positions = [];
        for (let i = 0; i < PARTICLE_COUNT; i++) {
          positions.push(
            (Math.random() - 0.5) * 10,
            (Math.random() - 0.5) * 6,
            (Math.random() - 0.5) * 10,
            Math.random(),
          );
        }
        const buf = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, buf);
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(positions), gl.STATIC_DRAW);
        glState.current.particleVAO = buf;
      }

      // Grid floor
      const gp = buildProgram(gl, GRID_VS, GRID_FS);
      if (gp) {
        glState.current.gridProgram = gp;
        const quad = new Float32Array([-1, -1, 1, -1, -1, 1, 1, -1, 1, 1, -1, 1]);
        const gbuf = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, gbuf);
        gl.bufferData(gl.ARRAY_BUFFER, quad, gl.STATIC_DRAW);
        glState.current.gridVBO = gbuf;
      }
    }

    const startTime = performance.now();

    function render() {
      const now = performance.now();
      const dt = (now - startTime) / 1000;
      timeRef.current = dt;
      frameRef.current++;

      // Spring camera
      const cs = camState.current;
      if (cs.autoOrbit && !cs.dragging) {
        cs.orbitAngle += AUTO_ORBIT_SPEED * 16;
        cs.targetEye[0] = Math.sin(cs.orbitAngle) * cs.orbitRadius + cs.orbitTarget[0];
        cs.targetEye[1] = cs.orbitHeight;
        cs.targetEye[2] = Math.cos(cs.orbitAngle) * cs.orbitRadius + cs.orbitTarget[2];
        cs.targetCenter = [...cs.orbitTarget];
      }

      for (let i = 0; i < 3; i++) {
        const [newVal, newVel] = springToward(cs.eye[i], cs.targetEye[i], cs.velEye[i], SPRING_STIFFNESS, SPRING_DAMPING);
        cs.eye[i] = newVal;
        cs.velEye[i] = newVel;
      }
      for (let i = 0; i < 3; i++) {
        const [newVal, newVel] = springToward(cs.center[i], cs.targetCenter[i], cs.velCenter[i], SPRING_STIFFNESS, SPRING_DAMPING);
        cs.center[i] = newVal;
        cs.velCenter[i] = newVel;
      }
      const [newFov, newFovVel] = springToward(cs.fov, cs.targetFov, cs.velFov, SPRING_STIFFNESS, SPRING_DAMPING);
      cs.fov = newFov;
      cs.velFov = newFovVel;

      // Smooth mouse
      mouse.current.x = lerp(mouse.current.x, mouse.current.nx, 0.08);
      mouse.current.y = lerp(mouse.current.y, mouse.current.ny, 0.08);

      // Subtle mouse parallax on camera target
      if (!cs.dragging && cs.autoOrbit) {
        const mx = (mouse.current.x - 0.5) * 0.3;
        const my = (mouse.current.y - 0.5) * 0.15;
        cs.targetCenter[0] = cs.orbitTarget[0] + mx;
        cs.targetCenter[1] = cs.orbitTarget[1] - my;
      }

      // Update gsplat camera
      camera.position = new SPLAT.Vector3(...cs.eye);
      camera.target = new SPLAT.Vector3(...cs.center);

      // Render splat scene
      renderer.render(scene, camera);

      // Overlay pass
      if (gl) {
        const w = overlayCanvas.width;
        const h = overlayCanvas.height;
        gl.viewport(0, 0, w, h);
        gl.clear(gl.COLOR_BUFFER_BIT);

        const aspect = w / h;
        const viewMat = buildViewMatrix(cs.eye, cs.center);
        const projMat = buildPerspective(cs.fov, aspect, 0.1, 100.0);
        const vpMat = new Float32Array(16);
        multiplyMat4(vpMat, projMat, viewMat);

        // Grid floor
        if (glState.current.gridProgram && glState.current.gridVBO) {
          const gp = glState.current.gridProgram;
          gl.useProgram(gp);
          gl.uniformMatrix4fv(gl.getUniformLocation(gp, 'u_vp'), false, vpMat);
          gl.uniform1f(gl.getUniformLocation(gp, 'u_time'), dt);
          gl.uniform2f(gl.getUniformLocation(gp, 'u_mouse'), mouse.current.x, mouse.current.y);
          gl.bindBuffer(gl.ARRAY_BUFFER, glState.current.gridVBO);
          const posAttr = gl.getAttribLocation(gp, 'a_pos');
          gl.enableVertexAttribArray(posAttr);
          gl.vertexAttribPointer(posAttr, 2, gl.FLOAT, false, 0, 0);
          gl.drawArrays(gl.TRIANGLES, 0, 6);
          gl.disableVertexAttribArray(posAttr);
        }

        // Particles
        if (glState.current.particleProgram && glState.current.particleVAO) {
          const pp = glState.current.particleProgram;
          gl.useProgram(pp);
          gl.uniformMatrix4fv(gl.getUniformLocation(pp, 'u_vp'), false, vpMat);
          gl.uniform1f(gl.getUniformLocation(pp, 'u_time'), dt);
          gl.uniform2f(gl.getUniformLocation(pp, 'u_mouse'), mouse.current.x, mouse.current.y);
          gl.bindBuffer(gl.ARRAY_BUFFER, glState.current.particleVAO);
          const posA = gl.getAttribLocation(pp, 'a_pos');
          const lifeA = gl.getAttribLocation(pp, 'a_life');
          gl.enableVertexAttribArray(posA);
          gl.vertexAttribPointer(posA, 3, gl.FLOAT, false, 16, 0);
          if (lifeA >= 0) {
            gl.enableVertexAttribArray(lifeA);
            gl.vertexAttribPointer(lifeA, 1, gl.FLOAT, false, 16, 12);
          }
          gl.drawArrays(gl.POINTS, 0, PARTICLE_COUNT);
          gl.disableVertexAttribArray(posA);
          if (lifeA >= 0) gl.disableVertexAttribArray(lifeA);
        }

        // GIS Boundary
        if (glState.current.boundaryProgram && glState.current.boundaryGeo && gisBoundaryVisibleRef.current) {
          const bp = glState.current.boundaryProgram;
          const geo = glState.current.boundaryGeo;
          gl.useProgram(bp);
          gl.uniformMatrix4fv(gl.getUniformLocation(bp, 'u_vp'), false, vpMat);
          gl.uniform1f(gl.getUniformLocation(bp, 'u_time'), dt);
          gl.bindBuffer(gl.ARRAY_BUFFER, geo.vbo);
          const bpos = gl.getAttribLocation(bp, 'a_position');
          gl.enableVertexAttribArray(bpos);
          gl.vertexAttribPointer(bpos, 3, gl.FLOAT, false, 0, 0);
          gl.drawArrays(gl.LINES, 0, geo.count);
          gl.disableVertexAttribArray(bpos);
        }

        // Project anchors (every 3 frames)
        if (frameRef.current % 3 === 0 && anchorsVisibleRef.current) {
          const batch = {};
          for (const a of ANCHORS) {
            const s = projectToScreen(a.pos, viewMat, projMat, w, h);
            if (s && s.x > 0 && s.x < w && s.y > 0 && s.y < h) {
              batch[a.id] = { x: s.x / window.devicePixelRatio, y: s.y / window.devicePixelRatio, depth: s.depth };
            }
          }
          setAnchorPositions(batch);
        }
      }

      animRef.current = requestAnimationFrame(render);
    }

    render();

    const handleResize = () => {
      camera.data.setSize(canvas.clientWidth, canvas.clientHeight);
      if (overlayCanvas) {
        overlayCanvas.width = canvas.clientWidth * window.devicePixelRatio;
        overlayCanvas.height = canvas.clientHeight * window.devicePixelRatio;
      }
    };
    window.addEventListener('resize', handleResize);

    return () => {
      window.removeEventListener('resize', handleResize);
      if (animRef.current) cancelAnimationFrame(animRef.current);
      renderer.dispose();
      overlayCanvas?.remove();
    };
  }, [buildProgram]);

  // ─── Mouse + Orbital Controls ──────────────────────────────────────────

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const onMove = (e) => {
      const rect = canvas.getBoundingClientRect();
      mouse.current.nx = (e.clientX - rect.left) / rect.width;
      mouse.current.ny = (e.clientY - rect.top) / rect.height;

      if (camState.current.dragging) {
        const dx = e.clientX - camState.current.lastMouse[0];
        const dy = e.clientY - camState.current.lastMouse[1];
        camState.current.orbitAngle -= dx * 0.005;
        camState.current.orbitHeight = clamp(camState.current.orbitHeight + dy * 0.02, 0.5, 12.0);
        camState.current.lastMouse = [e.clientX, e.clientY];
      }
    };

    const onDown = (e) => {
      camState.current.dragging = true;
      camState.current.autoOrbit = false;
      camState.current.lastMouse = [e.clientX, e.clientY];
      canvas.style.cursor = 'grabbing';
    };

    const onUp = () => {
      camState.current.dragging = false;
      canvas.style.cursor = 'grab';
      setTimeout(() => { camState.current.autoOrbit = true; }, 3000);
    };

    const onWheel = (e) => {
      e.preventDefault();
      camState.current.orbitRadius = clamp(camState.current.orbitRadius + e.deltaY * 0.005, 2.0, 15.0);
    };

    const onDblClick = () => {
      const views = Object.keys(VIEWS);
      const idx = views.indexOf(currentView);
      const next = views[(idx + 1) % views.length];
      setCurrentView(next);
      const v = VIEWS[next];
      camState.current.targetEye = [...v.eye];
      camState.current.targetCenter = [...v.center];
      camState.current.targetFov = v.fov;
      camState.current.autoOrbit = next === 'CINEMATIC_ENTRY' || next === 'ORBIT_EXTERIOR';
      if (camState.current.autoOrbit) {
        camState.current.orbitRadius = Math.hypot(v.eye[0], v.eye[2]);
        camState.current.orbitHeight = v.eye[1];
        camState.current.orbitAngle = Math.atan2(v.eye[0], v.eye[2]);
      }
    };

    canvas.addEventListener('mousemove', onMove);
    canvas.addEventListener('mousedown', onDown);
    canvas.addEventListener('mouseup', onUp);
    canvas.addEventListener('mouseleave', onUp);
    canvas.addEventListener('wheel', onWheel, { passive: false });
    canvas.addEventListener('dblclick', onDblClick);
    canvas.style.cursor = 'grab';

    return () => {
      canvas.removeEventListener('mousemove', onMove);
      canvas.removeEventListener('mousedown', onDown);
      canvas.removeEventListener('mouseup', onUp);
      canvas.removeEventListener('mouseleave', onUp);
      canvas.removeEventListener('wheel', onWheel);
      canvas.removeEventListener('dblclick', onDblClick);
    };
  }, [currentView]);

  // ─── Agent-driven view transitions ─────────────────────────────────────

  useEffect(() => {
    const view = resolveView(activeAgent, isFurnished);
    if (!view) return;
    camState.current.targetEye = [...view.eye];
    camState.current.targetCenter = [...view.center];
    camState.current.targetFov = view.fov;
    camState.current.autoOrbit = false;
  }, [activeAgent, isFurnished, resolveView]);

  // ─── Splat Loading ─────────────────────────────────────────────────────

  useEffect(() => {
    if (!sceneRef.current || !propertyData?.splatUrl) return;
    const url = propertyData.splatUrl;
    if (url === currentSplatUrl.current) return;
    currentSplatUrl.current = url;

    const scene = sceneRef.current;
    setLoading(true);
    setLoadProgress(0);
    setSplatLoaded(false);
    scene.reset();

    SPLAT.Loader.LoadAsync(url, scene, (progress) => {
      setLoadProgress(Math.round(progress * 100));
    }).then(() => {
      setSplatLoaded(true);
      setLoading(false);
    }).catch((err) => {
      console.error('[Spatial] Splat load failed:', err);
      setLoading(false);
    });
  }, [propertyData?.splatUrl]);

  // ─── Render ────────────────────────────────────────────────────────────

  return (
    <div className={styles.container} data-loaded={splatLoaded}>
      <canvas ref={canvasRef} className={styles.canvas} />

      {/* WebGL unsupported fallback — keeps the rest of the C2 UI alive */}
      {!webglSupported && (
        <div className={styles.webglFallback}>
          <span className={styles.webglFallbackTitle}>SPATIAL VIEWER UNAVAILABLE</span>
          <span className={styles.webglFallbackHint}>
            WebGL is disabled or unsupported in this browser. Property data and
            agent telemetry remain fully active.
          </span>
        </div>
      )}

      {/* Cinematic vignette */}
      <div className={styles.vignette} />

      {/* Ambient gradient glow following mouse */}
      <div
        className={styles.ambientGlow}
        style={{
          '--glow-x': `${mouse.current.x * 100}%`,
          '--glow-y': `${mouse.current.y * 100}%`,
        }}
      />

      {/* View mode indicator */}
      <div className={styles.viewIndicator}>
        <div className={styles.viewDot} />
        <span className={styles.viewLabel}>{currentView.replace(/_/g, ' ')}</span>
        <span className={styles.viewHint}>Double-click to cycle views</span>
      </div>

      {/* AR Spatial Anchors */}
      {anchorsVisible && (
        <div className={styles.anchorLayer}>
          {ANCHORS.map((anchor) => {
            const pos = anchorPositions[anchor.id];
            if (!pos) return null;
            const scale = 1.0 - pos.depth * 0.25;
            const isHovered = hoverAnchor === anchor.id;
            return (
              <div
                key={anchor.id}
                className={styles.anchorCard}
                data-active={isHovered}
                style={{
                  transform: `translate(-50%, -100%) translate(${pos.x}px, ${pos.y}px) scale(${scale})`,
                  opacity: clamp(scale + 0.2, 0, 1),
                }}
                onMouseEnter={() => setHoverAnchor(anchor.id)}
                onMouseLeave={() => setHoverAnchor(null)}
              >
                <span className={styles.anchorLabel}>{anchor.label}</span>
                <span className={styles.anchorValue}>{anchor.fmt(assessorData[anchor.key])}</span>
                <div className={styles.anchorStem} />
                <div className={styles.anchorPulse} />
              </div>
            );
          })}
        </div>
      )}

      {/* GIS indicator */}
      {gisBoundaryVisible && glState.current.boundaryGeo && (
        <div className={styles.gisIndicator}>
          <span className={styles.gisIndicatorDot} />
          <span>GIS BOUNDARY</span>
        </div>
      )}

      {/* Loading state */}
      {loading && (
        <div className={styles.loadingOverlay}>
          <div className={styles.loadingRing}>
            <svg viewBox="0 0 100 100">
              <circle cx="50" cy="50" r="40" className={styles.loadingTrack} />
              <circle
                cx="50" cy="50" r="40"
                className={styles.loadingProgress}
                style={{ strokeDashoffset: 251 - (251 * loadProgress / 100) }}
              />
            </svg>
            <span className={styles.loadingPct}>{loadProgress}%</span>
          </div>
          <span className={styles.loadingText}>RECONSTRUCTING SPATIAL MODEL</span>
          <div className={styles.loadingSubtext}>DUSt3R → Gaussian Splatting</div>
        </div>
      )}

      {/* Status badges */}
      <div className={styles.badgeRow}>
        {splatLoaded && <span className={styles.badge} data-type="splat">PHOTOREALISTIC</span>}
        {isFurnished && <span className={styles.badge} data-type="furnished">FURNISHED</span>}
        {propertyData?.address && <span className={styles.badge} data-type="spatial">SPATIAL</span>}
      </div>

      {/* Reconstruction trigger button */}
      {!splatLoaded && !loading && propertyData?.address && (
        <button
          className={styles.reconstructBtn}
          onClick={() => {
            window.__oracleRequestReconstruction?.(propertyData.address);
          }}
        >
          <span className={styles.reconstructIcon}>◎</span>
          RECONSTRUCT 3D
        </button>
      )}
    </div>
  );
}
