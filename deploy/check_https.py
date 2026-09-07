"""Verify public certificate selection and the protected app through Caddy.

Connect over the private Compose network while verifying the configured public
host. Python deliberately omits SNI for IP literals, like ordinary browsers.
No access token or trading credentials are sent.
"""
from __future__ import annotations

import http.client
import json
import socket
import ssl
import sys
import time


class ProxyHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        raw = socket.create_connection(('https', 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def verify(host: str) -> None:
    connection = ProxyHTTPSConnection(host, timeout=5, context=ssl.create_default_context())
    try:
        connection.request('GET', '/api/auth/session', headers={'Host': host})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f'Application returned HTTP {response.status}')
        state = json.loads(response.read(4096))
        if state.get('app') != 'grid-studio' or state.get('configured') is not True:
            raise ValueError('Access verification is not ready')
        if state.get('authenticated') is not False:
            raise ValueError('Anonymous access was unexpectedly authenticated')
    finally:
        connection.close()


def main() -> int:
    host = sys.argv[1]
    deadline = time.monotonic() + 90
    error = ''
    while time.monotonic() < deadline:
        try:
            verify(host)
            print('HTTPS certificate and access-verification endpoint passed.', flush=True)
            return 0
        except (OSError, http.client.HTTPException, ValueError) as exc:
            error = str(exc)
            print('Waiting for verified HTTPS access...', flush=True)
            time.sleep(3)
    print(f'HTTPS verification failed: {error}', file=sys.stderr)
    print('Check the public host, inbound TCP 80/443, and the https container logs. Services remain running.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
