"""Reset generated artifacts and replay the checked-in event batch."""

import os
from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUT_ROOT = PROJECT_ROOT / "reto-kontaktu"
STATE_ROOT = PROJECT_ROOT / "state"
OUTPUT_ROOT = PROJECT_ROOT / "output"
EXTRA_ROOT = PROJECT_ROOT / "tests" / "extra_events"


def reset_generated_artifacts() -> None:
    """Remove only this application's reproducible state and output files."""
    for path in (
        STATE_ROOT / "orchestrator.sqlite",
        OUTPUT_ROOT / "decisions.jsonl",
        OUTPUT_ROOT / "orders.jsonl",
    ):
        if path.exists():
            path.unlink()
    STATE_ROOT.mkdir(exist_ok=True)
    OUTPUT_ROOT.mkdir(exist_ok=True)


def event_paths() -> list[Path]:
    """Return base events in contract order followed by synthetic fixtures."""
    names = [
        line.strip()
        for line in (INPUT_ROOT / "eventos" / "orden.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    paths = [INPUT_ROOT / "eventos" / name for name in names]
    paths.extend(sorted(EXTRA_ROOT.glob("*.json")))
    return paths


def replay() -> None:
    """Run every fixture in offline mode, stopping on the first failure."""
    reset_generated_artifacts()
    environment = os.environ.copy()
    environment.update(
        {
            "KONTAKTU_BASE": str(INPUT_ROOT),
            "OFFLINE_REPLAY": "1",
        }
    )
    for path in event_paths():
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "run.py"), str(path)],
            cwd=PROJECT_ROOT,
            env=environment,
        )
        if result.returncode:
            raise SystemExit(result.returncode)


if __name__ == "__main__":
    replay()
