"""Run the same read-only diagnostic job as the web console; never sends trades."""
from __future__ import annotations

import argparse
import getpass
from http.cookiejar import CookieJar
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPCookieProcessor

urlopen = build_opener(HTTPCookieProcessor(CookieJar())).open


class AccessRequired(RuntimeError):
    pass

ROOT = Path(__file__).resolve().parents[1]


def local_url(value: str) -> str:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        raise argparse.ArgumentTypeError('Invalid local console URL or port') from None
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {'', '/'} or port == 0):
        raise argparse.ArgumentTypeError('Use the local console URL, e.g. http://127.0.0.1:18473')
    return value.rstrip('/')


def request(base: str, path: str, body: dict | None = None, timeout: float = 20) -> dict:
    allowed = path in {'/health', '/auth/login', '/diagnostics/start'} or bool(re.fullmatch(r'/diagnostics/[A-Za-z0-9_-]+(?:/cancel)?', path))
    if not allowed:
        raise ValueError('The latency script only supports health and diagnostic endpoints')
    req = Request(base + '/api' + path, method='GET' if body is None else 'POST',
                  headers={'Content-Type': 'application/json', 'X-Grid-Client': 'grid-studio'},
                  data=None if body is None else json.dumps(body).encode('utf-8'))
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code == 401:
            raise AccessRequired('Console access verification is required') from None
        raise RuntimeError(f'Local diagnostic endpoint returned HTTP {exc.code}') from None
    except (URLError, TimeoutError, OSError):
        raise RuntimeError('Cannot reach the local console; start Grid Studio and retry') from None


def milliseconds(value) -> str:
    return '—' if value is None else f'{value:,.2f} ms'


def authenticate(base: str) -> None:
    token = os.getenv('GRID_ACCESS_TOKEN')
    if not token:
        if not sys.stdin.isatty():
            raise AccessRequired('Set GRID_ACCESS_TOKEN for noninteractive diagnostic access')
        token = getpass.getpass('Console access token: ')
    try:
        request(base, '/auth/login', {'token': token})
    except AccessRequired:
        raise AccessRequired('Incorrect console access token') from None
    except RuntimeError as exc:
        raise AccessRequired(str(exc)) from None
    finally:
        del token


def markdown(job: dict, probe: dict) -> str:
    report = job.get('report') or {}
    lines = ['# Grid Studio 延迟诊断报告', '',
             f"品种：{job['symbol']} · 状态：{job['status']} · 每接口计划 {job['samples']} 次",
             f"记录时间：{probe['measured_at']}", '',
             '本脚本仅调用本机健康检查和只读诊断接口，不创建、取消或修改真实订单。', '',
             '## 脚本到本机后台', '',
             *[f"- 样本 {index+1}：{milliseconds(item['elapsed_ms'])} · {'成功' if item['success'] else '失败'}"
               for index, item in enumerate(probe['samples'])], '', '## 主要发现', '']
    for finding in report.get('findings', []):
        lines.append(f"- **{finding['title']}**：{finding['detail']}")
    lines.extend(['', '## Gate 接口耗时', '',
                  '| 接口 | 成功/总数 | 总耗时 P50 | 总耗时 P95 | 联网均值 | 排队均值 | 校时均值 | 重试等待均值 |',
                  '|---|---:|---:|---:|---:|---:|---:|---:|'])
    for row in report.get('endpoint_stats', []):
        stats = row['stats']
        metric = lambda name, field: milliseconds((stats.get(name) or {}).get(field))
        lines.append(f"| {row['label']} {row['endpoint']} | {row['success']}/{row['count']} | "
                     f"{metric('total_ms','p50')} | {metric('total_ms','p95')} | {metric('network_ms','avg')} | "
                     f"{metric('throttle_wait_ms','avg')} | {metric('clock_wait_ms','avg')} | {metric('backoff_ms','avg')} |")
    lines.extend(['', '联网包含连接、传输和 Gate 处理，未分别测 DNS/TLS/服务端执行。小样本 P95 不代表长期稳定性能。', '', '## 后台阶段', ''])
    for stage in report.get('stages', []):
        lines.append(f"- {stage['label']}：{milliseconds(stage.get('duration_ms'))} · {stage['status']}。{stage['note']}")
    lines.extend(['', '## 被动观测的真实委托请求', ''])
    real = report.get('recent_real_requests') or {}
    lines.append(real.get('note', '未观测到真实委托请求。'))
    if not real.get('items'):
        lines.append('无样本；不使用查询接口耗时代替真实下单接受或委托确认时间。')
    for row in real.get('items', []):
        lines.append(f"- {row['method']} {row['endpoint']}：{milliseconds(row['total_ms'])}，"
                     f"联网 {milliseconds(row['network_ms'])}，排队 {milliseconds(row['throttle_wait_ms'])}，{row['outcome']}")
    lines.extend(['', '## 推算值与测试限制', ''])
    for estimate in report.get('estimates', []):
        lines.append(f"- {estimate['label']}：{milliseconds(estimate.get('value_ms'))}，依据：{estimate['basis']}")
    for name, value in report.get('limits', {}).items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        lines.append(f'- {name}：{value}')
    lines.extend(['', '每次原始样本、失败状态和分项统计见同名 JSON。报告不含 API Key、签名、余额、订单号或仓位明细。'])
    return '\n'.join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description='Read-only Gate latency test, shared with the web console')
    parser.add_argument('--base-url', type=local_url, default='http://127.0.0.1:18473')
    parser.add_argument('--symbol', default='XAUUSD')
    parser.add_argument('--samples', type=int, choices=[3, 5], default=3)
    parser.add_argument('--output', type=Path, default=ROOT / 'data' / 'reports')
    args = parser.parse_args()
    symbol = args.symbol.strip().upper()
    if not re.fullmatch(r'[A-Z0-9][A-Z0-9_.-]{0,63}', symbol):
        parser.error('Invalid symbol')
    probe = {'source': 'script', 'measured_at': datetime.now(timezone.utc).isoformat(), 'samples': []}
    job = None
    try:
        for _ in range(3):
            began = time.perf_counter()
            try:
                try:
                    health = request(args.base_url, '/health', timeout=5)
                except AccessRequired:
                    authenticate(args.base_url)
                    began = time.perf_counter()
                    health = request(args.base_url, '/health', timeout=5)
                success = health.get('app') == 'grid-studio'
            except AccessRequired:
                raise
            except RuntimeError:
                success = False
            probe['samples'].append({'elapsed_ms': round((time.perf_counter() - began) * 1000, 3), 'success': success})
        job = request(args.base_url, '/diagnostics/start', {'symbol': symbol, 'samples': args.samples})
        print(f"Read-only diagnostic {job['id']} · {job['symbol']} · {job['samples']} samples/endpoint", flush=True)
        deadline = time.monotonic() + 125
        previous = None
        while job['status'] in {'queued', 'running'}:
            progress = job['progress']
            marker = (progress['completed'], progress['stage'])
            if marker != previous:
                print(f"{progress['completed']}/{progress['total']} · {progress['stage']}", flush=True)
                previous = marker
            if time.monotonic() >= deadline:
                job = request(args.base_url, f"/diagnostics/{job['id']}/cancel", {})
                break
            time.sleep(1)
            job = request(args.base_url, f"/diagnostics/{job['id']}")
    except KeyboardInterrupt:
        if job and job['status'] in {'queued', 'running'}:
            try: job = request(args.base_url, f"/diagnostics/{job['id']}/cancel", {})
            except RuntimeError: pass
        print('Stopped waiting. Reopen latency diagnostics to inspect the saved job.', file=sys.stderr)
    except (RuntimeError, KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not job:
        return 1
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    report_symbol = job.get('symbol', symbol)
    report_symbol = report_symbol if re.fullmatch(r'[A-Z0-9][A-Z0-9_.-]{0,63}', report_symbol) else symbol
    prefix = output / f'gate-latency-{report_symbol}-{stamp}'
    prefix.with_suffix('.json').write_text(json.dumps({**job, 'client_probe': probe}, ensure_ascii=False, indent=2), encoding='utf-8')
    prefix.with_suffix('.md').write_text(markdown(job, probe), encoding='utf-8')
    print(f"Status: {job['status']}\nReport: {prefix.with_suffix('.md')}\nData: {prefix.with_suffix('.json')}")
    return 0 if job['status'] == 'completed' else 2


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
