"""Run the real SecureBag verify_bags() on the sample pairs and cache the outputs.

Run with the dedicated venv:
    C:\\venv-securebag\\Scripts\\python.exe run_securebag.py

Images are encoded exactly the way SecureBag's own Flask routes do it
(routes_checkin.py / routes_verify.py): PIL open -> convert("RGB") ->
save as JPEG (Pillow default quality) -> raw base64 string, no data-URL prefix.
Nothing in bag_compare.py is modified; verdicts are written verbatim.
"""
import base64
import json
import platform
import sys
import time
import traceback
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
SCR = HERE.parent.parent
SECUREBAG = SCR / "challenge" / "mishandled-baggage-subproblem" / "securebag"
SAMPLES = SECUREBAG / "sample_img"
STATIC_BAGS = HERE.parent / "static" / "bags"

sys.path.insert(0, str(SECUREBAG))

PAIRS = {
    "test2": ("test2_1webp.webp", "test2_2.webp", "two different black suitcases that look alike"),
    "test3": ("test3_1.png", "test3_2.png", "same leopard-print bag in two settings"),
    "test4": ("test4_1.png", "test4_2.png", "two grey same-brand bags differing only in shell texture (tag-swap case)"),
}

MAPPING_NOTE = (
    "No mapping needed: VerifyResult['verdict'] already uses 'pass' | 'review' | 'flag' and is stored verbatim. "
    "From bag_compare.verify_bags(): combined = ORB_WEIGHT * orb + COLOR_WEIGHT * color (ORB_WEIGHT = 0.4, COLOR_WEIGHT = 0.6); "
    "'if combined >= PASS_THRESHOLD: verdict = \"pass\" / elif combined >= FLAG_THRESHOLD: verdict = \"review\" / else: verdict = \"flag\"' "
    "(PASS_THRESHOLD = 0.35, FLAG_THRESHOLD = 0.15). Then 'if verdict == \"pass\" and (color_mismatch or orb_mismatch or texture_mismatch): "
    "verdict = \"review\"' where color_mismatch = color < FLAG_THRESHOLD, "
    "orb_mismatch = orb < FLAG_THRESHOLD and orb_candidate_ratio >= ORB_CONFIDENCE_RATIO (0.35), "
    "texture_mismatch = texture < TEXTURE_MISMATCH_THRESHOLD (0.6). Note the texture veto can only demote pass->review, never produce 'flag'. "
    "Inputs were encoded like SecureBag's Flask routes: PIL convert('RGB') -> JPEG -> raw base64 (no data-URL prefix); argument order = (image_a as check-in, image_b as scan)."
)


def encode_like_app(path: Path) -> str:
    img = Image.open(path).convert("RGB")
    buf = BytesIO()
    img.save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode()


def web_copy(src: Path, stem: str) -> str:
    """Ensure a browser-friendly copy exists in static/bags; return path relative to static/."""
    STATIC_BAGS.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() == ".png":
        dst = STATIC_BAGS / f"{stem}.png"
        if not dst.exists():
            dst.write_bytes(src.read_bytes())
    else:
        orig = STATIC_BAGS / f"{stem}{src.suffix.lower()}"
        if not orig.exists():
            orig.write_bytes(src.read_bytes())
        dst = STATIC_BAGS / f"{stem}.png"
        if not dst.exists():
            Image.open(src).convert("RGB").save(dst, format="PNG")
    return f"bags/{dst.name}"


def timed_call(verify_bags, a_b64: str, b_b64: str) -> dict:
    t0 = time.perf_counter()
    try:
        res = verify_bags(a_b64, b_b64)
        return {"ok": True, "result": res, "seconds": round(time.perf_counter() - t0, 3), "error": None}
    except Exception as e:  # record honestly
        return {"ok": False, "result": None, "seconds": round(time.perf_counter() - t0, 3),
                "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}


def main() -> None:
    torch_version = None
    import_error = None
    detector_load = {"seconds": None, "error": None}
    try:
        import torch
        torch_version = torch.__version__
    except Exception as e:
        import_error = f"torch import failed: {type(e).__name__}: {e}"
    try:
        import bag_compare
        verify_bags = bag_compare.verify_bags
    except Exception as e:
        verify_bags = None
        import_error = (import_error + "; " if import_error else "") + f"bag_compare import failed: {type(e).__name__}: {e}"

    if verify_bags is not None:
        # Warm up the detector (downloads COCO weights on first use) so per-call
        # timings below reflect steady-state comparison time.
        t0 = time.perf_counter()
        try:
            bag_compare._get_bag_detector()
        except Exception as e:
            detector_load["error"] = f"{type(e).__name__}: {e}"
        detector_load["seconds"] = round(time.perf_counter() - t0, 3)

    pairs_out = {}
    detail = {}
    for key, (fa, fb, desc) in PAIRS.items():
        pa, pb = SAMPLES / fa, SAMPLES / fb
        image_a = web_copy(pa, f"{key}_a")
        image_b = web_copy(pb, f"{key}_b")
        entry = {"status": "error", "verdict": None, "scores": {}, "raw": None,
                 "reverse_verdict": None, "repeat_verdict": None, "seconds": None,
                 "image_a": image_a, "image_b": image_b, "description": desc, "error": None}
        if verify_bags is None:
            entry["error"] = import_error
            pairs_out[key] = entry
            continue
        a64, b64 = encode_like_app(pa), encode_like_app(pb)
        fwd = timed_call(verify_bags, a64, b64)
        rev = timed_call(verify_bags, b64, a64)
        rep = timed_call(verify_bags, a64, b64)
        detail[key] = {"forward": fwd, "reverse": rev, "repeat": rep}

        if fwd["ok"]:
            r = fwd["result"]
            entry["status"] = "ok"
            entry["verdict"] = r["verdict"]
            scores = {"combined_score": r["combined_score"]}
            scores.update({k: v for k, v in r["breakdown"].items() if isinstance(v, (int, float))})
            entry["scores"] = scores
            entry["raw"] = r
        entry["seconds"] = fwd["seconds"]
        entry["reverse_verdict"] = rev["result"]["verdict"] if rev["ok"] else None
        entry["repeat_verdict"] = rep["result"]["verdict"] if rep["ok"] else None
        errs = [f"{name}: {c['error']}" for name, c in (("forward", fwd), ("reverse", rev), ("repeat", rep)) if not c["ok"]]
        entry["error"] = "; ".join(errs) if errs else None
        pairs_out[key] = entry
        print(f"{key}: fwd={entry['verdict']} rev={entry['reverse_verdict']} rep={entry['repeat_verdict']} "
              f"secs={fwd['seconds']}/{rev['seconds']}/{rep['seconds']} err={entry['error']}")

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "torch": torch_version,
        "source": "IdeasClinicUWaterloo F26 repo, mishandled-baggage-subproblem/securebag/bag_compare.py verify_bags()",
        "pairs": pairs_out,
        "mapping_note": MAPPING_NOTE,
    }
    (HERE / "verdicts.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    # Full results of every call (forward / reverse / repeat), plus detector warm-up time.
    (HERE / "runs_detail.json").write_text(json.dumps(
        {"generated_at": out["generated_at"], "detector_load": detector_load, "import_error": import_error,
         "runs": detail}, indent=2), encoding="utf-8")
    print("detector_load:", detector_load)
    print("wrote", HERE / "verdicts.json")


if __name__ == "__main__":
    main()
