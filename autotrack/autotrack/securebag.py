"""
securebag.py - the photo check ("is it the same physical bag?").

We treat the organisers' SecureBag bag_compare.verify_bags() as a black box. It needs
PyTorch, so we ran it ahead of time on the repo's real sample photo pairs
(securebag_cache/run_securebag.py) and saved its REAL answers in securebag_cache/verdicts.json.
This file just looks those answers up. No verdict is ever made up: if a pair wasn't run,
we say so and a human checks the bag.

Verdicts: pass -> continue, review -> hold for a human, flag -> stop + call supervisor/security.
The photo match is decision support, not proof. A human always decides.
"""

import json

from .layout import ROOT

_PATH = ROOT / "securebag_cache" / "verdicts.json"


def load():
    try:
        return json.loads(_PATH.read_text(encoding="utf-8")).get("pairs", {})
    except (OSError, ValueError):
        return {}


_PAIRS = load()


def check(pair):
    """Returns {"verdict": "pass|review|flag|not_run", "score", "reason", "image_a", "image_b", "pair"}."""
    p = _PAIRS.get(pair) if pair else None
    if not p or p.get("status") != "ok":
        return {"verdict": "not_run", "pair": pair, "score": None,
                "reason": "No photo result available - manual visual check needed",
                "image_a": None, "image_b": None}
    raw = p.get("raw", {})
    return {"verdict": p["verdict"], "pair": pair, "score": raw.get("combined_score"),
            "reason": raw.get("flag_reason") or "photos match", "scores": p.get("scores", {}),
            "image_a": p.get("image_a"), "image_b": p.get("image_b"),
            "description": p.get("description")}
