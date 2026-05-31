// Build a silent captioned demo-reel MP4 from the real screenshots + key stats.
// Renders each scene with headless Chrome, stitches with ffmpeg (hard cuts +
// per-clip fades). No voiceover — you narrate over it or screen-record reel.html.
import { execSync } from "node:child_process";
import { writeFileSync, mkdirSync, rmSync, existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, "..");
const SHOTS = resolve(ROOT, "docs/screenshots").replace(/\\/g, "/");
const BUILD = resolve(ROOT, "tools/_build");
const OUT = resolve(ROOT, "docs/reel.mp4");
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const W = 1280, H = 720, FPS = 30;

const scenes = [
  { type: "title", title: "CrisisLine", sub: "A crisis-counselor voice agent that improves itself",
    tag: "YC · Cekura · Daily — Voice Agents Hackathon", dur: 4 },
  { type: "image", img: "landing.png", layout: "center",
    cap: "Stays on the line when 988 is busy", sub: "phone · SMS · web chat — open-weights Nemotron voice", dur: 4.5 },
  { type: "image", img: "dashboard.png", layout: "center",
    cap: "Reads condition, risk & safety — in real time", sub: "the agent's mind, streamed live", dur: 5 },
  { type: "image", img: "graph.png", layout: "side",
    cap: "It scores itself on real calls", sub: "a live Cekura eval harness — every point is a real run", dur: 5 },
  { type: "stat", big: "0.13 → 0.89", accent: "teal",
    cap: "The harness caught the agent was mute, then scored the fix", sub: "real Cekura runs · 7 of 8 metrics pass", dur: 5 },
  { type: "stat", big: "✕  rejected", accent: "red",
    cap: "It rejected its OWN rewrites that dropped the safety check", sub: "the accept-gate won't trade safety for brevity — holds at 0.89", dur: 5.5 },
  { type: "stat", big: "~1.2 s", accent: "teal",
    cap: "Voice-to-voice, on open weights", sub: "NVIDIA STT 19ms · Nemotron 402ms · OpenAI TTS 746ms", dur: 4.5 },
  { type: "stat", big: "server off  ✓", accent: "teal",
    cap: "The demo runs even if every server is down", sub: "cached results · offline reasoning reel · offline counselor", dur: 4.5 },
  { type: "end", title: "github.com/Kush614/Crisisline",
    sub: "a bridge to 988 — never a replacement for a human. If you're in crisis, call or text 988.", dur: 5 },
];

const FONTS = `<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">`;
const BASE = `
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{width:${W}px;height:${H}px;overflow:hidden}
  body{background:radial-gradient(900px 520px at 50% -10%,#1a1a2e00,#08080c),#08080c;color:#ececf5;
    font-family:Inter,system-ui,sans-serif;display:flex;align-items:center;justify-content:center}
  .dsp{font-family:'Space Grotesk',sans-serif;letter-spacing:-.02em;font-weight:700}
  .grad{background:linear-gradient(90deg,#2dd4bf,#8b8cf9 55%,#c084fc);-webkit-background-clip:text;background-clip:text;color:transparent}
  .brand{position:absolute;top:34px;left:42px;display:flex;align-items:center;gap:11px;font-family:'Space Grotesk';font-weight:700;font-size:20px}
  .dot{width:13px;height:13px;border-radius:50%;background:radial-gradient(circle at 30% 30%,#2dd4bf,#8b8cf9);box-shadow:0 0 18px #2dd4bf}
  .tag{position:absolute;bottom:30px;left:42px;color:#6b6b80;font-size:14px}
  .pageno{position:absolute;bottom:30px;right:42px;color:#6b6b80;font-size:13px;font-family:'Space Grotesk'}
  .cap{font-family:'Space Grotesk';font-weight:700;font-size:34px;line-height:1.12;letter-spacing:-.02em}
  .sub{color:#9595a8;font-size:18px;margin-top:14px;line-height:1.5}
  .shot{border-radius:14px;border:1px solid #2a2a3d;box-shadow:8px 10px 0 #000,0 0 60px #00000080}
`;
const brand = `<div class="brand"><span class="dot"></span>CrisisLine</div>`;
const pageno = (i) => `<div class="pageno">${String(i + 1).padStart(2, "0")} / ${String(scenes.length).padStart(2, "0")}</div>`;

function html(inner) {
  return `<!doctype html><html><head><meta charset="utf-8">${FONTS}<style>${BASE}</style></head><body>${inner}</body></html>`;
}

function render(s, i) {
  if (s.type === "title")
    return html(`${brand}
      <div style="text-align:center;padding:0 80px">
        <div class="dsp grad" style="font-size:92px;line-height:1">${s.title}</div>
        <div class="dsp" style="font-size:30px;margin-top:18px;color:#ececf5">${s.sub}</div>
        <div style="color:#9595a8;font-size:17px;margin-top:26px">${s.tag}</div>
      </div>${pageno(i)}`);
  if (s.type === "end")
    return html(`${brand}
      <div style="text-align:center;padding:0 90px">
        <div class="dsp" style="font-size:30px;color:#9595a8;margin-bottom:14px">open source ↗</div>
        <div class="dsp grad" style="font-size:46px;line-height:1.15;word-break:break-word">${s.title}</div>
        <div class="sub" style="font-size:19px;margin-top:26px;max-width:820px;margin-left:auto;margin-right:auto">${s.sub}</div>
      </div>${pageno(i)}`);
  if (s.type === "stat") {
    const col = s.accent === "red" ? "#f87171" : "#2dd4bf";
    return html(`${brand}
      <div style="text-align:center;padding:0 90px;max-width:1040px">
        <div class="dsp" style="font-size:96px;line-height:1;color:${col};text-shadow:0 0 50px ${col}55">${s.big}</div>
        <div class="cap" style="margin-top:26px">${s.cap}</div>
        <div class="sub" style="font-size:19px">${s.sub}</div>
      </div>${pageno(i)}`);
  }
  // image scene
  const src = `file:///${SHOTS}/${s.img}`;
  if (s.layout === "side")
    return html(`${brand}
      <div style="display:flex;align-items:center;gap:54px;padding:0 70px;width:100%;max-width:1180px">
        <img class="shot" src="${src}" style="height:600px;width:auto">
        <div style="flex:1">
          <div class="cap" style="font-size:38px">${s.cap}</div>
          <div class="sub" style="font-size:19px">${s.sub}</div>
        </div>
      </div>${pageno(i)}`);
  return html(`${brand}
    <div style="display:flex;flex-direction:column;align-items:center;gap:26px;padding:54px 0 0">
      <img class="shot" src="${src}" style="max-height:470px;max-width:1040px;width:auto">
      <div style="text-align:center;padding:0 80px">
        <div class="cap">${s.cap}</div><div class="sub">${s.sub}</div>
      </div>
    </div>${pageno(i)}`);
}

// --- build ---
if (existsSync(BUILD)) rmSync(BUILD, { recursive: true, force: true });
mkdirSync(BUILD, { recursive: true });

const clips = [];
scenes.forEach((s, i) => {
  const hp = `${BUILD}/scene_${i}.html`;
  const pp = `${BUILD}/frame_${i}.png`;
  const cp = `${BUILD}/clip_${i}.mp4`;
  writeFileSync(hp, render(s, i));
  console.log(`render scene ${i} (${s.type})`);
  execSync(`"${CHROME}" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 --window-size=${W},${H} --virtual-time-budget=2500 --screenshot="${pp}" "file:///${hp.replace(/\\/g, "/")}"`, { stdio: "ignore" });
  const d = s.dur, fo = (d - 0.45).toFixed(2);
  execSync(`ffmpeg -y -loop 1 -t ${d} -i "${pp}" -vf "scale=${W}:${H},fade=t=in:st=0:d=0.45,fade=t=out:st=${fo}:d=0.45,format=yuv420p" -r ${FPS} -c:v libx264 -pix_fmt yuv420p "${cp}"`, { stdio: "ignore" });
  clips.push(cp);
});

const list = `${BUILD}/list.txt`;
writeFileSync(list, clips.map(c => `file '${c.replace(/\\/g, "/")}'`).join("\n"));
console.log("concat → mp4");
execSync(`ffmpeg -y -f concat -safe 0 -i "${list}" -c:v libx264 -pix_fmt yuv420p -r ${FPS} -movflags +faststart "${OUT}"`, { stdio: "ignore" });
const total = scenes.reduce((a, s) => a + s.dur, 0);
console.log(`DONE → ${OUT}  (~${total}s, ${scenes.length} scenes)`);
