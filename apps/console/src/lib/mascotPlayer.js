/*
 * mascot-player.js — zero-dependency WebGL2 player for mascot_animations.glb
 *
 * Built for Tauri (WKWebView on macOS, WebView2 on Windows, WebKitGTK on Linux):
 *   - no CDN, no npm packages, no eval: works offline and under a strict CSP
 *   - transparent canvas, so the mascot floats over the desktop in a transparent window
 *   - plays the GLB's own animation clips (Swing_Joy loop, Listen_Nod one-shot) and switches between them
 *
 * Usage:
 *   import { createMascotPlayer } from './mascot-player.js';
 *   const player = await createMascotPlayer(document.querySelector('canvas'), {
 *     src: '/mascot_animations.glb',   // URL, or an ArrayBuffer you loaded yourself
 *     clip: 'Swing_Joy',               // idle loop
 *   });
 *   player.playOnce('Listen_Nod');     // stand up, listen with nods, sit back, return to Swing_Joy
 *   player.speak();                    // settle the swing, pop a voice bubble, talk (loops)
 *   player.stopSpeaking();             // bubble away, swing back up into Swing_Joy
 *   player.setVoiceLevel(rms);         // optional: drive mouth + bubble bars from real audio (null = baked)
 *   player.queue('Listen_Nod', { loop: false });  // or start it exactly when the joy loop wraps
 *   player.pause(); player.play(); player.seek(5); player.setSpeed(0.8); player.dispose();
 *
 * The camera is fixed on the approved FRONT view (slightly above, like the reference sheet).
 */

const FRONT = { yaw: 0, pitch: 8 };   // degrees; matches the sheet's FRONT panel

// ------------------------------------------------------------------ small math
const M4 = {
  identity() { const m = new Float32Array(16); m[0] = m[5] = m[10] = m[15] = 1; return m; },
  mul(a, b) {
    const o = new Float32Array(16);
    for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) {
      o[c * 4 + r] = a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1] + a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3];
    }
    return o;
  },
  fromTRS(t, q, s) {
    const [x, y, z, w] = q, [sx, sy, sz] = s;
    const xx = x * x, yy = y * y, zz = z * z, xy = x * y, xz = x * z, yz = y * z, wx = w * x, wy = w * y, wz = w * z;
    return new Float32Array([
      (1 - 2 * (yy + zz)) * sx, 2 * (xy + wz) * sx, 2 * (xz - wy) * sx, 0,
      2 * (xy - wz) * sy, (1 - 2 * (xx + zz)) * sy, 2 * (yz + wx) * sy, 0,
      2 * (xz + wy) * sz, 2 * (yz - wx) * sz, (1 - 2 * (xx + yy)) * sz, 0,
      t[0], t[1], t[2], 1]);
  },
  perspective(fovY, aspect, near, far) {
    const f = 1 / Math.tan(fovY / 2), nf = 1 / (near - far);
    return new Float32Array([f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) * nf, -1, 0, 0, 2 * far * near * nf, 0]);
  },
  lookAt(eye, at, up) {
    let zx = eye[0] - at[0], zy = eye[1] - at[1], zz = eye[2] - at[2];
    let l = Math.hypot(zx, zy, zz); zx /= l; zy /= l; zz /= l;
    let xx = up[1] * zz - up[2] * zy, xy = up[2] * zx - up[0] * zz, xz = up[0] * zy - up[1] * zx;
    l = Math.hypot(xx, xy, xz); xx /= l; xy /= l; xz /= l;
    const yx = zy * xz - zz * xy, yy = zz * xx - zx * xz, yz = zx * xy - zy * xx;
    return new Float32Array([xx, yx, zx, 0, xy, yy, zy, 0, xz, yz, zz, 0,
      -(xx * eye[0] + xy * eye[1] + xz * eye[2]), -(yx * eye[0] + yy * eye[1] + yz * eye[2]), -(zx * eye[0] + zy * eye[1] + zz * eye[2]), 1]);
  },
  normalMat(m) { // inverse-transpose of upper 3x3
    const a = m[0], b = m[1], c = m[2], d = m[4], e = m[5], f = m[6], g = m[8], h = m[9], i = m[10];
    const A = e * i - f * h, B = -(d * i - f * g), C = d * h - e * g;
    const det = a * A + b * B + c * C;
    if (Math.abs(det) < 1e-12) return null;
    const id = 1 / det;
    return new Float32Array([
      A * id, B * id, C * id,
      -(b * i - c * h) * id, (a * i - c * g) * id, -(a * h - b * g) * id,
      (b * f - c * e) * id, -(a * f - c * d) * id, (a * e - b * d) * id]);
  },
};

// ------------------------------------------------------------------ GLB parsing
function parseGLB(buf) {
  const dv = new DataView(buf);
  if (dv.getUint32(0, true) !== 0x46546c67) throw new Error('Not a GLB file');
  const jsonLen = dv.getUint32(12, true);
  const json = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 20, jsonLen)));
  const binOff = 20 + jsonLen + 8;
  const binLen = dv.getUint32(20 + jsonLen, true);
  return { json, bin: new Uint8Array(buf, binOff, binLen) };
}

const COMP = { 5126: [Float32Array, 4], 5125: [Uint32Array, 4], 5123: [Uint16Array, 2], 5121: [Uint8Array, 1] };
const NUM = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4, MAT4: 16 };

function readAccessor(g, i) {
  const a = g.json.accessors[i];
  const bv = g.json.bufferViews[a.bufferView];
  const [T, size] = COMP[a.componentType];
  const n = NUM[a.type];
  const off = g.bin.byteOffset + (bv.byteOffset || 0) + (a.byteOffset || 0);
  const stride = bv.byteStride || 0;
  if (!stride || stride === n * size) {
    // copy so the result is aligned regardless of the GLB's internal offsets
    return new T(g.bin.buffer.slice(off, off + a.count * n * size));
  }
  const out = new T(a.count * n);
  const src = new DataView(g.bin.buffer);
  for (let k = 0; k < a.count; k++) for (let c = 0; c < n; c++) {
    const p = off + k * stride + c * size;
    out[k * n + c] = size === 4 ? (T === Float32Array ? src.getFloat32(p, true) : src.getUint32(p, true))
      : size === 2 ? src.getUint16(p, true) : src.getUint8(p);
  }
  return out;
}

async function decodeImage(g, img) {
  const bv = g.json.bufferViews[img.bufferView];
  const bytes = g.bin.slice(bv.byteOffset || 0, (bv.byteOffset || 0) + bv.byteLength);
  const blob = new Blob([bytes], { type: img.mimeType });
  if (self.createImageBitmap) {
    try { return await createImageBitmap(blob, { premultiplyAlpha: 'none', colorSpaceConversion: 'none' }); } catch (_) { /* fall back */ }
  }
  const url = URL.createObjectURL(blob);
  try {
    const el = new Image();
    el.src = url;
    await el.decode();
    return el;
  } finally { URL.revokeObjectURL(url); }
}

// ------------------------------------------------------------------ shaders
const VS = `#version 300 es
layout(location=0) in vec3 aPos;
layout(location=1) in vec3 aNor;
layout(location=2) in vec2 aUV;
uniform mat4 uModel, uViewProj;
uniform mat3 uNormalMat;
out vec3 vPos; out vec3 vNor; out vec2 vUV;
void main(){
  vec4 wp = uModel * vec4(aPos, 1.0);
  vPos = wp.xyz; vNor = uNormalMat * aNor; vUV = aUV;
  gl_Position = uViewProj * wp;
}`;

const FS = `#version 300 es
precision highp float;
in vec3 vPos; in vec3 vNor; in vec2 vUV;
uniform vec4 uBase; uniform bool uHasTex; uniform sampler2D uTex;
uniform float uRough; uniform bool uBlend; uniform vec3 uCam;
out vec4 frag;
void main(){
  vec4 base = uBase;
  if (uHasTex) base *= texture(uTex, vUV);          // sRGB texture -> linear on fetch
  vec3 N = normalize(vNor);
  vec3 V = normalize(uCam - vPos);
  vec3 L1 = normalize(vec3(-0.55, 0.75, 0.60));     // key, upper left front
  vec3 L2 = normalize(vec3( 0.70, 0.25, 0.45));     // fill
  vec3 L3 = normalize(vec3( 0.10, 0.40, -0.90));    // back rim
  float hemi = 0.5 + 0.5 * N.y;
  float diff = 0.30 * hemi + 0.12
             + 0.85 * max(dot(N, L1), 0.0)
             + 0.30 * max(dot(N, L2), 0.0)
             + 0.25 * max(dot(N, L3), 0.0);
  vec3 H = normalize(L1 + V);
  float shin = min(2.0 / pow(max(uRough, 0.05), 4.0), 200.0);
  float spec = 0.06 * (1.0 - uRough) * pow(max(dot(N, H), 0.0), shin);
  float rim = 0.08 * pow(1.0 - max(dot(N, V), 0.0), 3.0);
  vec3 c = base.rgb * diff + spec + rim;
  c = pow(clamp(c, 0.0, 1.0), vec3(1.0 / 2.2));
  float a = uBlend ? base.a : 1.0;
  frag = vec4(c * a, a);                              // premultiplied for a transparent canvas
}`;

function compile(gl, type, src) {
  const s = gl.createShader(type);
  gl.shaderSource(s, src);
  gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
  return s;
}

// ------------------------------------------------------------------ player
export async function createMascotPlayer(canvas, opts = {}) {
  const o = Object.assign({
    src: 'mascot_animations.glb', autoplay: true, speed: 1,
    fov: 26, target: [0, 0.35, 0.1], distance: null, maxPixelRatio: 2,
    clip: 'Swing_Joy', loop: true, onClipEnd: null,
  }, opts);

  const gl = canvas.getContext('webgl2', { alpha: true, premultipliedAlpha: true, antialias: true });
  if (!gl) throw new Error('WebGL2 is not available in this webview');

  const buf = o.src instanceof ArrayBuffer ? o.src : await (await fetch(o.src)).arrayBuffer();
  const g = parseGLB(buf);
  const J = g.json;

  // program
  const prog = gl.createProgram();
  gl.attachShader(prog, compile(gl, gl.VERTEX_SHADER, VS));
  gl.attachShader(prog, compile(gl, gl.FRAGMENT_SHADER, FS));
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
  const U = {};
  for (const n of ['uModel', 'uViewProj', 'uNormalMat', 'uBase', 'uHasTex', 'uTex', 'uRough', 'uBlend', 'uCam']) U[n] = gl.getUniformLocation(prog, n);

  // textures
  const aniso = gl.getExtension('EXT_texture_filter_anisotropic');
  const images = await Promise.all((J.images || []).map(im => decodeImage(g, im)));
  const textures = (J.textures || []).map(t => {
    const s = (J.samplers || [])[t.sampler] || {};
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
    gl.pixelStorei(gl.UNPACK_COLORSPACE_CONVERSION_WEBGL, gl.NONE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.SRGB8_ALPHA8, gl.RGBA, gl.UNSIGNED_BYTE, images[t.source]);
    gl.generateMipmap(gl.TEXTURE_2D);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, s.wrapS || gl.REPEAT);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, s.wrapT || gl.REPEAT);
    if (aniso) gl.texParameterf(gl.TEXTURE_2D, aniso.TEXTURE_MAX_ANISOTROPY_EXT, Math.min(8, gl.getParameter(aniso.MAX_TEXTURE_MAX_ANISOTROPY_EXT)));
    return tex;
  });
  images.forEach(im => im && im.close && im.close());

  // meshes -> VAOs
  const toLinear = c => c; // baseColorFactor is already linear in glTF
  const meshes = J.meshes.map(m => m.primitives.map(p => {
    const vao = gl.createVertexArray();
    gl.bindVertexArray(vao);
    const bindAttr = (loc, acc, n) => {
      const b = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, b);
      gl.bufferData(gl.ARRAY_BUFFER, readAccessor(g, acc), gl.STATIC_DRAW);
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, n, gl.FLOAT, false, 0, 0);
    };
    bindAttr(0, p.attributes.POSITION, 3);
    bindAttr(1, p.attributes.NORMAL, 3);
    if (p.attributes.TEXCOORD_0 !== undefined) bindAttr(2, p.attributes.TEXCOORD_0, 2);
    else { gl.disableVertexAttribArray(2); gl.vertexAttrib2f(2, 0, 0); }
    const idx = readAccessor(g, p.indices);
    const ib = gl.createBuffer();
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ib);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, idx, gl.STATIC_DRAW);
    gl.bindVertexArray(null);
    const mat = J.materials[p.material];
    const pbr = mat.pbrMetallicRoughness || {};
    return {
      vao, count: idx.length,
      type: idx instanceof Uint32Array ? gl.UNSIGNED_INT : idx instanceof Uint16Array ? gl.UNSIGNED_SHORT : gl.UNSIGNED_BYTE,
      base: toLinear(pbr.baseColorFactor || [1, 1, 1, 1]),
      tex: pbr.baseColorTexture ? textures[pbr.baseColorTexture.index] : null,
      rough: pbr.roughnessFactor ?? 1,
      blend: mat.alphaMode === 'BLEND',
      cull: !mat.doubleSided,
    };
  }));

  // nodes + animation
  const nodes = J.nodes.map(n => ({
    t: Float32Array.from(n.translation || [0, 0, 0]),
    r: Float32Array.from(n.rotation || [0, 0, 0, 1]),
    s: Float32Array.from(n.scale || [1, 1, 1]),
    mesh: n.mesh, children: n.children || [], name: n.name || '',
  }));
  const nodeIndex = name => J.nodes.findIndex(n => n.name === name);
  const exprNodes = nodes.filter(n => n.name.startsWith('Expr_'));
  const exprByName = Object.fromEntries(exprNodes.map(n => [n.name.slice(5), n]));
  const tailNode = nodeIndex('Speech_Tail'), bubbleNode = nodeIndex('Speech_Bubble');
  const worldM = new Array(nodes.length).fill(null);
  // rest pose, restored before each clip is applied so nothing keeps a stale value
  const rest = nodes.map(n => ({ t: n.t.slice(), r: n.r.slice(), s: n.s.slice() }));
  const clips = {};
  for (const a of (J.animations || [])) {
    let duration = 0;
    const tracks = a.channels.map(ch => {
      const smp = a.samplers[ch.sampler];
      const times = readAccessor(g, smp.input);
      const values = readAccessor(g, smp.output);
      duration = Math.max(duration, times[times.length - 1]);
      return { node: nodes[ch.target.node], path: ch.target.path, times, values, n: values.length / times.length, step: smp.interpolation === 'STEP' };
    });
    clips[a.name] = { name: a.name, tracks, duration, speech: (a.extras && a.extras.speech) || null, extras: a.extras || null };
  }
  const clipNames = Object.keys(clips);

  function applyClip(clip, t) {
    nodes.forEach((n, k) => { n.t.set(rest[k].t); n.r.set(rest[k].r); n.s.set(rest[k].s); });
    if (!clip) return;
    for (const tr of clip.tracks) {
      const T = tr.times, V = tr.values, n = tr.n;
      let lo = 0, hi = T.length - 1;
      if (t <= T[0]) hi = 0; else if (t >= T[hi]) lo = hi;
      else { while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (T[mid] <= t) lo = mid; else hi = mid; } }
      const dst = tr.path === 'translation' ? tr.node.t : tr.path === 'rotation' ? tr.node.r : tr.node.s;
      if (tr.step || lo === hi) { for (let c = 0; c < n; c++) dst[c] = V[lo * n + c]; continue; }
      const u = (t - T[lo]) / (T[hi] - T[lo]);
      if (tr.path === 'rotation') {
        let dot = 0; for (let c = 0; c < 4; c++) dot += V[lo * 4 + c] * V[hi * 4 + c];
        const sgn = dot < 0 ? -1 : 1;
        let len = 0;
        for (let c = 0; c < 4; c++) { dst[c] = V[lo * 4 + c] * (1 - u) + sgn * V[hi * 4 + c] * u; len += dst[c] * dst[c]; }
        len = Math.sqrt(len); for (let c = 0; c < 4; c++) dst[c] /= len;
      } else {
        for (let c = 0; c < n; c++) dst[c] = V[lo * n + c] * (1 - u) + V[hi * n + c] * u;
      }
    }
  }

  // cross-fade from a snapshot of the previous pose (translation + rotation; face switches instantly)
  let blendFrom = null, blendTime = 0, blendLeft = 0;
  function snapshot() { return nodes.map(n => ({ t: n.t.slice(), r: n.r.slice() })); }
  function applyBlend() {
    if (!blendFrom || blendLeft <= 0) return;
    const u = 1 - blendLeft / blendTime;
    const w = u * u * (3 - 2 * u);
    nodes.forEach((n, k) => {
      const a = blendFrom[k];
      for (let c = 0; c < 3; c++) n.t[c] = a.t[c] * (1 - w) + n.t[c] * w;
      let dot = 0; for (let c = 0; c < 4; c++) dot += a.r[c] * n.r[c];
      const sgn = dot < 0 ? -1 : 1;
      let len = 0;
      for (let c = 0; c < 4; c++) { n.r[c] = a.r[c] * (1 - w) + sgn * n.r[c] * w; len += n.r[c] * n.r[c]; }
      len = Math.sqrt(len); for (let c = 0; c < 4; c++) n.r[c] /= len;
    });
  }

  // camera
  const yaw = FRONT.yaw * Math.PI / 180, pitch = FRONT.pitch * Math.PI / 180;
  const dist = o.distance;

  function fitDistance(aspect) {
    // front framing that fits every clip: the swing coming toward the camera, and the
    // standing, nodding pose in front of the seat; the ropes run out of the top of the frame
    const halfH = 1.745, halfW = 1.32;
    const tf = Math.tan(o.fov * Math.PI / 360);
    return Math.max(halfH / tf, halfW / (tf * aspect)) * 1.04;
  }

  // sizing
  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, o.maxPixelRatio);
    const w = Math.max(1, Math.round(canvas.clientWidth * dpr)), h = Math.max(1, Math.round(canvas.clientHeight * dpr));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  }
  const ro = new ResizeObserver(resize); ro.observe(canvas); resize();

  const cleanups = [];

  // draw
  const opaque = [], blended = [];
  function collect(ni, parent) {
    const n = nodes[ni];
    if (Math.abs(n.s[0]) < 1e-6 && Math.abs(n.s[1]) < 1e-6 && Math.abs(n.s[2]) < 1e-6) return; // hidden face decal
    const m = M4.mul(parent, M4.fromTRS(n.t, n.r, n.s));
    worldM[ni] = m;
    if (n.mesh !== undefined) for (const p of meshes[n.mesh]) (p.blend ? blended : opaque).push([p, m]);
    for (const c of n.children) collect(c, m);
  }

  function draw() {
    resize();
    const w = canvas.width, h = canvas.height, aspect = w / h;
    gl.viewport(0, 0, w, h);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    const d = dist || fitDistance(aspect);
    const tg = o.target;
    const eye = [tg[0] + d * Math.sin(yaw) * Math.cos(pitch), tg[1] + d * Math.sin(pitch), tg[2] + d * Math.cos(yaw) * Math.cos(pitch)];
    const vp = M4.mul(M4.perspective(o.fov * Math.PI / 180, aspect, 0.1, 100), M4.lookAt(eye, tg, [0, 1, 0]));
    opaque.length = 0; blended.length = 0;
    for (const r of J.scenes[J.scene || 0].nodes) collect(r, M4.identity());
    gl.useProgram(prog);
    gl.uniformMatrix4fv(U.uViewProj, false, vp);
    gl.uniform3fv(U.uCam, eye);
    gl.uniform1i(U.uTex, 0);
    gl.enable(gl.DEPTH_TEST);
    const pass = (list, blend) => {
      gl.depthMask(!blend);
      if (blend) { gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA); } else gl.disable(gl.BLEND);
      for (const [p, m] of list) {
        const nm = M4.normalMat(m);
        if (!nm) continue;
        if (p.cull) gl.enable(gl.CULL_FACE); else gl.disable(gl.CULL_FACE);
        gl.uniformMatrix4fv(U.uModel, false, m);
        gl.uniformMatrix3fv(U.uNormalMat, false, nm);
        gl.uniform4fv(U.uBase, p.base);
        gl.uniform1f(U.uRough, p.rough);
        gl.uniform1i(U.uBlend, blend ? 1 : 0);
        gl.uniform1i(U.uHasTex, p.tex ? 1 : 0);
        if (p.tex) { gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, p.tex); }
        gl.bindVertexArray(p.vao);
        gl.drawElements(gl.TRIANGLES, p.count, p.type, 0);
      }
    };
    pass(opaque, false);
    pass(blended, true);
    gl.depthMask(true);
    gl.bindVertexArray(null);
    drawBubble(vp, w, h);
  }

  // ------------------------------------------------------------------ speech bubble
  // Drawn into the same WebGL canvas (one transparent surface, nothing else to position).
  // It hangs off the Speech_Bubble anchor beside the head, its tail tapers toward the
  // Speech_Tail anchor near the mouth, and it never covers the mascot. No text.
  const B = Object.assign({ fill: '#FFFCF7', accent: '#EE5C1E', shadow: 'rgba(40,24,16,0.20)', width: 0.80, height: 0.38 }, o.bubble || {});
  const bubbleCanvas = document.createElement('canvas');
  const bctx = bubbleCanvas.getContext('2d');
  const bubbleTex = gl.createTexture();
  const qprog = gl.createProgram();
  gl.attachShader(qprog, compile(gl, gl.VERTEX_SHADER, `#version 300 es
layout(location=0) in vec2 aPos; layout(location=1) in vec2 aUV; out vec2 vUV;
void main(){ vUV = aUV; gl_Position = vec4(aPos, 0.0, 1.0); }`));
  gl.attachShader(qprog, compile(gl, gl.FRAGMENT_SHADER, `#version 300 es
precision mediump float; in vec2 vUV; uniform sampler2D uTex; out vec4 frag;
void main(){ frag = texture(uTex, vUV); }`));
  gl.linkProgram(qprog);
  const qTexLoc = gl.getUniformLocation(qprog, 'uTex');
  const qvao = gl.createVertexArray();
  const qbuf = gl.createBuffer();
  gl.bindVertexArray(qvao);
  gl.bindBuffer(gl.ARRAY_BUFFER, qbuf);
  gl.bufferData(gl.ARRAY_BUFFER, 16 * 4, gl.DYNAMIC_DRAW);
  gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 16, 0);
  gl.enableVertexAttribArray(1); gl.vertexAttribPointer(1, 2, gl.FLOAT, false, 16, 8);
  gl.bindVertexArray(null);

  let liveLevel = null, liveSmooth = 0;   // set by setVoiceLevel()

  function bubbleState(clip, t) {
    const sp = clip && clip.speech;
    if (!sp || !sp.bubble) return null;
    const keys = sp.bubble;
    let i = 0;
    for (let k = 0; k < keys.length; k++) if (t >= keys[k][0]) i = k;
    const [t0, state] = keys[i];
    if (state > 0) {
      let onAt = null;                      // pop-in only if the bubble was hidden earlier in this clip
      for (let k = i; k >= 1; k--) if (keys[k - 1][1] === 0 && keys[k][1] > 0) { onAt = keys[k][0]; break; }
      const u = onAt === null ? 1 : Math.min(1, (t - onAt) / 0.38);
      return { mode: state, vis: u, popping: u < 1, since: t - t0 };
    }
    if (i > 0 && keys[i - 1][1] > 0) {
      const u = 1 - Math.min(1, (t - t0) / 0.26);
      return u > 0 ? { mode: keys[i - 1][1], vis: u, closing: true, since: t - t0 } : null;
    }
    return null;
  }

  function bakedLevel(clip, t) {
    const sp = clip && clip.speech;
    if (!sp || !sp.voice || sp.voice.length < 3) return 0;
    const f = t * (sp.voiceFps || 30);
    const i = Math.min(sp.voice.length - 2, Math.max(0, Math.floor(f)));
    const u = Math.min(1, Math.max(0, f - i));
    return sp.voice[i] * (1 - u) + sp.voice[i + 1] * u;
  }

  function project(vp, p, w, h) {
    const x = vp[0] * p[0] + vp[4] * p[1] + vp[8] * p[2] + vp[12];
    const y = vp[1] * p[0] + vp[5] * p[1] + vp[9] * p[2] + vp[13];
    const ww = vp[3] * p[0] + vp[7] * p[1] + vp[11] * p[2] + vp[15];
    return [(x / ww * 0.5 + 0.5) * w, (1 - (y / ww * 0.5 + 0.5)) * h];
  }

  function roundedPill(c, x, y, w, h) {       // capsule in either orientation
    const r = Math.min(w, h) / 2;
    c.moveTo(x + r, y);
    c.arcTo(x + w, y, x + w, y + h, r);
    c.arcTo(x + w, y + h, x, y + h, r);
    c.arcTo(x, y + h, x, y, r);
    c.arcTo(x, y, x + w, y, r);
    c.closePath();
  }

  function drawBubble(vp, W, H) {
    const st = bubbleState(cur, Math.min(time, cur ? cur.duration : 0));
    if (!st || bubbleNode < 0 || tailNode < 0 || !worldM[bubbleNode] || !worldM[tailNode]) return;
    const bm = worldM[bubbleNode], tm = worldM[tailNode];
    const bc = [bm[12], bm[13], bm[14]], tip3 = [tm[12], tm[13], tm[14]];
    let [cx, cy] = project(vp, bc, W, H);
    const [ex] = project(vp, [bc[0] + B.width / 2, bc[1], bc[2]], W, H);
    const tip = project(vp, tip3, W, H);
    const halfW = Math.abs(ex - cx), bw = halfW * 2, bh = bw * B.height / B.width;
    const pad = Math.ceil(bh * 0.5);
    cx = Math.min(Math.max(cx, halfW + pad * 0.4), W - halfW - pad * 0.4);     // keep inside the window
    cy = Math.min(Math.max(cy, bh / 2 + pad * 0.4), H - bh / 2 - pad * 0.4);
    // pop: grow out of the tail (slight overshoot), shrink back into it when closing
    const v = st.vis;
    const sc = st.closing ? v * v * (3 - 2 * v) : 1 + 2.4 * Math.pow(v - 1, 3) + 1.4 * Math.pow(v - 1, 2);
    const alpha = Math.min(1, v * 1.6);
    const ox = tip[0] + (cx - tip[0]) * sc, oy = tip[1] + (cy - tip[1]) * sc;
    const sw = bw * sc, sh = bh * sc;
    const minX = Math.floor(Math.min(ox - sw / 2, tip[0]) - pad), maxX = Math.ceil(Math.max(ox + sw / 2, tip[0]) + pad);
    const minY = Math.floor(Math.min(oy - sh / 2, tip[1]) - pad), maxY = Math.ceil(Math.max(oy + sh / 2, tip[1]) + pad);
    const cw = Math.max(2, maxX - minX), ch = Math.max(2, maxY - minY);
    if (bubbleCanvas.width !== cw || bubbleCanvas.height !== ch) { bubbleCanvas.width = cw; bubbleCanvas.height = ch; }
    const c = bctx;
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.clearRect(0, 0, cw, ch);
    c.globalAlpha = alpha;
    c.translate(-minX, -minY);
    const x0 = ox - sw / 2, y0 = oy - sh / 2;
    // tail: tapered from the near lower edge of the pill to a point aimed at the mouth
    const dx = tip[0] - ox, dy = tip[1] - oy, dl = Math.hypot(dx, dy) || 1;
    const nx = dx / dl, ny = dy / dl;
    const bxp = ox + nx * sw * 0.32, byp = oy + ny * sh * 0.32;
    const tw = sh * 0.30;
    const tipX = ox + (tip[0] - ox) * sc, tipY = oy + (tip[1] - oy) * sc;
    c.save();
    c.shadowColor = B.shadow; c.shadowBlur = Math.max(4, sh * 0.22); c.shadowOffsetY = Math.max(1, sh * 0.05);
    c.fillStyle = B.fill;
    c.beginPath();
    c.moveTo(bxp - ny * tw, byp + nx * tw);
    c.quadraticCurveTo(bxp + nx * dl * 0.45 * sc - ny * tw * 0.2, byp + ny * dl * 0.45 * sc + nx * tw * 0.2, tipX, tipY);
    c.quadraticCurveTo(bxp + nx * dl * 0.45 * sc + ny * tw * 0.5, byp + ny * dl * 0.45 * sc - nx * tw * 0.5, bxp + ny * tw, byp - nx * tw);
    c.closePath();
    c.fill();
    c.beginPath(); roundedPill(c, x0, y0, sw, sh); c.fill();
    c.restore();
    c.fillStyle = B.fill;                    // cover the seam between tail and pill
    c.beginPath(); roundedPill(c, x0, y0, sw, sh); c.fill();
    // content
    const t = Math.min(time, cur ? cur.duration : 0);
    c.fillStyle = B.accent;
    const n = 5, gap = sw * 0.125, barW = sh * 0.15, maxH = sh * 0.6;
    const level = liveLevel !== null && cur && cur.name === 'Speak_Loop' ? liveSmooth : bakedLevel(cur, t);
    for (let i = 0; i < n; i++) {
      const x = ox + (i - (n - 1) / 2) * gap;
      let hgt = barW;
      if (st.mode === 1) {                   // typing dots: a soft travelling bounce
        const ph = (t * 1.25 - i * 0.16) % 1;
        const b = ph > 0 && ph < 0.5 ? Math.sin(ph * 2 * Math.PI) : 0;
        c.globalAlpha = alpha * (0.45 + 0.55 * b);
        const r = barW / 2 * (1 + 0.18 * b);
        c.beginPath(); c.arc(x, oy - b * sh * 0.07, r, 0, Math.PI * 2); c.fill();
        continue;
      }
      const weight = [0.55, 0.82, 1, 0.82, 0.55][i];
      const wob = 0.62 + 0.38 * Math.sin(2 * Math.PI * ([2.1, 2.9, 3.4, 2.6, 2.2][i] * t + i * 0.9));
      hgt = barW + (maxH - barW) * Math.min(1, level * weight * wob * 1.15);
      c.globalAlpha = alpha * (0.72 + 0.28 * Math.min(1, level * 1.4));
      c.beginPath(); roundedPill(c, x - barW / 2, oy - hgt / 2, barW, hgt);
      if (hgt <= barW + 0.5) { c.beginPath(); c.arc(x, oy, barW / 2, 0, Math.PI * 2); }
      c.fill();
    }
    c.globalAlpha = 1;
    // upload and draw as a screen-space quad (premultiplied)
    gl.bindTexture(gl.TEXTURE_2D, bubbleTex);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, bubbleCanvas);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    const X = px => px / W * 2 - 1, Y = py => 1 - py / H * 2;
    const q = new Float32Array([X(minX), Y(minY), 0, 0, X(maxX), Y(minY), 1, 0, X(minX), Y(maxY), 0, 1, X(maxX), Y(maxY), 1, 1]);
    gl.useProgram(qprog);
    gl.bindVertexArray(qvao);
    gl.bindBuffer(gl.ARRAY_BUFFER, qbuf);
    gl.bufferSubData(gl.ARRAY_BUFFER, 0, q);
    gl.disable(gl.DEPTH_TEST); gl.disable(gl.CULL_FACE);
    gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.activeTexture(gl.TEXTURE0); gl.uniform1i(qTexLoc, 0);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    gl.bindVertexArray(null);
    gl.enable(gl.DEPTH_TEST);
  }

  // live lip-sync: while Speak_Loop plays and a voice level is supplied, the mouth follows it
  let liveMouth = 'Default', liveMouthSince = 0, nextBlink = 3, blinkUntil = -1, liveClock = 0;
  function applyLiveFace(dt) {
    if (liveLevel === null || !cur || cur.name !== 'Speak_Loop' || !exprByName.Default) return;
    liveClock += dt;
    liveSmooth += (liveLevel - liveSmooth) * (liveLevel > liveSmooth ? 0.55 : 0.2);
    let want = liveSmooth < 0.1 ? 'Default' : liveSmooth < 0.32 ? 'Talk_E' : liveSmooth < 0.58 ? 'Talk_O' : 'Talk_A';
    if (!exprByName[want]) want = 'Default';
    if (want !== liveMouth && liveClock - liveMouthSince > 0.075) { liveMouth = want; liveMouthSince = liveClock; }
    if (liveClock > nextBlink) { blinkUntil = liveClock + 0.13; nextBlink = liveClock + 2.5 + Math.random() * 2.5; }
    const show = liveClock < blinkUntil && exprByName.Blink ? 'Blink' : liveMouth;
    for (const n of exprNodes) n.s.fill(0);
    exprByName[show].s.fill(1);
  }

  // playback
  // Redraw every frame while playing; when paused, only after a change (saves battery).
  let cur = clips[o.clip] || clips[clipNames[0]] || null;
  let loop = o.loop, idle = loop ? cur : null, queued = null, chain = null;
  let time = 0, playing = !!o.autoplay, last = null, raf = 0, disposed = false, dirty = true;
  let lastW = 0, lastH = 0;

  function switchTo(name, opts = {}) {
    const c = clips[name];
    if (!c) throw new Error(`Unknown clip "${name}". Available: ${clipNames.join(', ')}`);
    const bt = opts.blend ?? 0;
    if (bt > 0) { blendFrom = snapshot(); blendTime = blendLeft = bt; } else { blendFrom = null; blendLeft = 0; }
    cur = c; time = 0; loop = opts.loop ?? true;
    chain = opts.then || null;
    if (loop && opts.idle !== false) idle = c;
    dirty = true;
  }

  function step(dt) {
    if (!cur || !playing) return;
    time += dt * o.speed;
    if (blendLeft > 0) blendLeft = Math.max(0, blendLeft - dt);
    if (time >= cur.duration) {
      const ended = cur.name;
      if (queued) { const q = queued; queued = null; switchTo(q.name, { ...q.opts, blend: q.opts.blend ?? 0 }); }
      else if (!loop && chain) { const nx = chain; switchTo(nx.name, { blend: 0, ...nx.opts }); }
      else if (!loop) {
        if (idle && idle !== cur) switchTo(idle.name, { loop: true });   // one-shot finished: back to idle
        else { time = cur.duration; playing = false; }
      } else time %= cur.duration;
      if (o.onClipEnd) try { o.onClipEnd(ended); } catch (_) { /* user callback */ }
    }
  }

  let lastDt = 0;
  function evaluate() { applyClip(cur, Math.min(time, cur ? cur.duration : 0)); applyBlend(); applyLiveFace(lastDt); }

  function frame(now) {
    if (disposed) return;
    if (last !== null) { lastDt = (now - last) / 1000; step(lastDt); }
    last = now;
    const sized = canvas.clientWidth !== lastW || canvas.clientHeight !== lastH;
    if (playing || dirty || sized || liveLevel !== null) {
      lastW = canvas.clientWidth; lastH = canvas.clientHeight;
      evaluate();
      draw();
      dirty = false;
    }
    raf = requestAnimationFrame(frame);
  }
  evaluate();
  raf = requestAnimationFrame(frame);

  return {
    /** names of the clips in the GLB, e.g. ['Swing_Joy', 'Listen_Nod'] */
    get clips() { return clipNames.slice(); },
    /** a clip's extras (phase times etc.) and duration */
    clipInfo(name) { const c = clips[name]; return c ? { duration: c.duration, ...(c.extras || {}) } : null; },
    get clip() { return cur ? cur.name : null; },
    get time() { return time; },
    get duration() { return cur ? cur.duration : 0; },
    get playing() { return playing; },
    /** play(): resume. play(name, {loop, blend}): switch now, cross-fading over `blend` seconds (default 0.5). */
    play(name, opts = {}) {
      if (name) switchTo(name, { blend: 0.5, ...opts });
      playing = true; dirty = true;
    },
    /** Play a clip once, then return to the idle loop. Starts now with a short cross-fade. */
    playOnce(name, opts = {}) { switchTo(name, { blend: 0.5, ...opts, loop: false }); playing = true; },
    /** Switch when the current clip reaches its end (seamless when clips share that frame). */
    queue(name, opts = {}) { queued = { name, opts: { loop: true, ...opts } }; },
    /** Mascot starts talking: the swing settles, a speech bubble pops out, then it talks in a loop
     *  until stopSpeaking(). Needs the Speak_Start / Speak_Loop / Speak_End clips. */
    speak(opts = {}) {
      if (!clips.Speak_Start || !clips.Speak_Loop) throw new Error('This GLB has no speaking clips');
      if (cur && (cur.name === 'Speak_Start' || cur.name === 'Speak_Loop')) { if (chain && chain.name === 'Speak_End') chain = { name: 'Speak_Loop', opts: { loop: true, idle: false } }; return; }
      switchTo('Speak_Start', { blend: opts.blend ?? 0.5, loop: false, then: { name: 'Speak_Loop', opts: { loop: true, idle: false } } });
      playing = true;
    },
    /** Finish talking: the bubble pops away, a happy smile, and the swing picks back up into the idle loop. */
    stopSpeaking(opts = {}) {
      if (!cur) return;
      if (cur.name === 'Speak_Start') { chain = { name: 'Speak_End', opts: { loop: false } }; return; }
      if (cur.name === 'Speak_Loop') { switchTo('Speak_End', { blend: opts.blend ?? 0.35, loop: false }); playing = true; }
    },
    /** Live lip-sync for real speech: pass 0..1 (e.g. RMS of your TTS audio) every frame; null returns to the baked loop. */
    setVoiceLevel(v) { liveLevel = v === null || v === undefined ? null : Math.max(0, Math.min(1, +v)); },
    get speaking() { return !!cur && cur.name.startsWith('Speak_') && cur.name !== 'Speak_End'; },
    pause() { playing = false; },
    seek(t) { blendLeft = 0; time = cur && cur.duration ? Math.min(Math.max(t, 0), cur.duration) : 0; evaluate(); draw(); dirty = false; },
    setSpeed(sp) { o.speed = sp; },
    renderNow() { evaluate(); draw(); dirty = false; },
    dispose() {
      disposed = true; cancelAnimationFrame(raf); ro.disconnect(); cleanups.forEach(f => f());
      const lose = gl.getExtension('WEBGL_lose_context'); if (lose) lose.loseContext();
    },
  };
}

export function setMascotState(player, state, level = null) {
  if (!['idle', 'listening', 'speaking'].includes(state)) return;
  if (state === 'speaking') {
    player.speak();
    player.setVoiceLevel(Number.isFinite(level) ? Math.max(0, Math.min(1, level)) : null);
  } else {
    player.setVoiceLevel(null);
    player.stopSpeaking();
    if (player.speaking || player.clip === 'Speak_End') return;
    if (state === 'listening' && player.clip !== 'Listen_Nod') player.playOnce('Listen_Nod');
    else if (state === 'idle' && player.clip !== 'Swing_Joy') player.play('Swing_Joy', {loop: true});
  }
}

export async function mountMascot(host, reduced = false, size = 48) {
  const canvas = document.createElement('canvas');
  canvas.style.width = canvas.style.height = `${size}px`;
  canvas.setAttribute('aria-hidden', 'true');
  canvas.hidden = true;
  host.dataset.mascotReady = 'false';
  host.appendChild(canvas);
  const motion = window.matchMedia?.('(prefers-reduced-motion: reduce)');
  let player = null, failed = false, state = 'idle', level = null;
  const update = () => {
    const paused = failed || (motion?.matches ?? reduced) || document.hidden;
    host.dataset.mascotReady = String(!!player && !failed && !(motion?.matches ?? reduced));
    canvas.hidden = host.dataset.mascotReady !== 'true';
    if (!player) return;
    if (paused) player.pause();
    else { setMascotState(player, state, level); player.play(); }
  };
  const lost = event => { event.preventDefault(); failed = true; update(); };
  canvas.addEventListener('webglcontextlost', lost);
  motion?.addEventListener?.('change', update);
  document.addEventListener('visibilitychange', update);
  try {
    player = await createMascotPlayer(canvas, {
      src: '/charlie/mascot_animations.glb', clip: 'Swing_Joy', loop: true, autoplay: false,
      onClipEnd: update,
    });
  } catch { failed = true; }
  update();
  return {
    player,
    /** @param {number | null} rms */
    setState(next, rms = null) {
      if (!['idle', 'listening', 'speaking'].includes(next)) return;
      state = next; level = rms; update();
    },
    dispose() {
      canvas.removeEventListener('webglcontextlost', lost);
      motion?.removeEventListener?.('change', update);
      document.removeEventListener('visibilitychange', update);
      player?.dispose();
      canvas.remove();
      host.dataset.mascotReady = 'false';
    },
  };
}
