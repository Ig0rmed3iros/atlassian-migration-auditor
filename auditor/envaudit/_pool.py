"""Bounded, deterministic parallelism for the env-audit gathers.

The gather issues hundreds of INDEPENDENT per-object/per-project/per-area reads
(187 projects' components + versions, capped per-screen / per-group / per-field
probes, the simple area list-fetches, Confluence per-space counts/permissions).
Run sequentially they dominate wall time; they share nothing, so a small thread
pool collapses that.

Thread-safety rests on two facts:
  - httpx.Client.request is thread-safe, and the SAME shared client carries the
    existing per-call 429/5xx backoff — so a modest pool reuses one client and
    the instance is never hammered harder than the backoff allows.
  - We NEVER mutate a shared accumulator from a worker. Each task returns a
    (key, value) result; the MAIN thread merges results into dicts keyed by
    KEY/name and re-sorts lists. Completion order therefore can never affect the
    snapshot — output is byte-for-byte identical to the sequential version.

A task that raises is isolated: the exception is captured and returned to the
main thread as that task's result, so the caller records the error exactly as
the sequential code did (never a false clean, never a sibling abort).
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable

# Default pool width. Modest on purpose: the shared client's 429/5xx backoff is
# per-call, so a wide pool would multiply concurrent pressure on the instance.
# Override with MA_GATHER_WORKERS (clamped to >= 1; 1 == forced sequential, the
# equivalence baseline the tests pin against).
MAX_WORKERS = 10

# The apply (live-write) path fans destructive DELETEs out concurrently too, but
# gets a deliberately GENTLER default than the read-only gather: a delete storm
# is far costlier than a read storm, and the circuit-breaker wants a few in-flight
# results to react to, not a hundred. Override with MA_APPLY_WORKERS (1 == forced
# sequential, the equivalence baseline).
APPLY_MAX_WORKERS = 6


def _resolved(env_var: str, default: int) -> int:
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return n if n >= 1 else 1


def worker_count() -> int:
    """Resolved gather pool width: MA_GATHER_WORKERS if set and valid, else
    MAX_WORKERS. Always >= 1 (1 forces sequential, used by the equivalence test)."""
    return _resolved("MA_GATHER_WORKERS", MAX_WORKERS)


def apply_worker_count() -> int:
    """Resolved apply (live-write) pool width: MA_APPLY_WORKERS if set and valid,
    else APPLY_MAX_WORKERS. Always >= 1 (1 forces sequential, the equivalence
    baseline the apply tests pin against)."""
    return _resolved("MA_APPLY_WORKERS", APPLY_MAX_WORKERS)


def map_results(items: Iterable, fn: Callable, workers: int | None = None) -> list:
    """Apply `fn` to each item concurrently and return results in INPUT order.

    Determinism: results are placed back at each item's original index, so the
    returned list mirrors `items` regardless of completion order — the caller
    can merge them into a dict / sorted list with no order sensitivity.

    Isolation: a task that raises does NOT propagate or abort siblings; its
    exception object is returned in that slot, so the caller can record the
    error exactly where the sequential loop would have. With workers == 1 the
    work runs inline on the calling thread (no executor) — the equivalence
    baseline and the safest path when parallelism is disabled.
    """
    seq = list(items)
    if not seq:
        return []
    n = workers if workers is not None else worker_count()
    if n <= 1 or len(seq) == 1:
        out = []
        for it in seq:
            try:
                out.append(fn(it))
            except Exception as exc:  # noqa: BLE001 — isolate, surface to caller
                out.append(exc)
        return out

    results: list = [None] * len(seq)
    # Cap the pool at the item count: no point spawning idle threads.
    with ThreadPoolExecutor(max_workers=min(n, len(seq))) as ex:
        future_to_idx = {ex.submit(fn, it): i for i, it in enumerate(seq)}
        for fut in future_to_idx:
            idx = future_to_idx[fut]
            try:
                results[idx] = fut.result()
            except Exception as exc:  # noqa: BLE001 — isolate, surface to caller
                results[idx] = exc
    return results
