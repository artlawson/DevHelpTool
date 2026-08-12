from app.core.cache import TTLCache


def test_get_returns_none_for_missing_key():
    cache = TTLCache(ttl_seconds=60)
    assert cache.get("missing") is None


def test_set_then_get_returns_value_within_ttl():
    cache = TTLCache(ttl_seconds=60)
    cache.set("key", ["value"])
    assert cache.get("key") == ["value"]


def test_get_returns_none_after_ttl_expires():
    fake_time = [1000.0]
    cache = TTLCache(ttl_seconds=10, clock=lambda: fake_time[0])

    cache.set("key", "value")
    assert cache.get("key") == "value"

    fake_time[0] += 11  # past the 10s TTL
    assert cache.get("key") is None
