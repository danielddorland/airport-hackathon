#!/usr/bin/env node
/**
 * Fake 4 laptops + a moving ESP so you can try the dashboard without hardware.
 *
 *   npm run dev                                   (in one terminal)
 *   node tools/simulate.mjs                       (in another)
 *   node tools/simulate.mjs --server https://your-app.vercel.app --key SECRET
 *   node tools/simulate.mjs --hold 1.2,1.2        (park the ESP at a spot, e.g. to test calibration)
 *
 * Laptops register themselves at the room corners (default 6 x 5 m room), with the
 * first one as central. Each has a slightly different radio, like real laptops.
 */

const args = Object.fromEntries(
  process.argv.slice(2).reduce((acc, a, i, all) => {
    if (a.startsWith("--")) acc.push([a.slice(2), all[i + 1]?.startsWith("--") ? "true" : all[i + 1] ?? "true"]);
    return acc;
  }, [])
);

const SERVER = (args.server ?? "http://localhost:3000").replace(/\/$/, "");
const KEY = args.key ?? process.env.AGENT_KEY ?? "";
const W = 6;
const H = 5;
const TRUE_N = 2.4;
const NOISE_DB = 4;
const hold = args.hold ? args.hold.split(",").map(Number) : null;

const laptops = [
  { id: "sim-A", x: 0.3, y: 0.3, tx: -58, central: true },
  { id: "sim-B", x: W - 0.3, y: 0.3, tx: -62 },
  { id: "sim-C", x: W - 0.3, y: H - 0.3, tx: -55 },
  { id: "sim-D", x: 0.3, y: H - 0.3, tx: -60 },
];

const gauss = () => {
  const u = 1 - Math.random();
  const v = Math.random();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
};
const median = (a) => {
  const s = [...a].sort((x, y) => x - y);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};

function tagPosition(t) {
  if (hold) return { x: hold[0], y: hold[1] };
  // slow wandering loop that leaves the middle of the room now and then
  const a = (t / 40) * 2 * Math.PI;
  return { x: W / 2 + 2.3 * Math.cos(a), y: H / 2 + 1.7 * Math.sin(2 * a) };
}

const smooth = {};
const t0 = Date.now();

async function report(l, pos) {
  const d = Math.max(0.25, Math.hypot(pos.x - l.x, pos.y - l.y));
  const samples = Array.from({ length: 12 }, () => l.tx - 10 * TRUE_N * Math.log10(d) + NOISE_DB * gauss());
  const raw = median(samples);
  smooth[l.id] = smooth[l.id] == null ? raw : smooth[l.id] + 0.45 * (raw - smooth[l.id]);
  const body = {
    id: l.id,
    rssi: Math.round(smooth[l.id] * 10) / 10,
    raw: Math.round(raw * 10) / 10,
    samples: samples.length,
    central: !!l.central,
  };
  const res = await fetch(`${SERVER}/api/readings`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(KEY ? { "x-agent-key": KEY } : {}) },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

console.log(`Simulating ${laptops.length} laptops -> ${SERVER}${hold ? ` (ESP held at ${hold})` : ""}`);
setInterval(async () => {
  const pos = tagPosition((Date.now() - t0) / 1000);
  try {
    const results = await Promise.all(laptops.map((l) => report(l, pos)));
    const r = results[0];
    const est = r.position ? `${r.position.x.toFixed(2)},${r.position.y.toFixed(2)}` : "—";
    console.log(
      `true ${pos.x.toFixed(2)},${pos.y.toFixed(2)}  est ${est}  status ${r.status}${r.alert ? "  LED: BLINK" : ""}`
    );
  } catch (e) {
    console.error("post failed:", e.message);
  }
}, 1000);
