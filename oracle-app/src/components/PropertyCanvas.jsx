import { useRef, useEffect, useState, useMemo, useCallback } from 'react';
import { useOracleState } from '../state';
import * as SPLAT from 'gsplat';
import styles from './PropertyCanvas.module.css';

const VIEW_PRESETS = {
  EXTERIOR_CLOSE: {
    eye: [3.2, 2.4, 4.0],
    center: [0, 0.9, 0],
  },
  INTERIOR_ROOM: {
    eye: [0.0, 1.4, 0.3],
    center: [-0.8, 0.8, -1.0],
  },
  ISOMETRIC_STAGING: {
    eye: [5.0, 4.8, 5.0],
    center: [0, 0.5, 0],
  },
};

function resolveViewPreset(activeAgent, isFurnished) {
  if (activeAgent?.includes('INTERIOR_ROOM')) return VIEW_PRESETS.INTERIOR_ROOM;
  if (isFurnished) return VIEW_PRESETS.INTERIOR_ROOM;
  if (activeAgent) return VIEW_PRESETS.ISOMETRIC_STAGING;
  return VIEW_PRESETS.EXTERIOR_CLOSE;
}

function lerpScalar(a, b, t) {
  return a + (b - a) * t;
}

function lerpVec3(out, a, b, t) {
  out[0] = lerpScalar(a[0], b[0], t);
  out[1] = lerpScalar(a[1], b[1], t);
  out[2] = lerpScalar(a[2], b[2], t);
  return out;
}

const SPLAT_BASE_URL = import.meta.env.VITE_SPLAT_BASE_URL || 'http://localhost:8000/public/splats';
const LERP_SPEED = 0.04;

// ─── GIS Boundary Rendering ─────────────────────────────────────────────────

const BOUNDARY_VERTEX_SHADER = `
  attribute vec3 a_position;
  uniform mat4 u_viewProjection;
  uniform float u_time;
  varying float v_dist;

  void main() {
    gl_Position = u_viewProjection * vec4(a_position, 1.0);
    v_dist = length(a_position.xz) + u_time * 0.5;
  }
`;

const BOUNDARY_FRAGMENT_SHADER = `
  precision mediump float;
  uniform float u_time;
  uniform float u_opacity;
  varying float v_dist;

  void main() {
    float pulse = 0.6 + 0.4 * sin(v_dist * 4.0 - u_time * 2.0);
    vec3 amber = vec3(0.82, 0.58, 0.12);
    vec3 glow = amber * pulse;
    float edge = smoothstep(0.0, 0.02, gl_FragCoord.y / 1080.0);
    gl_FragColor = vec4(glow, u_opacity * pulse * edge);
  }
`;

function createBoundaryShader(gl) {
  function compile(type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      console.error('[GIS] Shader compile:', gl.getShaderInfoLog(shader));
      gl.deleteShader(shader);
      return null;
    }
    return shader;
  }

  const vs = compile(gl.VERTEX_SHADER, BOUNDARY_VERTEX_SHADER);
  const fs = compile(gl.FRAGMENT_SHADER, BOUNDARY_FRAGMENT_SHADER);
  if (!vs || !fs) return null;

  const program = gl.createProgram();
  gl.attachShader(program, vs);
  gl.attachShader(program, fs);
  gl.linkProgram(program);

  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    console.error('[GIS] Program link:', gl.getProgramInfoLog(program));
    return null;
  }

  return {
    program,
    attrs: { position: gl.getAttribLocation(program, 'a_position') },
    uniforms: {
      viewProjection: gl.getUniformLocation(program, 'u_viewProjection'),
      time: gl.getUniformLocation(program, 'u_time'),
      opacity: gl.getUniformLocation(program, 'u_opacity'),
    },
  };
}

function gisToLocalCoords(gisPoints, originLat, originLon, scale = 1.0) {
  const DEG_TO_M_LAT = 111320;
  const DEG_TO_M_LON = 111320 * Math.cos((originLat * Math.PI) / 180);

  return gisPoints.map(([lon, lat, alt = 0]) => [
    (lon - originLon) * DEG_TO_M_LON * scale,
    alt * scale,
    -(lat - originLat) * DEG_TO_M_LAT * scale,
  ]);
}

function buildBoundaryGeometry(gl, localCoords) {
  const vertices = [];
  const wallHeight = 0.15;

  for (let i = 0; i < localCoords.length; i++) {
    const curr = localCoords[i];
    const next = localCoords[(i + 1) % localCoords.length];

    // Ground line
    vertices.push(curr[0], curr[1], curr[2]);
    vertices.push(next[0], next[1], next[2]);

    // Vertical wall segments for brutalist volume
    vertices.push(curr[0], curr[1], curr[2]);
    vertices.push(curr[0], curr[1] + wallHeight, curr[2]);

    vertices.push(next[0], next[1], next[2]);
    vertices.push(next[0], next[1] + wallHeight, next[2]);

    // Top line
    vertices.push(curr[0], curr[1] + wallHeight, curr[2]);
    vertices.push(next[0], next[1] + wallHeight, next[2]);
  }

  const vbo = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(vertices), gl.STATIC_DRAW);

  return { vbo, vertexCount: vertices.length / 3 };
}

// ─── AR Spatial Data Anchors ─────────────────────────────────────────────────

const DEFAULT_ANCHORS = [
  {
    id: 'roof',
    worldPos: [0.0, 3.2, 0.0],
    label: 'ROOF CONDITION',
    dataKey: 'roofAge',
    format: (v) => `Shingle Age: ${v || '12'} Years`,
    fallback: 'Shingle Age: 12 Years',
  },
  {
    id: 'hvac',
    worldPos: [-1.8, 0.6, 1.2],
    label: 'HVAC SYSTEM',
    dataKey: 'hvacAge',
    format: (v) => `Unit Age: ${v || '8'} Years`,
    fallback: 'Unit Age: 8 Years',
  },
  {
    id: 'foundation',
    worldPos: [0.0, -0.1, 0.0],
    label: 'FOUNDATION',
    dataKey: 'foundationType',
    format: (v) => v || 'Poured Concrete — No Issues',
    fallback: 'Poured Concrete — No Issues',
  },
  {
    id: 'garage',
    worldPos: [2.5, 1.0, -0.5],
    label: 'GARAGE',
    dataKey: 'garageType',
    format: (v) => v || '2-Car Attached',
    fallback: '2-Car Attached',
  },
  {
    id: 'lot',
    worldPos: [3.0, 0.05, 3.0],
    label: 'LOT SIZE',
    dataKey: 'lotAcres',
    format: (v) => `${v || '0.34'} Acres`,
    fallback: '0.34 Acres',
  },
];

function projectToScreen(worldPos, viewMatrix, projMatrix, canvasWidth, canvasHeight) {
  const [x, y, z] = worldPos;

  // View transform
  const vx = viewMatrix[0] * x + viewMatrix[4] * y + viewMatrix[8] * z + viewMatrix[12];
  const vy = viewMatrix[1] * x + viewMatrix[5] * y + viewMatrix[9] * z + viewMatrix[13];
  const vz = viewMatrix[2] * x + viewMatrix[6] * y + viewMatrix[10] * z + viewMatrix[14];
  const vw = viewMatrix[3] * x + viewMatrix[7] * y + viewMatrix[11] * z + viewMatrix[15];

  // Projection transform
  const px = projMatrix[0] * vx + projMatrix[4] * vy + projMatrix[8] * vz + projMatrix[12] * vw;
  const py = projMatrix[1] * vx + projMatrix[5] * vy + projMatrix[9] * vz + projMatrix[13] * vw;
  const pz = projMatrix[2] * vx + projMatrix[6] * vy + projMatrix[10] * vz + projMatrix[14] * vw;
  const pw = projMatrix[3] * vx + projMatrix[7] * vy + projMatrix[11] * vz + projMatrix[15] * vw;

  if (pw <= 0) return null;

  const ndcX = px / pw;
  const ndcY = py / pw;
  const ndcZ = pz / pw;

  if (ndcZ < -1 || ndcZ > 1) return null;

  const screenX = ((ndcX + 1) / 2) * canvasWidth;
  const screenY = ((1 - ndcY) / 2) * canvasHeight;

  return { x: screenX, y: screenY, depth: ndcZ };
}

function buildViewMatrix(eye, center) {
  const fx = center[0] - eye[0];
  const fy = center[1] - eye[1];
  const fz = center[2] - eye[2];
  const fLen = Math.sqrt(fx * fx + fy * fy + fz * fz);
  const f = [fx / fLen, fy / fLen, fz / fLen];

  const up = [0, 1, 0];
  const s = [
    f[1] * up[2] - f[2] * up[1],
    f[2] * up[0] - f[0] * up[2],
    f[0] * up[1] - f[1] * up[0],
  ];
  const sLen = Math.sqrt(s[0] * s[0] + s[1] * s[1] + s[2] * s[2]);
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

function buildPerspectiveMatrix(fovY, aspect, near, far) {
  const f = 1.0 / Math.tan(fovY / 2);
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

// ─── Main Component ──────────────────────────────────────────────────────────

export function PropertyCanvas() {
  const canvasRef = useRef(null);
  const overlayRef = useRef(null);
  const { isFurnished, activeAgent, propertyData, gisBoundaryVisible, anchorsVisible } = useOracleState();
  const rendererRef = useRef(null);
  const sceneRef = useRef(null);
  const cameraRef = useRef(null);
  const animIdRef = useRef(null);
  const currentSplatUrl = useRef(null);
  const [loading, setLoading] = useState(false);

  const currentEye = useRef([...VIEW_PRESETS.EXTERIOR_CLOSE.eye]);
  const currentCenter = useRef([...VIEW_PRESETS.EXTERIOR_CLOSE.center]);

  // GIS boundary state
  const glRef = useRef(null);
  const boundaryShaderRef = useRef(null);
  const boundaryGeoRef = useRef(null);
  const [gisLoaded, setGisLoaded] = useState(false);

  // Refs for render loop to read React state without re-creating the loop
  const gisBoundaryVisibleRef = useRef(gisBoundaryVisible);
  const anchorsVisibleRef = useRef(anchorsVisible);
  gisBoundaryVisibleRef.current = gisBoundaryVisible;
  anchorsVisibleRef.current = anchorsVisible;

  // Anchor screen positions (updated each frame)
  const [anchorPositions, setAnchorPositions] = useState({});
  const anchorBatchRef = useRef({});

  const assessorData = useMemo(() => ({
    roofAge: propertyData?.roofAge || '12',
    hvacAge: propertyData?.hvacAge || '8',
    foundationType: propertyData?.foundationType || 'Poured Concrete — No Issues',
    garageType: propertyData?.garageType || '2-Car Attached',
    lotAcres: propertyData?.lotAcres || '0.34',
  }), [propertyData]);

  // ─── GIS Boundary Loading ───────────────────────────────────────────────

  const loadGISBoundary = useCallback((gisCoordinates, originLat, originLon) => {
    const gl = glRef.current;
    if (!gl) return;

    if (!boundaryShaderRef.current) {
      boundaryShaderRef.current = createBoundaryShader(gl);
    }
    if (!boundaryShaderRef.current) return;

    const localCoords = gisToLocalCoords(gisCoordinates, originLat, originLon, 0.08);
    boundaryGeoRef.current = buildBoundaryGeometry(gl, localCoords);
    setGisLoaded(true);
  }, []);

  // Public: ingest GIS boundary from GeoJSON
  const ingestGISBoundary = useCallback((geojson) => {
    if (!geojson?.geometry?.coordinates) return;

    const coords = geojson.geometry.type === 'Polygon'
      ? geojson.geometry.coordinates[0]
      : geojson.geometry.coordinates;

    const originLat = coords.reduce((s, c) => s + c[1], 0) / coords.length;
    const originLon = coords.reduce((s, c) => s + c[0], 0) / coords.length;

    loadGISBoundary(coords, originLat, originLon);
  }, [loadGISBoundary]);

  // Expose on window for external triggering
  useEffect(() => {
    window.__oracleIngestGIS = ingestGISBoundary;
    return () => { delete window.__oracleIngestGIS; };
  }, [ingestGISBoundary]);

  // ─── Auto-load GIS from property parcel data ────────────────────────────

  useEffect(() => {
    if (!propertyData?.parcelBoundary) return;
    ingestGISBoundary(propertyData.parcelBoundary);
  }, [propertyData?.parcelBoundary, ingestGISBoundary]);

  // ─── Demo: generate synthetic boundary if none provided ─────────────────

  useEffect(() => {
    if (gisLoaded || !propertyData?.address) return;

    const gl = glRef.current;
    if (!gl) return;

    const syntheticCoords = [
      [-75.5500, 39.7450],
      [-75.5495, 39.7450],
      [-75.5495, 39.7445],
      [-75.5500, 39.7445],
    ];
    loadGISBoundary(syntheticCoords, 39.74475, -75.54975, 0.08);
  }, [propertyData?.address, gisLoaded, loadGISBoundary]);

  // ─── WebGL Init + Render Loop ───────────────────────────────────────────

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const renderer = new SPLAT.WebGLRenderer(canvas);
    const scene = new SPLAT.Scene();
    const camera = new SPLAT.Camera();

    camera.data.setSize(canvas.clientWidth, canvas.clientHeight);
    camera.position = new SPLAT.Vector3(...VIEW_PRESETS.EXTERIOR_CLOSE.eye);
    camera.target = new SPLAT.Vector3(...VIEW_PRESETS.EXTERIOR_CLOSE.center);

    rendererRef.current = renderer;
    sceneRef.current = scene;
    cameraRef.current = camera;

    const gl = canvas.getContext('webgl', { alpha: true, premultipliedAlpha: false });
    glRef.current = gl;

    let startTime = performance.now();
    let frameCount = 0;

    function render() {
      renderer.render(scene, camera);

      // GIS boundary overlay pass
      if (gl && boundaryShaderRef.current && boundaryGeoRef.current && gisBoundaryVisibleRef.current) {
        const shader = boundaryShaderRef.current;
        const geo = boundaryGeoRef.current;
        const elapsed = (performance.now() - startTime) / 1000;

        const w = canvas.clientWidth;
        const h = canvas.clientHeight;
        const aspect = w / h;

        const viewMat = buildViewMatrix(currentEye.current, currentCenter.current);
        const projMat = buildPerspectiveMatrix(Math.PI / 4, aspect, 0.1, 100.0);
        const vpMat = new Float32Array(16);
        multiplyMat4(vpMat, projMat, viewMat);

        gl.useProgram(shader.program);
        gl.enable(gl.BLEND);
        gl.blendFunc(gl.SRC_ALPHA, gl.ONE);
        gl.depthMask(false);

        gl.uniformMatrix4fv(shader.uniforms.viewProjection, false, vpMat);
        gl.uniform1f(shader.uniforms.time, elapsed);
        gl.uniform1f(shader.uniforms.opacity, 0.85);

        gl.bindBuffer(gl.ARRAY_BUFFER, geo.vbo);
        gl.enableVertexAttribArray(shader.attrs.position);
        gl.vertexAttribPointer(shader.attrs.position, 3, gl.FLOAT, false, 0, 0);

        gl.lineWidth(2.0);
        gl.drawArrays(gl.LINES, 0, geo.vertexCount);

        gl.disableVertexAttribArray(shader.attrs.position);
        gl.depthMask(true);
        gl.disable(gl.BLEND);
      }

      // Project anchors to screen space every 3 frames for perf
      frameCount++;
      if (frameCount % 3 === 0 && anchorsVisibleRef.current) {
        const w = canvas.clientWidth;
        const h = canvas.clientHeight;
        const aspect = w / h;
        const viewMat = buildViewMatrix(currentEye.current, currentCenter.current);
        const projMat = buildPerspectiveMatrix(Math.PI / 4, aspect, 0.1, 100.0);

        const batch = {};
        for (const anchor of DEFAULT_ANCHORS) {
          const screen = projectToScreen(
            anchor.worldPos, viewMat, projMat, w, h
          );
          if (screen && screen.x > 0 && screen.x < w && screen.y > 0 && screen.y < h) {
            batch[anchor.id] = screen;
          }
        }
        anchorBatchRef.current = batch;
        setAnchorPositions(batch);
      }

      animIdRef.current = requestAnimationFrame(render);
    }

    render();

    const handleResize = () => {
      camera.data.setSize(canvas.clientWidth, canvas.clientHeight);
    };
    window.addEventListener('resize', handleResize);

    return () => {
      window.removeEventListener('resize', handleResize);
      if (animIdRef.current !== null) cancelAnimationFrame(animIdRef.current);
      renderer.dispose();
    };
  }, []);

  // ─── Camera Lerp ────────────────────────────────────────────────────────

  useEffect(() => {
    if (!cameraRef.current) return;

    const target = resolveViewPreset(activeAgent, isFurnished);
    let running = true;

    function animate() {
      if (!running) return;

      lerpVec3(currentEye.current, currentEye.current, target.eye, LERP_SPEED);
      lerpVec3(currentCenter.current, currentCenter.current, target.center, LERP_SPEED);

      const cam = cameraRef.current;
      cam.position = new SPLAT.Vector3(...currentEye.current);
      cam.target = new SPLAT.Vector3(...currentCenter.current);

      const dist = Math.hypot(
        currentEye.current[0] - target.eye[0],
        currentEye.current[1] - target.eye[1],
        currentEye.current[2] - target.eye[2],
      );

      if (dist > 0.001) {
        requestAnimationFrame(animate);
      }
    }

    animate();
    return () => { running = false; };
  }, [activeAgent, isFurnished]);

  // ─── Splat Loading ──────────────────────────────────────────────────────

  useEffect(() => {
    if (!sceneRef.current || !propertyData?.splatUrl) return;

    const splatUrl = propertyData.splatUrl;
    if (splatUrl === currentSplatUrl.current) return;
    currentSplatUrl.current = splatUrl;

    const scene = sceneRef.current;

    async function loadSplat() {
      setLoading(true);
      scene.reset();

      try {
        await SPLAT.Loader.LoadAsync(splatUrl, scene, null);
      } catch (err) {
        console.error('[PropertyCanvas] Failed to load splat:', err);
      } finally {
        setLoading(false);
      }
    }

    loadSplat();
  }, [propertyData?.splatUrl]);

  // ─── Render ─────────────────────────────────────────────────────────────

  return (
    <div className={styles.container}>
      <canvas ref={canvasRef} className={styles.canvas} />

      {/* AR Spatial Anchor Cards */}
      {anchorsVisible && (
      <div ref={overlayRef} className={styles.anchorLayer}>
        {DEFAULT_ANCHORS.map((anchor) => {
          const pos = anchorPositions[anchor.id];
          if (!pos) return null;

          const value = assessorData[anchor.dataKey];
          const displayText = anchor.format(value);
          const depthScale = 1.0 - pos.depth * 0.3;

          return (
            <div
              key={anchor.id}
              className={styles.anchorCard}
              style={{
                transform: `translate(-50%, -100%) translate(${pos.x}px, ${pos.y}px) scale(${depthScale})`,
                opacity: Math.min(1, depthScale + 0.2),
              }}
            >
              <span className={styles.anchorLabel}>{anchor.label}</span>
              <span className={styles.anchorValue}>{displayText}</span>
              <div className={styles.anchorStem} />
            </div>
          );
        })}
      </div>
      )}

      {/* GIS Boundary indicator */}
      {gisLoaded && gisBoundaryVisible && (
        <div className={styles.gisIndicator}>
          <span className={styles.gisIndicatorDot} />
          <span className={styles.gisIndicatorText}>GIS BOUNDARY ACTIVE</span>
        </div>
      )}

      {loading && (
        <div className={styles.loadingOverlay}>
          <span className={styles.loadingText}>RECONSTRUCTING SPACE...</span>
        </div>
      )}

      <div className={styles.overlay} data-furnished={isFurnished}>
        {isFurnished && <span className={styles.badge}>FURNISHED</span>}
        {propertyData?.splatUrl && <span className={styles.badge}>PHOTOREALISTIC</span>}
        {gisLoaded && <span className={styles.badge}>GIS MAPPED</span>}
      </div>
    </div>
  );
}
