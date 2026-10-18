import asyncio
import ssl

import certifi
import httpx
import pytest

from plugins.komodo.komodo import KomodoClient


def test_default_tls_bundle_is_loaded_once_for_repeated_checks(monkeypatch):
    original_context = ssl.create_default_context
    original_client = httpx.AsyncClient
    contexts = []
    upstream_verification = []

    def create_context(*args, **kwargs):
        assert not args
        assert kwargs == {"cafile": certifi.where()}
        context = original_context(*args, **kwargs)
        contexts.append(context)
        return context

    def create_client(**kwargs):
        upstream_verification.append(kwargs["verify"])
        assert kwargs["trust_env"] is False
        return original_client(**kwargs)

    # A configured client uses the same CA bundle regardless of ambient TLS
    # overrides, as its pre-existing trust_env=False clients do.
    monkeypatch.setenv("SSL_CERT_FILE", "/unused-environment-ca.pem")
    monkeypatch.setenv("SSL_CERT_DIR", "/unused-environment-ca-directory")
    monkeypatch.setattr("plugins.komodo.komodo.ssl.create_default_context", create_context)
    monkeypatch.setattr("plugins.komodo.komodo.httpx.AsyncClient", create_client)

    def upstream(request):
        if request.url.path == "/user":
            return httpx.Response(200, json={"username": "service", "enabled": True})
        return httpx.Response(200, text="2.3.3")

    client = KomodoClient(
        "https://komodo.example", "test-key", "test-secret",
        transport=httpx.MockTransport(upstream),
    )

    async def scenario():
        for _ in range(3):
            assert (await client.connection_status())["connected"] is True

    asyncio.run(scenario())
    assert len(contexts) == 1
    assert len(upstream_verification) == 3
    assert all(context is contexts[0] for context in upstream_verification)
    assert contexts[0].verify_mode == ssl.CERT_REQUIRED
    assert contexts[0].check_hostname is True
    assert contexts[0].cert_store_stats()["x509_ca"] > 0


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("verify", [False, ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)])
def test_explicit_tls_verification_is_preserved(monkeypatch, configured, verify):
    def unexpected_context(*args, **kwargs):
        pytest.fail("explicit TLS verification must not load another CA bundle")

    monkeypatch.setattr("plugins.komodo.komodo.ssl.create_default_context", unexpected_context)
    client = KomodoClient(
        "https://komodo.example" if configured else None,
        "test-key", "test-secret", verify=verify,
    )
    assert client._verify is verify


def test_unconfigured_client_does_not_load_a_ca_bundle(monkeypatch):
    def unexpected_context(*args, **kwargs):
        pytest.fail("an unconfigured integration must not load a CA bundle")

    monkeypatch.setattr("plugins.komodo.komodo.ssl.create_default_context", unexpected_context)
    client = KomodoClient(None, None, None)
    assert asyncio.run(client.connection_status()) == {"configured": False, "connected": False}
