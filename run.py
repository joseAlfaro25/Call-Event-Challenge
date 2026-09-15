"""Process exactly one campaign event."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent / "src"))

from config import CampaignConfig
from workflows.event_processing.workflow import build_graph
from infrastructure.sqlite_store import Store

PROJECT_ROOT = Path(__file__).parent.resolve()


def process_event(event_path: str | Path) -> None:
    """Process one event through the graph and durable SQLite state."""
    config = CampaignConfig.load(event_path)
    store_path = PROJECT_ROOT / "state" / "orchestrator.sqlite"
    store = Store(store_path)
    store.close()
    event_id = json.loads(Path(event_path).read_text(encoding="utf-8"))["event_id"]
    graph_input = {
        "event_path": str(Path(event_path).resolve()),
        "config": config,
        "store_path": str(store_path),
        "output_path": str(PROJECT_ROOT / "output"),
    }
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError:
        build_graph().invoke(graph_input)
        return
    with SqliteSaver.from_conn_string(str(store_path)) as checkpointer:
        build_graph(checkpointer).invoke(
            graph_input,
            config={"configurable": {"thread_id": event_id}},
        )


def main() -> int:
    """Parse the single-event CLI and return a shell-friendly exit code."""
    parser = argparse.ArgumentParser(description="Process one campaign event")
    parser.add_argument("event_path", type=Path)
    args = parser.parse_args()
    process_event(args.event_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"processing failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
