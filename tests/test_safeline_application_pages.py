"""Application pages use authenticated routes and a stateful WAF contract fixture."""

import copy
import asyncio
import json
import sqlite3

from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import Settings, create_app
from plugins.safeline.advanced import BOOLS
from plugins.safeline.application_pages import SECTIONS
from plugins.safeline.client import SafeLineClient
from plugins.safeline.connection import Connection
from test_safeline import BASE, TOKEN, configure
from test_safeline_editing import draft, waf_fixture
from test_site_routes import COOKIE, PVE, pve_transport


def application_fixture():
    state, original = waf_fixture()
    state.update(edition=2, forwarding=[], nginx={"config": "generated private config", "custom_location": ["proxy_hide_header X-Frame-Options;"]},
                 excludes={"prefixes": ["/assets"], "content_types": ["image/png"]}, modules={"m_sqli": "default", "m_xss": "strict", "m_response": "disable"},
                 waiting={"is_enabled": False, "max_concurrent": 5, "session_timeout": 2}, log_limits={"access": 1024, "error": 0}, fail_after=None)
    state["site"].update(title="Application", challenge_id=42, challenge_is_enabled=False, chaos_id=43, chaos_is_enabled=False,
                         auth_defense_id=41, portal_redirect="", auth_challenge_timeout=5, auth_challenge_timeout_unit="day", sp_enabled=False, semantics=True)
    state["auth"] = {"id": 41, "action": 3, "auth_source_ids": [0], "review": 1, "auth_callback": "", "tfa_enabled": True, "negate": False, "pattern": []}
    state["portal"] = {"enable": True, "auth_sources": [{"id": 0, "type": 0}, {"id": 1, "type": 4}], "tfa_enabled": False}
    state["challenge"] = {"id": 42, "action": 2, "level": 1, "expire": 3600, "replay": False, "negate": False, "pattern": []}
    state["chaos"] = {"is_enabled": False, "html_encryption": False, "html_fast_decryption": False, "js_encryption": False,
                       "js_path": [], "img_encryption": False, "img_watermark": "", "img_text": ""}
    state["global_proxy"] = {k: {"global": True, "value": False} for k in BOOLS}
    state["global_proxy"].update({"ip_source": {"global": False, "value": "xff_dynamic"}, "ip_value": {"global": False, "value": '{"position":"from_start","index":1}'},
        "host": {"global": False, "value": "$http_host"}, "xfp": {"global": True, "value": "$scheme"}, "xfh": {"global": True, "value": "$http_host"},
        "hsts_max_age": {"global": False, "value": "15768000"}, "ssl_protocols": {"global": False, "value": "TLSv1.2 TLSv1.3"}, "ssl_ciphers": {"global": False, "value": ""},
        "http_headers": {"global": False, "value": []}})
    state["proxy"] = copy.deepcopy(state["global_proxy"])
    state["proxy"]["http_headers"] = {"global": False, "value": [{"key": "Authorization", "val": "private-header", "target": "req", "op": "set"}]}
    state["proxy"]["http3"] = {"global": False, "value": True}
    state["acl"] = [{"id": i + 70, "name": kind, "type": kind, "priority": 1, "enabled": True, "period": 10, "count": 500,
                     "block_min": 30, "action": "challenge_v1", "conditions": [], "custom_status_codes": [], "built_in": True, "use_global": False}
                    for i, kind in enumerate(("req", "attack", "error"))]
    state["resources"] = [{"id": 61, "site_id": 12, "method": "GET", "path": "/index.html", "content_type": "text/html", "req_today": 3,
                           "req_month": 33, "updated_at": "2026-10-19T00:00:00Z", "req_header": "Authorization: private-header"},
                          {"id": 62, "site_id": 12, "method": "GET", "path": "/app.js", "content_type": "application/javascript", "req_today": 4}]
    state["tamper"] = {"data": [{"id": 51, "site_id": 12, "path": "/index.html", "changed": True, "cache_time": 100}], "refresh_time": 100, "refresh_state": "finish"}

    def handler(request):
        path, method = request.url.path, request.method
        if path in {"/api/open/site", "/api/open/site/12", "/api/open/site/mode", "/api/open/cert", "/api/open/cert/5", "/api/open/ipgroup", "/api/open/ipgroup/detail"}:
            return original.handle_request(request)
        state["waf"].append(request)
        assert request.headers["x-slce-api-token"] == TOKEN
        assert not {"cookie", "authorization", "x-pve-plugins-csrf", "csrfpreventiontoken"} & set(request.headers)
        if method == "GET":
            values = {"/api/open/system/edition": {"effective_edition": state["edition"]}, "/api/open/global/proxy": state["global_proxy"],
                "/api/open/site/12/proxy": {**state["proxy"], "global": state["global_proxy"]}, "/api/open/site/12/acl": state["acl"], "/api/open/site/0/acl": state["acl"],
                "/api/open/site/12/waiting": state["waiting"], "/api/open/site/12/chaos": state["chaos"], "/api/open/site/12/excludes": state["excludes"],
                "/api/open/site/12/nginx_config": state["nginx"], "/api/open/site/12/forwarding_rules": state["forwarding"],
                "/api/open/site/12/semantics": {"semantics": state["modules"]} if state["modules"] is not None else None,
                "/api/open/global/mode": {"semantics": {"m_sqli": "default", "m_xss": "default"}},
                "/api/open/auth_defense/source": {"data": [{"id": 1, "type": 4}]},
                "/api/open/portal": state["portal"],
                "/api/open/site/12/resources": {"nodes": state["resources"], "total": len(state["resources"])},
                "/api/business/site/12/anti_tamper": state["tamper"],
                "/api/open/site/12/log": [{"filename": "access.log", "size": 128}],
                "/api/open/site/12/log/limit": state["log_limits"][request.url.params.get("type", "access")],
                "/api/open/policy": {"data": [state["policy"]], "total": 1}}
            if path == "/api/open/policy/detail":
                value = state["auth"] if request.url.params["id"] == "41" else state["challenge"] if request.url.params["id"] == "42" else state["policy"]
            elif path == "/api/open/site/12/log/detail":
                return httpx.Response(200, text='event: line\ndata: GET /index.html\n\nevent: line\ndata: Authorization: private-auth\n\nevent: line\ndata: '+TOKEN+'\n\n', headers={"content-type": "text/event-stream"})
            else:
                assert path in values, path
                value = values[path]
            return httpx.Response(200, json={"data": value, "err": ""})
        payload = json.loads(request.content) if request.content else None
        state["writes"].append((method, path, payload))
        if state["fail_after"] is not None and len(state["writes"]) > state["fail_after"]:
            raise httpx.ReadTimeout("private failure " + TOKEN)
        if state["failure"] == "timeout":
            raise httpx.ReadTimeout(TOKEN)
        if state["failure"] == "business":
            return httpx.Response(200, json={"data": None, "err": "private-failure"})
        if state["failure"] != "mismatch":
            if path == "/api/open/site/12/proxy": state["proxy"].update(payload)
            elif path == "/api/open/site/12/nginx_config": state["nginx"].update(payload)
            elif path == "/api/open/site/12/excludes": state["excludes"].update(payload)
            elif path == "/api/open/site/12/waiting": state["waiting"].update(payload)
            elif path == "/api/open/site/12/chaos":
                state["chaos"].update(payload); state["site"]["chaos_is_enabled"] = payload["is_enabled"]
            elif path == "/api/open/site/challenge":
                state["challenge"].update({k:v for k,v in payload.items() if k not in {"id", "enable"}}); state["site"]["challenge_is_enabled"] = payload["enable"]
            elif path == "/api/open/site/defense":
                state["auth"].update({k:v for k,v in payload.items() if k not in {"id", "enable", "portal_redirect", "auth_challenge_timeout", "auth_challenge_timeout_unit"}})
                state["site"].update({k:payload[k] for k in ("portal_redirect", "auth_challenge_timeout", "auth_challenge_timeout_unit")}); state["site"]["auth_defense_id"] = 41 if payload["enable"] else 0
            elif path == "/api/open/site/12/semantics": state["modules"] = None if payload["use_global"] else payload["semantics"]
            elif path == "/api/open/security_posture/site": state["site"]["sp_enabled"] = payload["enabled"]
            elif path == "/api/open/site/12/log/limit": state["log_limits"][payload["type"]] = payload["limit"]
            elif path == "/api/open/site/12/resources": state["resources"] = [r for r in state["resources"] if r["id"] not in payload["ids"]]
            elif path == "/api/open/site/12/acl":
                if method == "POST": state["acl"].append({**payload, "id": max(r["id"] for r in state["acl"])+1})
                else:
                    for row in state["acl"]:
                        row.update(next((r for r in payload.get("rules", []) if r["id"]==row["id"]), {})); row["use_global"]=payload["use_global"]
            elif path.startswith("/api/open/site/12/acl/"):
                state["acl"]=[r for r in state["acl"] if r["id"]!=int(path.rsplit('/',1)[1])]
            elif path == "/api/open/site/12/forwarding_rules": state["forwarding"].append({"id": len(state["forwarding"])+100, "is_enabled":True, "updated_at":"now", **payload})
            elif path.startswith("/api/open/site/12/forwarding_rules/"):
                key=int(path.split('/')[6])
                if method=="DELETE": state["forwarding"]=[r for r in state["forwarding"] if r["id"]!=key]
                else: next(r for r in state["forwarding"] if r["id"]==key).update(payload)
            elif path == "/api/business/site/12/anti_tamper":
                if method=="PUT": state["tamper"]["refresh_time"]+=1
                else:
                    for key in payload["resource_ids"]:
                        resource=next(r for r in state["resources"] if r["id"]==key)
                        state["tamper"]["data"].append({"id": 100+key,"site_id":12,"path":resource["path"],"changed":False,"cache_time":101})
            elif path.startswith("/api/business/anti_tamper/"):
                key=int(path.rsplit('/',1)[1])
                if method=="DELETE":state["tamper"]["data"]=[r for r in state["tamper"]["data"] if r["id"]!=key]
                else:next(r for r in state["tamper"]["data"] if r["id"]==key)["changed"]=False
            else: pytest.fail("Unexpected mutation: "+path)
        return httpx.Response(200,json={"data":None,"err":""})
    return state, httpx.MockTransport(handler)


@pytest.fixture
def pages(tmp_path, monkeypatch):
    path=tmp_path/'private'/'connection.sqlite3'
    monkeypatch.setenv('SAFELINE_STATE_FILE',str(path))
    for key in ('SAFELINE_URL','SAFELINE_API_TOKEN'):monkeypatch.delenv(key,raising=False)
    state,transport=application_fixture()
    app=create_app(Settings(PVE,enabled_plugins=('safeline',)),pve_auth_transport=pve_transport(state),plugin_transports={'safeline':transport})
    with TestClient(app) as client:yield client,state,path


def save(client, section, changes, headers):
    path='/applications/12/'+section
    command=draft(client,path,changes,headers)
    response=client.put(BASE+path,json=command,headers=headers)
    assert response.status_code==200,response.text
    assert response.json()['outcome']=='confirmed',response.text
    return command,response


@pytest.mark.parametrize('section',sorted(SECTIONS))
def test_application_page_reads_are_safe_and_complete(pages,section):
    client,state,_=pages;headers=configure(client)
    response=client.get(BASE+'/applications/12/'+section,headers=headers)
    assert response.status_code==200,response.text
    assert len(response.json()['fingerprint'])==64
    assert TOKEN not in response.text and 'private-header' not in response.text and 'generated private config' not in response.text
    assert not state['writes']


@pytest.mark.parametrize('method,path', [('GET','/applications/12/auth'),('PUT','/applications/12/bot'),('GET','/applications/12/logs/access/preview?filename=access.log')])
def test_application_pages_authorize_before_storage_and_network(pages,method,path):
    client,state,db=pages
    response=client.request(method,BASE+path,json={})
    assert response.status_code==401 and not db.exists() and not state['waf']


@pytest.mark.parametrize('section,changes', [
    ('bot',{'enable':'yes'}), ('bot',{'chaos':[]}), ('bot',{'chaos':{'html_encryption':'yes'}}),
    ('bot',{'chaos':{'js_path':'/app.js'}}), ('bot',{'pattern':[[{'bad':'value'}]]}),
    ('auth',{'auth_source_ids':[99]}), ('auth',{'auth_source_ids':[True]}), ('auth',{'review':True}),
    ('auth',{'auth_challenge_timeout_unit':[]}), ('auth',{'portal_redirect':'https://['}),
    ('attack',{'modules':{'m_sqli':[]}}), ('attack',{'modules':{'future':'strict'}}),
    ('forwarding',{'action':[]}), ('forwarding',{'action':'edit','id':999,'values':{'comment':'other'}}),
    ('forwarding',{'action':'create','values':{'path_prefix':'/','upstreams':['http://one:80']}}),
    ('collection',{'prefixes':['assets']}), ('collection',{'content_types':'text/html'}),
    ('nginx',{'custom_location':'one line'}), ('log-access',{'limit':True}), ('log-error',{'limit':-1}),
    ('routes',{'action':'delete','ids':[999]}), ('tamper',{'action':'create','resource_ids':[999]}),
])
def test_invalid_application_changes_never_write(pages,section,changes):
    client,state,_=pages;headers=configure(client)
    command=draft(client,'/applications/12/'+section,changes,headers)
    response=client.put(BASE+'/applications/12/'+section,json=command,headers=headers)
    assert response.status_code==422,response.text
    assert not state['writes']


def test_basic_page_redacts_private_urls_and_reads_log_switches(pages):
    client,state,_=pages;headers=configure(client)
    state['site']['upstreams']=['https://user:private-password@backend:443']
    state['site']['comment']=TOKEN
    response=client.get(BASE+'/applications/12/basic',headers=headers)
    assert response.status_code==200 and TOKEN not in response.text and 'private-password' not in response.text
    values=response.json()['values']['resource']
    assert values['access_log_limit']==1024 and values['error_log_limit']==0


def test_authentication_source_secrets_stay_on_the_backend(pages):
    client,state,_=pages;headers=configure(client)
    state['portal']['auth_sources'][1]['client_secret']='private-client-secret'
    state['portal']['auth_sources'][1]['password']=TOKEN
    response=client.get(BASE+'/applications/12/auth',headers=headers)
    assert response.status_code==200 and 'private-client-secret' not in response.text and TOKEN not in response.text
    assert set(response.json()['portal']['auth_sources'][1])=={'id','type'}


def test_dynamic_protection_preserves_existing_large_watermark_images(pages):
    client,state,_=pages;headers=configure(client)
    image='data:image/png;base64,'+'a'*40000
    state['chaos']['img_watermark']=image
    response=client.get(BASE+'/applications/12/bot',headers=headers)
    assert response.status_code==200 and 'chaos' in response.json()['values'] and image not in response.text
    save(client,'bot',{'chaos':{'html_encryption':True}},headers)
    assert state['chaos']['img_watermark']==image


def test_free_statistics_are_locked_in_new_and_existing_editors(pages):
    client,state,_=pages;headers=configure(client);state['edition']=0
    for section in ('statistics',):
        command=draft(client,'/applications/12/'+section,{'stat_enabled':False},headers)
        assert client.put(BASE+'/applications/12/'+section,json=command,headers=headers).status_code==409
    edit=client.get(BASE+'/resources/applications/12/edit?section=settings',headers=headers).json()
    assert 'stat_enabled' not in edit['values'] and 'stat_enabled' in edit['locked']
    assert not state['writes']


def test_log_stream_processes_small_chunks_without_waiting_for_a_full_buffer():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for i in range(200):
                yield b'event: line\r'
                yield b'\ndata:   short line\r\n\r\n'
            # The client must already have reached its line limit.
            await asyncio.sleep(10)
            raise AssertionError('A bounded preview must stop reading the live stream')
    async def run():
        transport=httpx.MockTransport(lambda request:httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Stream()))
        client=SafeLineClient(Connection('https://fixture.invalid:9443',TOKEN),transport=transport)
        return await client.log_lines('12','access.log')
    lines,truncated=asyncio.run(run())
    assert len(lines)==200 and lines[0]=='  short line' and truncated


def test_application_proxy_preserves_headers_http3_and_supports_inheritance(pages):
    client,state,_=pages;headers=configure(client);before=copy.deepcopy(state['proxy'])
    save(client,'advanced',{'gzip':{'global':False,'value':True}},headers)
    assert state['proxy']['http_headers']==before['http_headers'] and state['proxy']['http3']==before['http3']
    state['edition']=0
    save(client,'advanced',{'gzip':{'global':True,'value':True}},headers)
    before=len(state['writes']);command=draft(client,'/applications/12/advanced',{'gzip':{'global':False,'value':False}},headers)
    assert client.put(BASE+'/applications/12/advanced',json=command,headers=headers).status_code==409
    assert len(state['writes'])==before


@pytest.mark.parametrize('section,changes', [
    ('nginx',{'custom_location':['proxy_hide_header X-Powered-By;']}),
    ('collection',{'prefixes':['/assets','/internal'],'content_types':['application/json']}),
    ('bot',{'enable':True,'chaos':{'html_encryption':True}}),
    ('auth',{'review':0,'auth_challenge_timeout':12,'auth_challenge_timeout_unit':'hour','auth_source_ids':[0,1]}),
    ('attack',{'use_global':False,'modules':{'m_sqli':'strict'}}),
    ('statistics',{'stat_enabled':False,'sp_enabled':True}),
    ('log-access',{'limit':2048}),
    ('log-error',{'limit':1024}),
])
def test_application_configuration_roundtrip_and_deduplication(pages,section,changes):
    client,state,db=pages;headers=configure(client)
    command,_=save(client,section,changes,headers);count=len(state['writes'])
    again=client.put(BASE+'/applications/12/'+section,json=command,headers=headers)
    assert again.json()['replayed'] is True and len(state['writes'])==count
    with sqlite3.connect(db) as connection:assert TOKEN not in str(connection.execute('SELECT * FROM edits').fetchall())
    assert state['modules'].get('m_response')=='disable'


def test_cc_updates_and_additions_preserve_other_rule_conditions(pages):
    client,state,_=pages;headers=configure(client)
    rules=copy.deepcopy(state['acl']);rules[0]['count']=600
    rules.append({**copy.deepcopy(rules[0]),'id':0,'name':'Custom','built_in':False,'type':'req'})
    save(client,'cc',{'rules':rules,'waiting':{'is_enabled':True}},headers)
    assert state['acl'][0]['count']==600 and len(state['acl'])==4
    assert [w[0] for w in state['writes']]==['POST','PUT','POST']
    save(client,'cc',{'use_global':True},headers)
    assert all(r['use_global'] for r in state['acl'])


def test_forwarding_crud_switch_and_license_limit(pages):
    client,state,_=pages;headers=configure(client)
    save(client,'forwarding',{'action':'create','values':{'comment':'API','path_prefix':'/api','upstreams':['https://upstream:8443']}},headers)
    key=state['forwarding'][0]['id']
    save(client,'forwarding',{'action':'edit','id':key,'values':{'comment':'API v2'}},headers)
    save(client,'forwarding',{'action':'switch','id':key,'values':{'is_enabled':False}},headers)
    assert state['forwarding'][0]['is_enabled'] is False
    save(client,'forwarding',{'action':'delete','id':key},headers)
    assert state['forwarding']==[]
    state['edition']=0
    for i in range(3):save(client,'forwarding',{'action':'create','values':{'comment':'','path_prefix':'/v'+str(i),'upstreams':['http://upstream']}},headers)
    command=draft(client,'/applications/12/forwarding',{'action':'create','values':{'path_prefix':'/v4','upstreams':['http://upstream']}},headers)
    assert client.put(BASE+'/applications/12/forwarding',json=command,headers=headers).json()['detail']=='safeline_forwarding_limit'


def test_route_inventory_omits_headers_and_deletes_only_owned_resource_ids(pages):
    client,state,_=pages;headers=configure(client)
    response=client.get(BASE+'/applications/12/routes',headers=headers)
    assert 'req_header' not in response.text and 'private-header' not in response.text
    command=draft(client,'/applications/12/routes',{'action':'delete','ids':[999]},headers)
    assert client.put(BASE+'/applications/12/routes',json=command,headers=headers).status_code==422
    assert not state['writes']
    save(client,'routes',{'action':'delete','ids':[61,62]},headers)
    assert not state['resources']


def test_commercial_tamper_actions_and_free_edition_do_not_contact_paid_endpoints(pages):
    client,state,_=pages;headers=configure(client)
    save(client,'tamper',{'action':'accept','id':51},headers)
    save(client,'tamper',{'action':'create','resource_ids':[62]},headers)
    save(client,'tamper',{'action':'refresh'},headers)
    save(client,'tamper',{'action':'delete','id':51},headers)
    assert len(state['tamper']['data'])==1
    state['edition']=0;state['waf'].clear()
    response=client.get(BASE+'/applications/12/tamper',headers=headers)
    assert response.json()['locked']==['accept','create','delete','refresh']
    assert not any('/business/' in r.url.path for r in state['waf'])


def test_log_preview_is_bounded_redacted_and_does_not_allow_arbitrary_files(pages):
    client,state,_=pages;headers=configure(client)
    response=client.get(BASE+'/applications/12/logs/access/preview?filename=access.log',headers=headers)
    assert response.status_code==200 and response.json()['lines'][0]=='GET /index.html'
    assert TOKEN not in response.text and 'private-auth' not in response.text
    assert client.get(BASE+'/applications/12/logs/access/preview?filename=another.log',headers=headers).status_code==404
    assert client.get(BASE+'/applications/12/logs/access/preview?filename=../secret',headers=headers).status_code==422


def test_partial_multi_endpoint_write_stays_unknown_and_never_retries(pages):
    client,state,_=pages;headers=configure(client);state['fail_after']=1
    command=draft(client,'/applications/12/bot',{'enable':True,'chaos':{'html_encryption':True}},headers)
    response=client.put(BASE+'/applications/12/bot',json=command,headers=headers)
    assert response.json()['outcome']=='unknown' and state['site']['challenge_is_enabled'] is True
    assert state['chaos']['html_encryption'] is False
    again=client.put(BASE+'/applications/12/bot',json=command,headers=headers)
    assert again.json()['replayed'] and len(state['writes'])==2


def test_stale_settings_and_missing_csrf_never_write(pages):
    client,state,_=pages;headers=configure(client)
    command=draft(client,'/applications/12/nginx',{'custom_location':['set $x 1;']},headers)
    assert client.put(BASE+'/applications/12/nginx',json=command,headers={'cookie':COOKIE}).status_code==403
    state['nginx']['custom_location'].append('set $other 1;')
    assert client.put(BASE+'/applications/12/nginx',json=command,headers=headers).status_code==409
    assert not state['writes']
