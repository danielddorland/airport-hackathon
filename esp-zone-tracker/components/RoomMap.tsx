"use client";

import { useRef, useState } from "react";
import type { Config, Point, Status, TrackerState } from "@/lib/types";

export type Mode = "view" | "anchors" | "zone" | "calib";

type Props = {
  config: Config;
  state: TrackerState | null;
  mode: Mode;
  draftZone: Point[];
  calibPoint: Point | null;
  showCalib: boolean;
  trail: Point[];
  showRanges: boolean;
  onZoneClick: (p: Point) => void;
  onAnchorMove: (id: string, p: Point) => void;
  onCalibMove: (p: Point) => void;
};

type Drag = { kind: "anchor"; id: string } | { kind: "calib" } | null;

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

export const STATUS_COLOR: Record<Status, string> = {
  safe: "var(--safe)",
  leaving: "var(--warn)",
  alert: "var(--danger)",
  nofix: "var(--muted)",
  nozone: "var(--accent)",
  disarmed: "var(--accent)",
};

export default function RoomMap(props: Props) {
  const { config, state, mode, draftZone, calibPoint, trail, showRanges } = props;
  const svgRef = useRef<SVGSVGElement>(null);
  const [drag, setDrag] = useState<Drag>(null);
  const [dragPos, setDragPos] = useState<Point | null>(null);

  const W = config.room.width;
  const H = config.room.height;
  const pad = 0.6;
  const unit = Math.max(W, H) / 30; // base size for glyphs/text, in metres

  const toRoom = (e: { clientX: number; clientY: number }): Point => {
    const svg = svgRef.current;
    const m = svg?.getScreenCTM();
    if (!svg || !m) return { x: 0, y: 0 };
    const pt = svg.createSVGPoint();
    pt.x = e.clientX;
    pt.y = e.clientY;
    const p = pt.matrixTransform(m.inverse());
    // snap to 5 cm and keep inside the room
    return {
      x: Math.round(clamp(p.x, 0, W) * 20) / 20,
      y: Math.round(clamp(p.y, 0, H) * 20) / 20,
    };
  };

  const startDrag = (d: Drag) => (e: React.PointerEvent) => {
    e.stopPropagation();
    svgRef.current?.setPointerCapture(e.pointerId);
    setDrag(d);
    setDragPos(toRoom(e));
  };

  const onMove = (e: React.PointerEvent) => {
    if (drag) setDragPos(toRoom(e));
  };

  const onUp = () => {
    if (drag && dragPos) {
      if (drag.kind === "anchor") props.onAnchorMove(drag.id, dragPos);
      else props.onCalibMove(dragPos);
    }
    setDrag(null);
    setDragPos(null);
  };

  const onClick = (e: React.MouseEvent) => {
    if (drag) return;
    if (mode === "zone") props.onZoneClick(toRoom(e));
    else if (mode === "calib") props.onCalibMove(toRoom(e));
  };

  const anchorStatus = new Map((state?.anchors ?? []).map((a) => [a.id, a]));
  const anchors = Object.entries(config.anchors).map(([id, a]) => {
    const p = drag?.kind === "anchor" && drag.id === id && dragPos ? dragPos : a;
    return { id, x: p.x, y: p.y, s: anchorStatus.get(id) };
  });

  const calib = drag?.kind === "calib" && dragPos ? dragPos : calibPoint;
  const status = state?.status ?? "nofix";
  const color = STATUS_COLOR[status];
  const showDraft = mode === "zone" && draftZone.length > 0;
  const zone = config.zone;

  const gridX = Array.from({ length: Math.floor(W) + 1 }, (_, i) => i);
  const gridY = Array.from({ length: Math.floor(H) + 1 }, (_, i) => i);

  return (
    <svg
      ref={svgRef}
      className={`room-map mode-${mode}`}
      viewBox={`${-pad} ${-pad} ${W + 2 * pad} ${H + 2 * pad}`}
      onPointerMove={onMove}
      onPointerUp={onUp}
      onPointerCancel={onUp}
      onClick={onClick}
      role="img"
      aria-label="Room map"
    >
      {/* room + 1 m grid */}
      <rect x={0} y={0} width={W} height={H} className="room-floor" />
      {gridX.map((x) => (
        <g key={`gx${x}`}>
          <line x1={x} y1={0} x2={x} y2={H} className="grid" />
          <text x={x} y={-unit * 0.6} className="axis" fontSize={unit * 0.7} textAnchor="middle">
            {x}m
          </text>
        </g>
      ))}
      {gridY.map((y) => (
        <g key={`gy${y}`}>
          <line x1={0} y1={y} x2={W} y2={y} className="grid" />
          <text x={-unit * 0.5} y={y + unit * 0.25} className="axis" fontSize={unit * 0.7} textAnchor="end">
            {y}
          </text>
        </g>
      ))}
      <rect x={0} y={0} width={W} height={H} className="room-wall" />

      {/* safe zone */}
      {zone.length >= 3 && !showDraft && (
        <polygon
          points={zone.map((p) => `${p.x},${p.y}`).join(" ")}
          className={`zone ${state?.alert ? "zone-alert" : ""}`}
        />
      )}
      {showDraft && (
        <g>
          <polyline points={draftZone.map((p) => `${p.x},${p.y}`).join(" ")} className="zone-draft" />
          {draftZone.map((p, i) => (
            <circle key={i} cx={p.x} cy={p.y} r={i === 0 ? unit * 0.45 : unit * 0.25} className="zone-vertex" />
          ))}
        </g>
      )}

      {/* estimated range circles */}
      {showRanges &&
        anchors.map((a) =>
          a.s?.distance != null ? (
            <circle key={`r${a.id}`} cx={a.x} cy={a.y} r={a.s.distance} className="range" />
          ) : null
        )}

      {/* trail */}
      {trail.length > 1 && <polyline points={trail.map((p) => `${p.x},${p.y}`).join(" ")} className="trail" />}

      {/* calibration spot */}
      {props.showCalib && calib && (
        <g className="calib" onPointerDown={mode === "calib" ? startDrag({ kind: "calib" }) : undefined}>
          <circle cx={calib.x} cy={calib.y} r={unit * 0.9} className="calib-ring" />
          <line x1={calib.x - unit * 1.3} y1={calib.y} x2={calib.x + unit * 1.3} y2={calib.y} className="calib-cross" />
          <line x1={calib.x} y1={calib.y - unit * 1.3} x2={calib.x} y2={calib.y + unit * 1.3} className="calib-cross" />
          <text x={calib.x} y={calib.y - unit * 1.6} fontSize={unit * 0.7} textAnchor="middle" className="label">
            put ESP here
          </text>
        </g>
      )}

      {/* laptops */}
      {anchors.map((a) => {
        const s = a.s;
        const cls = !s?.online ? "anchor offline" : s.seesTag ? "anchor live" : "anchor online";
        const size = unit * 1.1;
        return (
          <g
            key={a.id}
            className={cls}
            onPointerDown={mode === "anchors" ? startDrag({ kind: "anchor", id: a.id }) : undefined}
          >
            {s?.isCentral && <circle cx={a.x} cy={a.y} r={size * 0.95} className="central-ring" />}
            <rect x={a.x - size / 2} y={a.y - size / 2} width={size} height={size} rx={size * 0.18} />
            <text x={a.x} y={a.y + size * 1.25} fontSize={unit * 0.7} textAnchor="middle" className="label">
              {a.id.length > 16 ? a.id.slice(0, 15) + "…" : a.id}
            </text>
            {s?.rssi != null && (
              <text x={a.x} y={a.y + size * 1.25 + unit * 0.85} fontSize={unit * 0.6} textAnchor="middle" className="sublabel">
                {s.rssi.toFixed(0)} dBm · {s.distance!.toFixed(1)} m
              </text>
            )}
          </g>
        );
      })}

      {/* the ESP */}
      {state?.rawPosition && (
        <circle cx={state.rawPosition.x} cy={state.rawPosition.y} r={unit * 0.18} className="raw-pos" />
      )}
      {state?.position && (
        <g className={state.alert ? "esp esp-alert" : "esp"}>
          {state.errorM != null && (
            <circle cx={state.position.x} cy={state.position.y} r={state.errorM} fill={color} className="esp-err" />
          )}
          <circle cx={state.position.x} cy={state.position.y} r={unit * 0.42} fill={color} className="esp-dot" />
        </g>
      )}
    </svg>
  );
}
