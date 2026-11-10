#!/usr/bin/env python3
"""Run the actual CLI in isolated stores and preserve offline acceptance evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / "examples/inputs"
RUNS = ROOT / "examples/runs"
T18 = "2026-11-11T18:00:00+08:00"
T19 = "2026-11-11T19:00:00+08:00"


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def request(request_id, asset="M01", at=T19, **extra):
    return {"request_id": request_id, "materials": [{"asset_id": asset, "required_at": at, "hard_requirement": True, **extra}]}


def main() -> int:
    RUNS.mkdir(parents=True, exist_ok=True)
    scenarios = [
        ("normal", request("example_normal"), [], "completed", "catalog.json"),
        ("missing", request("example_missing", asset="M02"), [], "needs_information", "catalog.json"),
        ("risk", request("example_risk", asset="M03"), [], "blocked", "catalog.json"),
        ("conflict", request("example_conflict", at=T18), [], "pending_confirmation", "catalog.json"),
        ("duplicate_first", request("example_duplicate"), [], "completed", "catalog.json"),
        ("duplicate", request("example_duplicate"), [], "completed", "catalog.json"),
        ("save_failure", request("example_save_failure"), ["--fault", "save_draft=1"], "failed", "catalog.json"),
        ("tool_failure", request("example_tool_failure"), ["--fault", "read_material=1"], "completed", "catalog.json"),
        ("step_limit", request("example_step_limit"), ["--max-steps", "1"], "incomplete", "catalog.json"),
        ("changed_schedule", request("example_changed_schedule", at=T18), [], "completed", "catalog_changed_schedule.json"),
        ("confirmed_alternative", request("example_confirmed_alternative", at=T18, confirmed_alternative_at=T19), [], "completed", "catalog.json"),
    ]
    changed = json.loads((ROOT / "data/catalog.json").read_text(encoding="utf-8"))
    changed["schedule"] = [{"asset_id": "EXISTING01", "at": T19}]
    write(ROOT / "data/catalog_changed_schedule.json", changed)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    manifest = {"recorded_at": datetime.now(timezone.utc).isoformat(), "mode": "mock", "evidence_boundary": "Offline simulator and business-tool evidence only; no real LLM decision evaluation.", "scenarios": []}
    with tempfile.TemporaryDirectory(prefix="material-agent-examples-") as temp:
        for name, data, flags, expected, catalog in scenarios:
            input_path = INPUTS / (name + ".json")
            write(input_path, data)
            store_name = "duplicate" if name in {"duplicate_first", "duplicate"} else name
            command = [sys.executable, "-m", "material_agent", "--mode", "mock", "--input", str(input_path.relative_to(ROOT)),
                       "--catalog", "data/" + catalog, "--db", str(Path(temp) / (store_name + ".sqlite")),
                       "--output", f"examples/runs/{name}.output.json", "--trace-output", f"examples/runs/{name}.trace.json", *flags]
            completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
            output = json.loads(completed.stdout)
            if completed.stderr:
                (RUNS / (name + ".stderr.txt")).write_text(completed.stderr, encoding="utf-8")
            expected_code = 0 if expected in {"completed", "pending_confirmation"} else 1 if expected == "failed" else 2
            assert output["status"] == expected, (name, output)
            assert completed.returncode == expected_code, (name, completed.returncode)
            assert output["published"] is False
            if name == "duplicate":
                assert output["items"][0]["draft"]["status"] == "existing"
            manifest["scenarios"].append({"name": name, "input": str(input_path.relative_to(ROOT)), "output": f"examples/runs/{name}.output.json",
                                          "trace": f"examples/runs/{name}.trace.json", "command": command, "exit_code": completed.returncode,
                                          "expected_status": expected, "actual_status": output["status"], "actions": [e["action"] for e in output["trace"]]})
            print(f"{name}: {output['status']} (exit {completed.returncode})")
    write(RUNS / "manifest.json", manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
