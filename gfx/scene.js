// Graphics layer. A frame is a pure function of t. No wall clock, no React.
// HTML/CSS is the type engine. Three.js is the ring and particles behind a stat.
// The stage is the real frame size (9:16 or 16:9). Nothing is stretched.
import * as THREE from "./vendor/three.module.min.js";

const stage = document.getElementById("stage");
const hookEl = document.getElementById("hook");
const hookLine = document.getElementById("hookline");
const kickerEl = document.getElementById("kicker");
const statEl = document.getElementById("stat");
const statBig = statEl.querySelector(".big");
const statLabel = statEl.querySelector(".label");
const lowerEl = document.getElementById("lower");
const lowerBar = lowerEl.querySelector(".bar");
const lowerName = lowerEl.querySelector(".name");
const lowerSub = lowerEl.querySelector(".sub");
const capsEl = document.getElementById("caps");
const brackets = document.getElementById("brackets");
const canvas = document.getElementById("world");

const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: false });
renderer.setClearColor(0x000000, 0);
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(38, 9 / 16, 0.1, 40);
camera.position.z = 8;

const ring = new THREE.Mesh(
  new THREE.TorusGeometry(1.35, 0.055, 10, 64),
  new THREE.MeshBasicMaterial({ color: 0xe8222e })
);
scene.add(ring);
const N = 90;
const positions = new Float32Array(N * 3);
const seeds = [];
for (let i = 0; i < N; i++) {
  const a = (i / N) * Math.PI * 2;
  const r = 0.35 + (i % 8) * 0.16;
  seeds.push({ a, r, z: ((i * 17) % 10) / 10 });
  positions[i * 3] = Math.cos(a) * r;
  positions[i * 3 + 1] = Math.sin(a) * r;
  positions[i * 3 + 2] = seeds[i].z;
}
const dots = new THREE.Points(
  new THREE.BufferGeometry(),
  new THREE.PointsMaterial({ color: 0xffe14a, size: 0.055, transparent: true })
);
dots.geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
scene.add(dots);

let frame = { w: 1080, h: 1920, wide: false, u: 1 };
let metrics = {};
let timeline = {
  hook: "", hookIn: 0.12, hookOut: 2.6,
  kicker: "",
  person: "", personSub: "ON CAMERA", lowerIn: 0.35,
  stat: null,
  captions: [],
  brackets: true,
};
let capStart = -1;
let capText = "";

function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
function ease(p) { p = clamp(p, 0, 1); return 1 - Math.pow(1 - p, 3); }
function show(el, on) {
  // inline opacity wins over .hidden, so a hook that animated in
  // would otherwise stay on screen for the rest of the cut
  el.classList.toggle("hidden", !on);
  if (!on) el.style.opacity = "0";
}

function fit(el, maxPx, minPx) {
  let size = maxPx;
  el.style.fontSize = size + "px";
  const box = el.clientWidth || 0;
  if (!box) return;
  while (size > minPx && el.scrollWidth > box + 2) {
    size -= 2;
    el.style.fontSize = size + "px";
  }
}

function place(el, x, y, w) {
  el.style.left = x + "px";
  el.style.top = y + "px";
  if (w) el.style.width = w + "px";
}

function placeHookLine() {
  // under the wrapped title, never through it. offsetHeight is 0 until laid out.
  const h = hookEl.offsetHeight || Math.round((metrics.hook || 72) * 1.1);
  const top = hookEl.offsetTop + h + Math.round(8 * frame.u);
  const width = parseFloat(hookLine.style.width) || Math.round(frame.w * 0.22);
  hookLine.style.top = top + "px";
  hookLine.style.left = Math.round((frame.w - width) / 2) + "px";
}

window.__setSize = function (w, h) {
  frame = { w, h, wide: w >= h, u: Math.min(w, h) / 1080 };
  const u = frame.u;
  const wide = frame.wide;
  metrics = {
    hook: Math.round((wide ? 72 : 88) * u),
    hookMin: Math.round((wide ? 36 : 40) * u),
    cap: Math.round((wide ? 48 : 70) * u),
    capMin: Math.round((wide ? 28 : 34) * u),
    name: Math.round((wide ? 36 : 52) * u),
    sub: Math.round((wide ? 22 : 30) * u),
    stat: Math.round((wide ? 128 : 150) * u),
    statMin: Math.round((wide ? 64 : 72) * u),
    label: Math.round((wide ? 28 : 40) * u),
    kicker: Math.round((wide ? 20 : 26) * u),
  };
  stage.style.width = w + "px";
  stage.style.height = h + "px";
  stage.style.transform = "none";
  document.documentElement.style.width = w + "px";
  document.documentElement.style.height = h + "px";
  document.body.style.width = w + "px";
  document.body.style.height = h + "px";

  const sx = Math.round(w * (wide ? 0.055 : 0.07));
  const hookTop = Math.round(h * (wide ? 0.06 : 0.055));
  // chip sits centered over the title, not floating in the left corner
  kickerEl.style.left = "50%";
  kickerEl.style.right = "auto";
  kickerEl.style.width = "auto";
  kickerEl.style.top = hookTop + "px";
  kickerEl.style.fontSize = metrics.kicker + "px";
  kickerEl.style.textAlign = "center";
  const kickerBlock = Math.round((metrics.kicker || 22) * 2.4 + 8 * u);
  place(hookEl, sx, hookTop + kickerBlock, w - sx * 2);
  hookLine.style.height = Math.max(3, Math.round(5 * u)) + "px";
  hookLine.style.width = Math.round(w * (wide ? 0.16 : 0.28)) + "px";

  // wide: sit above the ASS caption band (bottom 16%). tall: mid-lower, captions are ours.
  const lowerTop = wide
    ? Math.round(h * 0.62)
    : Math.round(h * 0.58);
  place(lowerEl, sx, lowerTop, null);
  lowerName.style.fontSize = metrics.name + "px";
  lowerName.style.maxWidth = Math.round(w * 0.62) + "px";
  lowerSub.style.fontSize = metrics.sub + "px";
  lowerBar.style.height = Math.max(4, Math.round(7 * u)) + "px";

  const capBottom = Math.round(h * (wide ? 0.08 : 0.13));
  capsEl.style.left = sx + "px";
  capsEl.style.right = sx + "px";
  capsEl.style.bottom = capBottom + "px";
  capsEl.style.top = "auto";
  capsEl.style.width = "auto";

  statEl.style.left = sx + "px";
  statEl.style.right = sx + "px";
  statEl.style.top = Math.round(h * (wide ? 0.30 : 0.32)) + "px";
  statEl.style.width = "auto";

  const inset = Math.round(Math.min(w, h) * (wide ? 0.035 : 0.045));
  const b = Math.round(22 * u);
  for (const el of brackets.querySelectorAll(".bracket")) {
    el.style.width = b + "px";
    el.style.height = b + "px";
  }
  brackets.querySelector(".tl").style.cssText += `left:${inset}px;top:${inset}px;`;
  brackets.querySelector(".tr").style.cssText += `right:${inset}px;top:${inset}px;left:auto;`;
  brackets.querySelector(".bl").style.cssText += `left:${inset}px;bottom:${inset}px;top:auto;`;
  brackets.querySelector(".br").style.cssText += `right:${inset}px;bottom:${inset}px;left:auto;top:auto;`;

  renderer.setSize(w, h, false);
  camera.aspect = w / Math.max(1, h);
  camera.updateProjectionMatrix();
  camera.position.z = wide ? 7.4 : 8;
};

window.__setTimeline = function (tl) {
  timeline = tl || timeline;
  hookEl.textContent = (timeline.hook || "").toUpperCase();
  fit(hookEl, metrics.hook || 72, metrics.hookMin || 36);
  placeHookLine();
  kickerEl.textContent = (timeline.kicker || "").toUpperCase();
  lowerName.textContent = (timeline.person || "").toUpperCase();
  lowerSub.textContent = (timeline.personSub || "ON CAMERA").toUpperCase();
  if (timeline.stat) {
    statBig.textContent = timeline.stat.big || "";
    statLabel.textContent = timeline.stat.label || "";
    statLabel.style.fontSize = (metrics.label || 32) + "px";
    fit(statBig, metrics.stat || 128, metrics.statMin || 64);
  }
  brackets.style.display = timeline.brackets === false ? "none" : "block";
};

window.__render = function (t) {
  const tl = timeline;
  const hookOn = !!(tl.hook) && t >= (tl.hookIn || 0) && t < (tl.hookOut || 2.6);
  show(hookEl, hookOn);
  show(hookLine, hookOn);
  show(kickerEl, hookOn && !!tl.kicker);
  if (hookOn) {
    const u = ease((t - (tl.hookIn || 0)) / 0.28);
    const fade = t > tl.hookOut - 0.22 ? (tl.hookOut - t) / 0.22 : 1;
    const o = u * clamp(fade, 0, 1);
    const dy = (1 - u) * -28;
    hookEl.style.transform = `translateY(${dy}px)`;
    hookEl.style.opacity = String(o);
    kickerEl.style.opacity = String(o);
    kickerEl.style.transform = `translateX(-50%) translateY(${(1 - u) * -16}px)`;
    hookLine.style.opacity = String(o);
    // the line rides with the title, so the slide-in cannot cut through the words
    hookLine.style.transform = `translateY(${dy}px) scaleX(${0.15 + 0.85 * u})`;
    placeHookLine();
  }

  const st = tl.stat;
  const statNow = !!(st && t >= st.t && t < st.t + st.dur);
  const statSoon = !!(st && t >= st.t - 0.16 && t < st.t);
  const lowerOn = !!(tl.person) && t >= (tl.lowerIn || 0.4);
  const lowerShow = lowerOn && !statNow;
  show(lowerEl, lowerShow);
  if (lowerShow) {
    const u = ease((t - (tl.lowerIn || 0.4)) / 0.28);
    let o = u;
    if (statSoon) o *= (st.t - t) / 0.16;
    lowerEl.style.transform = `translateX(${(1 - u) * -64}px)`;
    lowerEl.style.opacity = String(clamp(o, 0, 1));
    lowerBar.style.width = Math.round(72 * frame.u * u) + "px";
  }

  show(statEl, statNow);
  let worldAlpha = 0;
  if (statNow) {
    const u = ease((t - st.t) / 0.32);
    const out = t > st.t + st.dur - 0.25 ? 1 - (t - (st.t + st.dur - 0.25)) / 0.25 : 1;
    const o = u * clamp(out, 0, 1);
    statEl.style.opacity = String(o);
    statEl.style.transform = `scale(${1.16 - 0.16 * u})`;
    worldAlpha = o;
    ring.rotation.z = t * 0.9;
    // a hard tilt turns the ring into an egg on 9:16. keep it a circle.
    ring.rotation.x = 0.12;
    ring.scale.setScalar(0.55 + 0.6 * u);
    const pos = dots.geometry.attributes.position.array;
    for (let i = 0; i < N; i++) {
      const s = seeds[i];
      const burst = s.r * (0.55 + u * 1.7);
      pos[i * 3] = Math.cos(s.a + t * 0.45) * burst;
      pos[i * 3 + 1] = Math.sin(s.a + t * 0.45) * burst;
      pos[i * 3 + 2] = s.z * u * 2;
    }
    dots.geometry.attributes.position.needsUpdate = true;
  }
  ring.visible = worldAlpha > 0.02;
  dots.visible = ring.visible;
  dots.material.opacity = worldAlpha;
  renderer.render(scene, camera);
  canvas.style.opacity = String(worldAlpha);

  let cap = "";
  let capT = 0;
  for (const c of tl.captions || []) {
    if (t >= c.t && t < c.t + c.dur) { cap = c.text; capT = c.t; break; }
  }
  show(capsEl, !!cap);
  if (cap) {
    if (cap !== capText) {
      capText = cap;
      capStart = capT;
      capsEl.textContent = cap;
      fit(capsEl, metrics.cap || 64, metrics.capMin || 32);
    }
    const u = ease((t - capStart) / 0.12);
    capsEl.style.opacity = "1";
    capsEl.style.transform = `scale(${1.08 - 0.08 * u})`;
  }
};

window.__ready = true;
