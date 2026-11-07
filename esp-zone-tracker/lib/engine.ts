import { pointInPolygon, rssiToDistance, solvePosition } from "./positioning";
import type { Store } from "./store";
import type { AnchorStatus, Config, Point, Runtime, Status, TrackerState } from "./types";

/** A laptop counts as online if it reported within this window. */
export const FRESH_MS = 5000;

export function defaultConfig(): Config {
  return {
    room: { width: 6, height: 5 },
    pathLossN: 2.5,
    defaultTxPower: -59,
    anchors: {},
    centralId: null,
    zone: [],
    alertDelayMs: 1500,
    smoothingS: 4,
    armed: true,
    calibPoint: null,
    updatedAt: 0,
  };
}

export function normalizeConfig(c: Partial<Config> | null): Config {
  const d = defaultConfig();
  if (!c) return d;
  return {
    ...d,
    ...c,
    room: { ...d.room, ...(c.room ?? {}) },
    anchors: { ...(c.anchors ?? {}) },
    zone: Array.isArray(c.zone) ? c.zone : [],
  };
}

/** Suggested spot for a newly seen laptop: the next free corner of the room. */
function nextFreeSpot(config: Config): Point {
  const { width: w, height: h } = config.room;
  const inset = 0.3;
  const spots = [
    { x: inset, y: inset },
    { x: w - inset, y: inset },
    { x: w - inset, y: h - inset },
    { x: inset, y: h - inset },
    { x: w / 2, y: inset },
    { x: w / 2, y: h - inset },
  ];
  const taken = Object.values(config.anchors);
  return (
    spots.find((s) => !taken.some((a) => Math.hypot(a.x - s.x, a.y - s.y) < 0.5)) ?? { x: w / 2, y: h / 2 }
  );
}

/** Default calibration spot: 1 m from the central laptop, toward the room centre. */
export function defaultCalibPoint(config: Config): Point | null {
  const c = config.centralId ? config.anchors[config.centralId] : null;
  if (!c) return null;
  const cx = config.room.width / 2;
  const cy = config.room.height / 2;
  const dx = cx - c.x;
  const dy = cy - c.y;
  const len = Math.hypot(dx, dy) || 1;
  return { x: c.x + dx / len, y: c.y + dy / len };
}

export async function computeState(
  store: Store,
  register?: { id: string; central: boolean }
): Promise<TrackerState> {
  const snap = await store.loadAll();
  let config = normalizeConfig(snap.config);
  const now = Date.now();

  // Auto-register laptops the first time they report in.
  if (register) {
    let changed = false;
    if (!config.anchors[register.id]) {
      const spot = nextFreeSpot(config);
      config.anchors[register.id] = { ...spot, txPower: config.defaultTxPower };
      changed = true;
    }
    if (register.central && config.centralId !== register.id) {
      config.centralId = register.id;
      changed = true;
    }
    if (changed) {
      config = { ...config, updatedAt: now };
      await store.setConfig(config);
    }
  }

  const n = config.pathLossN;
  const anchors: AnchorStatus[] = Object.entries(config.anchors).map(([id, a]) => {
    const r = snap.readings[id];
    const ageMs = r ? now - r.t : null;
    const online = ageMs != null && ageMs < FRESH_MS;
    const seesTag = online && r!.rssi != null;
    return {
      id,
      x: a.x,
      y: a.y,
      txPower: a.txPower,
      isCentral: config.centralId === id,
      online,
      seesTag,
      rssi: seesTag ? r!.rssi : null,
      raw: online ? r!.raw : null,
      samples: online ? r!.samples : 0,
      distance: seesTag ? rssiToDistance(r!.rssi!, a.txPower, n) : null,
      ageMs,
    };
  });

  const usable = anchors.filter((a) => a.seesTag);
  const sol = solvePosition(
    usable.map((a) => ({ x: a.x, y: a.y, rssi: a.rssi!, txPower: a.txPower })),
    config.room,
    n
  );
  const rawPosition = sol?.p ?? null;

  // Time-based exponential smoothing, so it behaves the same however often we're polled.
  // Small wobbles (inside the uncertainty radius) are damped extra hard; real moves
  // (well outside it) are followed at the normal rate.
  const prev = snap.runtime;
  let position: Point | null = null;
  if (rawPosition) {
    const tau = Math.max(0, config.smoothingS);
    if (tau > 0 && prev?.pos && now - prev.posT < FRESH_MS) {
      const dt = Math.max(0, now - prev.posT) / 1000;
      const jump = Math.hypot(rawPosition.x - prev.pos.x, rawPosition.y - prev.pos.y);
      const err = sol?.errorM ?? 1;
      const damp = Math.min(1, Math.max(0.25, jump / err));
      const k = (1 - Math.exp(-dt / tau)) * damp;
      position = {
        x: prev.pos.x + k * (rawPosition.x - prev.pos.x),
        y: prev.pos.y + k * (rawPosition.y - prev.pos.y),
      };
    } else {
      position = rawPosition;
    }
  }

  const hasZone = config.zone.length >= 3;
  const inZone = position && hasZone ? pointInPolygon(position, config.zone) : null;

  let outsideSince = prev?.outsideSince ?? null;
  if (inZone === true) outsideSince = null;
  else if (inZone === false && outsideSince == null) outsideSince = now;

  let alert = false;
  if (config.armed && hasZone) {
    if (inZone === false) alert = now - (outsideSince ?? now) >= config.alertDelayMs;
    // Lost track of the tag while alerting: keep alerting (it most likely left).
    else if (inZone === null && prev?.alert) alert = true;
  }

  let status: Status;
  if (!config.armed) status = "disarmed";
  else if (!hasZone) status = "nozone";
  else if (alert) status = "alert";
  else if (!position) status = "nofix";
  else if (inZone === false) status = "leaving";
  else status = "safe";

  const runtime: Runtime = {
    outsideSince,
    alert,
    pos: position ?? (prev && now - prev.posT < FRESH_MS ? prev.pos : null),
    posT: position ? now : prev?.posT ?? 0,
  };
  await store.setRuntime(runtime);

  return {
    now,
    config,
    anchors,
    position,
    rawPosition,
    errorM: sol?.errorM ?? null,
    usedAnchors: usable.length,
    inZone,
    alert,
    status,
    outsideSince,
    storage: store.kind,
  };
}
