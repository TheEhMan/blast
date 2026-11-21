#!/usr/bin/env python3
"""Offline-only class arg_types selection experiment. Does not edit selector or map."""
import ast
import copy
import json
import re
from pathlib import Path
import selector as S

S.FAMILY_FILES = ()
S.init()
WORK = Path('/home/ehman/blast-class-lab-v1')


def argtypes_attribute(class_node):
    attrs = [
        n for n in class_node.body
        if isinstance(n, ast.Assign)
        and len(n.targets) == 1
        and isinstance(n.targets[0], ast.Name)
        and n.targets[0].id == 'arg_types'
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


def eligible_class_key(patch):
    """Conservatively recognize one pure literal arg_types edit in one file."""
    files = S.parse_patch(patch)
    if len(files) != 1:
        return None
    f = files[0]
    path = f['path']
    if not (path.startswith('sqlglot/') and path.endswith('.py')):
        return None
    if any(f[flag] for flag in ('new', 'deleted', 'binary', 'renamed')):
        return None

    src = S.old_src(path)
    if src is None:
        return None
    new_lines = S.apply_hunks(src.split('\n'), f['hunks'])
    if new_lines is None:
        return None
    try:
        old_tree = ast.parse(src)
        new_tree = ast.parse('\n'.join(new_lines))
    except SyntaxError:
        return None

    old_classes = {
        n.name: n for n in old_tree.body if isinstance(n, ast.ClassDef)
    }
    new_classes = {
        n.name: n for n in new_tree.body if isinstance(n, ast.ClassDef)
    }

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
    # Compare complete ASTs after replacing only this attribute's value.
    # Any other change forces a fallback to the production selector.
    old_normalized = copy.deepcopy(old_tree)
    new_normalized = copy.deepcopy(new_tree)
    for tree in (old_normalized, new_normalized):
        target_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
        argtypes_attribute(target_class).value = ast.Constant(value=None)
    if ast.dump(old_normalized) != ast.dump(new_normalized):
        return None
    return f"{path[:-3].replace('/', '.')}:{name}"


def experimental_selection(patch):
    key = eligible_class_key(patch)
    if key is None:
        return None, S.select(patch)
    indices = S.M.get('class_readers', {}).get(key)
    if not indices:
        return None, S.select(patch)
    return key, S.nums(indices)


def score_seconds(selected):
    workers = S.split(selected)
    return round(max((sum(S.SEC[n] for n in w) for w in workers), default=0) + 2 * len(workers), 3)


def parse_failures(log_path):
    failures, unmapped = set(), set()
    for line in log_path.read_text().splitlines():
        if not line.startswith(('FAILED ', 'ERROR ', 'SUBFAILED[', 'SUBERROR[')):
            continue
        match = re.search(r'tests/[\w/.-]+\.py(?:::[\w]+)+', line)
        if not match:
            continue
        test_id = match.group(0)
        if test_id in S.NUM:
            failures.add(S.NUM[test_id])
        else:
            unmapped.add(test_id)
    return failures, unmapped


print('INDEPENDENT MUTATIONS (diagnostic only)')
for name, patch_name, log_name in (
    ('TriggerProperties', 'constraint-deletion.patch', 'constraint-validation.log'),
    ('Create', 'create-kind-deletion.patch', 'create-validation.log'),
    ('Update', 'update-this-required.patch', 'update-validation.log'),
):
    patch = (WORK / patch_name).read_text()
    key, selected = experimental_selection(patch)
    if key is None:
        raise SystemExit(f'{name}: narrow policy was not eligible; do not assume a result')
    failures, unmapped = parse_failures(WORK / log_name)
    if unmapped or not failures:
        raise SystemExit(f'{name}: unknown test IDs or no failures parsed; verify log')
    missing = failures - selected
    print(f'{name:19} failing={len(failures):3} selected={len(selected):3} '
          f'missed={len(missing):3} hypothetical_cost={score_seconds(selected):7.3f}s')
    if missing:
        print('  MISSED:', ', '.join(sorted(S.T[n]['id'] for n in missing)[:10]))

print('\nSAMPLE (diagnostic only)')
output = Path('/tmp/blast-argtypes-diagnostic.jsonl')
affected = 0
with output.open('w') as out:
    for path in sorted(Path('sample/commits').glob('*.patch')):
        patch = path.read_text()
        key, selected = experimental_selection(patch)
        if key is not None:
            original = S.select(patch)
            affected += 1
            print(f'{path.stem}: {key}  {len(original)} -> {len(selected)} tests; '
                  f'{score_seconds(original):.3f}s -> {score_seconds(selected):.3f}s')
        out.write(json.dumps({'commit': path.stem, 'workers': S.split(selected)}) + '\n')
print('Eligible sample commits:', affected)
print('Diagnostic output:', output)
print('No production files modified. Do NOT submit this candidate.')
