"""Stale-while-revalidate page cache for report_server.py.

Why: the dashboard rendered every page inside the request thread and a plain 30 s TTL cache only helped the
second viewer. With more tests and more viewers, every expiry made the next viewer wait for a full render (29
queries for /report, one per lane for /query), several viewers at once rendered the same page in parallel, and
one slow database moment turned into a 500 for whoever came next. This cache removes the three causes:

  * a COLD key is rendered once; concurrent callers wait for that one render (single flight);
  * a STALE key is answered at once from the old copy and refreshed in the background, so a viewer never waits
    for a render that already has an answer;
  * if a refresh fails the old copy keeps being served (up to max_stale), the error is logged, not shown.

A background warmer re-renders the keys somebody asked for recently, before they expire, so a normal viewer
always hits a warm copy. Keys nobody asks for within idle_limit are dropped.
"""

import threading
import time
import traceback


class SWRCache:
    def __init__(self, ttl=30.0, max_stale=3600.0, idle_limit=1800.0, max_keys=200):
        self.ttl = ttl
        self.max_stale = max_stale
        self.idle_limit = idle_limit
        self.max_keys = max_keys
        self._data = {}  # key -> (made_at, value)
        self._wanted = {}  # key -> (last_access, fn)
        self._locks = {}  # key -> Lock held while that key is being rendered
        self._mu = threading.Lock()
        self.stats = {"hit": 0, "stale": 0, "cold": 0, "refresh_err": 0}

    # -- internals -------------------------------------------------------------------------------------------
    def _lock(self, key):
        with self._mu:
            return self._locks.setdefault(key, threading.Lock())

    def _store(self, key, value):
        with self._mu:
            self._data[key] = (time.monotonic(), value)
            if len(self._data) > self.max_keys:  # drop the oldest copies; keys are few, this is a safety net
                for k, _ in sorted(self._data.items(), key=lambda kv: kv[1][0])[: len(self._data) - self.max_keys]:
                    self._data.pop(k, None)
                    self._wanted.pop(k, None)

    def _fill(self, key, fn):
        """Render under the key's lock. A caller that waited re-checks: the winner already stored a fresh copy."""
        with self._lock(key):
            hit = self._data.get(key)
            if hit and time.monotonic() - hit[0] < self.ttl:
                return hit[1]
            value = fn()
            self._store(key, value)
            return value

    def _refresh_async(self, key, fn):
        lk = self._lock(key)
        if lk.locked():  # somebody is rendering this key right now
            return

        def _run():
            try:
                self._fill(key, fn)
            except Exception:
                self.stats["refresh_err"] += 1
                traceback.print_exc()  # the old copy stays; nothing reaches the viewer

        threading.Thread(target=_run, daemon=True).start()

    # -- api -------------------------------------------------------------------------------------------------
    def get(self, key, fn):
        """Value for key; fn() renders it. Raises only when there is no usable copy at all."""
        now = time.monotonic()
        with self._mu:
            self._wanted[key] = (now, fn)
            hit = self._data.get(key)
        if hit is None:
            self.stats["cold"] += 1
            return self._fill(key, fn)
        age = now - hit[0]
        if age < self.ttl:
            self.stats["hit"] += 1
            return hit[1]
        if age > self.max_stale:  # too old to show without a fresh attempt; fall back to it only if the attempt fails
            try:
                return self._fill(key, fn)
            except Exception:
                traceback.print_exc()
                return hit[1]
        self.stats["stale"] += 1
        self._refresh_async(key, fn)
        return hit[1]

    def warm_once(self):
        """Refresh, one at a time, every wanted key that is past 80% of its ttl. Drop keys idle past idle_limit."""
        now = time.monotonic()
        with self._mu:
            todo = []
            for key, (seen, fn) in list(self._wanted.items()):
                if now - seen > self.idle_limit:
                    self._wanted.pop(key, None)
                    continue
                made = self._data.get(key)
                if made is None or now - made[0] >= self.ttl * 0.8:
                    todo.append((key, fn))
        for key, fn in todo:
            try:
                with self._lock(key):
                    value = fn()
                self._store(key, value)
            except Exception:
                self.stats["refresh_err"] += 1
                traceback.print_exc()
        return len(todo)

    def start_warmer(self, interval=10.0):
        def _loop():
            while True:
                time.sleep(interval)
                try:
                    self.warm_once()
                except Exception:
                    traceback.print_exc()

        t = threading.Thread(target=_loop, daemon=True, name="swr-warmer")
        t.start()
        return t


if __name__ == "__main__":
    # self-check: single flight, stale-while-revalidate, failed refresh keeps the old copy
    calls = []
    c = SWRCache(ttl=1.0, max_stale=60)

    def render():
        calls.append(1)
        time.sleep(0.5)
        return f"v{len(calls)}"

    out = []
    ts = [threading.Thread(target=lambda: out.append(c.get("k", render))) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(calls) == 1 and set(out) == {"v1"}, (calls, out)  # 8 cold callers, ONE render
    time.sleep(1.1)
    t0 = time.monotonic()
    assert c.get("k", render) == "v1" and time.monotonic() - t0 < 0.25  # stale answered at once (a render takes 0.5 s)
    time.sleep(1.0)
    assert c.get("k", render) == "v2"  # the background refresh landed
    boom = {"on": True}

    def failing():
        if boom["on"]:
            raise RuntimeError("db down")
        return "v3"

    time.sleep(1.1)
    assert c.get("k", failing) == "v2"  # stale served, refresh fails quietly
    time.sleep(0.3)
    assert c.get("k", failing) == "v2" and c.stats["refresh_err"] >= 1
    time.sleep(0.3)  # let daemon refresh threads finish before the interpreter exits
    print("swr_cache self-check OK", c.stats)
