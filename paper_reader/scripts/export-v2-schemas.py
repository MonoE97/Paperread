#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from paper_reader.contracts import V2_SCHEMA_MODELS, schema_filename


def main() -> int:
    parser = argparse.ArgumentParser(description="Export checked-in Paper Reader V2 JSON schemas.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "references" / "schemas",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for version, model in V2_SCHEMA_MODELS.items():
        destination = args.output_dir / schema_filename(version)
        payload = json.dumps(
            model.model_json_schema(mode="validation"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        destination.write_text(payload + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
