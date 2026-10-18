import asyncio
import json
import sqlite3
from uuid import uuid4

import httpx
import pytest

from app.main import Settings, create_app


def settings(**changes):
    values = {
        "upstream": "https://pve.example:8006",
        "tls_verify": False,
        "komodo_url": "http://komodo.example:9120",
        "komodo_api_key": "test-key",
        "komodo_api_secret": "test-secret",
        "komodo_tls_verify": False,
    }
    values.update(changes)
    return Settings(**values)


class FakePool(httpx.AsyncBaseTransport):
    def __init__(self, *, verify):
        self.verify = verify
        self.close_count = 0

    async def handle_async_request(self, request):
        pytest.fail("The lifecycle test must not contact Komodo")

    async def aclose(self):
        pytest.fail("A borrower must not own this pool")

    async def close_pool(self):
        self.close_count += 1


def install_pool(monkeypatch):
    pools = []

    def create_pool(**kwargs):
        pool = FakePool(**kwargs)
        pools.append(pool)
        return pool

    monkeypatch.setattr("plugins.komodo.plugin.KomodoTransport", create_pool)
    return pools


def test_configured_application_owns_and_closes_its_pool_once(monkeypatch):
    pools = install_pool(monkeypatch)
    application = create_app(settings())
    assert len(pools) == 1
    assert pools[0].verify is False

    async def scenario():
        async with application.router.lifespan_context(application):
            assert pools[0].close_count == 0
        assert pools[0].close_count == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("missing", ["komodo_url", "komodo_api_key", "komodo_api_secret"])
def test_incomplete_configuration_does_not_create_a_pool(monkeypatch, missing):
    pools = install_pool(monkeypatch)
    application = create_app(settings(**{missing: None}))

    async def scenario():
        async with application.router.lifespan_context(application):
            assert pools == []
        assert pools == []

    asyncio.run(scenario())


def test_injected_transport_remains_externally_owned(monkeypatch):
    pools = install_pool(monkeypatch)

    class InjectedTransport(httpx.AsyncBaseTransport):
        close_count = 0

        async def handle_async_request(self, request):
            pytest.fail("The lifecycle test must not contact Komodo")

        async def aclose(self):
            self.close_count += 1

    injected = InjectedTransport()
    application = create_app(settings(), komodo_transport=injected)

    async def scenario():
        async with application.router.lifespan_context(application):
            assert pools == []
            assert injected.close_count == 0
        assert pools == []
        assert injected.close_count == 0
        await injected.aclose()
        assert injected.close_count == 1

    asyncio.run(scenario())


def test_shutdown_finishes_disconnected_action_and_receipt_before_closing_pool(monkeypatch, tmp_path):
    pools = install_pool(monkeypatch)
    events = []
    receipt_path = tmp_path / "actions.sqlite3"
    identifier = "a" * 24
    result = {"results": [{"id": identifier, "ok": True, "status": "submitted"}]}

    async def authenticate(self, request):
        return "root@pam"

    async def dashboard(self):
        return {"configured": True}

    monkeypatch.setattr("app.main.PveAuthenticator.authenticate", authenticate)
    monkeypatch.setattr("plugins.komodo.plugin.KomodoClient.dashboard", dashboard)

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def perform(self, section, action, ids):
            assert (section, action, ids) == ("servers", "prune_images", [identifier])
            assert self._transport is pools[0]
            assert pools[0].close_count == 0
            events.append("action_started")
            started.set()
            await release.wait()
            assert pools[0].close_count == 0
            events.append("action_finished")
            return result

        monkeypatch.setattr("plugins.komodo.plugin.KomodoClient.perform_action", perform)
        application = create_app(settings(action_state_file=str(receipt_path)))
        lifespan = application.router.lifespan_context(application)
        await lifespan.__aenter__()
        shutdown = None
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://testserver") as client:
                response = await client.get("/pve-komodo/api/dashboard", headers={"cookie": "PVEAuthCookie=test-ticket"})
                assert response.status_code == 200
                request = asyncio.create_task(client.post("/pve-komodo/api/action", headers={
                    "cookie": "PVEAuthCookie=test-ticket",
                    "origin": "http://testserver",
                    "x-pve-komodo-csrf": response.json()["csrf_token"],
                }, json={"section": "servers", "action": "prune_images", "ids": [identifier], "request_id": str(uuid4())}))
                await asyncio.wait_for(started.wait(), timeout=2)
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
                # Give shutdown an event-loop turn to reach its pending-action drain.
                await asyncio.sleep(0)
                assert not shutdown.done()
                assert pools[0].close_count == 0
                release.set()
                await asyncio.wait_for(shutdown, timeout=2)
            assert events == ["action_started", "action_finished"]
            assert pools[0].close_count == 1
            with sqlite3.connect(receipt_path) as db:
                rows = db.execute("SELECT status, result FROM actions").fetchall()
            assert len(rows) == 1
            assert rows[0][0] == 200
            assert json.loads(rows[0][1]) == result
        finally:
            release.set()
            if shutdown is None:
                await lifespan.__aexit__(None, None, None)
            elif not shutdown.done():
                await asyncio.wait_for(shutdown, timeout=2)

    asyncio.run(scenario())
