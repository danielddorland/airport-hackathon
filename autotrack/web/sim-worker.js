// sim-worker.js - runs the REAL AutoTrack airport simulation (Python) inside the browser.
// Pyodide = Python compiled for the web. This worker loads it, copies our Python files in,
// and then answers the page's /api/... requests with autotrack/webapi.py (same answers as server.py).
// It also advances the simulation clock ~20 times a second and builds the "jump" checkpoints
// in small slices in the background. Running in a worker keeps the page itself smooth.

const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/";
importScripts(PYODIDE + "pyodide.js");

let app = null;
const queue = [];

function reply(m) {
  let status = 500, body = '{"detail":"simulation error"}';
  try {
    const r = app.handle(m.method, m.url, m.body == null ? null : String(m.body));
    const arr = r.toJs();
    r.destroy();
    status = arr[0];
    body = arr[1];
  } catch (e) {
    body = JSON.stringify({ detail: String(e) });
  }
  postMessage({ type: "reply", id: m.id, status, body });
}

onmessage = (e) => {
  const m = e.data;
  if (!m || m.type !== "req") return;
  if (app) reply(m); else queue.push(m);
};

async function boot() {
  postMessage({ type: "progress", text: "Downloading Python for the browser..." });
  const py = await loadPyodide({ indexURL: PYODIDE });
  await py.loadPackage("sqlite3");     // feed.py imports it (the web demo doesn't use a database)
  postMessage({ type: "progress", text: "Loading the airport simulation..." });
  const files = await (await fetch("py/files.json")).json();
  for (const f of files) {
    const text = await (await fetch("py/" + f)).text();
    const dir = "/app/" + f.split("/").slice(0, -1).join("/");
    py.FS.mkdirTree(dir);
    py.FS.writeFile("/app/" + f, text);
  }
  py.runPython("import sys\nsys.path.insert(0, '/app')\nfrom autotrack.webapi import WebApp\napp = WebApp()");
  app = py.globals.get("app");
  postMessage({ type: "ready" });
  queue.splice(0).forEach(reply);

  let last = performance.now();
  setInterval(() => {
    const now = performance.now();
    try { app.tick((now - last) / 1000); } catch (e) { console.error(e); }
    last = now;
    try { app.build_some(0.02); } catch (e) { console.error(e); }
  }, 50);
}

boot().catch((e) => postMessage({ type: "error", message: String(e) }));
