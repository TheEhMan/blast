import ast, os, pickle, subprocess, sys, json
from pathlib import Path
import coverage

ROOT = Path("/opt/sqlglot")
os.chdir(ROOT)
subprocess.run([sys.executable, "-m", "pytest", "tests", "-n", "4", "-q", "-p", "no:cacheprovider",
                "--cov=sqlglot", "--cov-context=test", "--cov-report=", "--timeout=300"], check=False)

cd = coverage.CoverageData(); cd.read()
line_map, file_map = {}, {}                      # file -> {line: set(testid)} ; file -> set(testid)
for f in cd.measured_files():
    rel = os.path.relpath(f, ROOT)
    per = {}
    for ln, ctxs in cd.contexts_by_lineno(f).items():
        ids = {c.split("|")[0] for c in ctxs if c}      # "" = import-time, not a test
        if ids: per[ln] = ids
    line_map[rel] = per
    file_map[rel] = set().union(*per.values()) if per else set()

# fixture -> test functions that mention its name as a string constant
fixture_map = {}
fixtures = [p for p in (ROOT / "tests/fixtures").rglob("*") if p.is_file()]
consts = {}                                              # test id -> set of string constants
for tf in (ROOT / "tests").rglob("test_*.py"):
    tree = ast.parse(tf.read_text())
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
            consts[f"{tf.relative_to(ROOT)}::{cls.name}::{fn.name}"] = {
                n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
for p in fixtures:
    rel = str(p.relative_to(ROOT)); names = {p.name, p.stem}
    fixture_map[rel] = {t for t, c in consts.items() if c & names}

# import graph: which tests live in which test file (for fallbacks)
pickle.dump({"line": line_map, "file": file_map, "fixture": fixture_map},
            open("/opt/map.pkl", "wb"))