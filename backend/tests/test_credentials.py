import json
import os

import pytest

from app.credentials import CredentialStore


def test_empty_store_and_clear_are_idempotent(tmp_path):
    store = CredentialStore(tmp_path / 'credentials.json')
    assert store.load() is None
    store.clear()
    store.clear()


@pytest.mark.skipif(os.name != 'nt', reason='Uses Windows user DPAPI')
def test_dpapi_roundtrip_never_stores_plaintext(tmp_path):
    store = CredentialStore(tmp_path / 'credentials.json')
    store.save('test-only-api-key', 'test-only-private-secret')
    text = store.path.read_text()
    assert 'test-only-api-key' not in text
    assert 'test-only-private-secret' not in text
    assert json.loads(text)['protection'] == 'windows-dpapi-user'
    assert store.load() == ('test-only-api-key', 'test-only-private-secret')
    store.clear()
    assert store.load() is None


def test_corrupt_store_gives_safe_error(tmp_path):
    store = CredentialStore(tmp_path / 'credentials.json')
    store.path.write_text('{"secret":"sensitive-test-value"}')
    with pytest.raises(ValueError) as error:
        store.load()
    assert 'sensitive-test-value' not in str(error.value)


def test_encrypt_failure_preserves_previous_config(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path / 'credentials.json')
    store.path.write_text('existing-ciphertext')
    def fail(*args, **kwargs):
        raise ValueError('encryption failed')
    monkeypatch.setattr(store, '_crypt', fail)
    with pytest.raises(ValueError):
        store.save('test-key', 'test-secret')
    assert store.path.read_text() == 'existing-ciphertext'
