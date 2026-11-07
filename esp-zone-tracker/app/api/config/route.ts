import { NextResponse } from "next/server";
import { normalizeConfig } from "@/lib/engine";
import { getStore } from "@/lib/store";
import type { Config, Point } from "@/lib/types";

export const dynamic = "force-dynamic";

export async function GET() {
  const config = normalizeConfig(await getStore().getConfig());
  return NextResponse.json(config, { headers: { "Cache-Control": "no-store" } });
}

const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
const isPoint = (p: unknown): p is Point =>
  !!p && typeof p === "object" && finite((p as Point).x) && finite((p as Point).y);

type AnchorPatch = Partial<{ x: number; y: number; txPower: number }> | null;

/**
 * Partial update. Top-level fields replace; `anchors` is merged per id
 * (send `null` for an id to remove that laptop).
 */
export async function PUT(req: Request) {
  let patch: Record<string, unknown>;
  try {
    patch = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid json" }, { status: 400 });
  }

  const store = getStore();
  const c: Config = normalizeConfig(await store.getConfig());

  if (patch.room && typeof patch.room === "object") {
    const r = patch.room as Record<string, unknown>;
    if (finite(r.width)) c.room.width = clamp(r.width, 1, 50);
    if (finite(r.height)) c.room.height = clamp(r.height, 1, 50);
  }
  if (finite(patch.pathLossN)) c.pathLossN = clamp(patch.pathLossN, 1.5, 5);
  if (finite(patch.defaultTxPower)) c.defaultTxPower = clamp(patch.defaultTxPower, -100, 0);
  if (finite(patch.alertDelayMs)) c.alertDelayMs = clamp(patch.alertDelayMs, 0, 60000);
  if (typeof patch.armed === "boolean") c.armed = patch.armed;
  if (patch.centralId === null || typeof patch.centralId === "string") c.centralId = patch.centralId as string | null;
  if (patch.calibPoint === null || isPoint(patch.calibPoint)) c.calibPoint = patch.calibPoint as Point | null;
  if (Array.isArray(patch.zone)) {
    c.zone = patch.zone.filter(isPoint).slice(0, 64).map((p) => ({ x: p.x, y: p.y }));
  }

  if (patch.anchors && typeof patch.anchors === "object") {
    for (const [id, a] of Object.entries(patch.anchors as Record<string, AnchorPatch>)) {
      if (a === null) {
        delete c.anchors[id];
        if (c.centralId === id) c.centralId = null;
        await store.deleteReading(id);
        continue;
      }
      const cur = c.anchors[id] ?? { x: 0, y: 0, txPower: c.defaultTxPower };
      c.anchors[id] = {
        x: finite(a.x) ? a.x : cur.x,
        y: finite(a.y) ? a.y : cur.y,
        txPower: finite(a.txPower) ? clamp(a.txPower, -100, 0) : cur.txPower,
      };
    }
  }

  c.updatedAt = Date.now();
  await store.setConfig(c);
  return NextResponse.json(c, { headers: { "Cache-Control": "no-store" } });
}
