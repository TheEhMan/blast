#!/usr/bin/env python3
"""Independent holdout mutations for Blast's arg_types narrow-selection hypothesis.

Run from a working Blast checkout with its SQLGlot profiler virtual environment.
Never changes selector.py, map.pkl.z, or the pinned SQLGlot checkout.
Creates/removes only fresh, named git worktrees beneath --work, and retains
patches, full pytest logs, and JSON results for inspection.

The tested selection rule is diagnostic ONLY: class readers observed in a baseline
are not a proof that all potential failing tests were observed.
"""

import argparse
import ast
import copy
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


@dataclass(frozen=True)
class Case:
    name: str
    path: str
    class_name: str
    attribute: str
    old: bool
    new: bool


CASES = (
    Case("column_table_required", "sqlglot/expressions/core.py", "Column", "table", False, True),
    Case("literal_is_string_optional", "sqlglot/expressions/core.py", "Literal", "is_string", True, False),
    Case("insert_this_required", "sqlglot/expressions/dml.py", "Insert", "this", False, True),
    Case("delete_where_required", "sqlglot/expressions/dml.py", "Delete", "where", False, True),
    Case("sequence_cache_required", "sqlglot/expressions/ddl.py", "SequenceProperties", "cache", False, True),
    Case("truncate_expressions_optional", "sqlglot/expressions/ddl.py", "TruncateTable", "expressions", True, False),
)


def run(command, **kwargs):
    return subprocess.run(command, check=True, text=True, **kwargs)


def argtypes_attribute(class_node):
    attrs = [
        n for n in class_node.body
        if isinstance(n, ast.Assign)
        and len(n.targets) == 1
        and isinstance(n.targets[0], ast.Name)
        and n.targets[0].id == "arg_types"
    ]
    if len(attrs) != 1:
        return None
    attr = attrs[0]
    value = attr.value
    if not isinstance(value, ast.Dict):
        return None
    if not all(
        isinstance(k, ast.Constant) and isinstance(k.value, str)
        and isinstance(v, ast.Constant) and isinstance(v.value, bool)
        for k, v in zip(value.keys, value.values)
    ):
        return None
    return attr


def eligible_class_key(S, patch):
    """Same strict AST-level gate as the earlier diagnostic."""
    files = S.parse_patch(patch)
    if len(files) != 1:
        return None
    f = files[0]
    path = f["path"]
    if not (path.startswith("sqlglot/") and path.endswith(".py")):
        return None
    if any(f[flag] for flag in ("new", "deleted", "binary", "renamed")):
        return None
    src = S.old_src(path)
    if src is None:
        return None
    new_lines = S.apply_hunks(src.split("\n"), f["hunks"])
    if new_lines is None:
        return None
    try:
        old_tree = ast.parse(src)
        new_tree = ast.parse("\n".join(new_lines))
    except SyntaxError:
        return None

    old_classes = {n.name: n for n in old_tree.body if isinstance(n, ast.ClassDef)}
    new_classes = {n.name: n for n in new_tree.body if isinstance(n, ast.ClassDef)}
    changed = []
    for name in old_classes.keys() & new_classes.keys():
        old_attr = argtypes_attribute(old_classes[name])
        new_attr = argtypes_attribute(new_classes[name])
        if old_attr is not None and new_attr is not None:
            if ast.dump(old_attr.value) != ast.dump(new_attr.value):
                changed.append(name)
    if len(changed) != 1:
        return None

    name = changed[0]
    old_normalized = copy.deepcopy(old_tree)
    new_normalized = copy.deepcopy(new_tree)
    for tree in (old_normalized, new_normalized):
        target = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
        argtypes_attribute(target).value = ast.Constant(value=None)
    if ast.dump(old_normalized) != ast.dump(new_normalized):
        return None
    return f"{path[:-3].replace('/', '.')}:{name}"


def mutate(source, case):
    """Edit precisely one AST-identified bool literal, preserving file layout."""
    tree = ast.parse(source)
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == case.class_name]
    if len(classes) != 1:
        raise ValueError(f"Class {case.class_name} missing or ambiguous")
    attr = argtypes_attribute(classes[0])
    if attr is None:
        raise ValueError(f"{case.class_name}.arg_types is not a literal dictionary")
    matching = [
        v for k, v in zip(attr.value.keys, attr.value.values)
        if isinstance(k, ast.Constant) and k.value == case.attribute
    ]
    if len(matching) != 1 or matching[0].value is not case.old:
        raise ValueError(f"Unexpected {case.class_name}.arg_types[{case.attribute!r}]")
    value = matching[0]
    lines = source.splitlines(keepends=True)
    data = lines[value.lineno - 1].encode("utf-8")
    expected = str(case.old).encode("ascii")
    if data[value.col_offset:value.end_col_offset] != expected:
        raise ValueError("Expected boolean token at AST location")
    replacement = str(case.new).encode("ascii")
    lines[value.lineno - 1] = (
        data[:value.col_offset] + replacement + data[value.end_col_offset:]
    ).decode("utf-8")
    result = "".join(lines)
    compile(result, case.path, "exec")
    return result


def parse_failures(log, S):
    failures, unmapped = set(), set()
    for line in log.splitlines():
        if not line.startswith(("FAILED ", "ERROR ", "SUBFAILED[", "SUBERROR[")):
            continue
        match = re.search(r"tests/[\w/.-]+\.py(?:::[\w]+)+", line)
        if not match:
            continue
        test_id = match.group(0)
        if test_id in S.NUM:
            failures.add(S.NUM[test_id])
        else:
            unmapped.add(test_id)
    return failures, unmapped


def cost(S, selected):
    workers = S.split(selected)
    return round(max((sum(S.SEC[n] for n in w) for w in workers), default=0) + 2 * len(workers), 3)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sqlglot", default="/home/ehman/sqlglot-pinned", type=Path)
    ap.add_argument("--work", default="/home/ehman/blast-class-holdouts-v1", type=Path)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=4,
                    help="Run the first N mutations (default 4, max 6)")
    ap.add_argument("--cases", nargs="*", default=None,
                    help="Explicit case names instead of --limit; see --help/source")
    args = ap.parse_args()

    if args.work.exists():
        ap.error(f"Refusing existing --work directory: {args.work}. Use a fresh path.")
    if args.work.resolve() == Path.cwd().resolve():
        ap.error("Work directory must be separate from the Blast repository")
    if args.work.resolve().is_relative_to(args.sqlglot.resolve()):
        ap.error("Work directory must be separate from the SQLGlot repository")
    if args.workers < 1:
        ap.error("--workers must be positive")
    if not 1 <= args.limit <= len(CASES):
        ap.error(f"--limit must be 1..{len(CASES)}")

    if args.cases is None:
        chosen = CASES[:args.limit]
    else:
        unknown = set(args.cases) - {c.name for c in CASES}
        if unknown or not args.cases:
            ap.error(f"Invalid --cases: {sorted(unknown)}; available: {[c.name for c in CASES]}")
        chosen = [c for c in CASES if c.name in set(args.cases)]

    root = Path(__file__).resolve().parent
    if not (root / "selector.py").is_file():
        ap.error("Save this script alongside selector.py in your Blast repository")
    os.environ.setdefault("BLAST_DATA", str(root / "sample"))
    os.environ.setdefault("BLAST_MAP", str(root / "map.pkl.z"))
    sys.path.insert(0, str(root))
    import selector as S
    S.FAMILY_FILES = ()
    S.init()
    if not S.M.get("class_readers"):
        ap.error("The production map has no class_readers")

    pinned = args.sqlglot.resolve()
    if not (pinned / ".git").exists():
        ap.error(f"SQLGlot pinned repository not found: {pinned}")
    run(["git", "-C", str(pinned), "cat-file", "-e", f"{PIN}^{{commit}}"])

    work = args.work.resolve()
    work.mkdir(parents=True)
    entries = []
    out = work / "results.json"
    print(f"Running {len(chosen)} independent mutations on clean pinned Git worktrees", flush=True)
    print("Files in Blast and sqlglot-pinned will not be changed.", flush=True)

    for i, case in enumerate(chosen, 1):
        print(f"\n[{i}/{len(chosen)}] {case.name}", flush=True)
        case_dir = work / case.name
        case_dir.mkdir()
        checkout = case_dir / "sqlglot"
        run(["git", "-C", str(pinned), "worktree", "add", "--detach", str(checkout), PIN],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            target = checkout / case.path
            pristine = target.read_text()
            old = S.old_src(case.path)
            if old is None or old.rstrip("\n") != pristine.rstrip("\n"):
                raise RuntimeError(f"Map baseline and pinned checkout differ for {case.path}")
            target.write_text(mutate(pristine, case))
            patch = run(["git", "-C", str(checkout), "diff", "--", case.path],
                        stdout=subprocess.PIPE).stdout
            if not patch.strip():
                raise RuntimeError("Empty mutation patch")
            (case_dir / "mutation.patch").write_text(patch)
            expected_key = f"{case.path[:-3].replace('/', '.')}:{case.class_name}"
            key = eligible_class_key(S, patch)
            if key != expected_key:
                raise RuntimeError(f"Strict gate rejected mutation: key={key!r}, expected={expected_key!r}")
            readers = S.M["class_readers"].get(key)
            if not readers:
                raise RuntimeError(f"No mapped readers for {key}; narrow selection should fallback")
            narrow = S.nums(readers)

            original_rule = S.class_reader_tests
            try:
                S.class_reader_tests = lambda f, analysis: set()
                broad = S.select(patch)
            finally:
                S.class_reader_tests = original_rule
            additive = S.select(patch)

            logpath = case_dir / "pytest.log"
            cmd = [sys.executable, "-m", "pytest", "tests", "-n", str(args.workers),
                   "-q", "-p", "no:cacheprovider", "--tb=no", "--timeout=300"]
            env = os.environ.copy()
            env["PYTHONPATH"] = str(checkout)
            env["SKIP_INTEGRATION"] = "0"
            with logpath.open("w") as f:
                completed = subprocess.run(cmd, cwd=checkout, env=env, stdout=f,
                                           stderr=subprocess.STDOUT, check=False)
            log = logpath.read_text(errors="replace")
            failures, unmapped = parse_failures(log, S)
            if completed.returncode not in (0, 1):
                raise RuntimeError(f"pytest exited {completed.returncode}; see {logpath}")
            if unmapped:
                raise RuntimeError(f"Unmapped failures ({len(unmapped)}): {sorted(unmapped)[:5]}")
            if completed.returncode == 1 and not failures:
                raise RuntimeError(f"pytest failed, but no numbered failures parsed; see {logpath}")
            summary_lines = [s.strip() for s in log.splitlines() if re.search(r"\d+ (?:failed|passed)", s)]
            summary_line = summary_lines[-1] if summary_lines else "summary not found"
            record = {
                "case": case.name,
                "class_key": key,
                "pytest_returncode": completed.returncode,
                "pytest_summary": summary_line,
                "failing": len(failures),
                "broad_tests": len(broad),
                "broad_missed": len(failures - broad),
                "broad_cost_s": cost(S, broad),
                "additive_tests": len(additive),
                "additive_missed": len(failures - additive),
                "additive_cost_s": cost(S, additive),
                "reader_tests": len(narrow),
                "reader_missed": len(failures - narrow),
                "reader_cost_s": cost(S, narrow),
                "reader_missed_ids": sorted(S.T[n]["id"] for n in failures - narrow),
                "additive_missed_ids": sorted(S.T[n]["id"] for n in failures - additive),
            }
            entries.append(record)
            out.write_text(json.dumps(entries, indent=2) + "\n")
            print(f"  {summary_line}", flush=True)
            print(f"  numbered failures: {len(failures)}, additive missed: "
                  f"{record['additive_missed']}, reader-only missed: {record['reader_missed']}", flush=True)
            print(f"  simulated costs: additive {record['additive_cost_s']:.3f}s, "
                  f"reader-only {record['reader_cost_s']:.3f}s", flush=True)
            if record["reader_missed_ids"]:
                print("  COUNTEREXAMPLE: " + ", ".join(record["reader_missed_ids"][:12]), flush=True)
                print("  Stopping because reader-only missed an actual failing test.", flush=True)
                break
        finally:
            # Only delete the worktree freshly created in this iteration.
            run(["git", "-C", str(pinned), "worktree", "remove", "--force", str(checkout)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print(f"\nRetained patches, logs, and comparison report: {work}", flush=True)
    print(f"Results: {out}", flush=True)
    if any(r["reader_missed"] for r in entries):
        print("RESULT: counterexample found. Do not deploy reader-only selection.", flush=True)
    elif all(r["failing"] > 0 for r in entries) and entries:
        print("RESULT: no misses on these holdouts. Still not a safety guarantee.", flush=True)
    else:
        print("RESULT: some mutations produced no failing tests; evidence is incomplete.", flush=True)


if __name__ == "__main__":
    main()
