"""Configure a token interactively, persisting only a salted password hash."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app.access import token_hash


def save_access(path: Path, token: str):
    record = json.dumps({'version': 1, 'token_hash': token_hash(token)})
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.access-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(record)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Configure the console access token')
    parser.add_argument('--path', type=Path, default=ROOT / 'data/access-control.json')
    parser.add_argument('--stdin', action='store_true', help='Read token from protected stdin instead of terminal')
    args = parser.parse_args()
    token = sys.stdin.readline().rstrip('\r\n') if args.stdin else getpass.getpass('Access token (8-256 characters): ')
    if not args.stdin and token != getpass.getpass('Confirm token: '):
        raise SystemExit('Tokens do not match; no changes made.')
    save_access(args.path, token)
    print('Access token configured. Restart the service to invalidate old sessions.')
