#!/usr/bin/env python3
"""Offline Blast Radius diagnostics. Never import this from the submission.

Examples (run from a clone of TheEhMan/blast):
  python audit_blast.py --repo .
  python audit_blast.py --repo . --output output.jsonl
  python audit_blast.py --repo . --run-selector
  python audit_blast.py --repo . --ablate --report /tmp/blast-ablation.json

This script uses sample/answers.json ONLY for local evaluation. It never writes
an answer lookup table, modifies the repository, or changes selector.py. Ablation
modes are intentionally unsafe diagnostic experiments, NOT recommended settings.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def millis(value: float) -> int:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f'Invalid duration: {value!r}')
    return math.floor(value * 1000 + 0.5)


def pack(ids: set[int], duration: dict[int, int]) -> list[list[int]]:
    """Deterministic integer-millisecond LPT; try all useful worker counts."""
    if not ids:
        return []
    items = sorted(ids, key=lambda n: (-duration[n], n))
    best: tuple[int, int, list[list[int]]] | None = None
    for count in range(1, min(8, len(items)) + 1):
        loads = [0] * count
        workers: list[list[int]] = [[] for _ in range(count)]
        for n in items:
            i = min(range(count), key=lambda j: (loads[j], j))
            workers[i].append(n)
            loads[i] += duration[n]
        candidate = (max(loads) + 2000 * count, count, workers)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    assert best is not None
    return best[2]


def worker_cost(workers: list[list[int]], duration: dict[int, int]) -> int:
    occupied = [w for w in workers if w]
    return max((sum(duration[n] for n in w) for w in occupied), default=0) + 2000 * len(occupied)


def lower_bound(ids: set[int], duration: dict[int, int]) -> int:
    if not ids:
        return 0
    total = sum(duration[n] for n in ids)
    longest = max(duration[n] for n in ids)
    return min(max(longest, (total + k - 1) // k) + 2000 * k
               for k in range(1, min(8, len(ids)) + 1))


def normalize_workers(value: Any, duration: dict[int, int]) -> list[list[int]]:
    if not isinstance(value, list):
        raise ValueError('workers must be a list')
    result, seen = [], set()
    for group in value:
        if not isinstance(group, list):
            raise ValueError('each worker must be a list')
        out = []
        for n in group:
            if isinstance(n, bool) or not isinstance(n, (int, float)):
                raise ValueError(f'invalid test number: {n!r}')
            if isinstance(n, float) and (not math.isfinite(n) or not n.is_integer()):
                raise ValueError(f'invalid test number: {n!r}')
            n = int(n)
            if n not in duration or n in seen:
                raise ValueError(f'unknown or repeated test number: {n}')
            out.append(n)
            seen.add(n)
        if out:
            result.append(out)
    if len(result) > 8:
        raise ValueError('more than eight occupied workers')
    return result


def read_output(path: Path, commits: set[str], duration: dict[int, int]) -> dict[str, list[list[int]]]:
    raw = path.read_bytes()
    if len(raw) > 2_000_000:
        raise ValueError('output exceeds conservative 2 MB limit')
    lines = raw.decode('utf-8').split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    result = {}
    for i, line in enumerate(lines, 1):
        obj = json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        if not isinstance(obj, dict) or set(obj) != {'commit', 'workers'}:
            raise ValueError(f'wrong output shape at line {i}')
        cid = obj['commit']
        if not isinstance(cid, str) or cid not in commits or cid in result:
            raise ValueError(f'unknown or repeated commit at line {i}')
        result[cid] = normalize_workers(obj['workers'], duration)
    if set(result) != commits:
        raise ValueError(f'missing commits: {sorted(commits - set(result))}')
    return result


def evaluate(outputs: dict[str, list[list[int]]], answers: dict[str, Any],
             tests: list[dict[str, Any]], duration: dict[int, int]) -> dict[str, Any]:
    by_num = {t['number']: t for t in tests}
    optimizer = {t['number'] for t in tests if t['file'] == 'tests/test_optimizer.py'}
    rows, total, misses, scheduler_lb = [], 0, 0, 0
    for cid in sorted(answers):
        workers = normalize_workers(outputs[cid], duration)
        selected = {n for w in workers for n in w}
        failing = set(answers[cid]['failing'])
        missing = failing - selected
        cost = worker_cost(workers, duration)
        bound = lower_bound(selected, duration)
        misses += bool(missing)
        total += cost
        scheduler_lb += bound
        expensive = []
        for n in sorted(selected, key=lambda n: (-duration[n], n))[:8]:
            without = selected - {n}
            new_cost = worker_cost(pack(without, duration), duration)
            expensive.append({
                'test': by_num[n]['id'], 'seconds': duration[n] / 1000,
                'fails_in_sample': n in failing,
                'cost_reduction_if_removed_and_repacked_seconds': (cost - new_cost) / 1000,
            })
        rows.append({
            'commit': cid, 'selected_tests': len(selected), 'failing_tests': len(failing),
            'missing_tests': len(missing), 'workers': len(workers),
            'cost_seconds': cost / 1000, 'scheduling_lower_bound_seconds': bound / 1000,
            'selected_test_seconds': sum(duration[n] for n in selected) / 1000,
            'optimizer_tests_selected': len(selected & optimizer),
            'whole_optimizer_file_selected': bool(optimizer) and optimizer <= selected,
            'slow_selected_tests': expensive,
        })
    return {
        'simulated_seconds': (total + 300_000 * misses) / 1000,
        'selection_cost_seconds': total / 1000, 'missed_commits': misses,
        'scheduling_improvement_upper_bound_seconds': (total - scheduler_lb) / 1000,
        'whole_optimizer_file_commits': sum(r['whole_optimizer_file_selected'] for r in rows),
        'rows': sorted(rows, key=lambda r: (-r['cost_seconds'], r['commit'])),
    }


def run_modes(repo: Path, data: Path, map_path: Path, answers: dict[str, Any],
              tests: list[dict[str, Any]], duration: dict[int, int], ablate: bool) -> dict[str, Any]:
    selector_path = repo / 'selector.py'
    if not selector_path.is_file() or not map_path.is_file():
        raise FileNotFoundError('Need selector.py and map.pkl.z (or --map) to run the selector')
    spec = importlib.util.spec_from_file_location('_blast_audited_selector', selector_path)
    if spec is None or spec.loader is None:
        raise RuntimeError('Could not load selector.py')
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(repo))
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.DATA, module.MAP = data, map_path
    start = time.process_time()
    module.init()
    init_cpu = time.process_time() - start
    modes: list[tuple[str, dict[str, Any]]] = [('baseline', {})]
    if ablate:
        modes += [
            ('DIAGNOSTIC_no_optimizer_family', {'FAMILY_FILES': ()}),
            ('DIAGNOSTIC_no_name_rule', {'USE_NAME_RULE': False}),
            ('DIAGNOSTIC_gate_heavy_names', {'GATE_HEAVY_NAMES': True}),
            ('DIAGNOSTIC_trust_uncovered', {'TRUST_UNCOVERED': True}),
            ('DIAGNOSTIC_function_level', {'LINE_LEVEL': False}),
        ]
    keys = {k for _, changes in modes for k in changes}
    originals = {k: getattr(module, k) for k in keys}
    report: dict[str, Any] = {'map_init_current_process_cpu_seconds': init_cpu, 'modes': {}}
    for label, changes in modes:
        for key, value in originals.items():
            setattr(module, key, value)
        for key, value in changes.items():
            setattr(module, key, value)
        outputs = {}
        start = time.process_time()
        for cid in sorted(answers):
            text = (data / 'commits' / f'{cid}.patch').read_text(encoding='utf-8', errors='replace')
            selection = module.select(text)
            outputs[cid] = module.split(selection)
        elapsed = time.process_time() - start
        result = evaluate(outputs, answers, tests, duration)
        result['selection_current_process_cpu_seconds'] = elapsed
        report['modes'][label] = result
        print(f"{label}: simulated={result['simulated_seconds']:.3f}s; "
              f"misses={result['missed_commits']}; CPU(after init)={elapsed:.3f}s", file=sys.stderr)
    for key, value in originals.items():
        setattr(module, key, value)
    if isinstance(getattr(module, 'M', None), dict) and 'vocab' in module.M:
        covered = set()
        for per in module.M.get('line', {}).values():
            for ids in per.values():
                covered.update(ids)
        vocab = module.M['vocab']
        covered_ids = {vocab[i] for i in covered}
        report['coverage_audit'] = {
            'numbered_tests_without_line_context': sum(t['id'] not in covered_ids for t in tests),
            'optimizer_tests_without_line_context': sum(
                t['file'] == 'tests/test_optimizer.py' and t['id'] not in covered_ids for t in tests),
            'note': 'A missing context is a diagnostic, not proof that a test is unaffected.',
        }
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--repo', type=Path, default=Path('.'))
    ap.add_argument('--data', type=Path)
    ap.add_argument('--map', dest='map_path', type=Path)
    ap.add_argument('--output', type=Path, help='Existing selector output.jsonl to audit')
    ap.add_argument('--run-selector', action='store_true')
    ap.add_argument('--ablate', action='store_true', help='Run unsafe diagnostic ablations locally')
    ap.add_argument('--report', type=Path, default=Path('/tmp/blast-audit.json'))
    args = ap.parse_args()
    repo = args.repo.resolve()
    data = (args.data or repo / 'sample').resolve()
    tests = load_json(data / 'tests.json')['tests']
    answers = load_json(data / 'answers.json')['commits']
    duration = {t['number']: millis(t['seconds']) for t in tests}
    if len(duration) != len(tests):
        raise ValueError('duplicate test numbers in tests.json')
    if {p.stem for p in (data / 'commits').glob('*.patch')} != set(answers):
        raise ValueError('sample answers and patch files disagree')
    bounds, feasible = [], []
    for a in answers.values():
        ids = set(a['failing'])
        if not ids <= duration.keys():
            raise ValueError('unknown failing test in sample answers')
        bounds.append(lower_bound(ids, duration))
        feasible.append(worker_cost(pack(ids, duration), duration))
    files = defaultdict(set)
    for t in tests:
        files[t['file']].add(t['number'])
    report: dict[str, Any] = {
        'purpose': 'OFFLINE ANALYSIS ONLY. Do not copy sample-label-derived data into the submission.',
        'test_count': len(tests), 'commit_count': len(answers),
        'breaking_commits': sum(bool(a['failing']) for a in answers.values()),
        'nonbreaking_commits': sum(not a['failing'] for a in answers.values()),
        'sample_zero_miss_cost_lower_bound_seconds': sum(bounds) / 1000,
        'sample_failing_only_feasible_cost_seconds': sum(feasible) / 1000,
        'sample_zero_miss_minimum_proved_exact': bounds == feasible,
        'file_costs': sorted([
            {'file': path, 'tests': len(ids), 'test_seconds': sum(duration[n] for n in ids) / 1000,
             'lpt_cost_seconds': worker_cost(pack(ids, duration), duration) / 1000,
             'lower_bound_seconds': lower_bound(ids, duration) / 1000}
            for path, ids in files.items()
        ], key=lambda row: -row['test_seconds']),
        'slowest_tests': sorted(tests, key=lambda t: -t['seconds'])[:20],
    }
    if args.output:
        outputs = read_output(args.output, set(answers), duration)
        report['existing_output'] = evaluate(outputs, answers, tests, duration)
    if args.run_selector or args.ablate:
        report['selector_experiments'] = run_modes(
            repo, data, (args.map_path or repo / 'map.pkl.z').resolve(),
            answers, tests, duration, args.ablate)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(f"Tests: {len(tests)}; sample commits: {len(answers)}")
    print(f"Zero-miss sample lower bound: {sum(bounds)/1000:.3f}s; "
          f"exact={bounds == feasible}; CPU excluded")
    if 'existing_output' in report:
        r = report['existing_output']
        print(f"Existing output: {r['simulated_seconds']:.3f}s, {r['missed_commits']} misses")
    print(f'Report: {args.report}')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, ImportError, RuntimeError) as exc:
        print(f'Audit failed: {exc}', file=sys.stderr)
        sys.exit(1)
