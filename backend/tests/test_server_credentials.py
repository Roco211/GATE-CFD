import json
import os

from cryptography.fernet import Fernet
import pytest

from app.credentials import CredentialStore


@pytest.fixture
def server_store(tmp_path):
    key = tmp_path / 'server.key'
    key.write_bytes(Fernet.generate_key())
    key.chmod(0o600)
    return CredentialStore(tmp_path / 'credentials.json', key_file=key)


def test_server_credentials_survive_restart_without_plaintext(server_store):
    server_store.save('fixture-public-key', 'fixture-private-secret')
    text = server_store.path.read_text()
    assert 'fixture-public-key' not in text and 'fixture-private-secret' not in text
    assert json.loads(text)['protection'] == 'fernet-file-key'
    restored = CredentialStore(server_store.path, key_file=server_store.key_file)
    assert restored.load() == ('fixture-public-key', 'fixture-private-secret')
    if os.name != 'nt':
        assert server_store.path.stat().st_mode & 0o077 == 0


def test_wrong_server_key_cannot_decrypt(server_store):
    server_store.save('fixture-key', 'fixture-secret')
    server_store.key_file.write_bytes(Fernet.generate_key())
    with pytest.raises(ValueError, match='无法读取'):
        server_store.load()


def test_missing_deployment_key_never_overwrites_saved_credentials(server_store):
    server_store.save('fixture-key', 'fixture-secret')
    original = server_store.path.read_bytes()
    server_store.key_file.unlink()
    with pytest.raises(ValueError, match='服务器加密密钥不可用'):
        server_store.save('new-fixture', 'new-secret')
    assert server_store.path.read_bytes() == original


def test_tampered_ciphertext_is_rejected(server_store):
    server_store.save('fixture-key', 'fixture-secret')
    record = json.loads(server_store.path.read_text())
    record['ciphertext'] = record['ciphertext'][:-8] + 'QUFBQUFB'
    server_store.path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match='无法读取'):
        server_store.load()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX key permissions')
def test_world_readable_key_is_rejected(server_store):
    server_store.key_file.chmod(0o644)
    with pytest.raises(ValueError, match='访问权限'):
        server_store.save('fixture-key', 'fixture-secret')


@pytest.mark.skipif(os.name == 'nt', reason='POSIX symlinks')
def test_symlink_key_is_rejected(server_store, tmp_path):
    link = tmp_path / 'key-link'
    link.symlink_to(server_store.key_file)
    with pytest.raises(ValueError, match='不可用'):
        CredentialStore(server_store.path, key_file=link).save('fixture-key', 'fixture-secret')


def test_windows_record_is_not_silently_treated_as_server_encryption(server_store):
    server_store.path.write_text(json.dumps({'version': 1, 'protection': 'windows-dpapi-user', 'ciphertext': 'fixture'}))
    with pytest.raises(ValueError):
        server_store.load()
