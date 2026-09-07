"""Exercise the CLI through a fake urlopen; no server, credentials, DB or Gate client."""
from __future__ import annotations
import argparse
import importlib.util
import io
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/test_latency.py'
spec = importlib.util.spec_from_file_location('grid_latency_script_under_test', SCRIPT)
latency = importlib.util.module_from_spec(spec)
spec.loader.exec_module(latency)


def report():
    def metric(value):
        return {'count': 1, 'min': value, 'p50': value, 'p95': value, 'max': value, 'avg': value, 'total': value}
    return {'source': 'gate', 'read_only': True, 'duration_ms': 120,
            'endpoint_stats': [{'endpoint': '/tradfi/orders', 'label': '委托读取', 'method': 'GET',
                'count': 1, 'success': 1, 'failed': 0,
                'stats': {'total_ms': metric(120), 'network_ms': metric(80),
                          'throttle_wait_ms': metric(40), 'clock_wait_ms': None, 'backoff_ms': metric(0), 'processing_ms': metric(0)},
                'samples': [{'index': 1, 'status': 'success', 'elapsed_ms': 120, 'error_code': None}]}],
            'stages': [{'label': '操作队列等待', 'duration_ms': 250, 'status': 'timeout', 'note': '等待下界'}],
            'findings': [{'title': '未测下单', 'detail': '只有只读采样。'}],
            'recent_real_requests': {'items': [], 'note': 'HTTP 返回不是最终委托确认。'},
            'estimates': [], 'limits': {'notes': ['小样本不代表长期表现']}}


def job(status='queued', with_report=False):
    return {'id': 'diag_test', 'symbol': 'XAUUSD', 'samples': 3, 'status': status,
            'created_at': '2026-09-07T14:00:00+00:00', 'started_at': '2026-09-07T14:00:01+00:00',
            'finished_at': None if status in ('queued', 'running') else '2026-09-07T14:00:10+00:00',
            'progress': {'completed': 1 if with_report else 0, 'total': 15, 'stage': '读取测试'},
            'report': report() if with_report else None}


class JsonResponse(io.StringIO):
    def __init__(self, value):
        super().__init__(json.dumps(value))


def fake_network(monkeypatch, poll=None, start=None):
    calls = []
    def open_(request, timeout=20):
        calls.append((request.method, request.full_url, request.data))
        assert request.full_url.startswith('http://127.0.0.1:18473/api/')
        path = request.full_url.removeprefix('http://127.0.0.1:18473/api')
        if path == '/health':
            assert request.method == 'GET'
            return JsonResponse({'app': 'grid-studio', 'ok': True})
        if path == '/diagnostics/start':
            assert request.method == 'POST'
            assert json.loads(request.data) == {'symbol': 'XAUUSD', 'samples': 3}
            return JsonResponse(start or job())
        if path == '/diagnostics/diag_test':
            assert request.method == 'GET'
            if isinstance(poll, BaseException): raise poll
            return JsonResponse(poll or job('completed', True))
        if path == '/diagnostics/diag_test/cancel':
            assert request.method == 'POST'
            return JsonResponse(job('cancelled', True))
        raise AssertionError(f'Unexpected diagnostic URL {path}')
    monkeypatch.setattr(latency, 'urlopen', open_)
    monkeypatch.setattr(latency.time, 'sleep', lambda _: None)
    return calls


def run(monkeypatch, tmp_path):
    monkeypatch.setattr(latency.sys, 'argv', ['test_latency.py', '--output', str(tmp_path)])
    return latency.main()


def test_cli_completed_job_only_calls_health_and_diagnostic_routes(monkeypatch, tmp_path):
    calls = fake_network(monkeypatch)
    assert run(monkeypatch, tmp_path) == 0
    assert [(method, url.rsplit('/api', 1)[1]) for method, url, _ in calls] == [
        ('GET', '/health'), ('GET', '/health'), ('GET', '/health'),
        ('POST', '/diagnostics/start'), ('GET', '/diagnostics/diag_test')]
    raw = json.loads(next(tmp_path.glob('*.json')).read_text(encoding='utf-8'))
    text = next(tmp_path.glob('*.md')).read_text(encoding='utf-8')
    assert raw['finished_at'] == '2026-09-07T14:00:10+00:00'
    assert raw['client_probe']['source'] == 'script'
    assert len(raw['client_probe']['samples']) == 3
    assert 'completed' in text and '120.00 ms' in text
    assert '校时均值' in text and '—' in text
    assert 'HTTP 返回不是最终委托确认' in text
    assert '无样本' in text


def test_cli_deadline_cancels_diagnostic_only_and_exports_partial_report(monkeypatch, tmp_path):
    calls = fake_network(monkeypatch)
    ticks = iter([0, 126])
    monkeypatch.setattr(latency.time, 'monotonic', lambda: next(ticks))
    assert run(monkeypatch, tmp_path) == 2
    assert calls[-1][0] == 'POST' and calls[-1][1].endswith('/diagnostics/diag_test/cancel')
    assert all('/strategies' not in url and '/tradfi/' not in url for _, url, _ in calls)
    raw = json.loads(next(tmp_path.glob('*.json')).read_text(encoding='utf-8'))
    assert raw['status'] == 'cancelled' and raw['report']['endpoint_stats'][0]['count'] == 1


def test_cli_keyboard_interrupt_cancels_diagnostic_not_live_strategy(monkeypatch, tmp_path):
    calls = fake_network(monkeypatch, poll=KeyboardInterrupt())
    assert run(monkeypatch, tmp_path) == 2
    assert calls[-1][1].endswith('/diagnostics/diag_test/cancel')
    assert all(method == 'GET' or '/diagnostics/' in url for method, url, _ in calls)


def test_cli_report_handles_iso_and_no_completed_report_without_faking_samples():
    partial = job('cancelled')
    text = latency.markdown(partial, {'measured_at': '2026-09-07T14:00:00+00:00', 'samples': []})
    assert 'cancelled' in text and '2026-09-07T14:00:00+00:00' in text
    assert '无样本' in text
    assert latency.milliseconds(None) == '—'
    assert latency.milliseconds(0) == '0.00 ms'


@pytest.mark.parametrize('path', ['/strategies', '/strategies/known/cancel', '/strategies/known/orders/12',
    '/tradfi/orders', '/diagnostics/../strategies', '/diagnostics/%2e%2e', '/diagnostics/x?next=/strategies'])
def test_cli_request_whitelist_rejects_trade_paths_before_transport(monkeypatch, path):
    calls = []
    monkeypatch.setattr(latency, 'urlopen', lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError):
        latency.request('http://127.0.0.1:18473', path, {})
    assert not calls


@pytest.mark.parametrize('url', ['https://127.0.0.1:18473', 'http://example.com', 'http://127.0.0.1.example.com',
    'http://user:pass@localhost:18473', 'http://localhost:18473/api', 'http://localhost:18473?x=1',
    'http://localhost:18473#fragment', 'file:///tmp/console', 'http://localhost:70000',
    'http://localhost:abc', 'http://localhost:-1'])
def test_cli_local_url_rejects_remote_or_malformed_destinations(url):
    with pytest.raises(argparse.ArgumentTypeError):
        latency.local_url(url)


def test_cli_local_url_accepts_only_plain_loopback_console_origin():
    assert latency.local_url('http://127.0.0.1:18473/') == 'http://127.0.0.1:18473'
    assert latency.local_url('http://localhost:18474') == 'http://localhost:18474'


def test_cli_http_error_never_exports_raw_endpoint_content(monkeypatch):
    def fail(*args, **kwargs):
        raise HTTPError('http://127.0.0.1:18473/api/diagnostics/start', 409, 'private marker', None, None)
    monkeypatch.setattr(latency, 'urlopen', fail)
    with pytest.raises(RuntimeError, match='HTTP 409') as error:
        latency.request('http://127.0.0.1:18473', '/diagnostics/start', {'symbol': 'XAUUSD', 'samples': 3})
    assert 'private marker' not in str(error.value)


def test_cli_invalid_arguments_make_no_transport_calls(monkeypatch, tmp_path):
    calls = fake_network(monkeypatch)
    monkeypatch.setattr(latency.sys, 'argv', ['test_latency.py', '--symbol', '../orders', '--output', str(tmp_path)])
    with pytest.raises(SystemExit) as error:
        latency.main()
    assert error.value.code == 2 and not calls


@pytest.mark.parametrize('valid', [True, False])
def test_cli_authenticates_once_and_never_exports_token(monkeypatch, tmp_path, capsys, valid):
    calls = fake_network(monkeypatch)
    normal_open = latency.urlopen
    authenticated = False
    logins = []
    def open_(request, timeout=20):
        nonlocal authenticated
        if request.full_url.endswith('/auth/login'):
            logins.append(json.loads(request.data))
            if valid:
                authenticated = True
                return JsonResponse({'authenticated': True})
        elif authenticated:
            return normal_open(request, timeout)
        raise HTTPError(request.full_url, 401, 'unauthorized', None, None)
    monkeypatch.setattr(latency, 'urlopen', open_)
    monkeypatch.setenv('GRID_ACCESS_TOKEN', 'test-only-cli-secret')
    assert run(monkeypatch, tmp_path) == (0 if valid else 1)
    assert logins == [{'token': 'test-only-cli-secret'}]
    captured = capsys.readouterr()
    assert 'test-only-cli-secret' not in captured.out + captured.err
    for output in tmp_path.iterdir():
        assert 'test-only-cli-secret' not in output.read_text(encoding='utf-8')
    if not valid:
        assert not calls and 'Incorrect console access token' in captured.err


def test_cli_missing_noninteractive_token_stops_before_diagnostics(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv('GRID_ACCESS_TOKEN', raising=False)
    monkeypatch.setattr(latency.sys.stdin, 'isatty', lambda: False)
    calls = []
    def fail(request, timeout=20):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, 401, 'unauthorized', None, None)
    monkeypatch.setattr(latency, 'urlopen', fail)
    assert run(monkeypatch, tmp_path) == 1
    assert calls == ['http://127.0.0.1:18473/api/health']
    assert 'Set GRID_ACCESS_TOKEN' in capsys.readouterr().err
