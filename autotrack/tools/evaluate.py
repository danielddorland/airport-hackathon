"""
evaluate.py - "Results in simulation" for the pitch.

Runs the whole simulated hour (718 bags) with no web page, several times, and measures how
well AutoTrack did. Because the simulator knows where every bag REALLY is (world.py), we can
check AutoTrack's answers against the truth.

Run it from the autotrack folder:
    .venv\\Scripts\\python tools/evaluate.py              (radio seeds 1, 2, 3 -> about 1.5-2 minutes)
    .venv\\Scripts\\python tools/evaluate.py --seeds 1    (one seed only, quicker)

It writes:
    results/metrics.json   every number, for the slides / other scripts
    results/metrics.md     a short plain-English table you can read out

What we measure (all IN SIMULATION, on a simplified 28-zone hall, not the real YYZ layout):
  a. Zone accuracy with smoothing ON vs OFF, and how much the zone "flickers".
  b. LATE early warning: did truly late bags get a LATE status before their cut-off?
  c. The 7 demo scenarios: did the expected alert fire, and when?
  d. False alerts on normal bags (bags with no scenario).
  e. Robustness: repeat a, b and d with different radio-noise seeds, so we don't show a lucky one.
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autotrack.sim import Simulation  # noqa: E402
from autotrack.tracker import PRIORITY, clock  # noqa: E402

RESULTS = ROOT / "results"
BOUNCE_S = 10   # a zone switch that goes straight back to the zone it just left within this many seconds = a "bounce"
ALERT_STATUSES = [s for s in PRIORITY if s != "ON TRACK"]


# ---------------------------------------------------------------------- small helpers

def pct(part, whole):
    """Percentage rounded to 1 decimal (0 if there is nothing to divide by)."""
    return round(100.0 * part / whole, 1) if whole else 0.0


def is_normal(truth):
    """A 'normal' bag is one that is not part of any demo scenario."""
    return truth.scenario is None


def count_true_zone_changes(world):
    """How many times does the TRUE nearest zone change, summed over all normal bags?
    After a bag reaches its make-up lane its zone stays the same, so we only need to look
    from check-in (release) to arrival in the lane."""
    changes = bounces = 0
    for b in world.bags:
        if not is_normal(b):
            continue
        seq = []  # list of (zone, time it started)
        for t in range(b.release, b.arrival + 1):
            z = b.true_zone(t)
            if z is not None and (not seq or seq[-1][0] != z):
                seq.append((z, t))
        changes += max(0, len(seq) - 1)
        bounces += count_bounces(seq)
    return changes, bounces


def count_bounces(seq):
    """seq = [(zone, start time), ...]. A bounce is A -> B -> A where B lasted less than BOUNCE_S seconds."""
    n = 0
    for i in range(2, len(seq)):
        if seq[i][0] == seq[i - 2][0] and seq[i][1] - seq[i - 1][1] < BOUNCE_S:
            n += 1
    return n


class FeedCounter:
    """Listens to every message on the feed while the simulation runs.
    (The feed only keeps the last 5000 messages in memory, so we count them as they happen.)"""

    def __init__(self, world):
        self.world = world
        self.zone_seq = {}        # bag id -> [(zone shown, since)] for normal bags
        self.detached = {}        # bag id -> time of its first TAG_DETACHED message
        self.errors = []          # the feed hides listener errors, so we keep our own

    def __call__(self, ev):
        try:
            bag = ev.get("bag")
            if ev["type"] == "BAG_CHECKED_IN" and bag in self.world.by_id and is_normal(self.world.by_id[bag]):
                self.zone_seq[bag] = [(ev.get("zone"), ev["t"])]
            elif ev["type"] == "ZONE_CHANGED" and bag in self.zone_seq:
                seq = self.zone_seq[bag]
                if seq[-1][0] != ev.get("zone"):   # the very first BLE fix is usually the check-in zone again
                    seq.append((ev.get("zone"), ev["t"]))
            elif ev["type"] == "TAG_DETACHED" and bag is not None:
                self.detached.setdefault(bag, ev["t"])
        except Exception as e:  # pragma: no cover - only if the message format changes
            self.errors.append(repr(e))

    def switches(self):
        changes = sum(len(s) - 1 for s in self.zone_seq.values())
        bounces = sum(count_bounces(s) for s in self.zone_seq.values())
        return changes, bounces


# ---------------------------------------------------------------------- one full hour

def run_hour(seed, smoothing):
    sim = Simulation(smoothing=smoothing, use_db=False, seed=seed, measure=True)
    counter = FeedCounter(sim.world)
    sim.feed.listeners.append(counter)
    t0 = time.time()
    sim.seek(sim.world.total_ticks)
    if counter.errors:
        raise RuntimeError(f"feed listener failed: {counter.errors[:3]}")
    return sim, counter, round(time.time() - t0, 1)


def zone_metrics(sim, counter, true_changes, true_bounces):
    s = sim.stats
    shown, bounces = counter.switches()
    n_bags = len(counter.zone_seq)
    return {
        "bag_seconds": s["n"],
        "primary_correct_pct": pct(s["ok"], s["n"]),
        "primary_or_nearby_pct": pct(s["near"], s["n"]),
        "makeup_bag_seconds": s["n_makeup"],
        "makeup_correct_pct": pct(s["ok_makeup"], s["n_makeup"]),
        "normal_bags": n_bags,
        "zone_switches_shown": shown,
        "true_zone_changes": true_changes,
        "switches_per_true_change": round(shown / true_changes, 2) if true_changes else None,
        "switches_per_bag": round(shown / n_bags, 1) if n_bags else None,
        "true_changes_per_bag": round(true_changes / n_bags, 1) if n_bags else None,
        "bounces_shown": bounces,
        "true_bounces": true_bounces,
    }


def late_metrics(sim, include):
    """include(truth) -> bool picks which bags to count (normal bags, or scenario bags)."""
    late, on_time = [], []
    for b in sim.world.bags:
        if not b.deadline or not include(b):
            continue
        (late if b.arrival > b.deadline else on_time).append(b)
    warned, after, missed, warnings = [], [], [], []
    for b in late:
        lf = sim.tracker.bags[b.bag_id].late_first
        if lf is None:
            missed.append(b.bag_id)
        elif lf < b.deadline:
            warned.append(b.bag_id)
            warnings.append(b.deadline - lf)
        else:
            after.append(b.bag_id)
    false_alarms = [b for b in on_time if sim.tracker.bags[b.bag_id].late_first is not None]
    margins = [b.deadline - b.arrival for b in false_alarms]   # how close to the cut-off those bags really arrived
    return {
        "bags_with_deadline": len(late) + len(on_time),
        "truly_late": len(late),
        "warned_before_deadline": len(warned),
        "warned_before_deadline_pct": pct(len(warned), len(late)),
        "late_flag_only_after_deadline": len(after),
        "never_flagged": len(missed),
        "warning_s_median": statistics.median(warnings) if warnings else None,
        "warning_s_mean": round(statistics.mean(warnings), 1) if warnings else None,
        "warning_s_min": min(warnings) if warnings else None,
        "warning_s_max": max(warnings) if warnings else None,
        "on_time_bags": len(on_time),
        "false_alarms": len(false_alarms),
        "false_alarm_pct": pct(len(false_alarms), len(on_time)),
        "false_alarm_margin_s_median": statistics.median(margins) if margins else None,
        "false_alarm_bags": [b.bag_id for b in false_alarms[:20]],
        "missed_bags": missed[:20],
    }


def false_alert_metrics(sim):
    """Alerts (other than LATE) raised on normal bags - every one of these is a false alert,
    because nothing was wrong with those bags."""
    by_status = {s: {"alerts": 0, "bags": set()} for s in ALERT_STATUSES if s != "LATE"}
    for a in sim.tracker.alerts.values():
        if a["status"] == "LATE" or not is_normal(sim.world.by_id[a["bag"]]):
            continue
        by_status[a["status"]]["alerts"] += 1
        by_status[a["status"]]["bags"].add(a["bag"])
    normal = sum(1 for b in sim.world.bags if is_normal(b))
    out = {s: {"alerts": v["alerts"], "bags": len(v["bags"])} for s, v in by_status.items()}
    total_bags = len(set().union(*[v["bags"] for v in by_status.values()]))
    return {"normal_bags": normal, "by_status": out,
            "total_alerts": sum(v["alerts"] for v in out.values()),
            "bags_with_any": total_bags, "bags_with_any_pct": pct(total_bags, normal)}


# ---------------------------------------------------------------------- scenarios

def scenario_checks(sim, counter):
    tr = sim.tracker

    def alerts_for(bag_id):
        return sorted((a for a in tr.alerts.values() if a["bag"] == bag_id), key=lambda a: a["t"])

    def first(bag_id, status):
        return next((a["t"] for a in alerts_for(bag_id) if a["status"] == status), None)

    rows = []
    for sc in sim.world.scenarios:
        sid, bid = sc["id"], sc["bag"]
        rec = tr.bags[bid]
        truth = sim.world.by_id[bid]
        statuses = [a["status"] for a in alerts_for(bid)]
        row = {"id": sid, "title": sc["title"], "bag": bid}
        if sid == "on_track":
            ok = not statuses and rec.phase == "loaded"
            row.update(expected="no alert, bag loaded", key_t=rec.loaded_t,
                       detail=f"phase {rec.phase}, alerts: {statuses or 'none'}")
        elif sid == "misrouted":
            key = first(bid, "MISROUTED")
            ok = key is not None
            row.update(expected="MISROUTED", key_t=key,
                       detail=f"bag reached the wrong lane at {clock(truth.arrival)}")
        elif sid == "wrong_aircraft":
            key = first(bid, "WRONG AIRCRAFT")
            ok = key is not None
            row.update(expected="WRONG AIRCRAFT", key_t=key, detail=f"final status {rec.status}")
        elif sid == "tag_swap":
            victim = sc["other"]
            times = [t for t in (first(victim, "IDENTITY REVIEW"), first(victim, "IDENTITY FLAG")) if t is not None]
            key = min(times) if times else None
            ok = key is not None
            vstat = [a["status"] for a in alerts_for(victim)]
            row.update(expected=f"IDENTITY REVIEW/FLAG on {victim}", key_t=key, bag=f"{bid} (victim {victim})",
                       detail=f"victim alerts: {vstat or 'none'}")
        elif sid == "tag_fell_off":
            key = counter.detached.get(bid)
            photo = (rec.photo or {}).get("verdict")
            ok = key is not None and photo == "pass" and rec.phase == "loaded"
            row.update(expected="TAG_DETACHED + photo PASS + loaded", key_t=key,
                       detail=f"photo {photo}, phase {rec.phase}")
        elif sid == "transfer_late":
            ns, late = first(bid, "NOT SEEN"), first(bid, "LATE")
            ok = ns is not None and late is not None
            key = late
            ahead = (truth.deadline - late) if late is not None else None
            row.update(expected="NOT SEEN + LATE", key_t=key,
                       detail=f"NOT SEEN at {clock(ns) if ns is not None else '-'}, LATE {ahead} s before the "
                              f"{clock(truth.deadline)} cut-off" if late is not None else "LATE never raised")
        elif sid == "tag_not_returned":
            key = first(bid, "TAG NOT RETURNED")
            ok = key is not None
            row.update(expected="TAG NOT RETURNED", key_t=key, detail=f"cleared at {clock(rec.cleared_t) if rec.cleared_t else '-'}")
        else:  # a new scenario we don't know how to check yet
            ok, key = None, None
            row.update(expected="?", key_t=None, detail="no check written for this scenario")
        expected_words = row["expected"]
        others = [s for s in statuses if s not in expected_words]
        row.update(passed=ok, key_clock=clock(row["key_t"]) if row["key_t"] is not None else None,
                   other_alerts=others)
        rows.append(row)
    return rows


# ---------------------------------------------------------------------- the report

def summarise_range(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return "-"
    lo, hi = min(vals), max(vals)
    return f"{lo}" if lo == hi else f"{lo} - {hi}"


def build_markdown(res):
    m = res["main"]
    z_on, z_off = m["zone_on"], m["zone_off"]
    lt, lsc = m["late_normal"], m["late_scenario"]
    fa = m["false_alerts"]
    L = []
    L.append("# AutoTrack - results in simulation")
    L.append("")
    L.append(f"All numbers below are **in simulation**: the organisers' baggage replay ({res['bags']} bags, 1 hour, "
             "1 tick = 1 s) with simulated BLE radio noise, RFID reads and loader portals. The hall is a "
             "**simplified layout (28 zones, about 88 m x 66 m), not the real YYZ baggage hall**. "
             f"Main run: radio seed {res['main_seed']}. Robustness: radio seeds {', '.join(map(str, res['seeds']))}.")
    L.append("")
    L.append("## 1. Which zone is the bag in? (smoothing ON vs OFF)")
    L.append("")
    L.append("| In simulation | Smoothing ON | Smoothing OFF |")
    L.append("|---|---|---|")
    L.append(f"| Zone shown = true nearest zone (% of bag-seconds) | {z_on['primary_correct_pct']}% | {z_off['primary_correct_pct']}% |")
    L.append(f"| True zone is the shown zone or a listed nearby zone | {z_on['primary_or_nearby_pct']}% | {z_off['primary_or_nearby_pct']}% |")
    L.append(f"| Correct while waiting in a make-up lane | {z_on['makeup_correct_pct']}% | {z_off['makeup_correct_pct']}% |")
    L.append(f"| Zone switches shown, normal bags (truth: {z_on['true_zone_changes']}) | {z_on['zone_switches_shown']} "
             f"({z_on['switches_per_true_change']}x truth) | {z_off['zone_switches_shown']} ({z_off['switches_per_true_change']}x truth) |")
    L.append(f"| Quick bounces A->B->A within {BOUNCE_S} s (truth: {z_on['true_bounces']}) | {z_on['bounces_shown']} | {z_off['bounces_shown']} |")
    L.append("")
    L.append(f"Bag-seconds = every second of every bag while it is moving or waiting in a lane ({z_on['bag_seconds']:,} in total). "
             "The 2 scenario bags whose tag fell off or went silent are left out of the zone numbers. "
             f"Zone switches are counted on the {z_on['normal_bags']} normal (non-scenario) bags.")
    L.append("")
    L.append("## 2. LATE early warning (smoothing ON)")
    L.append("")
    L.append("| In simulation, normal bags | Result |")
    L.append("|---|---|")
    L.append(f"| Bags that really missed their make-up cut-off | {lt['truly_late']} |")
    L.append(f"| ...got LATE **before** the cut-off passed | {lt['warned_before_deadline']} ({lt['warned_before_deadline_pct']}%) |")
    L.append(f"| ...got LATE only after the cut-off | {lt['late_flag_only_after_deadline']} |")
    L.append(f"| ...never got LATE | {lt['never_flagged']} |")
    L.append(f"| Warning time before the cut-off (median / mean) | {lt['warning_s_median']} s / {lt['warning_s_mean']} s |")
    L.append(f"| False alarms: on-time bags that were ever LATE | {lt['false_alarms']} of {lt['on_time_bags']} ({lt['false_alarm_pct']}%) |")
    if lt["false_alarm_margin_s_median"] is not None:
        L.append("")
        L.append(f"Most false alarms are near-misses: those bags reached their lane a median of "
                 f"{lt['false_alarm_margin_s_median']} s before the cut-off.")
    L.append("")
    L.append(f"Scenario bags, counted separately: {lsc['truly_late']} truly late "
             f"({lsc['warned_before_deadline']} warned in time), {lsc['false_alarms']} of {lsc['on_time_bags']} on-time ones were ever LATE. "
             f"{res['bags_without_deadline']} bags have no cut-off in the replay and are not counted here.")
    L.append("")
    L.append("## 3. The 7 demo scenarios")
    L.append("")
    L.append("| Scenario | Bag | Expected | Fired? | Time | Notes |")
    L.append("|---|---|---|---|---|---|")
    for r in m["scenarios"]:
        fired = "YES" if r["passed"] else ("NO" if r["passed"] is False else "?")
        note = r["detail"]
        if r["other_alerts"]:
            note += f"; also raised: {', '.join(r['other_alerts'])}"
        L.append(f"| {r['title']} | {r['bag']} | {r['expected']} | {fired} | {r['key_clock'] or '-'} | {note} |")
    L.append("")
    L.append("## 4. False alerts on normal bags (LATE is in section 2)")
    L.append("")
    L.append("| Status | Alerts | Bags |")
    L.append("|---|---|---|")
    for s, v in fa["by_status"].items():
        L.append(f"| {s} | {v['alerts']} | {v['bags']} |")
    L.append(f"| **Any of these** | {fa['total_alerts']} | {fa['bags_with_any']} of {fa['normal_bags']} ({fa['bags_with_any_pct']}%) |")
    L.append("")
    L.append(f"## 5. Robustness: radio seeds {', '.join(map(str, res['seeds']))} (min - max)")
    L.append("")
    rb = res["robustness"]
    L.append("| In simulation | Smoothing ON | Smoothing OFF |")
    L.append("|---|---|---|")
    L.append(f"| Zone shown = true zone | {rb['primary_correct_pct']['on']}% | {rb['primary_correct_pct']['off']}% |")
    L.append(f"| True zone in shown + nearby | {rb['primary_or_nearby_pct']['on']}% | {rb['primary_or_nearby_pct']['off']}% |")
    L.append(f"| Correct in make-up lane | {rb['makeup_correct_pct']['on']}% | {rb['makeup_correct_pct']['off']}% |")
    L.append(f"| Zone switches / true changes | {rb['switches_per_true_change']['on']}x | {rb['switches_per_true_change']['off']}x |")
    L.append(f"| Late bags warned before cut-off | {rb['warned_before_deadline_pct']['on']}% | {rb['warned_before_deadline_pct']['off']}% |")
    L.append(f"| Median warning (s) | {rb['warning_s_median']['on']} | {rb['warning_s_median']['off']} |")
    L.append(f"| LATE false alarms (on-time bags) | {rb['false_alarm_pct']['on']}% | {rb['false_alarm_pct']['off']}% |")
    L.append(f"| Other false alerts (alerts) | {rb['total_false_alerts']['on']} | {rb['total_false_alerts']['off']} |")
    L.append(f"| Demo scenarios passing | {rb['scenarios_passed']['on']} of {len(m['scenarios'])} | {rb['scenarios_passed']['off']} of {len(m['scenarios'])} |")
    L.append("")
    L.append(f"_Generated by tools/evaluate.py in {res['runtime_s']} s. Full numbers: results/metrics.json._")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="AutoTrack results in simulation")
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3], help="radio noise seeds (first one = main run)")
    args = ap.parse_args(argv)
    started = time.time()

    per_seed = {}
    true_changes = true_bounces = None
    world = None
    for seed in args.seeds:
        for smoothing in (True, False):
            label = "on" if smoothing else "off"
            print(f"Running the hour: radio seed {seed}, smoothing {label.upper()} ...", flush=True)
            sim, counter, secs = run_hour(seed, smoothing)
            if true_changes is None:   # the true bag movements are the same in every run
                world = sim.world
                true_changes, true_bounces = count_true_zone_changes(world)
            per_seed.setdefault(seed, {})[label] = {
                "seconds": secs,
                "zone": zone_metrics(sim, counter, true_changes, true_bounces),
                "late_normal": late_metrics(sim, is_normal),
                "late_scenario": late_metrics(sim, lambda b: not is_normal(b)),
                "false_alerts": false_alert_metrics(sim),
                "scenarios": scenario_checks(sim, counter),
            }
            print(f"   done in {secs} s", flush=True)

    main_seed = args.seeds[0]
    on, off = per_seed[main_seed]["on"], per_seed[main_seed]["off"]
    main_res = {"zone_on": on["zone"], "zone_off": off["zone"], "late_normal": on["late_normal"],
                "late_scenario": on["late_scenario"], "false_alerts": on["false_alerts"],
                "scenarios": on["scenarios"]}

    def rng(getter):
        return {lab: summarise_range([getter(per_seed[s][lab]) for s in args.seeds]) for lab in ("on", "off")}

    robustness = {
        "primary_correct_pct": rng(lambda r: r["zone"]["primary_correct_pct"]),
        "primary_or_nearby_pct": rng(lambda r: r["zone"]["primary_or_nearby_pct"]),
        "makeup_correct_pct": rng(lambda r: r["zone"]["makeup_correct_pct"]),
        "switches_per_true_change": rng(lambda r: r["zone"]["switches_per_true_change"]),
        "warned_before_deadline_pct": rng(lambda r: r["late_normal"]["warned_before_deadline_pct"]),
        "warning_s_median": rng(lambda r: r["late_normal"]["warning_s_median"]),
        "false_alarm_pct": rng(lambda r: r["late_normal"]["false_alarm_pct"]),
        "total_false_alerts": rng(lambda r: r["false_alerts"]["total_alerts"]),
        "scenarios_passed": rng(lambda r: sum(1 for x in r["scenarios"] if x["passed"])),
    }

    res = {
        "label": "in simulation",
        "note": "Simplified 28-zone hall (~88 m x 66 m) built from the organisers' conveyor network, not YYZ.",
        "bags": len(world.bags),
        "bags_without_deadline": sum(1 for b in world.bags if not b.deadline),
        "seeds": args.seeds,
        "main_seed": main_seed,
        "main": main_res,
        "robustness": robustness,
        "per_seed": {str(k): v for k, v in per_seed.items()},
        "runtime_s": round(time.time() - started, 1),
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "metrics.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
    md = build_markdown(res)
    (RESULTS / "metrics.md").write_text(md, encoding="utf-8")
    print()
    print(md)
    print(f"Saved {RESULTS / 'metrics.json'} and {RESULTS / 'metrics.md'}")
    return res


if __name__ == "__main__":
    main()
