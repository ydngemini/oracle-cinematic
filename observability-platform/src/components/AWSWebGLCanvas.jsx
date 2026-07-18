import { useRef, useEffect, useMemo, useState } from 'react';

const VERTEX_SHADER = `
attribute vec2 a_position;
uniform vec2 u_resolution;
void main() {
  vec2 clipSpace = (a_position / u_resolution) * 2.0 - 1.0;
  gl_Position = vec4(clipSpace * vec2(1, -1), 0, 1);
}
`;

const PARTICLE_VS = `
attribute vec3 a_position;
attribute float a_size;
attribute vec3 a_color;
uniform mat4 u_vp;
varying vec3 v_color;
void main() {
  gl_Position = u_vp * vec4(a_position, 1.0);
  gl_PointSize = a_size;
  v_color = a_color;
}
`;

const PARTICLE_FS = `
precision mediump float;
varying vec3 v_color;
void main() {
  float dist = length(gl_PointCoord - vec2(0.5));
  if (dist > 0.5) discard;
  float alpha = 1.0 - smoothstep(0.3, 0.5, dist);
  gl_FragColor = vec4(v_color, alpha * 0.8);
}
`;

const LINE_VS = `
attribute vec3 a_position;
attribute vec3 a_color;
uniform mat4 u_vp;
varying vec3 v_color;
void main() {
  gl_Position = u_vp * vec4(a_position, 1.0);
  v_color = a_color;
}
`;

const LINE_FS = `
precision mediump float;
varying vec3 v_color;
void main() {
  gl_FragColor = vec4(v_color, 0.6);
}
`;

function compileShader(gl, type, src) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, src);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    console.error('Shader compile error:', gl.getShaderInfoLog(shader));
    gl.deleteShader(shader);
    return null;
  }
  return shader;
}

function createProgram(gl, vsSrc, fsSrc) {
  const vs = compileShader(gl, gl.VERTEX_SHADER, vsSrc);
  const fs = compileShader(gl, gl.FRAGMENT_SHADER, fsSrc);
  if (!vs || !fs) return null;
  const prog = gl.createProgram();
  gl.attachShader(prog, vs);
  gl.attachShader(prog, fs);
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    console.error('Program link error:', gl.getProgramInfoLog(prog));
    return null;
  }
  return prog;
}

function mat4Perspective(fov, aspect, near, far) {
  const f = 1.0 / Math.tan(fov / 2);
  const nf = 1 / (near - far);
  return new Float32Array([
    f / aspect, 0, 0, 0,
    0, f, 0, 0,
    0, 0, (far + near) * nf, -1,
    0, 0, 2 * far * near * nf, 0,
  ]);
}

function mat4LookAt(eye, center, up) {
  const zAxis = normalize3(sub3(eye, center));
  const xAxis = normalize3(cross3(up, zAxis));
  const yAxis = cross3(zAxis, xAxis);
  return new Float32Array([
    xAxis[0], yAxis[0], zAxis[0], 0,
    xAxis[1], yAxis[1], zAxis[1], 0,
    xAxis[2], yAxis[2], zAxis[2], 0,
    -dot3(xAxis, eye), -dot3(yAxis, eye), -dot3(zAxis, eye), 1,
  ]);
}

function mat4Multiply(a, b) {
  const out = new Float32Array(16);
  for (let column = 0; column < 4; column++) {
    for (let row = 0; row < 4; row++) {
      out[column * 4 + row] =
        a[row] * b[column * 4] +
        a[4 + row] * b[column * 4 + 1] +
        a[8 + row] * b[column * 4 + 2] +
        a[12 + row] * b[column * 4 + 3];
    }
  }
  return out;
}

function sub3(a, b) { return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]; }
function cross3(a, b) { return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]; }
function dot3(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
function normalize3(v) { const len = Math.sqrt(dot3(v, v)); return len > 0 ? [v[0] / len, v[1] / len, v[2] / len] : [0, 0, 0]; }

const SERVICE_COLORS = {
  ec2: [0, 0.83, 1],
  rds: [1, 0.62, 0.1],
  lambda: [1, 0.6, 0.1],
  s3: [0, 1, 0.53],
};

export default function AWSWebGLCanvas({ ec2, rds, lambda, metrics, selectedService, onSelect }) {
  const canvasRef = useRef(null);
  const glRef = useRef(null);
  const programsRef = useRef({});
  const particlesRef = useRef([]);
  const linesRef = useRef([]);
  const rafRef = useRef(null);
  const timeRef = useRef(0);
  const userRotationRef = useRef(0);
  const [isDragging, setIsDragging] = useState(false);
  const lastTouchRef = useRef({ x: 0, y: 0 });
  const lineBufferRef = useRef(null);
  const particleBufferRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const gl = canvas.getContext('webgl', { antialias: true, alpha: true });
    if (!gl) {
      console.error('WebGL not supported');
      return;
    }
    glRef.current = gl;

    const particleProg = createProgram(gl, PARTICLE_VS, PARTICLE_FS);
    const lineProg = createProgram(gl, LINE_VS, LINE_FS);
    programsRef.current = { particle: particleProg, line: lineProg };

    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    gl.clearColor(0, 0, 0, 0);

    return () => {
      if (rafRef.current) {
        cancelAnimationFrame(rafRef.current);
      }
      if (particleProg) gl.deleteProgram(particleProg);
      if (lineProg) gl.deleteProgram(lineProg);
      if (lineBufferRef.current) gl.deleteBuffer(lineBufferRef.current);
      if (particleBufferRef.current) gl.deleteBuffer(particleBufferRef.current);
      programsRef.current = {};
      lineBufferRef.current = null;
      particleBufferRef.current = null;
      if (glRef.current === gl) glRef.current = null;
    };
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const resize = () => {
      const gl = glRef.current;
      if (!gl) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const width = Math.max(1, Math.floor(canvas.clientWidth * dpr));
      const height = Math.max(1, Math.floor(canvas.clientHeight * dpr));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
        gl.viewport(0, 0, width, height);
      }
    };

    const observer = new ResizeObserver(resize);
    observer.observe(canvas);
    resize();
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const particles = [];
    const allEc2 = ec2 || [];
    const allRds = rds || [];
    const allLambda = lambda || [];

    allEc2.forEach((inst, i) => {
      const angle = (i / Math.max(allEc2.length, 1)) * Math.PI * 2;
      const radius = 3 + (inst.state === 'running' ? 0 : 0.5);
      const x = Math.cos(angle) * radius;
      const y = Math.sin(angle) * radius;
      const color = inst.state === 'running' ? [0, 1, 0.53] : inst.state === 'stopped' ? [1, 0.23, 0.36] : [1, 0.62, 0.1];
      const size = inst.state === 'running' ? 12 : 8;
      const cpuMetrics = metrics?.ec2?.[inst.id]?.cpu?.avg || 0;
      particles.push({
        x, y, z: 0,
        color,
        size: size + (cpuMetrics > 0 ? cpuMetrics / 10 : 0),
        type: 'ec2',
        id: inst.id,
      });
    });

    allRds.forEach((inst, i) => {
      const angle = (i / Math.max(allRds.length, 1)) * Math.PI * 2 + Math.PI / 4;
      const radius = 5;
      const x = Math.cos(angle) * radius;
      const y = Math.sin(angle) * radius;
      const color = inst.status === 'available' ? [1, 0.62, 0.1] : [0.5, 0.5, 0.5];
      particles.push({
        x, y, z: 0.5,
        color,
        size: 14,
        type: 'rds',
        id: inst.id,
      });
    });

    allLambda.forEach((fn, i) => {
      const angle = (i / Math.max(allLambda.length, 1)) * Math.PI * 2 + Math.PI / 2;
      const radius = 6;
      const x = Math.cos(angle) * radius;
      const y = Math.sin(angle) * radius;
      const color = fn.state === 'Active' ? [1, 0.6, 0.1] : [0.5, 0.5, 0.5];
      particles.push({
        x, y, z: 0.3,
        color,
        size: 10,
        type: 'lambda',
        id: fn.name,
      });
    });

    particlesRef.current = particles;

    const lines = [];
    allEc2.forEach((inst, i) => {
      allRds.forEach((db, j) => {
        const p1 = particles.find(p => p.type === 'ec2' && p.id === inst.id);
        const p2 = particles.find(p => p.type === 'rds' && p.id === db.id);
        if (p1 && p2) {
          lines.push({ start: [p1.x, p1.y, p1.z], end: [p2.x, p2.y, p2.z], color: [0, 0.5, 0.7] });
        }
      });
    });
    linesRef.current = lines;
  }, [ec2, rds, lambda, metrics]);

  useEffect(() => {
    const gl = glRef.current;
    const canvas = canvasRef.current;
    const programs = programsRef.current;
    if (!gl || !programs.particle || !programs.line) return;

    // Initialize persistent buffers once
    if (!lineBufferRef.current) {
      lineBufferRef.current = gl.createBuffer();
    }
    if (!particleBufferRef.current) {
      particleBufferRef.current = gl.createBuffer();
    }

    const render = () => {
      timeRef.current += 0.016;
      const time = timeRef.current;

      gl.clear(gl.COLOR_BUFFER_BIT);

      const aspect = canvas.width / Math.max(canvas.height, 1);
      const proj = mat4Perspective(Math.PI / 4, aspect, 0.1, 100);
      const camAngle = time * 0.1 + userRotationRef.current;
      const camDist = 12;
      const eye = [Math.sin(camAngle) * camDist, Math.cos(camAngle) * camDist, 8];
      const view = mat4LookAt(eye, [0, 0, 0], [0, 0, 1]);
      const vp = mat4Multiply(proj, view);

      const lineProg = programs.line;
      if (linesRef.current.length > 0) {
        gl.useProgram(lineProg);
        gl.uniformMatrix4fv(gl.getUniformLocation(lineProg, 'u_vp'), false, vp);

        const lineVerts = [];
        linesRef.current.forEach(line => {
          lineVerts.push(...line.start, ...line.color, ...line.end, ...line.color);
        });

        gl.bindBuffer(gl.ARRAY_BUFFER, lineBufferRef.current);
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(lineVerts), gl.STATIC_DRAW);

        const posLoc = gl.getAttribLocation(lineProg, 'a_position');
        gl.enableVertexAttribArray(posLoc);
        gl.vertexAttribPointer(posLoc, 3, gl.FLOAT, false, 24, 0);

        const colorLoc = gl.getAttribLocation(lineProg, 'a_color');
        gl.enableVertexAttribArray(colorLoc);
        gl.vertexAttribPointer(colorLoc, 3, gl.FLOAT, false, 24, 12);

        gl.drawArrays(gl.LINES, 0, linesRef.current.length * 2);
      }

      const particleProg = programs.particle;
      gl.useProgram(particleProg);
      gl.uniformMatrix4fv(gl.getUniformLocation(particleProg, 'u_vp'), false, vp);

      const particleData = [];
      particlesRef.current.forEach(p => {
        const pulse = Math.sin(time * 2 + p.x) * 0.2;
        particleData.push(
          p.x, p.y, p.z,
          p.size + pulse * 3,
          p.color[0], p.color[1], p.color[2]
        );
      });

      if (particleData.length > 0) {
        gl.bindBuffer(gl.ARRAY_BUFFER, particleBufferRef.current);
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(particleData), gl.DYNAMIC_DRAW);

        const posLoc = gl.getAttribLocation(particleProg, 'a_position');
        const sizeLoc = gl.getAttribLocation(particleProg, 'a_size');
        const colorLoc = gl.getAttribLocation(particleProg, 'a_color');

        const stride = 7 * 4;
        gl.enableVertexAttribArray(posLoc);
        gl.vertexAttribPointer(posLoc, 3, gl.FLOAT, false, stride, 0);

        gl.enableVertexAttribArray(sizeLoc);
        gl.vertexAttribPointer(sizeLoc, 1, gl.FLOAT, false, stride, 12);

        gl.enableVertexAttribArray(colorLoc);
        gl.vertexAttribPointer(colorLoc, 3, gl.FLOAT, false, stride, 16);

        gl.drawArrays(gl.POINTS, 0, particlesRef.current.length);
      }

      rafRef.current = requestAnimationFrame(render);
    };

    render();

    return () => {
      if (rafRef.current) {
        cancelAnimationFrame(rafRef.current);
      }
    };
  }, []);

  const handleTouchStart = (e) => {
    setIsDragging(true);
    lastTouchRef.current = {
      x: e.touches[0].clientX,
      y: e.touches[0].clientY,
    };
  };

  const handleTouchMove = (e) => {
    if (!isDragging) return;
    const dx = e.touches[0].clientX - lastTouchRef.current.x;
    userRotationRef.current += dx * 0.01;
    lastTouchRef.current = {
      x: e.touches[0].clientX,
      y: e.touches[0].clientY,
    };
  };

  const handleTouchEnd = () => {
    setIsDragging(false);
  };

  const handleMouseDown = (e) => {
    setIsDragging(true);
    lastTouchRef.current = { x: e.clientX, y: e.clientY };
  };

  const handleMouseMove = (e) => {
    if (!isDragging) return;
    const dx = e.clientX - lastTouchRef.current.x;
    userRotationRef.current += dx * 0.005;
    lastTouchRef.current = { x: e.clientX, y: e.clientY };
  };

  const handleMouseUp = () => {
    setIsDragging(false);
  };

  return (
    <canvas 
      ref={canvasRef} 
      role="img"
      aria-label="AWS infrastructure topology"
      style={{ width: '100%', height: '100%', touchAction: 'none' }}
      onTouchStart={handleTouchStart}
      onTouchMove={handleTouchMove}
      onTouchEnd={handleTouchEnd}
      onMouseDown={handleMouseDown}
      onMouseMove={handleMouseMove}
      onMouseUp={handleMouseUp}
      onMouseLeave={handleMouseUp}
    />
  );
}
