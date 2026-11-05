"""Registered-site icon discovery stays bounded and independent of browser credentials."""

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import Settings, create_app
from plugins.sites import icon_discovery as module
from plugins.sites.icon_discovery import SiteIconDiscovery, fallback_icon, parse_icon
from plugins.sites.site_store import SitesStore
from test_site_routes import PVE, PATH, COOKIE, credentials, pve_transport


@pytest.mark.parametrize("html,expected", [
    ('<link rel="icon" href="../assets/logo.svg">', 'https://site.example/assets/logo.svg'),
    ('<link rel="apple-touch-icon" href="/touch.png"><link rel="shortcut icon" href="icon.ico">', 'https://site.example/admin/icon.ico'),
    ('<base href="/static/"><link rel="icon" href="icons/logo.svg?a=1&amp;b=2">', 'https://site.example/static/icons/logo.svg?a=1&b=2'),
    ('<base href="javascript:x"><base href="https://cdn.example/assets/"><link rel="icon" href="logo.png">', 'https://cdn.example/assets/logo.png'),
    ('<LINK REL="ICON" HREF="//cdn.example/logo.png"/>', 'https://cdn.example/logo.png'),
    ('<link rel="icon" href="data:image/png,x"><link rel="apple-touch-icon" href="/touch.png">', 'https://site.example/touch.png'),
    ('<link rel="icon" href="https://user:pass@secret.example/x"><link rel="icon" href="/safe.ico">', 'https://site.example/safe.ico'),
    ('<link rel="stylesheet" href="/style.css"><link rel="icon" href="javascript:alert(1)">', None),
])
def test_html_icon_resolution_and_regular_icon_precedence(html, expected):
    assert parse_icon(html, 'https://site.example/admin/home?x=1') == expected


def test_fallback_uses_only_registered_site_origin():
    assert fallback_icon('https://site.example:9443/admin/login?secret=x#tab') == 'https://site.example:9443/favicon.ico'


def test_redirect_discovery_does_not_forward_headers_cookies_or_fetch_icon_bytes():
    requests = []
    def remote(request):
        requests.append(str(request.url))
        assert not any(name in request.headers for name in ('cookie', 'authorization', 'x-api-key', 'x-pve-plugins-csrf'))
        assert request.headers['accept-encoding'] == 'identity'
        if request.url.path == '/start':
            return httpx.Response(302, headers={'location': '/landing/', 'set-cookie': 'private=remote; Path=/'})
        assert request.url.path == '/landing/'
        return httpx.Response(200, headers={'content-type': 'text/html'}, text='<link rel="icon" href="brand.svg">')
    async def scenario():
        service = SiteIconDiscovery(transport=httpx.MockTransport(remote))
        assert await service.discover('https://site.example/start') == 'https://site.example/landing/brand.svg'
        assert await service.discover('https://site.example/start') == 'https://site.example/landing/brand.svg'
        assert len(requests) == 2
        await service.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['too_many_redirects', 'invalid_redirect', 'error_status', 'image_response', 'compressed', 'too_large_header', 'too_large_stream'])
def test_remote_failures_and_bounds_return_origin_favicon(kind):
    requests, streamed, closed = [], [], []
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            streamed.append(True)
            yield b'x' * (module.MAX_HTML_BYTES + 1)
        async def aclose(self):
            closed.append(True)
    def remote(request):
        requests.append(request.url)
        if kind == 'too_many_redirects':
            return httpx.Response(302, headers={'location': '/next'})
        if kind == 'invalid_redirect':
            return httpx.Response(302, headers={'location': 'https://user:pass@secret.example/'})
        if kind == 'error_status':
            return httpx.Response(403, text='private remote error')
        headers = {'content-type': 'text/html'}
        if kind == 'image_response':
            headers['content-type'] = 'image/png'
        if kind == 'compressed':
            headers['content-encoding'] = 'gzip'
        if kind == 'too_large_header':
            headers['content-length'] = str(module.MAX_HTML_BYTES + 1)
        return httpx.Response(200, headers=headers, stream=Stream())
    async def scenario():
        service = SiteIconDiscovery(transport=httpx.MockTransport(remote))
        assert await service.discover('https://site.example/private?token=x') == 'https://site.example/favicon.ico'
        assert len(requests) <= module.MAX_REDIRECTS + 1
        assert bool(streamed) == (kind == 'too_large_stream')
        if kind in {'image_response', 'compressed', 'too_large_header', 'too_large_stream'}:
            assert closed == [True]
        await service.close()
    asyncio.run(scenario())


def test_timeout_is_bounded_and_deduplicated(monkeypatch):
    monkeypatch.setattr(module, 'DISCOVERY_TIMEOUT', 0.02)
    calls = []
    async def remote(request):
        calls.append(request.url)
        await asyncio.sleep(10)
        return httpx.Response(200, text='unreachable')
    async def scenario():
        service = SiteIconDiscovery(transport=httpx.MockTransport(remote))
        result = await asyncio.wait_for(asyncio.gather(*(service.discover('https://site.example/') for _ in range(10))), timeout=0.5)
        assert result == ['https://site.example/favicon.ico'] * 10
        assert len(calls) == 1
        await service.close()
    asyncio.run(scenario())


def test_discovery_cache_is_lru_bounded_expires_and_limits_concurrent_fetches():
    active, peak, calls = 0, 0, 0
    async def remote(request):
        nonlocal active, peak, calls
        calls += 1
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        return httpx.Response(200, headers={"content-type": "text/html"}, text='<link rel="icon" href="/logo.svg">')
    async def scenario():
        service = SiteIconDiscovery(transport=httpx.MockTransport(remote))
        await asyncio.gather(*(service.discover(f'https://site-{i}.example/') for i in range(100)))
        assert peak == 4 and len(service._cache) == 100
        await service.discover('https://site-100.example/')
        assert len(service._cache) == 100 and 'https://site-0.example/' not in service._cache
        before = calls
        await service.discover('https://site-100.example/')
        assert calls == before
        _, icon = service._cache['https://site-100.example/']
        service._cache['https://site-100.example/'] = (0, icon)
        await service.discover('https://site-100.example/')
        assert calls == before + 1
        await service.close()
    asyncio.run(scenario())


def test_plugin_shutdown_cancels_pending_discovery():
    async def scenario():
        started = asyncio.Event()
        async def remote(request):
            started.set()
            await asyncio.Future()
        service = SiteIconDiscovery(transport=httpx.MockTransport(remote))
        request = asyncio.create_task(service.discover('https://site.example/'))
        await started.wait()
        await service.close()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert not service._pending and not service._cache
    asyncio.run(scenario())


@pytest.fixture
def integrated_icons(tmp_path):
    state = {'requests': [], 'icon_requests': []}
    path = tmp_path / 'sites.sqlite3'
    def remote(request):
        assert state['requests'][-1] == ('POST', '/api2/json/access/ticket'), 'Authenticate before remote requests'
        assert not any(name in request.headers for name in ('cookie', 'authorization', 'x-api-key', 'x-pve-plugins-csrf'))
        state['icon_requests'].append(str(request.url))
        if state.get('change_during_fetch'):
            external = SitesStore(path, pve_origin=PVE)
            site = external.list()[0]
            if state['change_during_fetch'] == 'delete':
                external.delete(site['id'], expected_revision=site['revision'])
            elif state['change_during_fetch'] == 'disable':
                external.update(site['id'], enabled=False, expected_revision=site['revision'])
            elif state['change_during_fetch'] == 'manual':
                external.update(site['id'], icon={'type': 'pve', 'value': 'fa fa-server'}, expected_revision=site['revision'])
            else:
                external.update(site['id'], 'changed', 'https://changed.example/', expected_revision=site['revision'])
            external.close()
        return httpx.Response(200, headers={'content-type': 'text/html'}, text='<link rel="icon" href="/images/site.svg">')
    settings = Settings(PVE, enabled_plugins=('sites',), site_state_file=str(path))
    application = create_app(settings, pve_auth_transport=pve_transport(state), plugin_transports={'sites': httpx.MockTransport(remote)})
    with TestClient(application) as client:
        yield client, state, path


def test_icon_route_authenticates_and_uses_only_registered_snapshot(integrated_icons):
    client, state, path = integrated_icons
    unknown = PATH + '/' + 'a' * 32 + '/icon'
    assert client.get(unknown).status_code == 401
    assert not path.exists() and state['requests'] == state['icon_requests'] == []
    headers = credentials(client)
    assert client.get(PATH + '/invalid/icon', headers={'cookie': COOKIE}).status_code == 422
    assert client.get(unknown, headers={'cookie': COOKIE}).status_code == 404
    assert state['icon_requests'] == []
    site = client.post(PATH, json={'name': 'site', 'url': 'https://site.example/app/'}, headers=headers).json()['site']
    assert state['icon_requests'] == [], 'Saving a site must not fetch it'
    endpoint = PATH + '/' + site['id'] + '/icon'
    assert client.get(endpoint + '?url=https://arbitrary.example/', headers={'cookie': COOKIE}).status_code == 422
    response = client.get(endpoint, headers={'cookie': COOKIE, 'authorization': 'private-browser-auth'})
    assert response.status_code == 200
    assert response.json() == {'site_id': site['id'], 'revision': 1, 'icon_url': 'https://site.example/images/site.svg'}
    assert response.headers['cache-control'] == 'private, no-store'
    count = len(state['requests'])
    assert client.get(endpoint, headers={'cookie': COOKIE}).status_code == 200
    assert len(state['icon_requests']) == 1 and len(state['requests']) == count + 2
    state['allowed'] = False
    assert client.get(endpoint, headers={'cookie': COOKIE}).status_code == 401
    assert len(state['icon_requests']) == 1


@pytest.mark.parametrize('change,status', [('update', 409), ('delete', 404), ('disable', 409), ('manual', 409)])
def test_icon_route_rejects_revision_changed_during_discovery(integrated_icons, change, status):
    client, state, _ = integrated_icons
    headers = credentials(client)
    site = client.post(PATH, json={'name': 'site', 'url': 'https://site.example/start'}, headers=headers).json()['site']
    state['change_during_fetch'] = change
    response = client.get(PATH + '/' + site['id'] + '/icon', headers={'cookie': COOKIE})
    assert response.status_code == status


def test_builtin_sites_remain_available_without_optional_modules_and_discovery_is_lazy(tmp_path):
    path = tmp_path / 'sites.sqlite3'
    state = {'requests': []}
    def unexpected(request):
        pytest.fail('An empty built-in site list must not make outbound requests')
    with TestClient(create_app(Settings(PVE, enabled_plugins=(), site_state_file=str(path)),
            pve_auth_transport=pve_transport(state), plugin_transports={'sites': httpx.MockTransport(unexpected)})) as client:
        assert client.get(PATH + '/' + 'a' * 32 + '/icon').status_code == 401
        assert not path.exists()
        assert client.get(PATH, headers={'cookie': COOKIE}).json()['sites'] == []


@pytest.mark.parametrize('metadata,status,code', [
    ({'enabled': False}, 404, 'site_disabled'),
    ({'icon': {'type': 'pve', 'value': 'fa fa-server'}}, 409, 'site_icon_is_manual'),
    ({'icon': {'type': 'url', 'value': 'https://cdn.example/site.svg'}}, 409, 'site_icon_is_manual'),
])
def test_disabled_and_manual_icons_never_start_discovery(integrated_icons, metadata, status, code):
    client, state, _ = integrated_icons
    headers = credentials(client)
    site = client.post(PATH, json={'name': 'Site', 'url': 'https://site.example/', **metadata}, headers=headers).json()['site']
    response = client.get(PATH + '/' + site['id'] + '/icon', headers={'cookie': COOKIE})
    assert response.status_code == status and response.json() == {'detail': code}
    assert state['icon_requests'] == []


def test_malformed_html_declaration_returns_fallback_on_initial_discovery():
    assert parse_icon('<![unexpected marked section]>', 'https://site.example/') is None
    async def scenario():
        service = SiteIconDiscovery(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={'content-type': 'text/html'}, text='<![unexpected marked section]>')))
        assert await service.discover('https://site.example/') == 'https://site.example/favicon.ico'
        await service.close()
    asyncio.run(scenario())


def test_injected_transport_remains_open_across_fetches_and_service_shutdown():
    class SharedTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.closed = False
            self.calls = 0
        async def handle_async_request(self, request):
            assert not self.closed
            self.calls += 1
            return httpx.Response(200, headers={"content-type": "text/html"}, text='<link rel="icon" href="/logo.svg">')
        async def aclose(self):
            self.closed = True
    async def scenario():
        transport = SharedTransport()
        service = SiteIconDiscovery(transport=transport)
        assert await service.discover('https://one.example/') == 'https://one.example/logo.svg'
        assert await service.discover('https://two.example/') == 'https://two.example/logo.svg'
        assert transport.calls == 2 and not transport.closed
        await service.close()
        assert not transport.closed
        await transport.aclose()
        assert transport.closed
    asyncio.run(scenario())


def test_http_transport_rejecting_an_existing_url_returns_fallback():
    # Legacy site validation accepts RFC IPvFuture hosts that HTTPX cannot use.
    async def scenario():
        service = SiteIconDiscovery()
        assert await service.discover('http://[v1.example]/page') == 'http://[v1.example]/favicon.ico'
        await service.close()
    asyncio.run(scenario())
