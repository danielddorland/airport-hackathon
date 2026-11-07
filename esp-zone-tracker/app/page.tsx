"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import RoomMap, { STATUS_COLOR, type Mode } from "@/components/RoomMap";
import { defaultCalibPoint } from "@/lib/engine";
import { median } from "@/lib/positioning";
import type { AnchorConfig, Config, Point, Status, TrackerState } from "@/lib/types";

type ConfigPatch = Partial<Omit<Config, "anchors" | "room">> & {
  room?: Partial<Config["room"]>;
  anchors?: Record<string, Partial<AnchorConfig> | null>;
};

type CalibResult = { id: string; ok: boolean; before?: number; after?: number; d?: number; rssi?: number; n?: number };
type CalibState =
  | { phase: "idle" }
  | { phase: "running"; progress: number }
  | { phase: "done" | "error"; results: CalibResult[]; message: string };

const POLL_MS = 500;
const CAL_MS = 12000;
const CAL_SETTLE_MS = 2500;
const TRAIL_LEN = 40;

const STATUS_TEXT: Record<Status, string> = {
  safe: "Safe: inside zone",
  leaving: "Leaving zone…",
  alert: "ALERT: outside safe zone",
  nofix: "No position fix",
  nozone: "No safe zone drawn",
  disarmed: "Alerts disarmed",
};

function applyLocal(c: Config, p: ConfigPatch): Config {
  const next: Config = { ...c, ...p, room: { ...c.room, ...(p.room ?? {}) }, anchors: { ...c.anchors } } as Config;
  if (p.anchors) {
    for (const [id, a] of Object.entries(p.anchors)) {
      if (a === null) delete next.anchors[id];
      else next.anchors[id] = { ...(c.anchors[id] ?? { x: 0, y: 0, txPower: c.defaultTxPower }), ...a };
    }
  }
  return next;
}

export default function Dashboard() {
  const [state, setState] = useState<TrackerState | null>(null);
  const [config, setConfig] = useState<Config | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<Mode>("view");
  const [draftZone, setDraftZone] = useState<Point[]>([]);
  const [showRanges, setShowRanges] = useState(true);
  const [trail, setTrail] = useState<Point[]>([]);
  const [calib, setCalib] = useState<CalibState>({ phase: "idle" });
  const [muted, setMuted] = useState(false);
  const [origin, setOrigin] = useState("https://your-app.vercel.app");

  const pending = useRef(0);
  const lastUpdated = useRef(0);
  const configRef = useRef<Config | null>(null);
  configRef.current = config;
  const calibRef = useRef<{ start: number; point: Point; samples: Record<string, number[]>; wasArmed: boolean } | null>(
    null
  );
  const audioRef = useRef<AudioContext | null>(null);

  useEffect(() => setOrigin(window.location.origin), []);

  /* ---------- config updates (optimistic) ---------- */
  const patch = useCallback(async (p: ConfigPatch) => {
    pending.current++;
    setConfig((c) => (c ? applyLocal(c, p) : c));
    try {
      const r = await fetch("/api/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(p),
      });
      if (r.ok) {
        const c: Config = await r.json();
        lastUpdated.current = c.updatedAt;
        if (pending.current === 1) setConfig(c);
      }
    } finally {
      pending.current--;
    }
  }, []);

  /* ---------- calibration ---------- */
  const finishCalibration = useCallback(
    async (cfg: Config) => {
      const cr = calibRef.current;
      if (!cr) return;
      calibRef.current = null;
      const n = cfg.pathLossN;
      const results: CalibResult[] = [];
      const anchors: Record<string, Partial<AnchorConfig>> = {};
      for (const [id, a] of Object.entries(cfg.anchors)) {
        const s = cr.samples[id];
        if (!s || s.length < 4) {
          results.push({ id, ok: false });
          continue;
        }
        const d = Math.max(0.3, Math.hypot(a.x - cr.point.x, a.y - cr.point.y));
        const rssi = median(s);
        const tx = Math.round((rssi + 10 * n * Math.log10(d)) * 10) / 10;
        anchors[id] = { txPower: tx };
        results.push({ id, ok: true, before: a.txPower, after: tx, d, rssi, n: s.length });
      }
      await patch({ anchors, armed: cr.wasArmed });
      const centralOk = !cfg.centralId || results.find((r) => r.id === cfg.centralId)?.ok;
      const okCount = results.filter((r) => r.ok).length;
      if (okCount === 0) {
        setCalib({ phase: "error", results, message: "No laptop heard the ESP during calibration. Is it powered and advertising?" });
      } else if (!centralOk) {
        setCalib({ phase: "error", results, message: "The central laptop didn't hear the ESP; other laptops were calibrated." });
      } else {
        setCalib({ phase: "done", results, message: `Calibrated ${okCount} laptop${okCount > 1 ? "s" : ""}.` });
      }
    },
    [patch]
  );

  const collectCalibSample = useCallback(
    (s: TrackerState) => {
      const cr = calibRef.current;
      if (!cr) return;
      const elapsed = Date.now() - cr.start;
      setCalib({ phase: "running", progress: Math.min(1, elapsed / CAL_MS) });
      if (elapsed > CAL_SETTLE_MS) {
        for (const a of s.anchors) {
          const v = a.raw ?? a.rssi;
          if (a.seesTag && v != null) (cr.samples[a.id] ??= []).push(v);
        }
      }
      if (elapsed >= CAL_MS) void finishCalibration(s.config);
    },
    [finishCalibration]
  );

  const startCalibration = async () => {
    const c = configRef.current;
    if (!c) return;
    const point = c.calibPoint ?? defaultCalibPoint(c);
    if (!point) {
      setCalib({
        phase: "error",
        results: [],
        message: "Start one laptop with --central first, or place the calibration spot on the map.",
      });
      return;
    }
    calibRef.current = { start: Date.now(), point, samples: {}, wasArmed: c.armed };
    setMode("view");
    setCalib({ phase: "running", progress: 0 });
    if (c.armed) await patch({ armed: false });
  };

  /* ---------- polling ---------- */
  const collectRef = useRef(collectCalibSample);
  collectRef.current = collectCalibSample;

  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try {
        const r = await fetch("/api/state", { cache: "no-store" });
        if (!r.ok) throw new Error(`Server returned ${r.status}`);
        const s: TrackerState = await r.json();
        if (stop) return;
        setState(s);
        setError(null);
        if (pending.current === 0 && s.config.updatedAt >= lastUpdated.current) {
          lastUpdated.current = s.config.updatedAt;
          setConfig(s.config);
        }
        if (s.position) {
          const p = s.position;
          setTrail((t) => [...t.slice(-(TRAIL_LEN - 1)), p]);
        }
        collectRef.current(s);
      } catch (e) {
        if (!stop) setError(e instanceof Error ? e.message : String(e));
      }
      if (!stop) timer = setTimeout(tick, POLL_MS);
    };
    tick();
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, []);

  /* ---------- alert sound + title ---------- */
  useEffect(() => {
    const unlock = () => {
      if (!audioRef.current) {
        try {
          audioRef.current = new AudioContext();
        } catch {
          /* no audio */
        }
      }
      audioRef.current?.resume().catch(() => {});
    };
    window.addEventListener("pointerdown", unlock);
    return () => window.removeEventListener("pointerdown", unlock);
  }, []);

  const alerting = !!state?.alert;
  useEffect(() => {
    if (!alerting) {
      setMuted(false);
      document.title = "ESP Zone Tracker";
      return;
    }
    let flip = false;
    const beep = () => {
      flip = !flip;
      document.title = flip ? "⚠ ESP LEFT ZONE" : "ESP Zone Tracker";
      const ctx = audioRef.current;
      if (muted || !ctx || ctx.state !== "running") return;
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.frequency.value = 880;
      g.gain.setValueAtTime(0.0001, ctx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.35);
      o.connect(g).connect(ctx.destination);
      o.start();
      o.stop(ctx.currentTime + 0.4);
    };
    beep();
    const id = setInterval(beep, 900);
    return () => clearInterval(id);
  }, [alerting, muted]);

  /* ---------- zone drawing ---------- */
  const onZoneClick = (p: Point) => {
    const z = draftZone;
    if (z.length >= 3 && Math.hypot(p.x - z[0].x, p.y - z[0].y) < 0.25) {
      void patch({ zone: z });
      setDraftZone([]);
      setMode("view");
    } else {
      setDraftZone([...z, p]);
    }
  };

  const finishZone = () => {
    if (draftZone.length >= 3) void patch({ zone: draftZone });
    setDraftZone([]);
    setMode("view");
  };

  const switchMode = (m: Mode) => {
    setDraftZone([]);
    setMode((cur) => (cur === m ? "view" : m));
  };

  if (!config) {
    return (
      <main className="loading">
        <p>{error ? `Can't reach the server: ${error}` : "Connecting…"}</p>
      </main>
    );
  }

  const status: Status = state?.status ?? "nofix";
  const calibPoint = config.calibPoint ?? defaultCalibPoint(config);
  const anchors = state?.anchors ?? [];
  const onlineCount = anchors.filter((a) => a.online).length;

  return (
    <div className={alerting ? "app alerting" : "app"}>
      {alerting && (
        <div className="alert-banner" role="alert">
          <strong>⚠ The ESP has left the safe zone</strong>
          <span>Its LED is blinking.</span>
          <button onClick={() => setMuted(true)} disabled={muted}>
            {muted ? "Muted" : "Mute sound"}
          </button>
          <button onClick={() => patch({ armed: false })}>Disarm</button>
        </div>
      )}

      <header className="topbar">
        <h1>ESP Zone Tracker</h1>
        <span className="pill" style={{ background: STATUS_COLOR[status] }}>
          {STATUS_TEXT[status]}
        </span>
        <label className="toggle">
          <input type="checkbox" checked={config.armed} onChange={(e) => patch({ armed: e.target.checked })} />
          Armed
        </label>
        {error && <span className="conn-error">Connection problem: {error}</span>}
      </header>

      <main className="layout">
        <section className="map-panel">
          <div className="toolbar">
            <button className={mode === "anchors" ? "active" : ""} onClick={() => switchMode("anchors")}>
              Move laptops
            </button>
            <button className={mode === "zone" ? "active" : ""} onClick={() => switchMode("zone")}>
              Draw safe zone
            </button>
            <button className={mode === "calib" ? "active" : ""} onClick={() => switchMode("calib")}>
              Place calibration spot
            </button>
            <label className="toggle small">
              <input type="checkbox" checked={showRanges} onChange={(e) => setShowRanges(e.target.checked)} />
              Range circles
            </label>
          </div>

          <div className="hint">
            {mode === "view" && "Pick a tool above to edit the map."}
            {mode === "anchors" && "Drag each laptop to where it really is in the room."}
            {mode === "zone" && (
              <>
                Click to add corners. Click the first corner (or Finish) to close the zone.{" "}
                <button onClick={finishZone} disabled={draftZone.length < 3}>
                  Finish
                </button>
                <button onClick={() => setDraftZone((z) => z.slice(0, -1))} disabled={!draftZone.length}>
                  Undo
                </button>
                {config.zone.length > 0 && (
                  <button
                    onClick={() => {
                      void patch({ zone: [] });
                      setDraftZone([]);
                    }}
                  >
                    Delete zone
                  </button>
                )}
              </>
            )}
            {mode === "calib" && "Click or drag to choose where you'll hold the ESP while calibrating."}
          </div>

          <div className="map-wrap">
            <RoomMap
              config={config}
              state={state}
              mode={mode}
              draftZone={draftZone}
              calibPoint={calibPoint}
              showCalib={mode === "calib" || calib.phase === "running"}
              trail={trail}
              showRanges={showRanges}
              onZoneClick={onZoneClick}
              onAnchorMove={(id, p) => patch({ anchors: { [id]: { x: p.x, y: p.y } } })}
              onCalibMove={(p) => patch({ calibPoint: p })}
            />
          </div>

          <div className="readout">
            <span>
              Position:{" "}
              <b>{state?.position ? `${state.position.x.toFixed(2)} m, ${state.position.y.toFixed(2)} m` : "—"}</b>
            </span>
            <span>
              ± <b>{state?.errorM != null ? `${state.errorM.toFixed(1)} m` : "—"}</b>
            </span>
            <span>
              Laptops hearing ESP: <b>{state?.usedAnchors ?? 0}</b> / {anchors.length}
              {(state?.usedAnchors ?? 0) < 3 && " (need 3)"}
            </span>
          </div>
        </section>

        <aside className="sidebar">
          <section className="card">
            <h2>Calibrate</h2>
            <p className="muted">
              Hold the ESP still at the crosshair (by default 1 m from the central laptop, toward the room centre),
              then press Calibrate. Each laptop's signal strength is measured for {CAL_MS / 1000} s and compared with
              its known distance to that spot.
            </p>
            {calib.phase === "running" ? (
              <div className="progress">
                <div style={{ width: `${Math.round(calib.progress * 100)}%` }} />
                <span>Measuring… keep the ESP still</span>
              </div>
            ) : (
              <div className="row">
                <button className="primary" onClick={startCalibration} disabled={onlineCount === 0}>
                  Calibrate
                </button>
                <button
                  onClick={() =>
                    patch({
                      anchors: Object.fromEntries(
                        Object.keys(config.anchors).map((id) => [id, { txPower: config.defaultTxPower }])
                      ),
                    })
                  }
                >
                  Reset
                </button>
              </div>
            )}
            {(calib.phase === "done" || calib.phase === "error") && (
              <div className={calib.phase === "error" ? "calib-msg err" : "calib-msg"}>
                <p>{calib.message}</p>
                {calib.results.length > 0 && (
                  <table>
                    <tbody>
                      {calib.results.map((r) => (
                        <tr key={r.id}>
                          <td>{r.id}</td>
                          <td>
                            {r.ok
                              ? `${r.rssi!.toFixed(0)} dBm @ ${r.d!.toFixed(1)} m → 1 m ref ${r.after} dBm`
                              : "didn't hear ESP"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            )}
          </section>

          <section className="card">
            <h2>Laptops</h2>
            {anchors.length === 0 ? (
              <p className="muted">No laptops yet. Start the agent on each laptop (see below); each appears here.</p>
            ) : (
              <table className="anchors">
                <thead>
                  <tr>
                    <th>Laptop</th>
                    <th>Central</th>
                    <th>RSSI</th>
                    <th>Dist</th>
                    <th>1 m ref</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {anchors.map((a) => (
                    <tr key={a.id}>
                      <td>
                        <span className={`dot ${a.seesTag ? "live" : a.online ? "online" : "offline"}`} />
                        {a.id}
                      </td>
                      <td>
                        <input
                          type="radio"
                          name="central"
                          checked={a.isCentral}
                          onChange={() => patch({ centralId: a.id })}
                          aria-label={`Make ${a.id} central`}
                        />
                      </td>
                      <td>{a.rssi != null ? a.rssi.toFixed(0) : "—"}</td>
                      <td>{a.distance != null ? `${a.distance.toFixed(1)} m` : "—"}</td>
                      <td>{a.txPower.toFixed(0)}</td>
                      <td>
                        {!a.online && (
                          <button
                            className="icon"
                            title="Remove laptop"
                            onClick={() => patch({ anchors: { [a.id]: null } })}
                          >
                            ×
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <p className="legend muted">
              <span className="dot live" /> hears ESP <span className="dot online" /> online, no signal{" "}
              <span className="dot offline" /> offline
            </p>
          </section>

          <section className="card">
            <h2>Settings</h2>
            <div className="grid2">
              <NumberField label="Room width (m)" value={config.room.width} onCommit={(v) => patch({ room: { width: v } })} />
              <NumberField label="Room depth (m)" value={config.room.height} onCommit={(v) => patch({ room: { height: v } })} />
              <NumberField
                label="Path-loss n"
                value={config.pathLossN}
                step={0.1}
                onCommit={(v) => patch({ pathLossN: v })}
                hint="2 open space, 2.5–3.5 indoors"
              />
              <NumberField
                label="Alert delay (s)"
                value={config.alertDelayMs / 1000}
                step={0.5}
                onCommit={(v) => patch({ alertDelayMs: v * 1000 })}
              />
            </div>
          </section>

          <section className="card">
            <h2>Connect a laptop</h2>
            <p className="muted">
              Laptops don&apos;t connect to each other; each one just talks to this website. On every laptop, install
              Python and run:
            </p>
            <pre>
              {`pip install bleak\npython anchor.py --server ${origin} --id laptop-A --key YOUR_KEY`}
            </pre>
            <p className="muted">
              Use a different <code>--id</code> per laptop and add <code>--central</code> on the one that should
              control the ESP&apos;s LED. Then drag each laptop to its real spot with <em>Move laptops</em>.
            </p>
          </section>
        </aside>
      </main>
    </div>
  );
}

function NumberField(props: {
  label: string;
  value: number;
  step?: number;
  hint?: string;
  onCommit: (v: number) => void;
}) {
  const [text, setText] = useState(String(props.value));
  useEffect(() => setText(String(props.value)), [props.value]);
  const commit = () => {
    const v = parseFloat(text);
    if (Number.isFinite(v) && v !== props.value) props.onCommit(v);
    else setText(String(props.value));
  };
  return (
    <label className="field">
      <span>{props.label}</span>
      <input
        type="number"
        step={props.step ?? 0.5}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
      />
      {props.hint && <small className="muted">{props.hint}</small>}
    </label>
  );
}
