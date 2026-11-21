# Blast Radius optimization audit

Inspected repository: TheEhMan/blast

Inspected commit: 7e6f6f8aabe3a483ddda933f8dcbbc5ea2e6c67e

## Status

This is an OFFLINE diagnostic kit, not a replacement submission. No GitHub changes
or competition submissions were made. It does not contain a patch-to-answer lookup.
Do not import these diagnostics into selector.py or COPY sample labels/reports into
the runtime image.

The supplied sample/tests.json and sample/answers.json were checked against the
current repository's Git blob hashes after normalizing uploaded CRLF line endings.
The files match. The current binary map could not be downloaded through the
available connector, and Docker was unavailable. Consequently, the current
257.081-second result was NOT independently reproduced and no new submission score
is claimed.

## Verified numerical findings

- Sample: 1,620 numbered tests, 30 commits, 18 breaking and 12 nonbreaking.
- Exact minimum simulated cost for a zero-miss answer on THIS SAMPLE: 94.351 s.
  This uses sample labels for offline analysis only. It is not a viable selector.
- Target 130.8 s leaves 36.449 s above that sample floor, before CPU.
- Reducing a 257.081 s simulated result below 130.8 s requires saving more than
  126.281 s (about 49.12%). Sample and hidden-batch totals must not be conflated.
- Selecting the entire tests/test_optimizer.py file means 167 tests and 19.818 s
  of recorded work. Its optimal standalone simulated cost is 12.606 s on 3 workers.
- test_max_nodes: 28.817 s; test_execute_tpcds: 21.454 s;
  test_generator_memory: 6.686 s. These tests are indivisible scheduling tasks.
- The current selector already tries 1 through 8 workers with longest-first
  allocation. Choosing more workers indiscriminately is not the main opportunity.

### How the floor is proved

For a nonempty test set, with total duration W and longest test P (milliseconds),
any allocation to k workers costs at least max(P, ceil(W/k)) + 2000*k.
Take the minimum for k=1..min(8, number_of_tests). For every sample commit's
failing-test set, the script's feasible longest-first schedule achieves that bound.
Therefore their summed 94.351 s is exact for zero misses, excluding runtime CPU.
This does not establish any bound for hidden practice or final patches.

## Highest-priority repository finding

The pinned SQLGlot tests/test_optimizer.py creates a ProcessPoolExecutor using
spawn. Its check_file method submits parse_and_optimize jobs to that pool.
The repository's build_map.py does not explicitly propagate the owning test ID
into each submitted child task. The selector compensates for under-recorded
optimizer coverage by expanding any optimizer hit to the whole optimizer file.

This is a strong causal hypothesis, not proof about the inaccessible binary map.
First audit/rebuild per-test child coverage; only then consider replacing the
blanket optimizer expansion with targeted dependencies.

Starting coverage in a child and assigning coverage to the right test are two
separate requirements. A persistent worker must receive a fresh node ID with EACH
submitted task. Simply enabling subprocess measurement does not establish that
attribution. Preserve coverage for setup, teardown, imports, and fixture consumers.

The coverage_probe directory demonstrates the mechanism on a tiny spawn pool:
parent-only coverage records none of the worker function's lines, while explicitly
passing a test context and measuring in the child records both test identities.
This was executed with Python 3.13.5 and coverage 7.13.3. It is NOT an end-to-end
SQLGlot map-builder implementation.

## Run the audit

Use Python 3.9+ and keep this kit outside the submitted repository when possible.
The audit itself uses only the standard library. Running the actual selector needs
its existing map.pkl.z and any dependencies that selector requires.

```sh
# Establish sample floors and baseline duration statistics.
python /path/to/audit_blast.py --repo /path/to/blast --report /tmp/blast-facts.json

# Audit output from make sample (no selector re-execution needed).
python /path/to/audit_blast.py --repo /path/to/blast \
  --output /path/to/blast/output.jsonl --report /tmp/blast-output-audit.json

# Run the current selector, then score its output locally.
python /path/to/audit_blast.py --repo /path/to/blast \
  --run-selector --report /tmp/blast-baseline.json

# Local diagnostic ablations. These settings are deliberately NOT endorsed.
python /path/to/audit_blast.py --repo /path/to/blast \
  --ablate --report /tmp/blast-ablation.json
```

The report gives per-commit costs, misses, expensive selected tests, optimizer-file
expansions, and a rigorous upper bound on possible scheduling improvements for the
unchanged selections. This lets you distinguish selection waste from bin-packing
waste. Removing a test in the marginal-cost report also repacks the remaining set;
that number is not proof that the test can be safely omitted.

Ablations turn off optimizer-family expansion, turn off the dialect-name rule,
activate the existing heavy-name gate, trust uncovered lines, or switch to
function-level coverage. They reveal where cost and safety come from. A low sample
score from an unsafe setting is not a sufficient reason to deploy it.

CPU measurements in --run-selector are current-process Python CPU measurements,
not Docker/server whole-container accounting. Initialization is reported separately.
The repeated modes share initialized data; use actual isolated container runs for
submission comparisons.

## Recommended implementation sequence

1. Freeze the current zero-miss baseline and inspect output.jsonl with this audit.
2. Make the map builder reproducible: exact source revision, pinned dependencies,
   SKIP_INTEGRATION=0, correct test IDs, subprocess attribution, and fail on pytest
   failures or incomplete collection. Record a build manifest.
3. Trace actual fixture reads and propagate shared setup/fixture dependencies to
   every consuming test. Preserve import/collection dependencies separately from
   execution-line coverage; never discard unassigned coverage as irrelevant.
4. Rebuild the map, validate it, and replace blanket optimizer expansion only when
   dependencies are known. Keep conservative fallbacks for unknown behavior.
5. Refine coarse dialect/module rules using dependencies from the pinned revision,
   not sample labels, edit-class tables, or patch hashes.
6. Improve patch handling, insertion/control-flow coverage, compilation checks,
   new-file/import handling, and source-sensitive comment cases. An equal AST is
   not an unconditional guarantee of unchanged test behavior.
7. Keep worker-count search, use integer milliseconds, and compare allocation cost
   with its lower bound before considering a more expensive scheduling search.
8. Profile startup and selection CPU last; cache decompressed source, deduplicate
   coverage sets or use bitsets, and avoid repeated expensive AST work.
9. Validate on the sample and independent held-out mutations, keeping labels only
   in development. Runtime must consume patches and allowed baseline maps only.
10. Verify the actual container constraints and archive size, retain earlier
    submitted commits, and submit only a tested improvement. A development target
    around <=120 simulated seconds with low CPU leaves margin, but it is not a
    prediction for hidden data.

## Validation performed for this kit

Five unit tests passed. The diagnostic scorer was cross-checked against the
supplied score.py on an all-tests/all-30-commits output: both reported 1,104.510 s
and zero misses. The sample-floor calculation and the child-coverage reproduction
were run locally. The current-map ablation mode was not run against the real map.

```sh
cd /path/to/this/kit
python -m unittest -v test_audit
# Requires coverage installed in the active development Python environment:
python coverage_probe/probe.py
```

## Research sources

Primary sources inspected were the selected repository at the commit above;
SQLGlot tests/test_optimizer.py at eb11aec4be8cb2122afe713a4816662c3f0fb2fa;
the uploaded SCORING.md, README.md, score.py and sample files; Coverage.py's
measurement-context, multiprocessing and configuration documentation; pytest-cov's
context and subprocess documentation; and Python's AST and audit-hook documentation.

Key documentation facts: pytest-cov 7 removed its own subprocess support;
Coverage.py requires appropriate child-process measurement/combination;
pytest-cov contexts include test ID and phase; sysmon currently lacks dynamic
context support; and ast.parse alone does not perform every compilation check.
