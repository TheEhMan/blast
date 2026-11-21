import json
import sys

sys.path.insert(0, "/app")
import selector as S

S.init()
ans = json.load(open("/answers.json"))["commits"]
vix = {n: i for i, n in enumerate(S.VN) if n is not None}
inv = {t["number"]: t["id"] for t in S.T}
cov_lines, cov_files = {}, {}
for p, per in S.M["line"].items():
    for ln, ixs in per.items():
        for i in ixs:
            cov_lines[i] = cov_lines.get(i, 0) + 1
            cov_files.setdefault(i, set()).add(p)
zero = [n for n in S.SEC if cov_lines.get(vix.get(n), 0) == 0]
print("tests with ZERO covered lines:", len(zero), "of", len(S.SEC))
print("  e.g.", [inv[n].split("/", 1)[-1] for n in zero[:12]])
for c, v in ans.items():
    text = open(f"/data/commits/{c}.patch", errors="replace").read()
    sel = S.select(text)
    miss = sorted(set(v["failing"]) - sel)
    if not miss:
        continue
    print(f"\n== {c} ({v['kind']}): selected {len(sel)}, failing {len(v['failing'])}, MISSING {len(miss)}")
    for f in S.parse_patch(text):
        p = f["path"]
        if not (p.startswith("sqlglot/") and p.endswith(".py")):
            print("  non-sqlglot file:", p, "| fixture tests:", len(S.M["fixture"].get(p, ())))
            continue
        r = S.analyze_change(f)
        print(f"  {p}: status={r.get('status')} skeleton_changed={r.get('skel')} changed_funcs={r.get('changed')}")
        per = S.M["line"].get(p, {})
        for k in r.get("changed", []):
            _, s, e = r["sem"]["funcs"][k]
            for l in sorted(x for x in r["old_touch"] if s <= x <= e):
                st = S.stmt_start(r["sem"]["stmts"], l)
                print(f"     func lines {s}-{e}; touched line {l} -> stmt starts {st}; tests covering it: {len(per.get(st, ()))}")
    for n in miss:
        i = vix.get(n)
        print(f"   MISSED {inv[n].split('/', 1)[-1]} ({S.SEC[n]}s) lines_covered={cov_lines.get(i, 0)} files_covered={len(cov_files.get(i, ()))}")
        for f in S.parse_patch(text):
            fl = S.M["file"].get(f["path"])
            if fl is not None:
                print(f"       in file-level coverage of {f['path']}: {i in fl}")
