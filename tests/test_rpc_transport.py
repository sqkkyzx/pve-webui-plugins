import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from plugins.komodo.rpc_transport import KomodoTransport


@asynccontextmanager
async def upstream():
    requests, connections, tasks, writers = [], [], set(), set()
    blocked = asyncio.Event()

    async def serve(reader, writer):
        task = asyncio.current_task()
        tasks.add(task); writers.add(writer); connections.append(writer)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                lines = head.decode().split("\r\n")
                method, path, _ = lines[0].split(" ")
                headers = httpx.Headers(line.split(": ", 1) for line in lines[1:] if line)
                body = await reader.readexactly(int(headers.get("content-length", "0")))
                requests.append((method, path, headers, body))
                if path == "/blocked":
                    blocked.set()
                    await reader.read()
                    break
                status = b"302 Found" if path == "/redirect" else b"200 OK"
                writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Length: 2\r\n"
                             b"Set-Cookie: upstream=private; Path=/\r\nLocation: /unwanted\r\n\r\n{}")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            writers.discard(writer); tasks.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    origin = "http://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
    pool = KomodoTransport(verify=True)
    try:
        yield origin, pool, requests, connections, blocked
    finally:
        await pool.close_pool()
        server.close(); await server.wait_closed()
        for writer in tuple(writers):
            writer.close()
        if tasks:
            await asyncio.gather(*tuple(tasks), return_exceptions=True)


def test_real_keepalive_is_reused_by_isolated_clients_without_cookies_redirects_or_environment_proxies(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")

    async def run():
        async with upstream() as (origin, pool, requests, connections, _):
            for key in ("first-key", "second-key"):
                async with httpx.AsyncClient(transport=pool, trust_env=False, follow_redirects=False) as client:
                    response = await client.post(origin + "/read", headers={"x-api-key": key}, json={"type": "GetCoreInfo"})
                    assert response.status_code == 200
            async with httpx.AsyncClient(transport=pool, trust_env=False, follow_redirects=False) as client:
                assert (await client.get(origin + "/redirect")).status_code == 302
            assert len(connections) == 1, "ending a borrower keeps the actual TCP connection reusable"
            assert [request[2].get("x-api-key") for request in requests] == ["first-key", "second-key", None]
            assert all("cookie" not in request[2] for request in requests)
            assert [request[1] for request in requests] == ["/read", "/read", "/redirect"]

    asyncio.run(asyncio.wait_for(run(), 10))


def test_cancelling_one_borrower_does_not_close_another_borrower_or_the_pool():
    async def run():
        async with upstream() as (origin, pool, requests, connections, blocked):
            async def get(path):
                async with httpx.AsyncClient(transport=pool, trust_env=False) as client:
                    return await client.get(origin + path)
            pending = asyncio.create_task(get("/blocked"))
            await blocked.wait()
            assert (await get("/read")).status_code == 200
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert (await get("/read")).status_code == 200
            assert len(connections) == 2, "the healthy pooled connection survives cancellation of the other one"
            assert [request[1] for request in requests] == ["/blocked", "/read", "/read"]

    asyncio.run(asyncio.wait_for(run(), 10))
