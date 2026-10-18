"""Application-owned Komodo connection pool, without shared HTTP client state."""

import ssl

import httpx


class KomodoTransport(httpx.AsyncBaseTransport):
    """Borrow the pool while keeping each caller's headers and cookies isolated.

    Short-lived AsyncClients close their borrowed transport on exit. Only the
    application lifespan owns the underlying pool and closes it at shutdown.
    """

    def __init__(self, *, verify: ssl.SSLContext | bool):
        self._pool = httpx.AsyncHTTPTransport(verify=verify, trust_env=False, retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._pool.handle_async_request(request)

    async def aclose(self) -> None:
        # A request ending must not close connections used by other requests.
        pass

    async def close_pool(self) -> None:
        await self._pool.aclose()
