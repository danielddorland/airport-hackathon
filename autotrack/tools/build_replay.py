"""
build_replay.py - run the organisers' baggage simulation once and save what happened.

The challenge repo's simulation (bhs/evaluator.py + bhs/solution.py, copied unmodified)
moves 718 bags through a 28-point conveyor network for one hour (3,600 ticks, 1 tick = 1 s).
We don't change it. We just run it and record, for every bag, which conveyor point it
reached at which second, plus when each belt was jammed.

AutoTrack then *replays* that ground truth and pretends to be the sensors (BLE receivers,
RFID portals) watching those bags. Ground truth lets us measure how accurate AutoTrack is.

Run:  python tools/build_replay.py      (run.py does this for you if data/replay.json is missing)

Important: the evaluator's results change between runs because of Python's random string
hashing. We force PYTHONHASHSEED=0 so every run gives the same answer (189 late bags).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BHS = ROOT / "bhs"
OUT = ROOT / "data" / "replay.json"


def _inner():
    """Runs inside a child Python process that has PYTHONHASHSEED=0."""
    sys.path.insert(0, str(BHS))
    import evaluator  # noqa: E402  (the organisers' file, unmodified)
    import solution   # noqa: E402

    ev = evaluator.BaggageEvaluator(BHS / "data" / "network_layout.json",
                                    BHS / "data" / "simulation_scenario.json")
    ev.run(solution)

    bags = []
    for bag_id, b in sorted(ev.completed_bags.items()):
        bags.append({
            "bag_id": bag_id,
            "intake": b["history"][0][0],
            "dest": b["destination_gate"],
            "release": b["release_tick"],
            "deadline": b["deadline_tick"],
            "arrival": b["arrival_tick"],
            "history": [[n, t] for n, t in b["history"]],
        })

    # Turn the per-tick telemetry into jam intervals per directed belt.
    edge_ft = {eid: (e["from"], e["to"]) for eid, e in ev.edge_states.items()}
    open_jams, jams = {}, []
    for row in ev.dhs_log_data:
        eid, tick, jammed = row["edge_id"], row["tick"], row["status"] == "JAMMED"
        if jammed and eid not in open_jams:
            open_jams[eid] = (tick, row["fault_reason"])
        elif not jammed and eid in open_jams:
            start, reason = open_jams.pop(eid)
            f, t = edge_ft[eid]
            jams.append({"edge": eid, "from": f, "to": t, "start": start, "end": tick, "reason": reason})
    for eid, (start, reason) in open_jams.items():
        f, t = edge_ft[eid]
        jams.append({"edge": eid, "from": f, "to": t, "start": start, "end": ev.total_ticks + 1, "reason": reason})
    jams.sort(key=lambda j: (j["start"], j["edge"]))

    late = sum(1 for b in bags if b["deadline"] and b["arrival"] > b["deadline"])
    out = {
        "source": "IdeasClinicUWaterloo/F26-Airport-Innovation-Challenge bhs-simulation (evaluator.py + baseline solution.py, unmodified), PYTHONHASHSEED=0",
        "total_ticks": ev.total_ticks,
        "stranded": len(ev.active_bags),
        "late_bags": late,
        "bags": bags,
        "jams": jams,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out), encoding="utf-8")
    print(f"Saved {len(bags)} bags, {len(jams)} jam intervals, {late} late bags -> {OUT}")


def build():
    env = dict(os.environ, PYTHONHASHSEED="0")
    # cwd=BHS so the evaluator writes its live_telemetry.csv inside bhs/, not elsewhere.
    subprocess.run([sys.executable, __file__, "--inner"], env=env, cwd=BHS, check=True)


if __name__ == "__main__":
    if "--inner" in sys.argv:
        _inner()
    else:
        build()
