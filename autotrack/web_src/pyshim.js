// pyshim.js - lets the unchanged dashboard page run without a server.
// Every fetch("/api/...") the page makes is sent to sim-worker.js (the Python simulation running
// in the browser) and answered from there. Everything else (CSS, photos) loads normally.
(function () {
  const worker = new Worker("sim-worker.js");
  const pending = new Map();
  let nextId = 1;

  const overlay = document.createElement("div");
  overlay.id = "web-demo-loading";
  overlay.setAttribute("role", "status");
  overlay.style.cssText = "position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;" +
    "background:rgba(28,29,32,.92);color:#fff;font:600 16px system-ui,sans-serif;text-align:center;padding:24px";
  overlay.innerHTML = '<div><div style="font-size:20px;margin-bottom:8px">AutoTrack web demo</div>' +
    '<div id="web-demo-msg">Starting the airport simulation in your browser...</div>' +
    '<div style="font-weight:400;opacity:.75;margin-top:10px;font-size:13px">First visit downloads Python (~10 MB). ' +
    'Simulation only: the live ESP32 and room demo need the real laptops.</div></div>';
  const show = () => document.body && !overlay.isConnected && document.body.appendChild(overlay);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", show); else show();

  worker.onmessage = (e) => {
    const m = e.data;
    if (m.type === "reply") {
      const done = pending.get(m.id);
      if (done) {
        pending.delete(m.id);
        done(new Response(m.body, { status: m.status, headers: { "Content-Type": "application/json" } }));
      }
    } else if (m.type === "progress") {
      const el = document.getElementById("web-demo-msg");
      if (el) el.textContent = m.text;
    } else if (m.type === "ready") {
      overlay.remove();
    } else if (m.type === "error") {
      const el = document.getElementById("web-demo-msg");
      if (el) el.textContent = "Could not start the simulation: " + m.message;
    }
  };

  const realFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    if (url.origin !== location.origin || !url.pathname.startsWith("/api/")) return realFetch(input, init);
    return new Promise((resolve) => {
      const id = nextId++;
      pending.set(id, resolve);
      worker.postMessage({
        type: "req", id,
        method: (init && init.method) || "GET",
        url: url.pathname + url.search,
        body: init && init.body != null ? String(init.body) : null,
      });
    });
  };
})();
