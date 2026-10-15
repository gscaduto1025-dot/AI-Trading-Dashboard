"""Freeze record for the validation runs: the exact strategy rules, parameters and cost
assumptions, hashed, so every later run can prove it used the same ones.

  .venv/bin/python research/freeze.py snapshot   -> prints the current snapshot (JSON)
  .venv/bin/python research/freeze.py check      -> exit 1 unless it matches the frozen one

The frozen snapshot lives in research/validation/protocol.json ("frozen" key). "rules_hash"
covers the strategy, swing and planning configuration, the backtest cost and execution
assumptions, the volume/entry policy, and every source file under app/ (the rules
themselves). Changing any of them makes `check` fail.
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
    frozen = json.loads(PROTOCOL.read_text())["frozen"]
    now = snapshot()
    if now["rules_hash"] != frozen["rules_hash"]:
        diff = [k for k in frozen["part_hashes"] if frozen["part_hashes"][k] != now["part_hashes"][k]]
        raise SystemExit(f"rules differ from the frozen protocol: {diff}")
    return now


if __name__ == "__main__":
    if sys.argv[1:] == ["check"]:
        print("rules match the frozen protocol:", check()["rules_hash"])
    else:
        json.dump(snapshot(), sys.stdout, indent=1)
