"""Quick smoke checks for runtime.route_cache. Deleted after run."""

from runtime.route_cache import RouteCache, make_key, DEFAULT_CAPACITY, DEFAULT_TTL_SEC
from runtime.types import RouteRequest


def main() -> None:
    # 1) defaults
    assert DEFAULT_CAPACITY == 32
    assert DEFAULT_TTL_SEC == 30.0

    # 2) LRU eviction at capacity
    c: RouteCache[int] = RouteCache(capacity=3, ttl_sec=10.0)
    c.put("a", 1, now=0.0)
    c.put("b", 2, now=0.0)
    c.put("c", 3, now=0.0)
    assert len(c) == 3
    c.put("d", 4, now=0.0)  # should evict 'a'
    assert c.get("a", now=0.0) is None
    assert c.get("b", now=0.0) == 2
    # accessing 'b' makes it most-recent; next eviction should drop 'c'
    c.put("e", 5, now=0.0)  # drops 'c'
    assert c.get("c", now=0.0) is None
    assert c.get("b", now=0.0) == 2
    assert c.get("d", now=0.0) == 4
    assert c.get("e", now=0.0) == 5

    # 3) disabled mode
    d: RouteCache[int] = RouteCache(disabled=True)
    d.put("x", 1, now=0.0)
    assert d.get("x", now=0.0) is None
    assert len(d) == 0

    d.set_disabled(False)
    d.put("x", 1, now=0.0)
    assert d.get("x", now=0.0) == 1
    d.set_disabled(True)
    assert d.get("x", now=0.0) is None
    assert "x" not in d

    # 4) make_key determinism
    req1 = RouteRequest(kind="chat", messages=[{"role": "user", "content": "hi"}])
    req2 = RouteRequest(kind="chat", messages=[{"role": "user", "content": "hi"}])
    assert make_key("foo", req1) == make_key("foo", req2)
    assert make_key("foo", req1) != make_key("bar", req1)

    # 5) injectable time
    seq = iter([100.0, 105.0, 140.0])
    cc: RouteCache[str] = RouteCache(ttl_sec=30.0, time_provider=lambda: next(seq))
    cc.put("k", "v")          # uses 100.0
    assert cc.get("k") == "v"  # uses 105.0 (within TTL)
    assert cc.get("k") is None  # uses 140.0 (TTL expired)

    # 6) clear
    cc2: RouteCache[int] = RouteCache()
    cc2.put("a", 1, now=0.0)
    cc2.put("b", 2, now=0.0)
    cc2.clear()
    assert len(cc2) == 0

    # 7) constructor validation
    for bad_cap in (0, -1):
        try:
            RouteCache(capacity=bad_cap)
        except ValueError:
            pass
        else:
            raise AssertionError(f"capacity={bad_cap} should raise")
    for bad_ttl in (0.0, -1.0):
        try:
            RouteCache(ttl_sec=bad_ttl)
        except ValueError:
            pass
        else:
            raise AssertionError(f"ttl_sec={bad_ttl} should raise")

    print("ALL SMOKE CHECKS OK")


if __name__ == "__main__":
    main()
