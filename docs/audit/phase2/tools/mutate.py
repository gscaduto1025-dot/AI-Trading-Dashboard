"""Mutation check: plant one fault per throwaway copy of the backend and run the tests.
Production code is never modified (each copy lives in a temporary directory and is discarded)."""
import json, re, shutil, subprocess, sys, pathlib

SRC = pathlib.Path(__file__).resolve().parents[4] / "backend"
WORK = pathlib.Path(__import__("tempfile").mkdtemp(prefix="tradescope-mutants-"))
PY = str(SRC / ".venv/bin/python")
MUTANTS = [
    ("stop buffer on the wrong side", "app/planning/plan.py",
     "stop = anchor.price - s * buf_atr * atr", "stop = anchor.price + s * buf_atr * atr"),
    ("reward/risk miscalculated by 10%", "app/planning/plan.py",
     "rr1, rr2 = rew1 / risk, rew2 / risk", "rr1, rr2 = rew1 / risk * 1.1, rew2 / risk"),
    ("2:1 minimum reward/risk disabled", "app/planning/plan.py",
     "rr_ok = rr1 >= min_rr - 1e-9", "rr_ok = True"),
    ("reward/risk boundary loosened to 1.7", "app/planning/plan.py",
     "rr_ok = rr1 >= min_rr - 1e-9", "rr_ok = rr1 >= min_rr - 0.3"),
    ("look-ahead: forming/future candles kept", "app/swing/context.py",
     'if bar_end(b, s, cal, "regular") <= as_ts]', "if True]"),
    ("stale/missing-data problems ignored", "app/swing/context.py",
     "gate = list(data_problems or [])", "gate = []"),
    ("S/R reversal accepts a close AT the trigger", "app/swing/sr_reversal.py",
     "if s * (cj - trig) > 0:", "if s * (cj - trig) >= 0:"),
    ("trend continuation accepts a close AT the prior low", "app/swing/trend_continuation.py",
     "(b.close < b.open and b.close < prev.low)", "(b.close < b.open and b.close <= prev.low)"),
    ("trend continuation LONG ignores candle colour", "app/swing/trend_continuation.py",
     "confirm = (b.close > b.open and b.close > prev.high)", "confirm = (b.close > prev.high)"),
    ("consolidation volume threshold lowered 1.3 -> 0.9", "app/swing/config.py",
     "    min_rel_volume: float = 1.3", "    min_rel_volume: float = 0.9"),
    ("consolidation: one of two volume checks disabled (defence in depth)",
     "app/swing/consolidation_breakout.py", "if vol.rejects(cfg.min_rel_volume):", "if False:"),
    ("trend continuation LONG accepts a close AT the prior high", "app/swing/trend_continuation.py",
     "confirm = (b.close > b.open and b.close > prev.high)",
     "confirm = (b.close > b.open and b.close >= prev.high)"),
    ("trend continuation SHORT ignores candle colour", "app/swing/trend_continuation.py",
     "(b.close < b.open and b.close < prev.low)", "(b.close < prev.low)"),
]


def run(copy, test):
    r = subprocess.run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", test], cwd=copy,
                       capture_output=True, text=True, timeout=900)
    out = r.stdout + r.stderr
    if "error" in out.lower() and "passed" not in out and "failed" not in out:
        raise RuntimeError(out[-2000:])
    m = re.search(r"(\d+) failed", out)
    p = re.search(r"(\d+) passed", out)
    return int(m.group(1)) if m else 0, int(p.group(1)) if p else 0


results = []
for name, rel, old, new in MUTANTS:
    copy = WORK / re.sub(r"\W+", "_", name)
    if copy.exists():
        shutil.rmtree(copy)
    def ignore(d, names):  # skip the venv, runtime data (top level only), databases, caches
        top = pathlib.Path(d) == SRC
        return [n for n in names if n in (".venv", "__pycache__") or n.endswith(".db")
                or (top and n == "data")]
    shutil.copytree(SRC, copy, ignore=ignore)
    f = copy / rel
    text = f.read_text()
    assert text.count(old) == 1, (name, text.count(old))
    f.write_text(text.replace(old, new))
    focus = run(copy, "tests/test_swing_focus.py")
    existing = run(copy, "tests/test_swing.py")
    row = {"fault": name, "file": rel, "focus_failed": focus[0], "focus_passed": focus[1],
           "existing_swing_failed": existing[0], "existing_swing_passed": existing[1],
           "detected_by_new_tests": focus[0] > 0}
    results.append(row)
    print(json.dumps(row), flush=True)
    shutil.rmtree(copy)
json.dump(results, open(sys.argv[1] if len(sys.argv) > 1 else "mutation_results.json", "w"), indent=1)
# production untouched
print("production diff:", subprocess.run(["git", "status", "--short", "app/"], cwd=SRC,
                                         capture_output=True, text=True).stdout or "(none)")
