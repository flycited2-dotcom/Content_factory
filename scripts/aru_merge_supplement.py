"""Merge browser-captured ARU rows from sections missing in /vse-tovary/ into aru-catalog.json.

  python scripts/aru_merge_supplement.py --base state/prices/aru-catalog.json \
      --rows rows.json --output candidate.json

rows.json: {"authenticated": true, "items": [<card rows captured with scripts/aru_harvest.js>]}.
Writes the candidate only; replacing the working snapshot (with a backup) is a separate step.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from content_factory.ingest.aru_account import merge_supplement  # noqa: E402
from content_factory.ingest.aru_site import save_snapshot  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--rows", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = json.loads(args.base.read_text(encoding="utf-8"))
    captured = json.loads(args.rows.read_text(encoding="utf-8"))
    data = merge_supplement(
        base, captured["items"], authenticated=captured.get("authenticated") is True
    )
    save_snapshot(args.output, data)
    print(json.dumps({k: v for k, v in data.items() if k != "items"}, ensure_ascii=False))
    print("items", len(base["items"]), "->", len(data["items"]))


if __name__ == "__main__":
    main()
