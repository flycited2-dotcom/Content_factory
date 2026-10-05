"""Restore the last validated bot entrypoint if a deployment drops owner controls."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path


def validate_controls(source: bytes) -> None:
    tree = ast.parse(source)
    functions = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    routed = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "handle_command"
                 and {"avito_fn", "generation_fn", "generation_state_fn", "sources_fn"} <= {k.arg for k in n.keywords}
                 for n in ast.walk(tree))
    callbacks = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    required = {"avito_fn", "avito_markup", "generation_fn", "generation_state_fn", "generation_markup",
                "make_sources_fn", "sources_markup", "toggle_tg_source"}
    if not required <= functions or "ControlMenu" not in names or not routed or not {"avito:", "generation:", "srctg:"} <= callbacks:
        raise ValueError("Bot entrypoint is missing Avito or generation controls, callbacks or owner menu")


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def protect(entrypoint: Path, state: Path) -> str:
    state.mkdir(parents=True, exist_ok=True)
    snapshot = state / "last-good.py"
    receipt = state / "last-good.json"
    raw = entrypoint.read_bytes()
    try:
        validate_controls(raw)
    except (ValueError, SyntaxError):
        good = snapshot.read_bytes()
        expected = json.loads(receipt.read_text(encoding="utf-8"))["sha256"]
        if hashlib.sha256(good).hexdigest() != expected:
            raise ValueError("Last good bot snapshot checksum mismatch")
        validate_controls(good)
        _write(state / ("rejected-" + hashlib.sha256(raw).hexdigest()[:16] + ".py"), raw)
        _write(entrypoint, good)
        return "restored_last_good_controls"
    _write(snapshot, raw)
    _write(receipt, json.dumps({"sha256": hashlib.sha256(raw).hexdigest()}).encode())
    return "controls_verified"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/opt/content-factory"))
    args = parser.parse_args()
    print(protect(args.root / "src/content_factory/bot/run.py", args.root / "state/bot-controls"))


if __name__ == "__main__":
    main()
