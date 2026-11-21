import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import coverage

ROOT = Path(__file__).resolve().parent

def task(value):
    from work_unit import transform
    return transform(value)

def attributed_task(nodeid, value):
    cov = coverage.Coverage(data_file=str(ROOT / '.child'), data_suffix=True, source=['work_unit'], config_file=False, context=nodeid)
    cov.start()
    try:
        return task(value)
    finally:
        cov.stop()
        cov.save()

def data_contexts(cov):
    data = cov.get_data()
    result = {}
    for filename in data.measured_files():
        if filename.endswith('work_unit.py'):
            result[filename] = data.contexts_by_lineno(filename)
    return result

if __name__ == '__main__':
    cov = coverage.Coverage(data_file=str(ROOT / '.parent'), source=['work_unit'], config_file=False)
    cov.start()
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context('spawn')) as pool:
        cov.switch_context('test_positive|run')
        a = pool.submit(task, 3).result()
        cov.switch_context('test_negative|run')
        b = pool.submit(task, -3).result()
    cov.stop(); cov.save()
    parent_only = data_contexts(cov)
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context('spawn')) as pool:
        c = pool.submit(attributed_task, 'test_positive|run', 3).result()
        d = pool.submit(attributed_task, 'test_negative|run', -3).result()
    combined = coverage.Coverage(data_file=str(ROOT / '.child'), config_file=False)
    combined.combine()
    combined.save()
    print(json.dumps({'coverage_version':coverage.__version__, 'same_results': (a,b)==(c,d), 'parent_only':parent_only, 'explicit_child_context':data_contexts(combined)}, indent=2))
