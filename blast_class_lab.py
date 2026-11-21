#!/usr/bin/env python3
"""Offline class-metadata evidence collector for Blast Radius. NOT a selector.

Never copy the instrumented SQLGlot tree or its ordinary line coverage into the
submission. This tool does not change selector.py, map.pkl.z, or Git branches.
Observed readers are positive dependency evidence, not proof that other tests
are unaffected. Reflection, cached import-time values, native code, and new
control-flow paths are not exhaustively modeled.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
ATTRS = {"arg_types", "required_args"}
MARKER_NAME = "_blast_metadata_probe.py"


def fail(message):
    raise RuntimeError(message)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def module_name(relative):
    parts = list(Path(relative).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def class_inventory(tree, module, path):
    rows = []
    def visit_body(body, parents):
        for node in body:
            if isinstance(node, ast.ClassDef):
                qual = ".".join([*parents, node.name])
                rows.append({"module": module, "qualname": qual, "path": path,
                             "key": module + ":" + qual, "line": node.lineno,
                             "bases": [ast.unparse(b) for b in node.bases]})
                visit_body(node.body, [*parents, node.name])
            elif isinstance(node, (ast.If, ast.Try)):
                visit_body(node.body, parents)
                visit_body(node.orelse, parents)
                if isinstance(node, ast.Try):
                    visit_body(node.finalbody, parents)
                    for handler in node.handlers:
                        visit_body(handler.body, parents)
    visit_body(tree.body, [])
    return rows


class ReadTransformer(ast.NodeTransformer):
    def __init__(self, allow_getattr=True):
        self.sites = []
        self.allow_getattr = allow_getattr

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load) and node.attr in ATTRS:
            self.sites.append({"line": node.lineno, "attribute": node.attr,
                               "kind": "attribute"})
            return ast.copy_location(ast.Call(
                func=ast.Name(id="_blast_meta_read", ctx=ast.Load()),
                args=[node.value, ast.Constant(node.attr)], keywords=[]), node)
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        if (self.allow_getattr and isinstance(node.func, ast.Name) and node.func.id == "getattr"
                and len(node.args) in (2, 3)
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in ATTRS and not node.keywords):
            self.sites.append({"line": node.lineno, "attribute": node.args[1].value,
                               "kind": "literal_getattr"})
            node.func = ast.copy_location(ast.Name(id="_blast_meta_read", ctx=ast.Load()),
                                         node.func)
        return node


def instrument_text(text, filename):
    tree = ast.parse(text, filename=filename)
    # A shadowed getattr may not have builtin semantics. Leave those calls alone.
    shadowed_getattr = any(
        (isinstance(n, ast.Name) and n.id == "getattr" and isinstance(n.ctx, ast.Store))
        or (isinstance(n, ast.arg) and n.arg == "getattr")
        or (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.name == "getattr")
        or (isinstance(n, ast.alias) and (n.asname or n.name.split(".")[0]) == "getattr")
        or (isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names))
        for n in ast.walk(tree)
    )
    transformer = ReadTransformer(allow_getattr=not shadowed_getattr)
    tree = transformer.visit(tree)
    if not transformer.sites:
        return text, []
    if "_blast_meta_read" in text:
        fail(f"Existing probe name in {filename}; refusing to overwrite it")
    pos = 0
    if (tree.body and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)):
        pos = 1
    while (pos < len(tree.body) and isinstance(tree.body[pos], ast.ImportFrom)
           and tree.body[pos].module == "__future__"):
        pos += 1
    tree.body.insert(pos, ast.ImportFrom(
        module="sqlglot._blast_metadata_probe",
        names=[ast.alias(name="read_metadata", asname="_blast_meta_read")], level=0))
    ast.fix_missing_locations(tree)
    result = ast.unparse(tree) + "\n"
    compile(result, filename, "exec")
    return result, transformer.sites


def make_marker_module(classes):
    lines = ['"""Generated test-only metadata access markers."""',
             "import builtins as _b", ""]
    records = []
    names = []
    for i, row in enumerate(classes):
        for attribute in sorted(ATTRS):
            name = f"_hit_{i}_{attribute}"
            hit_line = len(lines) + 2
            lines.extend([f"def {name}():", "    return None", ""])
            records.append({**row, "attribute": attribute, "hit_line": hit_line})
            names.append(((row["module"], row["qualname"], attribute), name))
    unknown_line = len(lines) + 2
    lines.extend(["def _unresolved():", "    return None", "", "_MARKERS = {"])
    for key, name in names:
        lines.append(f"    {key!r}: {name},")
    lines.extend(["}", "_CACHE = {}", "", '''def _record(obj, attribute):
    try:
        cls = obj if isinstance(obj, type) else type(obj)
        key = (cls, attribute)
        callbacks = _CACHE.get(key)
        if callbacks is None:
            callbacks = []
            mro = type.__getattribute__(cls, "__mro__")
            exact_class_known = False
            for index, base in enumerate(mro):
                module = type.__getattribute__(base, "__module__")
                qualname = type.__getattribute__(base, "__qualname__")
                callback = _MARKERS.get((module, qualname, attribute))
                if callback is not None:
                    callbacks.append(callback)
                    if index == 0:
                        exact_class_known = True
            # Still record mapped ancestors, but flag dynamic/unmapped classes.
            if not exact_class_known:
                callbacks.append(_unresolved)
            callbacks = tuple(callbacks)
            _CACHE[key] = callbacks
        for callback in callbacks:
            callback()
    except Exception:
        _unresolved()


def read_metadata(obj, attribute, *default):
    # Preserve one attribute lookup, including getattr's default/exception behavior.
    try:
        return _b.getattr(obj, attribute, *default)
    finally:
        _record(obj, attribute)
'''])
    return "\n".join(lines) + "\n", records, unknown_line


def patch_worker_tests(root):
    opt = root / "tests/test_optimizer.py"
    text = opt.read_text(encoding="utf-8")
    anchor = "def qualify_then_canonicalize("
    old = "future = self.pool.submit(parse_and_optimize, func, sql, dialect, **func_kwargs)"
    wrapper = '''def _blast_profile_optimizer(test_id, func, sql, read_dialect, **kwargs):
    import coverage
    import pickle
    cov = coverage.Coverage.current()
    if cov is None:
        raise RuntimeError("No active coverage in optimizer worker")
    cov.switch_context(test_id + "|optimizer-worker")
    try:
        result = parse_and_optimize(func, sql, read_dialect, **kwargs)
        pickle.dumps(result, protocol=pickle.HIGHEST_PROTOCOL)
        return result
    finally:
        cov.switch_context("")


'''
    if text.count(anchor) != 1 or text.count(old) != 1:
        fail("Pinned optimizer source does not match expected worker locations")
    text = text.replace(anchor, wrapper + anchor, 1)
    text = text.replace(old, '''future = self.pool.submit(
                _blast_profile_optimizer,
                "tests/test_optimizer.py::" + type(self).__name__ + "::" + self._testMethodName,
                func, sql, dialect, **func_kwargs,
            )''', 1)
    compile(text, str(opt), "exec")
    opt.write_text(text, encoding="utf-8")

    exe = root / "tests/test_executor.py"
    text = exe.read_text(encoding="utf-8")
    anchor = '@unittest.skipIf(SKIP_INTEGRATION, "Skipping Integration Tests since `SKIP_INTEGRATION` is set")'
    old = 'futures = [pool.submit(mp_execute, parse_one(sql), args) for args, sql, _ in sqls]'
    wrapper = '''def _blast_profile_executor(test_id, expression, meta):
    import coverage
    cov = coverage.Coverage.current()
    if cov is None:
        raise RuntimeError("No active coverage in executor worker")
    cov.switch_context(test_id + "|executor-worker")
    try:
        return mp_execute(expression, meta)
    finally:
        cov.switch_context("")


'''
    if text.count(anchor) != 1 or text.count(old) != 1:
        fail("Pinned executor source does not match expected worker locations")
    text = text.replace(anchor, wrapper + anchor, 1)
    submit_line = next(line for line in text.splitlines() if line.strip() == old)
    indent = submit_line[:len(submit_line) - len(submit_line.lstrip())]
    replacement = (
        'test_id = "tests/test_executor.py::" + type(self).__name__ + "::" + self._testMethodName\n'
        + indent + 'futures = [pool.submit(_blast_profile_executor, test_id, parse_one(sql), args)\n'
        + indent + '           for args, sql, _ in sqls]'
    )
    text = text.replace(old, replacement, 1)
    compile(text, str(exe), "exec")
    exe.write_text(text, encoding="utf-8")


def prepare(args):
    blast = args.blast.resolve()
    sqlglot = args.sqlglot.resolve()
    work = args.work.resolve()
    if work.exists():
        fail(f"{work} already exists. Use a new --work directory; nothing was overwritten.")
    if work.is_relative_to(blast) or work.is_relative_to(sqlglot):
        fail("--work must be outside both existing repositories")
    if git(sqlglot, "rev-parse", PIN).decode().strip() != PIN:
        fail("Pinned SQLGlot commit is unavailable")
    for filename in ("selector.py", "map.pkl.z", "score.py", "sample/tests.json",
                     "sample/answers.json"):
        if not (blast / filename).is_file():
            fail(f"Missing {blast / filename}")
    # Archive only the immutable commit. Existing profiling edits are not copied.
    archive = git(sqlglot, "archive", "--format=tar", PIN)
    work.mkdir(parents=True)
    original = work / "baseline"
    original.mkdir()
    for filename in ("selector.py", "map.pkl.z", "score.py"):
        shutil.copy2(blast / filename, original / filename)
    shutil.copytree(blast / "sample", original / "sample")
    tree = work / "sqlglot-profile"
    tree.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tf:
        tf.extractall(tree, filter="data")
    classes = []
    for p in sorted((tree / "sqlglot/expressions").rglob("*.py")):
        rel = p.relative_to(tree).as_posix()
        classes.extend(class_inventory(ast.parse(p.read_text()), module_name(rel), rel))
    # Duplicate definitions need a more precise identity than module + qualname.
    keys = [c["key"] for c in classes]
    if len(keys) != len(set(keys)):
        fail("Duplicate expression-class identities; probe setup is incomplete")
    marker, records, unknown = make_marker_module(classes)
    (tree / "sqlglot" / MARKER_NAME).write_text(marker, encoding="utf-8")
    patch_worker_tests(tree)
    files = []
    for subdir in ("sqlglot", "tests"):
        for p in sorted((tree / subdir).rglob("*.py")):
            if p.name == MARKER_NAME:
                continue
            text, sites = instrument_text(p.read_text(encoding="utf-8"), str(p))
            if sites:
                p.write_text(text, encoding="utf-8")
                files.append({"path": p.relative_to(tree).as_posix(), "sites": sites})
    collection = work / "collected"
    collection.mkdir()
    (work / "blast_collection.py").write_text('''import json, os
from pathlib import Path

def pytest_collection_modifyitems(items):
    root = Path(os.environ["BLAST_CLASS_COLLECTION"])
    (root / (str(os.getpid()) + ".json")).write_text(
        json.dumps([item.nodeid for item in items]))

def pytest_runtest_logreport(report):
    if report.skipped:
        root = Path(os.environ["BLAST_CLASS_COLLECTION"])
        with (root / ("skips-" + str(os.getpid()) + ".jsonl")).open("a") as fh:
            fh.write(json.dumps({"nodeid": report.nodeid, "when": report.when}) + "\\n")
''', encoding="utf-8")
    config = work / "profile.ini"
    config.write_text("[run]\nsource = sqlglot\nparallel = true\npatch = subprocess\ncore = ctrace\n",
                      encoding="utf-8")
    write_json(work / "manifest.json", {
        "purpose": "OFFLINE POSITIVE DEPENDENCY EVIDENCE ONLY; not a selector",
        "pinned_commit": PIN, "blast_repository": str(blast),
        "blast_commit": git(blast, "rev-parse", "HEAD").decode().strip(),
        "snapshot_sha256": {name: sha(original / name)
                            for name in ("selector.py", "map.pkl.z", "score.py")},
        "classes": classes, "marker_records": records, "unknown_marker_line": unknown,
        "instrumented_files": files,
        "limitations": ["No inference of safety from absent reads",
                        "Reflection and cached import-time values are not exhaustively modeled",
                        "Instrumented source lines MUST NOT be merged into map.pkl.z",
                        "Marker attribution alone does not establish hidden-test safety"],
    })
    print(f"Prepared isolated lab: {work}")
    print(f"Expression classes indexed: {len(classes)}")
    print(f"Files with metadata-read probes: {len(files)}")
    print("Existing repositories, selector, map, and branches were not modified.")


def get_work(args):
    work = args.work.resolve()
    manifest = read_json(work / "manifest.json")
    for name, expected in manifest["snapshot_sha256"].items():
        if sha(work / "baseline" / name) != expected:
            fail(f"Baseline snapshot changed: {name}")
    return work, manifest


def baseline(args):
    work, manifest = get_work(args)
    base = work / "baseline"
    env = os.environ.copy()
    env.update(BLAST_DATA=str(base / "sample"), BLAST_MAP=str(base / "map.pkl.z"))
    command = [sys.executable, "-c", "import selector as S; S.FAMILY_FILES = (); S.main()"]
    out = work / "baseline-output.jsonl"
    with out.open("wb") as stdout, (work / "baseline-stderr.log").open("wb") as stderr:
        result = subprocess.run(command, cwd=base, env=env, stdout=stdout, stderr=stderr)
    if result.returncode:
        fail(f"Baseline selector failed. See {work / 'baseline-stderr.log'}")
    scorer = load_module("_blast_lab_score", base / "score.py")
    tests = read_json(base / "sample/tests.json")["tests"]
    answers = read_json(base / "sample/answers.json")["commits"]
    score = scorer.evaluate_run(out.read_bytes(), tests, answers)
    write_json(work / "baseline-score.json", score)
    if not score.get("ok"):
        fail(f"Invalid baseline output: {score}")
    print(f"Precise snapshot: {score['simulated_milliseconds'] / 1000:.3f}s simulated")
    print(f"Misses: {len(score['missed_commits'])}; CPU not included")
    if score["missed_commits"]:
        fail("Baseline has sample misses; stop before profiling")
    if abs(score["simulated_milliseconds"] - 204185) > 1:
        fail("Expected the established 204.185s baseline; inspect the snapshot before continuing")
    print("Baseline verified. Canary fallback was overridden only in the subprocess.")


def profile(args):
    work, manifest = get_work(args)
    prior = read_json(work / "baseline-score.json")
    if (not prior.get("ok") or prior.get("missed_commits")
            or abs(prior.get("simulated_milliseconds", -1) - 204185) > 1):
        fail("Run a successful 204.185s baseline check before profiling")
    if (work / "profile-status.json").exists():
        fail("This lab already has a profiling attempt. Use a fresh --work directory for a rerun.")
    env = os.environ.copy()
    env.update(PYTHONPATH=str(work / "sqlglot-profile") + os.pathsep + str(work),
               SKIP_INTEGRATION="0", COVERAGE_RCFILE=str(work / "profile.ini"),
               COVERAGE_FILE=str(work / "metadata.coverage"),
               BLAST_CLASS_COLLECTION=str(work / "collected"), PYTEST_ADDOPTS="")
    env.pop("COVERAGE_PROCESS_START", None)
    command = [sys.executable, "-m", "pytest", "tests", "-n", str(args.workers),
               "-q", "-p", "no:cacheprovider", "-p", "blast_collection",
               "--cov=sqlglot", "--cov-context=test", "--cov-report=",
               "--cov-config=" + str(work / "profile.ini"), "--timeout=300"]
    write_json(work / "profile-status.json", {"state": "running", "command": command})
    print("Profiling the isolated baseline; no sample patches are applied.", flush=True)
    print(f"Complete pytest output: {work / 'profile.log'}", flush=True)
    with (work / "profile.log").open("w", encoding="utf-8") as log:
        result = subprocess.run(command, cwd=work / "sqlglot-profile", env=env,
                                stdout=log, stderr=subprocess.STDOUT, text=True)
    lines = (work / "profile.log").read_text(errors="replace").splitlines()
    print("\n".join(lines[-14:]))
    collected = set()
    for path in (work / "collected").glob("*.json"):
        collected.update(read_json(path))
    expected = {t["id"] for t in read_json(work / "baseline/sample/tests.json")["tests"]}
    missing = sorted(expected - collected)
    skipped = set()
    for path in (work / "collected").glob("skips-*.jsonl"):
        for line in path.read_text().splitlines():
            test_id = json.loads(line)["nodeid"]
            if test_id in expected:
                skipped.add(test_id)
    state = {"state": "passed" if not result.returncode and not missing and not skipped else "failed",
             "returncode": result.returncode, "numbered_tests": len(expected),
             "collected": len(collected), "missing_numbered_tests": missing,
             "skipped_numbered_tests": sorted(skipped)}
    write_json(work / "profile-status.json", state)
    if state["state"] != "passed":
        fail(f"Profiling did not pass all checks. See {work / 'profile-status.json'} and profile.log")
    print(f"Profiling passed; all {len(expected)} numbered tests were collected.")


def report(args):
    from coverage import CoverageData
    work, manifest = get_work(args)
    state = read_json(work / "profile-status.json")
    if state.get("state") != "passed":
        fail("A successful profiling run is required before interpreting the results")
    database = work / "metadata.coverage"
    if not database.is_file():
        fail(f"Combined coverage database is missing: {database}")
    data = CoverageData(basename=str(database))
    data.read()
    marker = [f for f in data.measured_files() if Path(f).name == MARKER_NAME]
    if len(marker) != 1:
        fail(f"Expected one measured marker module, found {len(marker)}")
    contexts = data.contexts_by_lineno(marker[0])
    tests = read_json(work / "baseline/sample/tests.json")["tests"]
    valid = {t["id"]: t for t in tests}
    rows = {}
    for record in manifest["marker_records"]:
        row = rows.setdefault(record["key"], {"path": record["path"],
                    "qualname": record["qualname"], "readers": set(), "attributes": {},
                    "unattributed_reads": False, "unrecognized_contexts": set()})
        cs = contexts.get(record["hit_line"], [])
        readers = {c.split("|", 1)[0] for c in cs if c.split("|", 1)[0] in valid}
        row["readers"].update(readers)
        row["attributes"][record["attribute"]] = sorted(readers)
        row["unattributed_reads"] |= "" in cs
        row["unrecognized_contexts"].update(c for c in cs if c and c.split("|", 1)[0] not in valid)
    for row in rows.values():
        row["readers"] = sorted(row["readers"])
        row["unrecognized_contexts"] = sorted(row["unrecognized_contexts"])
    summary = {"purpose": "Observed readers ONLY. Do not use absence of reads as a skip rule.",
               "pinned_commit": PIN, "classes": rows,
               "unresolved_contexts": sorted(contexts.get(manifest["unknown_marker_line"], [])),
               "limitations": manifest["limitations"]}
    write_json(work / "class-readers.json", summary)
    active = sum(bool(r["readers"]) for r in rows.values())
    if not active:
        fail("No class has attributed metadata reads. Do not interpret this as no dependencies.")
    print(f"Classes with attributed metadata reads: {active} / {len(rows)}")
    print(f"Unresolved lookup contexts: {len(summary['unresolved_contexts'])}")
    focus = [(key, row) for key, row in rows.items()
             if row["qualname"] == args.focus or key == args.focus]
    if not focus:
        fail(f"Class {args.focus!r} was not indexed; inspect class-readers.json")
    for key, row in focus:
        print(f"\nFOCUS: {key}")
        print(f"Observed numbered-test readers: {len(row['readers'])}")
        print(f"Unattributed/import-time read seen: {row['unattributed_reads']}")
        for attribute, readers in row["attributes"].items():
            print(f"  {attribute}: {len(readers)} test readers")
        for test_id in row["readers"][:25]:
            print(f"  {valid[test_id]['seconds']:.3f}s  {test_id}")
        if len(row["readers"]) > 25:
            print("  ... remaining readers are in class-readers.json")
        print("No readers observed is NOT proof that no test can fail.")
    print(f"\nReport: {work / 'class-readers.json'}")
    print("No production map or selector was changed. Do not merge rewritten-source line coverage.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--blast", type=Path, required=True)
    p.add_argument("--sqlglot", type=Path, required=True)
    p.add_argument("--work", type=Path, required=True)
    for name in ("baseline", "profile", "report"):
        p = sub.add_parser(name)
        p.add_argument("--work", type=Path, required=True)
        if name == "profile":
            p.add_argument("--workers", type=int, choices=range(1, 5), default=4)
        if name == "report":
            p.add_argument("--focus", default="TriggerProperties")
    args = parser.parse_args()
    if sys.version_info < (3, 12):
        parser.error("Use the existing Python 3.13 profiling environment (3.12+ required)")
    try:
        globals()[args.command](args)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
