"""Blast Radius selector.

Pick the tests a patch can break, using maps built from the pinned sqlglot commit:
  * line coverage per test (which tests executed which line),
  * the AST of every original file (to tell a real change from a comment),
  * which tests read which fixture file,
  * which tests concern which dialect name.

Everything is derived from the pinned repo, never from the patches.
"""
import ast
import bisect
import hashlib
import json
import os
import pickle
import re
import sys
import zlib
from pathlib import Path

sys.setrecursionlimit(20000)

DATA = Path(os.environ.get("BLAST_DATA", "/data"))
MAP = Path(os.environ.get("BLAST_MAP", "/opt/map.pkl.z"))

# ---- tuning knobs (change one at a time, re-run `make sample`) ----------------
LINE_LEVEL = True         # look up the changed statement; else union over the whole function
TRUST_UNCOVERED = False   # changed statement covered by no test: True = run nothing (risky)
USE_NAME_RULE = True      # module-level change in dialects/<x>.py etc. -> tests about <x>
GATE_HEAVY_NAMES = False  # milestone 3: keep slow tests out of the name rule
HEAVY_SECONDS = 1.0
# --------------------------------------------------------------------------------

NAME_RE = re.compile(r"^sqlglot/(dialects|parsers|generators|typing)/([A-Za-z0-9_]+)\.py$")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)

M = T = NUM = SEC = VN = ALL = BYFILE = None


# ============================ AST helpers (also used by augment_map.py) ==========
def md5(s):
    return hashlib.md5(s.encode()).hexdigest()[:16]


class _Skel(ast.NodeTransformer):
    """Replace every function body by `pass`: what is left is the module-level shape."""

    def visit_FunctionDef(self, node):
        node.body = [ast.Pass()]
        return node

    visit_AsyncFunctionDef = visit_FunctionDef


def skeleton_hash(tree):  # mutates tree: call it last
    return md5(ast.dump(_Skel().visit(tree)))


def collect_funcs(tree):
    """{(qualname, occurrence): FunctionDef node}, nested functions included."""
    funcs, seen = {}, {}

    def rec(node, stack):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, FUNC):
                q = ".".join(stack + [ch.name])
                i = seen.get(q, 0)
                seen[q] = i + 1
                funcs[(q, i)] = ch
                rec(ch, stack + [ch.name])
            elif isinstance(ch, ast.ClassDef):
                rec(ch, stack + [ch.name])
            else:
                rec(ch, stack)

    rec(tree, [])
    return funcs


def body_hash(fn):
    return md5(ast.dump(ast.Module(body=fn.body, type_ignores=[])))


def analyze_old(src):
    tree = ast.parse(src)
    funcs = {k: (body_hash(f), f.body[0].lineno, f.end_lineno) for k, f in collect_funcs(tree).items()}
    stmts = sorted((n.lineno, n.end_lineno) for n in ast.walk(tree) if isinstance(n, ast.stmt))
    return {"funcs": funcs, "stmts": stmts, "skel": skeleton_hash(tree)}


# ============================ patch parsing ======================================
def parse_patch(text):
    files, cur, hunk = [], None, None
    rem_old = rem_new = 0
    for raw in text.split("\n"):
        l = raw.rstrip("\r")
        if l.startswith("diff --git "):
            a, _, _ = l[11:].partition(" b/")
            cur = {"path": a[2:] if a.startswith("a/") else a, "new": False, "deleted": False,
                   "binary": False, "renamed": False, "hunks": []}
            files.append(cur)
            hunk, rem_old, rem_new = None, 0, 0
            continue
        if cur is None:
            continue
        if hunk is not None and (rem_old > 0 or rem_new > 0):
            tag = l[:1] if l else " "
            if tag == "\\":
                continue
            if tag not in "+- ":
                rem_old = rem_new = 0
            else:
                hunk["lines"].append((tag, l[1:]))
                if tag in " -":
                    rem_old -= 1
                if tag in " +":
                    rem_new -= 1
                continue
        m = HUNK_RE.match(l)
        if m:
            os_, ol, ns, nl = m.groups()
            hunk = {"os": int(os_), "ol": 1 if ol is None else int(ol),
                    "ns": int(ns), "nl": 1 if nl is None else int(nl), "lines": []}
            rem_old, rem_new = hunk["ol"], hunk["nl"]
            cur["hunks"].append(hunk)
        elif hunk is None:
            if l.startswith("new file mode"):
                cur["new"] = True
            elif l.startswith("deleted file mode"):
                cur["deleted"] = True
            elif l.startswith("rename ") or l.startswith("copy "):
                cur["renamed"] = True
            elif l.startswith("Binary files") or l.startswith("GIT binary patch"):
                cur["binary"] = True
    return files


def touched(hunks):
    """Old-file and new-file line numbers a patch touches (insert/delete points include neighbours)."""
    old, new = set(), set()
    for h in hunks:
        o = h["os"] if h["ol"] > 0 else h["os"] + 1
        n = h["ns"] if h["nl"] > 0 else h["ns"] + 1
        for tag, _ in h["lines"]:
            if tag == " ":
                o += 1
                n += 1
            elif tag == "-":
                old.add(o)
                new.update((n - 1, n))
                o += 1
            else:
                new.add(n)
                old.update((o - 1, o))
                n += 1
    return old, new


def apply_hunks(old_lines, hunks):
    """New file as a list of lines, or None if the patch does not fit the original."""
    out, pos = [], 0
    for h in hunks:
        start = h["os"] - 1 if h["ol"] > 0 else h["os"]
        if start < pos or start > len(old_lines):
            return None
        out.extend(old_lines[pos:start])
        pos = start
        for tag, c in h["lines"]:
            if tag == "+":
                out.append(c)
                continue
            if pos >= len(old_lines) or old_lines[pos].rstrip("\r") != c.rstrip("\r"):
                return None
            if tag == " ":
                out.append(old_lines[pos])
            pos += 1
    out.extend(old_lines[pos:])
    return out


# ============================ runtime helpers ====================================
def init():
    global M, T, NUM, SEC, VN, ALL, BYFILE
    raw = MAP.read_bytes()
    M = pickle.loads(zlib.decompress(raw) if MAP.suffix == ".z" else raw)
    T = json.loads((DATA / "tests.json").read_text())["tests"]
    NUM = {t["id"]: t["number"] for t in T}
    SEC = {t["number"]: t["seconds"] for t in T}
    ALL = set(SEC)
    BYFILE = {}
    for t in T:
        BYFILE.setdefault(t["file"], set()).add(t["number"])
    VN = [NUM.get(i) for i in M["vocab"]]
    global TINY
    TINY = nums(M.get("tiny", ()))


def nums(ixs):
    return {VN[i] for i in ixs if VN[i] is not None}


def old_src(path):
    z = M["src"].get(path)
    return zlib.decompress(z).decode() if z else None


def hits(sorted_lines, lo, hi):
    i = bisect.bisect_left(sorted_lines, lo)
    return i < len(sorted_lines) and sorted_lines[i] <= hi


def stmt_start(stmts, line):
    best = None
    for s, e in stmts:
        if s > line:
            break
        if line <= e and (best is None or s > best[0] or (s == best[0] and e < best[1])):
            best = (s, e)
    return best[0] if best else line


def analyze_change(f):
    """What did this patch change in one .py file?
    -> {"status": "ok"|"syntax"|"unknown", "skel": bool, "changed": [keys], "sem": ..., "old_touch": set}"""
    path = f["path"]
    sem, src = M["sem"].get(path), old_src(path)
    if sem is None or src is None:
        return {"status": "unknown"}
    new_lines = apply_hunks(src.split("\n"), f["hunks"])
    if new_lines is None:
        return {"status": "unknown"}
    try:
        tree = ast.parse("\n".join(new_lines))
    except SyntaxError:
        return {"status": "syntax"}
    old_touch, new_touch = touched(f["hunks"])
    tn = sorted(new_touch)
    changed = []
    for key, fn in collect_funcs(tree).items():
        if hits(tn, fn.lineno, fn.end_lineno):
            o = sem["funcs"].get(key)
            if o is not None and o[0] != body_hash(fn):
                changed.append(key)
    skel = skeleton_hash(tree) != sem["skel"]   # last: it mutates the tree
    return {"status": "ok", "skel": skel, "changed": changed, "sem": sem, "old_touch": old_touch}


# ============================ per-kind selection =================================
def module_level_tests(path):
    """Import-time / signature / class-attribute change: coverage cannot attribute it to a test."""
    s = nums(M["file"].get(path, ()))
    m = NAME_RE.match(path)
    if USE_NAME_RULE and m:
        n = nums(M["names"].get(m.group(2), ()))
        if GATE_HEAVY_NAMES:
            n = {x for x in n if SEC[x] < HEAVY_SECONDS}
        s |= n
    return s



def class_reader_tests(f, analysis):
    """Add observed readers when class-level structure changes."""
    if not analysis["skel"]:
        return set()

    path = f["path"]
    old = old_src(path)
    if old is None:
        return set()

    new_lines = apply_hunks(old.split("\n"), f["hunks"])
    if new_lines is None:
        return set()

    old_touch, new_touch = touched(f["hunks"])
    module = path[:-3].replace("/", ".")
    dependencies = M.get("class_readers", {})
    indices = set()

    for source, changed_lines in (
        (old, old_touch),
        ("\n".join(new_lines), new_touch),
    ):
        if not changed_lines:
            continue

        tree = ast.parse(source)

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue

            if any(
                node.lineno <= line <= node.end_lineno
                for line in changed_lines
            ):
                key = f"{module}:{node.name}"
                indices.update(dependencies.get(key, ()))

    return nums(indices)



def func_tests(per, stmts, start, end, old_touch):
    lines = [l for l in old_touch if start <= l <= end]
    ix = set()
    if LINE_LEVEL and lines:
        for l in lines:
            ix |= per.get(stmt_start(stmts, l), frozenset())
        if ix or TRUST_UNCOVERED:
            return nums(ix)
    for l in range(start, end + 1):
        ix |= per.get(l, frozenset())
    return nums(ix)


# Coverage under-records these test files, so one hit means run the whole file.
FAMILY_FILES = ()


def expand_family(chosen):
    for fpath in FAMILY_FILES:
        base = BYFILE.get(fpath, set())
        if chosen & base:
            chosen = chosen | base
    return chosen


def sel_sqlglot(f):
    path = f["path"]
    if f["deleted"] or f["binary"] or f["renamed"]:
        return set(ALL)
    if f["new"]:
        return module_level_tests(path)
    r = analyze_change(f)
    if r["status"] != "ok":
        return set(ALL)          # unparsable or does not fit: be safe
    out = set()
    if r["skel"]:
        out |= module_level_tests(path)
        out |= class_reader_tests(f, r)
    per = M["line"].get(path, {})
    for key in r["changed"]:
        _, start, end = r["sem"]["funcs"][key]
        out |= func_tests(per, r["sem"]["stmts"], start, end, r["old_touch"])
    if not r["skel"] and not r["changed"] and not r["old_touch"]:
        return set()
    return expand_family(out)    # identical AST (comment/whitespace only) -> empty


def sel_test_file(f):
    path = f["path"]
    base = BYFILE.get(path, set())
    if f["deleted"] or f["binary"] or f["renamed"] or f["new"]:
        return base
    r = analyze_change(f)
    if r["status"] != "ok":
        return base
    if r["skel"]:
        return base
    out = set()
    for q, _ in r["changed"]:
        parts, found = q.split("."), None
        for n in range(len(parts), 0, -1):
            found = NUM.get(path + "::" + "::".join(parts[:n]))
            if found is not None:
                break
        if found is None:
            return base          # a helper changed: run the whole file
        out.add(found)
    return out


def sel_fixture(f):
    ix = M["fixture"].get(f["path"])
    if ix:
        return nums(ix)
    if f["path"].startswith("tests/fixtures/optimizer/"):   # unmapped optimizer fixture:
        base = BYFILE.get("tests/test_optimizer.py")         # its own test file, not the suite
        if base:
            return set(base)
    return set(ALL)


def harmless(path):
    if path.startswith((".github/", "docs/")):
        return True
    return path.endswith((".md", ".rst", ".txt")) and not path.startswith(("tests/", "sqlglot/"))


def select(text):
    chosen = set()
    for f in parse_patch(text):
        p = f["path"]
        try:
            if p.startswith("tests/fixtures/"):
                s = sel_fixture(f)
            elif p.startswith("sqlglot/") and p.endswith(".py"):
                s = sel_sqlglot(f)
            elif p.startswith("tests/") and p.endswith(".py") and os.path.basename(p).startswith("test_"):
                s = sel_test_file(f)
            elif harmless(p):
                s = set()
            else:
                s = set(ALL)
        except Exception as e:  # never crash: fall back to everything
            print(f"fallback ALL for {p}: {e!r}", file=sys.stderr)
            s = set(ALL)
        chosen |= s
    # end of select(), replacing the existing tail
    if not chosen:
        chosen = set(ALL)
    else:
        chosen |= TINY
    return chosen


def split(nums_):
    """Longest first onto the lightest worker; pick the worker count minimising max + 2k."""
    if not nums_:
        return []
    items = sorted(nums_, key=lambda n: -SEC[n])
    best = None
    for k in range(1, 9):
        w, load = [[] for _ in range(k)], [0.0] * k
        for n in items:
            i = load.index(min(load))
            w[i].append(n)
            load[i] += SEC[n]
        cost = max(load) + 2 * k
        if best is None or cost < best[0]:
            best = (cost, [x for x in w if x])
    return best[1]


def main():
    init()
    for p in sorted((DATA / "commits").glob("*.patch")):
        try:
            sel = select(p.read_text(errors="replace"))
        except Exception as e:
            print(f"fallback ALL for {p.stem}: {e!r}", file=sys.stderr)
            sel = set(ALL)
        print(json.dumps({"commit": p.stem, "workers": split(sel)}))
        print(f"{p.stem}: {len(sel)} tests, {sum(SEC[n] for n in sel):.1f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
