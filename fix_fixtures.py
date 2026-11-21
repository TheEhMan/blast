import argparse
import ast
import pickle
import sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--repo", required=True)
ap.add_argument("--map", required=True)
ap.add_argument("--out", required=True)
a = ap.parse_args()
repo = Path(a.repo).expanduser()
M = pickle.load(open(a.map, "rb"))
vocab = M["vocab"]
vid = {v: i for i, v in enumerate(vocab)}


def ix(i):
    if i not in vid:
        vid[i] = len(vocab)
        vocab.append(i)
    return vid[i]


FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


def strs(node):
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and len(n.value) < 300]


per_test, per_file, ids_of_file = {}, {}, {}
for tf in sorted((repo / "tests").rglob("test_*.py")):
    rel = str(tf.relative_to(repo))
    tree = ast.parse(tf.read_text(encoding="utf-8"))
    per_file[rel] = strs(tree)
    fns = [(None, n) for n in tree.body if isinstance(n, FUNC)]
    for c in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        fns += [(c.name, n) for n in c.body if isinstance(n, FUNC)]
    for cls, fn in fns:
        if fn.name.startswith("test"):
            tid = f"{rel}::{cls}::{fn.name}" if cls else f"{rel}::{fn.name}"
            per_test[tid] = strs(fn)
            ids_of_file.setdefault(rel, set()).add(tid)


def hit(c, keys):
    return c in keys or any(c.endswith("/" + k) for k in keys)


fixture, stats = {}, {"function": 0, "file": 0, "none": 0}
for p in sorted((repo / "tests" / "fixtures").rglob("*")):
    if not p.is_file():
        continue
    rel = str(p.relative_to(repo))
    keys = {p.name, p.stem}
    ids = {t for t, cs in per_test.items() if any(hit(c, keys) for c in cs)}
    if ids:
        stats["function"] += 1
    else:
        for fr, cs in per_file.items():
            if any(hit(c, keys) for c in cs):
                ids |= ids_of_file.get(fr, set())
        stats["file" if ids else "none"] += 1
    fixture[rel] = frozenset(ix(i) for i in ids)

M["fixture"] = fixture
M["vocab"] = vocab
pickle.dump(M, open(a.out, "wb"), protocol=5)
print("fixtures:", stats, file=sys.stderr)
for name in ("annotate_types.sql", "qualify_columns__invalid.sql", "qualify_columns_ddl.sql"):
    for rel, v in fixture.items():
        if rel.endswith(name):
            print(" ", rel, "->", sorted(vocab[i].split("::", 1)[-1] for i in v)[:6], file=sys.stderr)
