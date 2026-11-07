export type Point = { x: number; y: number };

export type AnchorConfig = {
  x: number; // metres from the room's left wall
  y: number; // metres from the room's top wall
  txPower: number; // expected RSSI (dBm) at 1 m from this laptop, set by calibration
};

export type Config = {
  room: { width: number; height: number };
  pathLossN: number; // path-loss exponent (2 = free space, 2.5-3.5 indoors)
  defaultTxPower: number;
  anchors: Record<string, AnchorConfig>;
  centralId: string | null;
  zone: Point[]; // safe-zone polygon, metres
  alertDelayMs: number; // must be outside this long before alerting
  smoothingS: number; // position smoothing time constant (seconds); higher = steadier but slower
  armed: boolean;
  calibPoint: Point | null; // where the ESP sits during calibration
  updatedAt: number;
};

export type Reading = {
  id: string;
  rssi: number | null; // smoothed RSSI, null if the tag isn't currently heard
  raw: number | null; // median of the last window, for display
  samples: number;
  central: boolean;
  t: number; // server receive time
};

export type Runtime = {
  outsideSince: number | null;
  alert: boolean;
  pos: Point | null;
  posT: number;
};

export type AnchorStatus = {
  id: string;
  x: number;
  y: number;
  txPower: number;
  isCentral: boolean;
  online: boolean;
  seesTag: boolean;
  rssi: number | null;
  raw: number | null;
  samples: number;
  distance: number | null;
  ageMs: number | null;
};

export type Status = "safe" | "leaving" | "alert" | "nofix" | "nozone" | "disarmed";

export type TrackerState = {
  now: number;
  config: Config;
  anchors: AnchorStatus[];
  position: Point | null; // smoothed
  rawPosition: Point | null;
  errorM: number | null; // rough 1-sigma radius
  usedAnchors: number;
  inZone: boolean | null;
  alert: boolean;
  status: Status;
  outsideSince: number | null;
};
