import asyncio
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from app import main
from app.access import AccessControl, COOKIE, SESSION_SECONDS, check_token, token_hash
from conftest import TEST_TOKEN

pytestmark = pytest.mark.access_control
HEADERS = {'Origin':'http://testserver','X-Grid-Client':'grid-studio'}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('GATE_API_KEY','')
    monkeypatch.setenv('GATE_API_SECRET','')
    with TestClient(main.create_app(tmp_path/'live.sqlite3',auto_tick=False)) as client:
        yield client


@pytest.mark.parametrize('path',['/api/state','/api/health','/api/connection','/api/templates',
    '/api/diagnostics/latest','/api/market/stream','/api/gate/symbols'])
def test_anonymous_cannot_read_any_account_or_market_api(client,path):
    response=client.get(path)
    assert response.status_code==401
    assert response.json()['code']=='access_required'
    assert 'account_id' not in response.text


@pytest.mark.parametrize('path',['/api/strategies','/api/connection/check','/api/diagnostics/start','/api/auth/logout'])
def test_anonymous_cannot_write(client,path):
    assert client.post(path,headers=HEADERS,json={}).status_code==401


def test_pages_and_api_schema_require_access(client):
    for path in ('/','/docs','/openapi.json','/redoc'):
        response=client.get(path,follow_redirects=False)
        assert response.status_code==303 and response.headers['location']=='/login'


def test_wrong_token_never_creates_cookie_or_echoes_input(client):
    response=client.post('/api/auth/login',headers=HEADERS,json={'token':'private-wrong-token'})
    assert response.status_code==401 and COOKIE not in client.cookies
    assert 'private-wrong-token' not in response.text


def test_success_cookie_reuse_logout_and_old_cookie_rejected(client):
    response=client.post('/api/auth/login',headers=HEADERS,json={'token':TEST_TOKEN})
    assert response.status_code==200
    header=response.headers['set-cookie'].lower()
    assert 'httponly' in header and 'samesite=strict' in header and 'max-age=43200' in header
    cookie=client.cookies.get(COOKIE)
    assert TEST_TOKEN not in cookie and len(cookie)>30
    assert client.get('/api/state').status_code==200
    assert client.post('/api/auth/logout',headers=HEADERS).status_code==200
    client.cookies.set(COOKIE,cookie)
    assert client.get('/api/state').status_code==401


def test_https_cookie_secure_and_sessions_expire(client):
    response=client.post('https://testserver/api/auth/login',headers={**HEADERS,'Origin':'https://testserver'},json={'token':TEST_TOKEN})
    assert 'secure' in response.headers['set-cookie'].lower()
    access=client.app.state.access
    current=access.clock()
    access.clock=lambda:current+SESSION_SECONDS+1
    assert client.get('https://testserver/api/state').status_code==401


def test_cross_origin_login_and_mutation_rejected(client):
    assert client.post('/api/auth/login',headers={**HEADERS,'Origin':'https://attacker.example'},json={'token':TEST_TOKEN}).status_code==403
    assert client.post('/api/auth/login',json={'token':TEST_TOKEN}).status_code==403
    assert not client.app.state.access.sessions


def test_five_bad_attempts_throttle_without_hashing_sixth(client):
    for _ in range(5):
        assert client.post('/api/auth/login',headers=HEADERS,json={'token':'wrong-token'}).status_code==401
    with patch('app.main.check_token',side_effect=AssertionError('must not hash blocked attempt')):
        response=client.post('/api/auth/login',headers=HEADERS,json={'token':TEST_TOKEN})
    assert response.status_code==429 and response.headers['retry-after']=='300'


def test_missing_configuration_fails_closed(tmp_path,monkeypatch):
    monkeypatch.delenv('GRID_ACCESS_TOKEN_HASH')
    monkeypatch.setenv('GATE_API_KEY','')
    monkeypatch.setenv('GATE_API_SECRET','')
    with TestClient(main.create_app(tmp_path/'live.sqlite3',auto_tick=False)) as client:
        assert client.get('/api/auth/session').json()=={'app':'grid-studio','authenticated':False,'configured':False}
        assert client.post('/api/auth/login',headers=HEADERS,json={'token':TEST_TOKEN}).status_code==503
        assert client.get('/api/state').status_code==401


def test_session_is_not_valid_after_restart_or_forgery(tmp_path):
    first=AccessControl(tmp_path/'missing.json')
    second=AccessControl(tmp_path/'missing.json')
    cookie=first.create_session('client')
    assert first.valid_session(cookie)
    assert not second.valid_session(cookie)
    assert not first.valid_session(cookie+'x')


def test_hash_is_salted_and_malformed_values_fail_closed():
    first=token_hash(TEST_TOKEN)
    assert TEST_TOKEN not in first and first!=token_hash(TEST_TOKEN)
    assert check_token(TEST_TOKEN,first)
    assert not check_token('wrong-token',first)
    for broken in ('','sha256$1$xx$00','pbkdf2_sha256$999999999999$aa$bb'):
        assert not check_token(TEST_TOKEN,broken)


def test_logout_stops_an_existing_market_stream(client):
    cookie=client.app.state.access.create_session('stream-test')
    route=next(r for r in client.app.routes if getattr(r,'path',None)=='/api/market/stream')
    class RequestDouble:
        cookies={COOKIE:cookie}
        async def is_disconnected(self):
            return False
    async def check():
        response=await route.endpoint(RequestDouble(),symbol='XAUUSD')
        assert (await anext(response.body_iterator)).startswith('event: market')
        client.app.state.access.logout(cookie)
        with pytest.raises(StopAsyncIteration):
            await anext(response.body_iterator)
    asyncio.run(check())
