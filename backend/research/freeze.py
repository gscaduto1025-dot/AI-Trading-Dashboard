"""Freeze record for the validation runs: the exact strategy rules, parameters and cost
assumptions, hashed, so every later run can prove it used the same ones.

  .venv/bin/python research/freeze.py snapshot   -> prints the current snapshot (JSON)
  .venv/bin/python research/freeze.py check      -> exit 1 unless it matches the frozen one

The frozen snapshot lives in research/validation/protocol.json ("frozen" key). Its
"rules_hash" covered the configuration, the cost and execution assumptions and every
source file under app/.

Amendment 1 (2026-10-15, see protocol.json "protocol_amendments"): `check` now compares the
RULE SURFACE instead of the whole app source, so presentation features (scanner labels,
filters, presets, research dashboard) can be added without touching the rules. The rule
surface = the engine configuration, the cost/execution assumptions, every file of the
rule-defining packages (RULE_DIRS / RULE_FILES) and the source of the live-path functions
that feed the engines (RULE_FUNCTIONS). Its hash was computed at the frozen commit a7e60d7
and must stay identical; changing any rule still makes `check` fail.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PROTOCOL = ROOT / "research" / "validation" / "protocol.json"


def _plain(x):
    if dataclasses.is_dataclass(x):
        return {k: _plain(v) for k, v in dataclasses.asdict(x).items()}
    if isinstance(x, dict):
        return {str(k): _plain(v) for k, v in sorted(x.items(), key=lambda kv: str(kv[0]))}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    return x if isinstance(x, (int, float, str, bool, type(None))) else str(x)


def _h(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def source_hash() -> str:
    h = hashlib.sha256()
    for p in sorted((ROOT / "app").rglob("*.py")):
        h.update(str(p.relative_to(ROOT)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


RULE_DIRS = ("strategies", "swing", "planning", "backtest", "levels", "market", "analysis",
             "data")
RULE_FILES = ("config.py",)
RULE_FUNCTIONS = ("app.main:Service._window", "app.main:Service.volume_source",
                  "app.main:Service.strategy_setups", "app.main:Service.swing_setups",
                  "app.scanner.engine:setup_row", "app.scanner.engine:entry_env",
                  "app.scanner.engine:live_quote")


def rule_surface_hash(root: Path = ROOT) -> str:
    """sha256 over the rule-defining source files and live-path functions under `root`
    (a checkout's backend/ directory)."""
    import ast
    h = hashlib.sha256()
    app = root / "app"
    files = [p for d in RULE_DIRS for p in sorted((app / d).rglob("*.py"))]
    files += [app / f for f in RULE_FILES]
    for p in files:
        h.update(str(p.relative_to(root)).encode())
        h.update(p.read_bytes())
    for ref in RULE_FUNCTIONS:
        mod, qual = ref.split(":")
        src = (root / (mod.replace(".", "/") + ".py")).read_text()
        node = ast.parse(src)
        for part in qual.split("."):
            node = next(n for n in ast.iter_child_nodes(node)
                        if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == part)
        h.update(ref.encode())
        h.update((ast.get_source_segment(src, node) or "").encode())
    return h.hexdigest()


def snapshot() -> dict:
    from app.backtest.config import BacktestConfig
    from app.backtest.data import MarketHistory
    from app.backtest.manager import engine_config
    from app.config import load_settings
    from app.main import Service
    svc = Service(replace(load_settings(), database_path=Path(tempfile.mkdtemp()) / "f.db",
                          scan_interval_seconds=0))
    eng = _plain(engine_config(svc, MarketHistory(svc.market)))
    parts = {"engine": eng, "backtest_costs_and_execution": _plain(BacktestConfig()),
             "app_source_sha256": source_hash()}
    return {"rules_hash": _h(parts),
            "part_hashes": {k: _h(v) for k, v in parts.items()}, **parts}


def check() -> dict:
    """Refuse unless the configuration, the cost assumptions and the rule surface equal the
    frozen ones (Amendment 1). Returns {"rules_hash": <frozen id>, "rule_surface": <hash>}."""
    proto = json.loads(PROTOCOL.read_text())
    frozen = proto["frozen"]
    now = snapshot()
    diff = [k for k in ("engine", "backtest_costs_and_execution")
            if frozen["part_hashes"][k] != now["part_hashes"][k]]
    surface = rule_surface_hash()
    want = proto["protocol_amendments_data"]["rule_surface_sha256"]
    if surface != want:
        diff.append("rule surface (strategy/planning/backtest/market source)")
    if diff:
        raise SystemExit(f"rules differ from the frozen protocol: {diff}")
    return {"rules_hash": frozen["rules_hash"], "rule_surface": surface}


if __name__ == "__main__":
    if sys.argv[1:] == ["check"]:
        print("rules match the frozen protocol:", check())
    elif sys.argv[1:2] == ["surface"]:  # surface [backend dir of another checkout]
        print(rule_surface_hash(Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT))
    else:
        json.dump(snapshot(), sys.stdout, indent=1)
