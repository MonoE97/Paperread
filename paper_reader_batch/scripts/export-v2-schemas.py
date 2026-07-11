#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from paper_reader_batch.v2_contracts import export_contract_schemas, schema_filename


def main() -> int:
    batch_root = Path(__file__).resolve().parents[1]
    schema_root = batch_root / "references" / "schemas"
    schema_root.mkdir(parents=True, exist_ok=True)
    for schema_version, schema in export_contract_schemas().items():
        target = schema_root / schema_filename(schema_version)
        target.write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
