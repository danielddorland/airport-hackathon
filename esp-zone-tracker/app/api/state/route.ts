import { NextResponse } from "next/server";
import { computeState } from "@/lib/engine";
import { getStore } from "@/lib/store";

export const dynamic = "force-dynamic";

/** Polled by the dashboard: full tracker state including config. */
export async function GET() {
  const state = await computeState(getStore());
  return NextResponse.json(state, { headers: { "Cache-Control": "no-store" } });
}
