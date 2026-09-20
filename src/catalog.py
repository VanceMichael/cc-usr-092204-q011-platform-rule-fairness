import json
from pathlib import Path


def load_context(path: Path | None = None) -> dict:
    source = path or Path(__file__).resolve().parents[1] / "fixtures" / "context.json"
    return json.loads(source.read_text(encoding="utf-8"))
