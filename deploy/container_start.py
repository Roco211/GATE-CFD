"""Initialize private persistent files, then run exactly one trading worker."""
import json
import os
from pathlib import Path
import sys

from cryptography.fernet import Fernet

root = Path(__file__).resolve().parents[1]
database = Path(os.environ.get('GRID_DATABASE', '/var/lib/grid-studio/live.sqlite3'))
key = Path(os.environ['GRID_CREDENTIAL_KEY_FILE'])
database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
access = database.parent / 'access-control.json'
if not access.exists():
    default = json.loads((root/'deploy/access-default.json').read_text())
    descriptor = os.open(access, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(default, stream)
if not key.exists():
    descriptor = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(Fernet.generate_key())
os.execv(sys.executable, [sys.executable, '-m', 'uvicorn', 'app.main:app', '--app-dir', 'backend',
         '--host', '0.0.0.0', '--port', '18473', '--workers', '1', '--proxy-headers',
         '--forwarded-allow-ips', '*', '--timeout-graceful-shutdown', '15', '--no-access-log'])
