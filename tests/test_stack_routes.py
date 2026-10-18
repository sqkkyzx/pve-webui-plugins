import json
import sqlite3
import uuid

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from plugins.komodo.actions import KomodoActionError
from app.main import Settings, create_app
from app.pve_auth import PveAuthenticator
from plugins.komodo.komodo import KomodoClient
import plugins.komodo.stack_routes as routes

STACK='a'*24
SERVER='b'*24
VERSION='c'*64
PATH='/pve-komodo/api/stacks/'+STACK
COOKIE={'cookie':'PVEAuthCookie=test-ticket'}


@pytest.fixture
def boundary(monkeypatch,tmp_path):
    state={'reads':[], 'logs':[], 'writes':[], 'actions':[], 'auth':[], 'error':None, 'unknown':False, 'version':VERSION}
    ledger=tmp_path/'actions.sqlite3'
    async def auth(self,request):
        state['auth'].append(request.cookies.get('PVEAuthCookie'))
        if request.cookies.get('PVEAuthCookie')!='test-ticket': raise HTTPException(401,'pve_auth_required')
        return 'root@pam'
    async def read(stack_id,**kwargs):
        state['reads'].append(stack_id)
        return {'stack_id':stack_id,'server_id':SERVER,'config':{},'version':state['version'],'can_write':True,'can_execute':True}
    async def logs(stack_id,**kwargs):
        state['logs'].append((stack_id,kwargs))
        return {'stack_id':stack_id,'stdout':'safe-output','stderr':''}
    async def write(stack_id,**kwargs):
        await kwargs['before_write']()
        with sqlite3.connect(ledger) as db:
            assert db.execute('SELECT COUNT(*) FROM actions WHERE status IS NULL').fetchone()[0]==1
        state['writes'].append((stack_id,kwargs))
        if state['error']: raise state['error']
        if state['unknown']: raise RuntimeError('PRIVATE_UPSTREAM_SECRET')
        return {'ok':True,'status':'completed','operation_id':None,'error_code':None}
    async def action(self,section,action,ids,*,before_write=None):
        await before_write()
        state['actions'].append((section,action,ids))
        return {'section':section,'action':action,'results':[{'id':ids[0],'ok':True,'status':'submitted'}]}
    monkeypatch.setattr(PveAuthenticator,'authenticate',auth)
    monkeypatch.setattr(routes,'get_stack_details',read)
    monkeypatch.setattr(routes,'get_stack_logs',logs)
    monkeypatch.setattr(routes,'update_stack_details',write)
    monkeypatch.setattr(KomodoClient,'perform_action',action)
    client=TestClient(create_app(Settings('https://pve.example:8006',komodo_url='http://core:9120',
        komodo_api_key='private-key',komodo_api_secret='private-secret',action_state_file=str(ledger))))
    response=client.get(PATH,headers=COOKIE)
    assert response.status_code==200
    state['headers']={**COOKIE,'origin':'http://testserver','x-pve-komodo-csrf':response.json()['csrf_token']}
    state['ledger']=ledger
    return client,state


def command(**changes):
    return {'action':'config','server_id':SERVER,'base_version':VERSION,'request_id':str(uuid.uuid4()),'config':{'auto_pull':True},**changes}


def test_authentication_precedes_stack_reads_and_writes(boundary):
    client,state=boundary
    before=len(state['reads'])
    for method,path in [('GET',PATH),('POST',PATH),('GET',PATH+'/logs')]:
        assert client.request(method,path).status_code==401
    assert len(state['reads'])==before and not state['writes'] and not state['logs']
    response=client.get(PATH,headers=COOKIE)
    assert response.status_code==200 and 'no-store' in response.headers['cache-control']
    assert 'private-key' not in response.text and 'private-secret' not in response.text


@pytest.mark.parametrize('suffix',['?_dc=1','?server_id='+SERVER,'/logs?tail=1&tail=2','/logs?tail=0',
    '/logs?tail=5001','/logs?timestamps=1','/logs?invert=TRUE','/logs?unknown=x','/logs?search='+'a'*1001])
def test_strict_queries_fail_before_module_reads(boundary,suffix):
    client,state=boundary
    before=len(state['reads'])
    assert client.get(PATH+suffix,headers=COOKIE).status_code==422
    assert len(state['reads'])==before and not state['logs']


def test_log_filters_accept_multiple_services_and_exact_boolean_values(boundary):
    client,state=boundary
    r=client.get(PATH+'/logs',headers=COOKIE,params=[('services','web'),('services','worker'),('tail','200'),
        ('timestamps','true'),('search','error'),('invert','true')])
    assert r.status_code==200 and 'no-store' in r.headers['cache-control']
    _,params=state['logs'][0]
    assert params['services']==['web','worker'] and params['tail']==200 and params['timestamps'] is True
    assert params['search']=='error' and params['invert'] is True


@pytest.mark.parametrize('headers',[COOKIE,{**COOKIE,'x-pve-komodo-csrf':'invalid'},
    {**COOKIE,'origin':'https://attacker.example','x-pve-komodo-csrf':'invalid'}])
def test_csrf_and_origin_fail_closed(boundary,headers):
    client,state=boundary
    assert client.post(PATH,headers=headers,json=command()).status_code==403
    assert not state['writes']


@pytest.mark.parametrize('change',[{'action':'exec'},{'server_id':'name'},{'base_version':'old'}, {'extra':'x'},
    {'config':{}},{'file_path':'compose.yaml'},{'contents':'x'}, {'action':'file'}, {'request_id':'bad'}])
def test_write_shape_rejects_ambiguous_or_invalid_payloads(boundary,change):
    client,state=boundary
    assert client.post(PATH,headers=state['headers'],json=command(**change)).status_code==422
    assert not state['writes']


def test_known_file_write_is_authenticated_again_and_deduplicated_without_plaintext_ledger(boundary):
    client,state=boundary
    cmd=command(action='file',file_path='compose.yaml',contents='USER_FILE_SECRET')
    del cmd['config']
    assert client.post(PATH,headers=state['headers'],json=cmd).status_code==200
    auth=len(state['auth'])
    assert client.post(PATH,headers=state['headers'],json=cmd).status_code==200
    assert len(state['writes'])==1 and len(state['auth'])==auth+1
    assert state['writes'][0][1]['expected_server_id']==SERVER
    assert client.post(PATH,headers=state['headers'],json={**cmd,'contents':'other'}).status_code==409
    with sqlite3.connect(state['ledger']) as db:
        dump='\n'.join(db.iterdump())
        assert 'USER_FILE_SECRET' not in dump


def test_unknown_write_is_sticky_and_never_retried(boundary):
    client,state=boundary
    state['unknown']=True
    cmd=command()
    first=client.post(PATH,headers=state['headers'],json=cmd)
    assert first.status_code==200 and first.json()['status']=='outcome_unknown'
    assert 'PRIVATE_UPSTREAM' not in first.text
    assert client.post(PATH,headers=state['headers'],json=cmd).json()==first.json()
    assert len(state['writes'])==1


def test_known_preflight_rejection_is_a_sanitized_receipt(boundary):
    client,state=boundary
    state['error']=KomodoActionError('stack_version_conflict',409)
    cmd=command()
    assert client.post(PATH,headers=state['headers'],json=cmd).json()=={'detail':'stack_version_conflict'}
    assert client.post(PATH,headers=state['headers'],json=cmd).status_code==409
    assert len(state['writes'])==1


@pytest.mark.parametrize('action',['deploy','pull','restart','pause','stop','destroy'])
def test_detail_actions_pin_current_target_version_and_deduplicate(boundary,action):
    client,state=boundary
    cmd=command(action=action); del cmd['config']
    first=client.post(PATH+'/actions',headers=state['headers'],json=cmd)
    assert first.status_code==200 and first.json()['results'][0]['status']=='submitted'
    assert client.post(PATH+'/actions',headers=state['headers'],json=cmd).json()==first.json()
    assert state['actions']==[('stacks',action,[STACK])]


def test_detail_action_rechecks_version_immediately_before_execute(boundary):
    client,state=boundary
    cmd=command(action='deploy'); del cmd['config']
    state['version']='d'*64
    response=client.post(PATH+'/actions',headers=state['headers'],json=cmd)
    assert response.status_code==409 and response.json()=={'detail':'stack_version_conflict'}
    assert not state['actions']
    state['version']=VERSION
    assert client.post(PATH+'/actions',headers=state['headers'],json=cmd).status_code==409
    assert not state['actions']


def test_detail_action_authentication_csrf_and_schema_reject_before_execution(boundary):
    client,state=boundary
    cmd=command(action='deploy'); del cmd['config']
    assert client.post(PATH+'/actions',json=cmd).status_code==401
    assert client.post(PATH+'/actions',headers=COOKIE,json=cmd).status_code==403
    assert client.post(PATH+'/actions',headers=state['headers'],json={**cmd,'action':'delete'}).status_code==422
    assert client.post(PATH+'/actions',headers=state['headers'],json={**cmd,'base_version':'old'}).status_code==422
    assert not state['actions']
