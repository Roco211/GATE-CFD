"""Use real local TLS to check the deployment probe; no external services."""
import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import ipaddress
import json
from pathlib import Path
import ssl
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import pytest

spec = importlib.util.spec_from_file_location('deploy_https_probe', Path(__file__).resolve().parents[2]/'deploy/check_https.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    host = '192.0.2.45'
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Isolated test')])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=5))
        .not_valid_after(now+datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(host))]),critical=False)
        .sign(key, hashes.SHA256()))
    cert_file, key_file = tmp_path/'cert.pem', tmp_path/'cert.key'
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    state = {'app': 'grid-studio', 'configured': True, 'authenticated': False}
    observed = {'sni': [], 'requests': []}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observed['requests'].append((self.path, self.headers.get('Host'), self.headers.get('Cookie')))
            body = json.dumps(state).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    context.set_servername_callback(lambda sock, name, ctx: observed['sni'].append(name))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':0.01}, daemon=True)
    thread.start()
    original_connect = probe.socket.create_connection
    original_context = ssl.create_default_context
    def connect(address, timeout):
        assert address == ('https',443)
        return original_connect(server.server_address, timeout=timeout)
    monkeypatch.setattr(probe.socket, 'create_connection', connect)
    monkeypatch.setattr(probe.ssl, 'create_default_context', lambda: original_context(cafile=str(cert_file)))
    try:
        yield host, state, observed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_https_probe_verifies_ip_certificate_without_sni_or_credentials(endpoint):
    host, _, observed = endpoint
    probe.verify(host)
    assert observed['sni'] == [None]
    assert observed['requests'] == [('/api/auth/session', host, None)]


def test_https_probe_rejects_certificate_for_another_ip(endpoint):
    _, _, observed = endpoint
    with pytest.raises(ssl.SSLCertVerificationError):
        probe.verify('192.0.2.46')
    assert not observed['requests']


@pytest.mark.parametrize('changed', [{'configured': False}, {'app':'other-app'}, {'authenticated':True}])
def test_https_probe_rejects_unready_or_unprotected_app(endpoint, changed):
    host, state, _ = endpoint
    state.update(changed)
    with pytest.raises(ValueError):
        probe.verify(host)
