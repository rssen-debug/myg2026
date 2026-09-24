// Frame pump. Chromium is the compositor. t is the only clock.
// Writes a transparent PNG per frame. Python overlays those on the footage.
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer-core";

const HERE = path.dirname(fileURLToPath(import.meta.url));

function which(cmd) {
  for (const bin of process.platform === "win32" ? ["where"] : ["which"]) {
    try {
      const out = execFileSync(bin, [cmd], { encoding: "utf8" }).trim().split(/\r?\n/)[0];
      if (out && fs.existsSync(out)) return out;
    } catch { /* next */ }
  }
  return "";
}

function findChrome() {
  if (process.env.CHROME && fs.existsSync(process.env.CHROME)) return process.env.CHROME;
  const candidates = [
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/microsoft-edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  ];
  for (const c of candidates) if (fs.existsSync(c)) return c;
  for (const name of ["chromium", "chromium-browser", "google-chrome", "microsoft-edge", "msedge", "chrome"]) {
    const hit = which(name);
    if (hit) return hit;
  }
  return "";
}

const CHROME = findChrome();

function arg(name, fallback) {
  const i = process.argv.indexOf(name);
  return i >= 0 ? process.argv[i + 1] : fallback;
}

const timelinePath = arg("--timeline");
const outDir = arg("--out");
const onlyStill = arg("--still", "");
if (!timelinePath || !outDir) {
  console.error("usage: node gfx/domgfx.mjs --timeline t.json --out dir/ [--still 1.2]");
  process.exit(1);
}
if (!CHROME) {
  console.error("No Chrome/Edge/Chromium. Install one, or set CHROME to the exe.");
  process.exit(1);
}
const timeline = JSON.parse(fs.readFileSync(timelinePath, "utf8"));
const W = timeline.w, H = timeline.h, FPS = timeline.fps || 24;
const frames = Math.max(1, Math.round(timeline.duration * FPS));
fs.mkdirSync(outDir, { recursive: true });

const TYPES = { ".html": "text/html", ".js": "text/javascript", ".mjs": "text/javascript",
                ".ttf": "font/ttf", ".css": "text/css" };

function serve() {
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      const url = decodeURIComponent(req.url.split("?")[0]);
      const file = path.normalize(path.join(HERE, url));
      if (!file.startsWith(HERE) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
        res.writeHead(404); res.end("no"); return;
      }
      res.writeHead(200, { "Content-Type": TYPES[path.extname(file)] || "application/octet-stream" });
      fs.createReadStream(file).pipe(res);
    });
    server.listen(0, "127.0.0.1", () => resolve(server));
  });
}

const server = await serve();
const port = server.address().port;
const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage", "--hide-scrollbars", "--font-render-hinting=none"],
});
const page = await browser.newPage();
await page.setViewport({ width: W, height: H, deviceScaleFactor: 1 });
await page.goto(`http://127.0.0.1:${port}/scene.html`, { waitUntil: "networkidle0" });
await page.waitForFunction("window.__ready === true");
await page.evaluate(() => document.fonts.ready);
await page.evaluate((tl, w, h) => {
  window.__setSize(w, h);
  window.__setTimeline(tl);
}, timeline, W, H);

async function shoot(t, file) {
  await page.evaluate((time) => window.__render(time), t);
  await page.screenshot({ path: file, omitBackground: true, type: "png" });
}

if (onlyStill) {
  const t = Number(onlyStill);
  await shoot(t, path.join(outDir, "still.png"));
  console.log(JSON.stringify({ event: "still", t, out: path.join(outDir, "still.png"), chrome: CHROME }));
} else {
  for (let i = 0; i < frames; i++) {
    const t = i / FPS;
    await shoot(t, path.join(outDir, String(i).padStart(4, "0") + ".png"));
    if (i % 24 === 0 || i === frames - 1) {
      console.log(JSON.stringify({ event: "progress", done: i + 1, total: frames }));
    }
  }
  console.log(JSON.stringify({ event: "done", frames, fps: FPS, chrome: CHROME }));
}

await browser.close();
server.close();
