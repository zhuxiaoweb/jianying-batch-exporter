"""Small, bounded pools for independent read-only preflight checks."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import os

from .storage import Cancelled
from .resources import memory_status


def check_all(check, items, cancel=None):
    """Return results in input order, only after every check succeeds.

    Keep at most four submitted checks, including running work. On failure or
    cancellation, stop submitting and join running readers before returning.
    No results are cached across files, stages, jobs, or export attempts.
    """
    workers = min(4, max(1, (os.cpu_count() or 2) // 2))
    try:
        memory = memory_status()
        available = [memory[key] for key in ('physical_available_mib','commit_available_mib')
                     if memory.get(key) is not None]
    except (OSError, ValueError, RuntimeError):
        available = []
    if not available or min(available) < 2048:
        workers = 1
    elif min(available) < 3072:
        workers = min(workers, 2)
    def invoke(item):
        if cancel is not None and cancel.is_set():
            raise Cancelled('素材预检已停止，未启动新的检查')
        return check(item)
    if cancel is not None and cancel.is_set():
        raise Cancelled('素材预检已停止，未启动新的检查')
    if workers == 1:
        return [invoke(item) for item in items]
    iterator = iter(items)
    pending = deque()
    results = []
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='jy-preflight')
    try:
        for _ in range(workers):
            try: item = next(iterator)
            except StopIteration: break
            pending.append(pool.submit(invoke, item))
        while pending:
            results.append(pending.popleft().result())
            if cancel is not None and cancel.is_set():
                raise Cancelled('素材预检已停止，未启动新的检查')
            try: item = next(iterator)
            except StopIteration: continue
            pending.append(pool.submit(invoke, item))
        return results
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
