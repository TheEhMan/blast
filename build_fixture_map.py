"""Build an allowed SQLGlot fixture-to-test index from the pinned source.

Run once locally against the pinned upstream checkout, then commit fixture_map.json.
The index is derived from tests/test_optimizer.py, not from labeled patch answers.
"""

import argparse
import ast
import json
import sys
from pathlib import Path


def create_index(source_root: Path) -> dict[str, list[str]]:
    source_file = source_root / "tests" / "test_optimizer.py"
    if not source_file.is_file():
        raise FileNotFoundError(f"Cannot find {source_file}")

    tree = ast.parse(source_file.read_text(encoding="utf-8"))
    index: dict[str, set[str]] = {}
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef) or cls.name != "TestOptimizer":
            continue
        for method in cls.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not method.name.startswith("test_"):
                continue
            test_id = f"tests/test_optimizer.py::{cls.name}::{method.name}"
            for node in ast.walk(method):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr != "check_file" or not node.args:
                    continue
                name = node.args[0]
                if isinstance(name, ast.Constant) and isinstance(name.value, str):
                    fixture_path = f"tests/fixtures/optimizer/{name.value}.sql"
                    index.setdefault(fixture_path, set()).add(test_id)

    if not index:
        raise RuntimeError("No literal check_file calls were found. Check pinned SQLGlot source.")
    return {key: sorted(values) for key, values in sorted(index.items())}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path.home() / "sqlglot-pinned")
    parser.add_argument("--output", type=Path, default=Path("fixture_map.json"))
    args = parser.parse_args()
    index = create_index(args.source)
    args.output.write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")
    print(f"Indexed {len(index)} optimizer SQL fixtures -> {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
