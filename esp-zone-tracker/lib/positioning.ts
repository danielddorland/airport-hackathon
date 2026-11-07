import type { Point } from "./types";

const MIN_D = 0.25;

/** Log-distance path-loss model: RSSI -> metres. */
export function rssiToDistance(rssi: number, txPower: number, n: number): number {
  return Math.pow(10, (txPower - rssi) / (10 * n));
}

export function predictRssi(d: number, txPower: number, n: number): number {
  return txPower - 10 * n * Math.log10(Math.max(d, MIN_D));
}

export type Observation = { x: number; y: number; rssi: number; txPower: number };

export type Solution = { p: Point; errorM: number; rmsDb: number };

/**
 * Finds the point whose *predicted* RSSI at every anchor best matches what was
 * measured (least squares in the dB domain, where BLE noise is roughly
 * Gaussian). A brute-force grid search is used because it never gets stuck in
 * local minima and a room is small enough to make it cheap.
 */
export function solvePosition(
  obs: Observation[],
  room: { width: number; height: number },
  n: number,
  sigmaDb = 3
): Solution | null {
  if (obs.length < 3) return null;
  const margin = 0.5;

  const cost = (x: number, y: number) => {
    let c = 0;
    for (const o of obs) {
      const d = Math.hypot(x - o.x, y - o.y);
      const e = predictRssi(d, o.txPower, n) - o.rssi;
      c += e * e;
    }
    return c;
  };

  // Coarse pass (10 cm grid), remembering costs to estimate uncertainty.
  const step = 0.1;
  const costs: number[] = [];
  let best = Infinity;
  let bx = 0;
  let by = 0;
  for (let x = -margin; x <= room.width + margin + 1e-9; x += step) {
    for (let y = -margin; y <= room.height + margin + 1e-9; y += step) {
      const c = cost(x, y);
      costs.push(c);
      if (c < best) {
        best = c;
        bx = x;
        by = y;
      }
    }
  }

  // Fine pass (2 cm) around the coarse minimum.
  const fine = 0.02;
  let fx = bx;
  let fy = by;
  for (let x = bx - step; x <= bx + step + 1e-9; x += fine) {
    for (let y = by - step; y <= by + step + 1e-9; y += fine) {
      const c = cost(x, y);
      if (c < best) {
        best = c;
        fx = x;
        fy = y;
      }
    }
  }

  // ~68% confidence region for 2 parameters: delta chi^2 < 2.3.
  const threshold = best + 2.3 * sigmaDb * sigmaDb;
  let cells = 0;
  for (const c of costs) if (c <= threshold) cells++;
  const errorM = Math.max(0.1, Math.sqrt((cells * step * step) / Math.PI));

  return {
    p: { x: fx, y: fy },
    errorM,
    rmsDb: Math.sqrt(best / obs.length),
  };
}

/** Ray-casting point-in-polygon test. */
export function pointInPolygon(p: Point, poly: Point[]): boolean {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const a = poly[i];
    const b = poly[j];
    if (a.y > p.y !== b.y > p.y && p.x < ((b.x - a.x) * (p.y - a.y)) / (b.y - a.y) + a.x) {
      inside = !inside;
    }
  }
  return inside;
}

export function median(values: number[]): number {
  const s = [...values].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}
