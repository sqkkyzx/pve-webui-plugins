"""Durable sites are isolated by PVE and protected against stale edits."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import threading
import uuid

import pytest

from plugins.sites.site_store import SiteError, SitesStore


PVE = "https://pve.example:8006"
URL = "https://grafana.example:3000/d/overview?orgId=1#top"


def store(path=":memory:", origin=PVE):
    return SitesStore(path, pve_origin=origin)


def fails(code, operation, status=503):
    with pytest.raises(SiteError) as error:
        operation()
    assert (error.value.code, error.value.status_code, str(error.value)) == (code, status, code)


def test_lazy_persistent_crud_retains_order_identity_and_revisions(tmp_path):
    path = tmp_path / "new-parent" / "sites.sqlite3"
    sites = store(path)
    assert not path.parent.exists() and sites._connection is None
    assert sites.list() == []
    first = sites.create("  監控  ", "  " + URL + "  ")
    second = sites.create("控制台", "http://192.168.1.10:8000/")
    assert uuid.UUID(hex=first["id"]).version == 4 and len(first["id"]) == 32
    assert first == {"id": first["id"], "name": "監控", "url": URL, "revision": 1}
    updated = sites.update(first["id"], "監控二", URL + "-two", expected_revision=1)
    assert updated["id"] == first["id"] and updated["revision"] == 2
    assert sites.list() == [updated, second]
    sites.close()
    reopened = store(path)
    assert reopened.list() == [updated, second]
    fails("site_revision_conflict", lambda: reopened.delete(first["id"], expected_revision=1), 409)
    assert reopened.delete(first["id"], expected_revision=2) == {"deleted": True, "id": first["id"]}
    assert reopened.list() == [second]
    fails("site_not_found", lambda: reopened.update(first["id"], "old", URL, expected_revision=2), 404)
    fails("site_not_found", lambda: reopened.delete(first["id"], expected_revision=2), 404)
    reopened.close()


def test_same_pve_canonical_origin_is_shared_but_different_pve_cannot_adopt_database(tmp_path):
    path = tmp_path / "sites.sqlite3"
    initial = store(path, "https://PVE.EXAMPLE:443/")
    site = initial.create("monitor", URL)
    initial.close()
    assert store(path, "https://pve.example").list() == [site]
    wrong = store(path, "https://different-pve.example")
    for operation in (wrong.list, lambda: wrong.create("other", URL),
                      lambda: wrong.update(site["id"], "other", URL, expected_revision=1),
                      lambda: wrong.delete(site["id"], expected_revision=1)):
        fails("pve_instance_mismatch", operation)
    assert wrong._connection is None
    assert store(path, "https://pve.example").list() == [site]


def test_two_database_connections_cannot_silently_overwrite_confirmed_revision(tmp_path):
    path = tmp_path / "sites.sqlite3"
    first, second = store(path), store(path)
    site = first.create("before", URL)
    assert second.list() == [site]
    winner = second.update(site["id"], "winner", URL, expected_revision=1)
    fails("site_revision_conflict", lambda: first.update(site["id"], "stale", URL, expected_revision=1), 409)
    fails("site_revision_conflict", lambda: first.delete(site["id"], expected_revision=1), 409)
    assert first.list() == [winner]


def test_simultaneous_threads_have_one_edit_winner(tmp_path):
    path = tmp_path / "sites.sqlite3"
    original = store(path).create("original", URL)
    barrier = threading.Barrier(2)

    def edit(name):
        sites = store(path)
        barrier.wait(timeout=3)
        try:
            return sites.update(original["id"], name, URL, expected_revision=1)
        except SiteError as error:
            return error.code
        finally:
            sites.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["one", "two"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("site_revision_conflict") == 1
    assert store(path).list()[0]["revision"] == 2


def test_site_limit_is_atomic_and_deleting_frees_capacity(tmp_path):
    path = tmp_path / "sites.sqlite3"
    sites = store(path)
    first = None
    for index in range(100):
        site = sites.create(str(index), URL)
        first = first or site
    fails("site_limit_reached", lambda: store(path).create("overflow", URL), 409)
    assert len(sites.list()) == 100
    sites.delete(first["id"], expected_revision=1)
    assert sites.create("last", URL)["revision"] == 1
    assert len(sites.list()) == 100


@pytest.mark.parametrize("url", [
    "http://localhost:3000", "https://10.20.30.40/a?q=1#fragment", "http://[fd00::1]:3000/",
    "https://例子.测试/路徑?項目=一#詳情", "https://grafana.internal.", "HTTPS://EXAMPLE.COM/a",
    "https://example.com/path?redirect=https%3A%2F%2Fother.example#overview",
])
def test_internal_ipv6_unicode_and_complete_path_query_fragment_urls_are_saved_without_fetch(url):
    assert store().create("name", url)["url"] == url


@pytest.mark.parametrize("url", [
    "", " ", None, True, 123, "javascript:alert(1)", "data:text/html,x", "file:///etc/passwd",
    "//example.com/", "https:/example.com", "http://", "https://user:password@example.com/",
    "https://user@example.com/", "https://@example.com", "https://example.com\\@evil.example",
    "https://example.com/\npath", "\thttps://example.com", "https://example.com/\x00path",
    "https://example.com/\ud800", "https://example.com/\x80path", "https://example.com:0", "https://example.com:65536",
    "https://example.com:", "https://example.com:abc", "https://bad host/", "https://bad_host/",
    "https://-bad.example/", "https://bad..example/", "https://example.com%2f.evil/",
    "http://[fe80::1%25eth0]/", "https://example.com/with space", "https://" + "x" * 2050,
])
def test_invalid_urls_fail_before_creating_database(tmp_path, url):
    path = tmp_path / "sites.sqlite3"
    fails("invalid_site_url", lambda: store(path).create("name", url), 422)
    assert not path.exists()


@pytest.mark.parametrize("name", ["", " ", None, True, 12, "x" * 101, "with\nnewline", "nul\x00", "bad\x80", "bad\udfff"])
def test_invalid_names_do_not_create_database(tmp_path, name):
    path = tmp_path / "sites.sqlite3"
    fails("invalid_site_name", lambda: store(path).create(name, URL), 422)
    assert not path.exists()


def test_name_and_url_limits_apply_after_trimming():
    sites = store()
    url = "https://example.com/" + "x" * (2048 - len("https://example.com/"))
    result = sites.create("  " + "名" * 100 + "  ", "  " + url + "  ")
    assert len(result["name"]) == 100 and len(result["url"]) == 2048
    fails("invalid_site_url", lambda: sites.create("ok", url + "x"), 422)


@pytest.mark.parametrize("revision", [0, -1, True, False, 1.0, "1", None, 9_007_199_254_740_992])
def test_revision_is_a_strict_positive_safe_integer(revision):
    sites = store()
    site = sites.create("name", URL)
    fails("invalid_site_revision", lambda: sites.update(site["id"], "edit", URL, expected_revision=revision), 422)
    fails("invalid_site_revision", lambda: sites.delete(site["id"], expected_revision=revision), 422)


@pytest.mark.parametrize("site_id", [None, True, "a" * 31, "a" * 33, "A" * 32, "g" * 32, "../sites", "' OR 1=1 --"])
def test_site_ids_cannot_be_coerced_or_used_as_sql(site_id):
    sites = store()
    fails("invalid_site_id", lambda: sites.update(site_id, "edit", URL, expected_revision=1), 422)
    fails("invalid_site_id", lambda: sites.delete(site_id, expected_revision=1), 422)


def test_corrupt_records_are_not_returned_or_echoed_in_errors(tmp_path):
    path = tmp_path / "sites.sqlite3"
    sites = store(path)
    site = sites.create("name", URL)
    sites.close()
    secret = "database-private-secret"
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE sites SET url=? WHERE id=?", ("https://user:" + secret + "@example.com", site["id"]))
    for operation in (store(path).list, lambda: store(path).delete(site["id"], expected_revision=1)):
        with pytest.raises(SiteError) as error:
            operation()
        assert error.value.code == "site_store_unavailable" and secret not in repr(error.value)


@pytest.mark.parametrize("path", ["", "  ", None])
def test_no_accidental_ephemeral_fallback_for_invalid_path(path):
    sites = store(path)
    fails("site_store_unavailable", sites.list)
    assert sites._connection is None


def test_unavailable_or_corrupt_database_never_leaks_private_path(tmp_path):
    for path in (tmp_path, tmp_path / "corrupt.sqlite3"):
        if path != tmp_path:
            path.write_bytes(b"not a database")
        with pytest.raises(SiteError) as error:
            store(path).list()
        assert error.value.code == "site_store_unavailable" and str(tmp_path) not in repr(error.value)


@pytest.mark.parametrize("origin", [
    "http://pve.example:8006", "https://user:pass@pve.example", "https://pve.example/api2",
    "https://pve.example?target=other", "https://pve.example#fragment", "https://pve.example\\evil",
])
def test_database_namespace_requires_fixed_https_pve_origin(origin):
    with pytest.raises(ValueError, match="^Sites require a fixed PVE HTTPS origin$"):
        store(origin=origin)
