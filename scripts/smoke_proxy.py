"""Exercise the actual HTTPS gateway and app-only image against disposable PVE peers."""

import argparse
import base64
import hashlib
import json
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener
from urllib.parse import urlsplit
from uuid import uuid4

from smoke_image import docker


MOCK_PVE = r'''
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64, hashlib, json, pathlib, socket, ssl, subprocess, sys, time
from urllib.parse import parse_qs

label = sys.argv[1]
root = pathlib.Path('/tmp/mock-pve')
root.mkdir(exist_ok=True)
subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                '-keyout', str(root/'key.pem'), '-out', str(root/'cert.pem'), '-subj', '/CN=' + label + '.mock.test',
                '-addext', 'subjectAltName=DNS:' + label + '.mock.test'],
               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
desktop = ("<!doctype html><html><head><title>" + label + "</title>"
           '<script src="/pve2/js/pvemanagerlib.js?ver=9.2.21"></script></head><body>'
           "<script>Ext.History.fieldid = 'history-field';"
           "Ext.onReady(function() { Ext.create('PVE.StdWorkspace'); });</script></body></html>").encode()
state = {'node': label, 'posts': 0, 'failed_puts': 0, 'verifications': 0}
ticket = 'PVE:root@pam:65AAE123::c21va2U='

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def reply(self, body, content_type='application/json'):
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/api2/json/mock-websocket':
            assert self.headers.get('Upgrade', '').lower() == 'websocket'
            accept = base64.b64encode(hashlib.sha1((self.headers['Sec-WebSocket-Key'] +
                '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            self.send_response(101)
            for key, value in [('Upgrade', 'websocket'), ('Connection', 'Upgrade'), ('Sec-WebSocket-Accept', accept)]:
                self.send_header(key, value)
            self.end_headers()
            frame = self.rfile.read(2)
            mask = self.rfile.read(4)
            payload = self.rfile.read(frame[1] & 127)
            body = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            self.wfile.write(bytes([0x81, len(body)]) + body)
            self.close_connection = True
        elif self.path == '/api2/json/access/permissions':
            assert self.headers.get('Cookie') == 'PVEAuthCookie=' + ticket
            self.reply(json.dumps({'data': {'/': {'Sys.Audit': 1}}}).encode())
        elif self.path == '/':
            self.reply(desktop, 'text/html; charset=utf-8')
        elif self.path == '/pve2/mock-asset.js':
            self.reply(('native-' + label).encode(), 'application/javascript')
        elif self.path == '/api2/json/mock-slow':
            if label == 'primary':
                time.sleep(15)
            try:
                self.reply(json.dumps({'node': label}).encode())
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                pass
        else:
            self.reply(json.dumps(state).encode())

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        if self.path == '/api2/json/access/ticket':
            assert self.headers.get('Cookie') == 'PVEAuthCookie=' + ticket
            assert parse_qs(body.decode()) == {'username': ['root@pam'], 'password': [ticket], 'path': ['/'], 'privs': ['Sys.Audit']}
            state['verifications'] += 1
            self.reply(json.dumps({'data': {'username': 'root@pam'}}).encode())
            return
        state['posts'] += 1
        self.reply(json.dumps({'node': label, 'body': body.decode()}).encode())

    def do_PUT(self):
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        state['failed_puts'] += 1
        self.connection.shutdown(socket.SHUT_RDWR)
        self.connection.close()

    def log_message(self, *arguments):
        pass

server = ThreadingHTTPServer(('0.0.0.0', 8006), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(root/'cert.pem', root/'key.pem')
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
'''


def fetch(base, path, *, method='GET', body=None, headers=None, timeout=8):
    # These disposable fixtures deliberately use their generated self-signed certificates.
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ssl._create_unverified_context()))
    request = Request(base + path, data=body, method=method, headers=headers or {})
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.read()


def ready(base):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if fetch(base, '/pve-plugins/health')[0] == 200:
                return
        except (URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(0.25)
    raise AssertionError('Image endpoint did not become ready')


def published(name, port, scheme):
    host_port = docker('port', name, str(port) + '/tcp').splitlines()[0].rpartition(':')[2]
    return scheme + '://127.0.0.1:' + host_port


def mock_status(name):
    code = """import json,ssl,urllib.request
context=ssl._create_unverified_context()
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context))
with opener.open('https://127.0.0.1:8006/__mock/status',timeout=3) as response:
    print(response.read().decode())
"""
    return json.loads(subprocess.check_output(['docker', 'exec', name, 'python', '-c', code],
                                             text=True, stderr=subprocess.STDOUT, timeout=10))


def authenticated_configuration(base):
    cookie = 'PVEAuthCookie=PVE:root@pam:65AAE123::c21va2U='
    code, body = fetch(base, '/pve-plugins/api/config', headers={'Cookie': cookie})
    assert code == 200
    data = json.loads(body)
    preferences = data['navigation']
    preferences['module_aliases'] = {'komodo': 'gateway-smoke'}
    command = {'expected_revision': data['revision'], 'enabled_plugins': data['enabled_plugins'],
               'navigation': preferences}
    code, body = fetch(base, '/pve-plugins/api/config', method='PUT', body=json.dumps(command).encode(),
                       headers={'Cookie': cookie, 'Origin': base, 'Content-Type': 'application/json',
                                'X-PVE-Plugins-CSRF': data['csrf_token']})
    assert code == 200 and json.loads(body)['revision'] > data['revision'], (code, body)
    assert json.loads(body)['reload_required'] is False


def websocket_echo(base):
    target = urlsplit(base)
    key = base64.b64encode(b'0123456789abcdef').decode()
    expected_accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
    context = ssl._create_unverified_context()
    with socket.create_connection((target.hostname, target.port), timeout=8) as connection:
        with context.wrap_socket(connection, server_hostname=target.hostname) as stream:
            request = ('GET /api2/json/mock-websocket HTTP/1.1\r\nHost: ' + target.netloc +
                       '\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: ' + key + '\r\n\r\n')
            stream.sendall(request.encode())
            with stream.makefile('rb') as reader:
                assert reader.readline().startswith(b'HTTP/1.1 101')
                headers = {}
                while True:
                    line = reader.readline()
                    if line == b'\r\n':
                        break
                    assert line
                    name, value = line.split(b':', 1)
                    headers[name.lower()] = value.strip()
                assert headers[b'sec-websocket-accept'] == expected_accept
                payload, mask = b'gateway-echo', b'abcd'
                frame = bytes([0x81, 0x80 | len(payload)]) + mask
                stream.sendall(frame + bytes(value ^ mask[index % 4] for index, value in enumerate(payload)))
                response = reader.read(2)
                assert response == bytes([0x81, len(payload)])
                assert reader.read(len(payload)) == payload


def verify(image):
    suffix = uuid4().hex[:12]
    network = 'pve-proxy-smoke-network-' + suffix
    data = 'pve-proxy-smoke-data-' + suffix
    app_data = 'pve-proxy-smoke-app-data-' + suffix
    primary, backup, gateway, app = ['pve-proxy-smoke-' + kind + '-' + suffix
                                     for kind in ('primary', 'backup', 'gateway', 'app')]
    names = []
    volumes = []
    try:
        docker('network', 'create', network)
        for name, label in ((primary, 'primary'), (backup, 'backup')):
            names.append(name)
            docker('run', '--detach', '--name', name, '--network', network,
                   '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
                   '--memory=64m', '--pids-limit=32', '--tmpfs',
                   '/tmp:rw,noexec,nosuid,size=4m,uid=10001,gid=10001,mode=0700',
                   '--entrypoint', 'python', image, '-c', MOCK_PVE, label)
        addresses = [docker('inspect', '--format', '{{(index .NetworkSettings.Networks "' + network + '").IPAddress}}',
                            name) for name in (primary, backup)]
        for name in (primary, backup):
            deadline = time.monotonic() + 30
            while True:
                try:
                    mock_status(name)
                    break
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    if time.monotonic() >= deadline:
                        raise AssertionError('Disposable PVE peer did not become ready')
                    time.sleep(0.25)
        upstreams = ','.join(addresses)
        for volume in (data, app_data):
            docker('volume', 'create', volume)
            volumes.append(volume)

        def start(name, volume, enabled, ca_file=None):
            names.append(name)
            arguments = ['run', '--detach', '--name', name, '--network', network,
                   '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
                   '--memory=384m', '--pids-limit=128', '--tmpfs',
                   '/run/pve-webui-plugins:rw,noexec,nosuid,size=8m,uid=10001,gid=10001,mode=0700',
                   '--mount', 'type=volume,source=' + volume + ',target=/app/data',
                   '--publish', '127.0.0.1::8008', '--publish', '127.0.0.1::8006',
                   '--env', 'PVE_UPSTREAM=' + upstreams,
                   '--env', 'PVE_INTERNAL_PROXY_ENABLED=' + enabled]
            if ca_file:
                arguments.extend(['--mount', 'type=bind,source=' + str(ca_file) + ',target=/run/mock-ca.pem,readonly',
                                  '--env', 'PVE_TLS_VERIFY=true', '--env', 'PVE_CA_FILE=/run/mock-ca.pem',
                                  '--env', 'PVE_TLS_SERVER_NAMES=primary.mock.test,backup.mock.test'])
            docker(*arguments, image)

        start(gateway, data, 'true')
        https = published(gateway, 8006, 'https')
        ready(https)
        code, body = fetch(https, '/')
        assert code == 200 and b'pve-webui-plugins:begin' in body and b'<title>primary</title>' in body
        assert fetch(https, '/pve2/mock-asset.js') == (200, b'native-primary')
        authenticated_configuration(https)
        websocket_echo(https)
        code, body = fetch(https, '/api2/json/mock-write', method='POST', body=b'write-once')
        assert code == 200 and json.loads(body) == {'node': 'primary', 'body': 'write-once'}
        cert = json.loads(docker('exec', gateway, 'python', '-c', """import hashlib,json,pathlib
root=pathlib.Path('/app/data/proxy')
print(json.dumps({'sha256':hashlib.sha256((root/'cert.pem').read_bytes()).hexdigest(),
                  'key_mode':(root/'key.pem').stat().st_mode & 0o777,
                  'notices':all(pathlib.Path('/usr/share/doc',name,'copyright').is_file() for name in ('nginx','openssl'))}))
"""))
        assert cert['key_mode'] == 0o600 and cert['notices']
        docker('restart', gateway)
        https = published(gateway, 8006, 'https')
        ready(https)
        after = docker('exec', gateway, 'python', '-c',
                       "import hashlib;print(hashlib.sha256(open('/app/data/proxy/cert.pem','rb').read()).hexdigest())")
        assert after == cert['sha256'], 'Generated gateway certificate changed after restart'
        code, body = fetch(https, '/api2/json/fail-write', method='PUT', body=b'never-replay')
        assert code in (502, 504)
        assert mock_status(primary)['failed_puts'] == 1
        assert mock_status(backup)['failed_puts'] == 0, 'Gateway replayed a write on the backup'
        docker('stop', '--time', '2', primary)
        assert fetch(https, '/pve2/mock-asset.js') == (200, b'native-backup')
        code, body = fetch(https, '/')
        assert code == 200 and b'pve-webui-plugins:begin' in body and b'<title>backup</title>' in body
        assert fetch(https, '/pve-plugins/api/config')[0] == 401
        authenticated_configuration(https)
        assert mock_status(backup)['verifications'] > 0
        docker('exec', gateway, 'python', '-m', 'app.container', '--healthcheck')
        docker('rm', '--force', gateway)
        names.remove(gateway)

        start(app, app_data, 'false')
        http = published(app, 8008, 'http')
        ready(http)
        assert fetch(http, '/api2/json/version')[0] == 404
        code, body = fetch(http, '/')
        assert code == 200 and b'<title>backup</title>' in body
        state = json.loads(docker('exec', app, 'python', '-c', """import json,pathlib,socket
processes=[]
for path in pathlib.Path('/proc').glob('[0-9]*/comm'):
    try: processes.append(path.read_text().strip())
    except OSError: pass
with socket.socket() as connection:
    listening=connection.connect_ex(('127.0.0.1',8006)) == 0
print(json.dumps({'nginx_running':'nginx' in processes,'gateway_listening':listening,
                  'cert_directory_exists':pathlib.Path('/app/data/proxy').exists()}))
"""))
        assert state == {'nginx_running': False, 'gateway_listening': False, 'cert_directory_exists': False}
        docker('exec', app, 'python', '-m', 'app.container', '--healthcheck')
        docker('rm', '--force', app)
        names.remove(app)

        docker('start', primary)
        addresses = [docker('inspect', '--format', '{{(index .NetworkSettings.Networks "' + network + '").IPAddress}}',
                            name) for name in (primary, backup)]
        assert len(set(addresses)) == 2 and all(addresses)
        upstreams = ','.join(addresses)
        deadline = time.monotonic() + 30
        while True:
            try:
                mock_status(primary)
                break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                if time.monotonic() >= deadline:
                    raise AssertionError('Primary peer did not restart')
                time.sleep(0.25)
        with tempfile.TemporaryDirectory(prefix='pve-proxy-smoke-ca-') as directory:
            public = Path(directory)
            public.chmod(0o755)
            ca_file = public / 'ca.pem'
            ca_file.write_text(''.join(docker('exec', name, 'cat', '/tmp/mock-pve/cert.pem') + '\n'
                                       for name in (primary, backup)))
            ca_file.chmod(0o644)
            start(gateway, data, 'true', ca_file)
            https = published(gateway, 8006, 'https')
            ready(https)
            assert fetch(https, '/pve2/mock-asset.js') == (200, b'native-primary')
            authenticated_configuration(https)
            started = time.monotonic()
            code, body = fetch(https, '/api2/json/mock-slow', timeout=20)
            assert code == 200 and json.loads(body)['node'] == 'backup'
            assert time.monotonic() - started < 18, 'Ordinary read did not fail over within its bounded timeout'
            docker('stop', '--time', '2', primary)
            assert fetch(https, '/pve2/mock-asset.js') == (200, b'native-backup')
            code, body = fetch(https, '/')
            assert code == 200 and b'<title>backup</title>' in body and b'pve-webui-plugins:begin' in body
            authenticated_configuration(https)
            websocket_echo(https)
            docker('rm', '--force', gateway)
            names.remove(gateway)
        print('Verified HTTPS gateway, authenticated writes, WebSocket, backup failover, strict CA/SNI, persistent certificate and app-only mode', flush=True)
    except Exception:
        for name in names:
            subprocess.run(['docker', 'logs', '--tail', '60', name], check=False, timeout=15)
        raise
    finally:
        for name in reversed(names):
            subprocess.run(['docker', 'rm', '--force', name], check=False, capture_output=True, timeout=60)
        for volume in volumes:
            subprocess.run(['docker', 'volume', 'rm', volume], check=False, capture_output=True, timeout=30)
        subprocess.run(['docker', 'network', 'rm', network], check=False, capture_output=True, timeout=30)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('image')
    verify(parser.parse_args().image)
