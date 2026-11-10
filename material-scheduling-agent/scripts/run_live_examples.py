#!/usr/bin/env python3
"""Collect two genuine model paths after credentials are configured externally."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if not os.getenv("OPENAI_API_KEY") or not os.getenv("OPENAI_MODEL"):
        print("Configure OPENAI_API_KEY and OPENAI_MODEL securely; no live call was made.", file=sys.stderr)
        return 1
    output_dir = ROOT / "examples/live"
    output_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    records = []
    with tempfile.TemporaryDirectory(prefix="material-agent-live-") as temp:
        for name, expected in [("normal", "completed"), ("conflict", "pending_confirmation")]:
            command = [sys.executable, "-m", "material_agent", "--mode", "live", "--input", f"examples/inputs/{name}.json",
                       "--catalog", "data/catalog.json", "--db", str(Path(temp) / (name + ".sqlite")),
                       "--output", f"examples/live/{name}.output.json", "--trace-output", f"examples/live/{name}.trace.json"]
            run = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=950)
            output = json.loads(run.stdout)
            passed = output["status"] == expected and output.get("mode") == "live"
            records.append({"name": name, "expected_status": expected, "actual_status": output["status"], "passed": passed,
                            "exit_code": run.returncode, "actions": [e["action"] for e in output.get("trace", [])]})
            print(f"{name}: {output['status']} (expected {expected})")
    summary = {"recorded_at": datetime.now(timezone.utc).isoformat(), "model": os.environ["OPENAI_MODEL"], "mode": "live", "records": records,
               "note": "Inspect actual citations, tool results, confirmation state and decision reasons before claiming model quality."}
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if all(record["passed"] for record in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
