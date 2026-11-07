"""
layout.py - the map: zones, receivers, belts and flights.

We reuse the organisers' 28-point conveyor network (bhs/data/network_layout.json) as a
SIMPLIFIED baggage hall. It is not YYZ's real layout. Each conveyor point is one AutoTrack
zone with one BLE receiver in it:

    IA1, IA2  -> Check-in (bag drop)          BLE tag is clipped on here
    IB1, IB2  -> Transfer input               transfer bags get their BLE tag here
    JA*, JB*  -> Sortation                    RFID read tunnel at every junction
    S1-S5     -> Transport belts
    S6        -> Early bag storage
    GA*, GB*  -> Make-up lane + belt loader   RFID portal + camera at the loader

Positions come from the organisers' dashboard (bsd_dashboard.py NODE_COORDINATES), in
pixels. We treat 1 pixel = 0.1 m, so the hall is about 88 m x 66 m and neighbouring
receivers are 12-18 m apart.
"""

import heapq
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
M_PER_PX = 0.1

# Copied from bhs-simulation/bsd_dashboard.py (NODE_COORDINATES).
NODE_XY = {
    "IA1": (80, 160), "IA2": (80, 280),
    "JA1": (240, 160), "JA2": (420, 160), "JA3": (600, 160), "JA4": (780, 160), "JA5": (510, 60),
    "GA1": (240, 40), "GA2": (420, 40), "GA3": (600, 40), "GA4": (780, 40),
    "S1": (240, 400), "S2": (420, 400), "S3": (600, 400),
    "S4": (780, 400), "S5": (960, 400), "S6": (600, 520),
    "IB1": (80, 640), "IB2": (80, 520),
    "JB1": (240, 640), "JB2": (420, 640), "JB3": (600, 640), "JB4": (780, 640), "JB5": (510, 700),
    "GB1": (960, 160), "GB2": (960, 280), "GB3": (960, 520), "GB4": (960, 640),
}

# Made-up demo flights: which flight each belt loader is serving this hour.
# (Airline codes AC / WS match the organisers' gate-assignment data.)
FLIGHTS = {
    "GA1": {"flight": "AC 412", "to": "Montreal (YUL)", "closes": 3540},
    "GA2": {"flight": "AC 1234", "to": "Vancouver (YVR)", "closes": 3540},
    "GA3": {"flight": "WS 653", "to": "Calgary (YYC)", "closes": 3560},
    "GA4": {"flight": "AC 857", "to": "London (LHR)", "closes": 3570},
    "GB1": {"flight": "AC 870", "to": "Paris (CDG)", "closes": 3560},
    "GB2": {"flight": "WS 1122", "to": "Halifax (YHZ)", "closes": 3540},
    "GB3": {"flight": "AC 033", "to": "Sydney (SYD)", "closes": 3580},
    "GB4": {"flight": "AC 848", "to": "Frankfurt (FRA)", "closes": 3570},
}
AIRLINE_NUMERIC = {"AC": "014", "WS": "838"}  # IATA 3-digit codes used in bag tag numbers


def zone_kind(node):
    if node.startswith("IA"):
        return "checkin"
    if node.startswith("IB"):
        return "transfer"
    if node.startswith("J"):
        return "sortation"
    if node == "S6":
        return "storage"
    if node.startswith("S"):
        return "transport"
    return "makeup"


KIND_LABEL = {
    "checkin": "Check-in",
    "transfer": "Transfer input",
    "sortation": "Sortation",
    "transport": "Transport belt",
    "storage": "Early bag storage",
    "makeup": "Make-up & loader",
}


def zone_name(node):
    if node is None:
        return "unknown"
    return f"{KIND_LABEL[zone_kind(node)]} {node}"


def has_rfid(node):
    """Where exact RFID reads happen: check-in/transfer acceptance, sortation tunnels.
    (Belt loaders also have a portal, but that read is done separately when the bag is loaded.)"""
    return zone_kind(node) in ("checkin", "transfer", "sortation")


class Layout:
    def __init__(self):
        data = json.loads((ROOT / "bhs" / "data" / "network_layout.json").read_text())
        self.nodes = [n["id"] for n in data["nodes"]]
        self.labels = {n["id"]: n["label"] for n in data["nodes"]}
        # Directed belts (bidirectional ones become two belts, like the evaluator does).
        self.adj = {n: {} for n in self.nodes}
        for e in data["edges"]:
            self.adj[e["from"]][e["to"]] = e["base_cost"]
            if e.get("bidirectional"):
                self.adj[e["to"]][e["from"]] = e["base_cost"]
        self.xy_m = {n: (x * M_PER_PX, y * M_PER_PX) for n, (x, y) in NODE_XY.items()}
        self._dist_cache = {}

    def receivers(self):
        """One BLE receiver per zone, at the zone's position (metres)."""
        return {f"R-{n}": {"zone": n, "x": self.xy_m[n][0], "y": self.xy_m[n][1]} for n in self.nodes}

    def costs_from(self, src, blocked=None):
        """Shortest travel time (ticks, using belt base costs) from src to every node.
        blocked: {(from, to): extra_ticks} for belts we know are jammed right now."""
        key = (src, tuple(sorted((blocked or {}).items())))
        if key in self._dist_cache:
            return self._dist_cache[key]
        dist = {src: 0}
        pq = [(0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, 1e18):
                continue
            for v, c in self.adj[u].items():
                nd = d + c + (blocked or {}).get((u, v), 0)
                if nd < dist.get(v, 1e18):
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        if len(self._dist_cache) > 5000:
            self._dist_cache.clear()
        self._dist_cache[key] = dist
        return dist

    def shortest_path(self, src, dst):
        prev, dist = {}, {src: 0}
        pq = [(0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if u == dst:
                break
            if d > dist.get(u, 1e18):
                continue
            for v, c in self.adj[u].items():
                if d + c < dist.get(v, 1e18):
                    dist[v], prev[v] = d + c, u
                    heapq.heappush(pq, (d + c, v))
        path = [dst]
        while path[-1] != src:
            path.append(prev[path[-1]])
        return path[::-1]

    def to_json(self):
        """What the web page needs to draw the map."""
        belts = sorted({(u, v) for u in self.adj for v in self.adj[u]})
        return {
            "nodes": [{"id": n, "x": NODE_XY[n][0], "y": NODE_XY[n][1], "kind": zone_kind(n),
                       "name": zone_name(n), "rfid": has_rfid(n) or zone_kind(n) == "makeup",
                       "flight": FLIGHTS.get(n)} for n in self.nodes],
            "belts": [{"from": u, "to": v} for u, v in belts],
            "m_per_px": M_PER_PX,
        }
