"""Turn the first map.pkl into the one selector.py needs. Run once, then commit the result.

  docker run --rm -v "$PWD":/out -v "$PWD/selector.py":/s/selector.py \
     -v "$PWD/augment_map.py":/s/augment_map.py --entrypoint python \
     blast-radius-entry /s/augment_map.py --out /out/map.pkl
"""
import argparse
import ast
import pickle
import re
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import selector as S

ap = argparse.ArgumentParser()
ap.add_argument("--repo", default="/opt/sqlglot")
ap.add_argument("--map", default="/opt/map.pkl")
ap.add_argument("--out", required=True)
a = ap.parse_args()
repo = Path(a.repo)
old = pickle.load(open(a.map, "rb"))

vocab, vid = [], {}


def ix(i):
    if i not in vid:
        vid[i] = len(vocab)
        vocab.append(i)
    return vid[i]


def conv(ids):
    return frozenset(ix(i) for i in ids)


M = {
    "line": {p: {ln: conv(ids) for ln, ids in per.items()} for p, per in old["line"].items()},
    "file": {p: conv(ids) for p, ids in old["file"].items()},
    "fixture": {p: conv(ids) for p, ids in old["fixture"].items()},
    "src": {}, "sem": {},
}

for sub in ("sqlglot", "tests"):
    for p in sorted((repo / sub).rglob("*.py")):
        rel = str(p.relative_to(repo))
        text = p.read_text(encoding="utf-8")
        M["src"][rel] = zlib.compress(text.encode())
        try:
            M["sem"][rel] = S.analyze_old(text)
        except SyntaxError:
            print("skip", rel, file=sys.stderr)

# ---- dialect-name map: tests about <name>, found from the repo's own tests + fixtures
names = set()
for d in ("dialects", "parsers", "generators", "typing"):
    for p in (repo / "sqlglot" / d).glob("*.py"):
        if p.stem not in ("__init__", "dialect", "base", "helper"):
            names.add(p.stem)

consts, by_file = {}, {}
for tf in sorted((repo / "tests").rglob("test_*.py")):
    rel = str(tf.relative_to(repo))
    tree = ast.parse(tf.read_text(encoding="utf-8"))
    fns = [(None, n) for n in tree.body if isinstance(n, S.FUNC)]
    for c in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        fns += [(c.name, n) for n in c.body if isinstance(n, S.FUNC)]
    for cls, fn in fns:
        if not fn.name.startswith("test"):
            continue
        tid = f"{rel}::{cls}::{fn.name}" if cls else f"{rel}::{fn.name}"
        by_file.setdefault(rel, set()).add(tid)
        consts[tid] = [n.value for n in ast.walk(fn)
                       if isinstance(n, ast.Constant) and isinstance(n.value, str) and len(n.value) < 300]

fixtext = {}
for p in (repo / "tests" / "fixtures").rglob("*"):
    if p.is_file():
        try:
            fixtext[str(p.relative_to(repo))] = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            pass

M["names"] = {}
for name in names:
    w = re.compile(rf"\b{re.escape(name)}\b", re.I)
    ids = set(by_file.get(f"tests/dialects/test_{name}.py", ()))
    ids |= {tid for tid, cs in consts.items() if any(w.search(c) for c in cs)}
    dre = re.compile(rf"dialect:\s*[^\n]*\b{re.escape(name)}\b", re.I)
    for fp, txt in fixtext.items():
        if dre.search(txt):
            ids |= {vocab[i] for i in M["fixture"].get(fp, ())}
    M["names"][name] = conv(ids)

M["vocab"] = vocab
with open(a.out, "wb") as fh:
    pickle.dump(M, fh, protocol=5)
print("wrote", a.out, Path(a.out).stat().st_size // 1024, "KB;", len(vocab), "test ids;",
      len(M["sem"]), "files analysed", file=sys.stderr)