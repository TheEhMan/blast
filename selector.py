"""Conservative, fixture-aware test selector for Blast Radius.

The fixture map is generated from the pinned SQLGlot tests, not the sample labels.
For all changes not fully understood by that map, run the entire suite.
"""

import json
import sys
from pathlib import Path

DATA = Path("/data")
FIXTURES = Path("/app/fixture_map.json")


def split_optimally(tests: list[dict]) -> list[list[int]]:
    if not tests:
        return []
    durations = [(test["number"], round(test["seconds"] * 1000)) for test in tests]
    durations.sort(key=lambda item: (-item[1], item[0]))
    best_cost = None
    best_workers = None

    for worker_count in range(1, min(8, len(durations)) + 1):
        workers = [[] for _ in range(worker_count)]
        loads = [0] * worker_count
        for number, millis in durations:
            i = loads.index(min(loads))
            workers[i].append(number)
            loads[i] += millis
        cost = max(loads) + 2000 * worker_count
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best_workers = workers
    return best_workers or []


def changed_paths(patch_text: str) -> set[str]:
    paths = set()
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            paths.add(line[6:].strip())
        elif line.startswith("--- a/"):
            paths.add(line[6:].strip())
    return paths


def choose_tests(patch_text: str, tests: list[dict], by_id: dict, fixture_map: dict) -> list[dict]:
    paths = changed_paths(patch_text)
    prefix = "tests/fixtures/optimizer/"
    if paths and all(path.startswith(prefix) and path in fixture_map for path in paths):
        ids = set().union(*(fixture_map[path] for path in paths))
        # Unknown IDs indicate an incomplete index: don't risk a miss.
        if ids and all(test_id in by_id for test_id in ids):
            return [by_id[test_id] for test_id in sorted(ids)]
    return tests


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text(encoding="utf-8"))["tests"]
    fixture_map = json.loads(FIXTURES.read_text(encoding="utf-8"))
    by_id = {test["id"]: test for test in tests}
    for patch in sorted((DATA / "commits").glob("*.patch")):
        selected = choose_tests(patch.read_text(encoding="utf-8"), tests, by_id, fixture_map)
        workers = split_optimally(selected)
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))
    print(f"processed patches; optimizer fixture index entries: {len(fixture_map)}", file=sys.stderr)


if __name__ == "__main__":
    main()
