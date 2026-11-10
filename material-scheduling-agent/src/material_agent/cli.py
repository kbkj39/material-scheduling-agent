from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .agent import Agent
from .contracts import validate_request
from .models import LiveModel, MockModel, ModelError
from .tools import ToolBox


def _write(path: str, value) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="审核素材并保存排期草稿；不会发布。")
    parser.add_argument("--input", required=True, help="JSON request path")
    parser.add_argument("--catalog", default="data/catalog.json")
    parser.add_argument("--db", default=".state/drafts.sqlite")
    parser.add_argument("--mode", choices=["mock", "live"], default="mock")
    parser.add_argument("--max-steps", type=int, default=20, help="Maximum model decisions, 1-100, including finish")
    parser.add_argument("--fault", action="append", default=[], metavar="TOOL=COUNT", help="Inject deterministic local tool failures; example save_draft=1")
    parser.add_argument("--output", help="Write complete actual output, including trace")
    parser.add_argument("--trace-output", help="Also write trace as a separate JSON array")
    args = parser.parse_args(argv)
    try:
        user_request = validate_request(json.loads(Path(args.input).read_text(encoding="utf-8")))
        faults = {}
        for spec in args.fault:
            name, count = spec.split("=", 1)
            if name not in {"read_material", "check_rules", "query_slots", "save_draft"} or not count.isdecimal() or int(count) > 100:
                raise ValueError("fault must be a known TOOL=COUNT with count in 0-100")
            faults[name] = int(count)
        toolbox = ToolBox(user_request, args.catalog, args.db, faults)
        model = MockModel() if args.mode == "mock" else LiveModel()
        result = Agent(model, toolbox, args.max_steps).run()
    except ModelError as exc:
        result = {"status": "failed", "mode": args.mode, "published": False, "error": {"code": "model_configuration", "message": str(exc)}}
    except (OSError, ValueError, TypeError) as exc:
        result = {"status": "invalid_input", "mode": args.mode, "published": False, "error": {"code": "invalid_input", "message": str(exc)[:200]}}
    try:
        if args.output:
            _write(args.output, result)
        if args.trace_output:
            _write(args.trace_output, result.get("trace", []))
    except OSError as exc:
        print(json.dumps({"status": "failed", "error": {"code": "output_write_failed", "message": type(exc).__name__}}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"completed", "pending_confirmation"} else 64 if result["status"] == "invalid_input" else 1 if result["status"] == "failed" else 2
