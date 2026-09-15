"""Durable JSON Lines output helpers."""

import json
import os
from pathlib import Path
from typing import Any


def append_jsonl(path: str | Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as output_file:
        output_file.write(line)
        output_file.flush()
        os.fsync(output_file.fileno())
