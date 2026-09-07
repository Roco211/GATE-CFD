import pytest
from fastapi.testclient import TestClient
from app.access import COOKIE, token_hash

TEST_TOKEN = 'isolated-test-access-token'
TEST_HASH = token_hash(TEST_TOKEN)


@pytest.fixture(autouse=True)
def isolated_access_configuration(monkeypatch, request):
    monkeypatch.setenv('GRID_ACCESS_TOKEN_HASH', TEST_HASH)
    monkeypatch.delenv('GRID_CREDENTIAL_KEY_FILE', raising=False)
    if request.node.get_closest_marker('access_control'):
        return
    original = TestClient.__enter__
    def authenticated_enter(client):
        result = original(client)
        # Existing trading tests run as authenticated users; access tests below
        # exercise the real login endpoint without this fixture session.
        client.cookies.set(COOKIE, client.app.state.access.create_session('isolated-test'))
        return result
    monkeypatch.setattr(TestClient, '__enter__', authenticated_enter)
