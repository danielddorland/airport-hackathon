import { NextResponse } from "next/server";
import { computeState } from "@/lib/engine";
import { getStore } from "@/lib/store";

export const dynamic = "force-dynamic";

const ID_RE = /^[\w.\- ]{1,40}$/;

function num(v: unknown, lo: number, hi: number): number | null {
  return typeof v === "number" && Number.isFinite(v) && v >= lo && v <= hi ? v : null;
}

/** Laptop agents POST here about once a second. */
export async function POST(req: Request) {
  const key = process.env.AGENT_KEY;
  if (key && req.headers.get("x-agent-key") !== key) {
    return NextResponse.json({ error: "bad agent key" }, { status: 401 });
  }

  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid json" }, { status: 400 });
  }

  const id = typeof body.id === "string" ? body.id.trim() : "";
  if (!ID_RE.test(id)) {
    return NextResponse.json({ error: "id must be 1-40 chars of letters, digits, space, . _ -" }, { status: 400 });
  }
  const central = body.central === true;

  const store = getStore();
  await store.putReading({
    id,
    rssi: num(body.rssi, -127, 20),
    raw: num(body.raw, -127, 20),
    samples: num(body.samples, 0, 10000) ?? 0,
    central,
    t: Date.now(),
  });

  const s = await computeState(store, { id, central });
  return NextResponse.json(
    { alert: s.alert, status: s.status, position: s.position, usedAnchors: s.usedAnchors },
    { headers: { "Cache-Control": "no-store" } }
  );
}
