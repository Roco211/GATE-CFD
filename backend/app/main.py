"""Loopback-only FastAPI console backed by real Gate CFD accounts and quotes."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import inspect
import json
import os
from pathlib import Path
import re
import time
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool

from .core import GridConfig, decimal
from .access import AccessControl, COOKIE, SESSION_SECONDS, check_token
from .credentials import CredentialStore
from .diagnostics import DiagnosticsRequest, DiagnosticsService, submission_intervals
from .gate import GateClient, GateError
from .live import LiveEngine
from .market import LiveMarket
from .planner import PlannerRequest, plan_grid

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / '.env')
ORIGINS = ['http://127.0.0.1:5173', 'http://localhost:5173',
           'http://127.0.0.1:18473', 'http://localhost:18473']
if os.getenv('GRID_TUNNEL_PORT'):
    tunnel_port = int(os.environ['GRID_TUNNEL_PORT'])
    if not 1024 <= tunnel_port <= 65535:
        raise ValueError('GRID_TUNNEL_PORT must be between 1024 and 65535')
    ORIGINS.extend([f'http://127.0.0.1:{tunnel_port}', f'http://localhost:{tunnel_port}'])


class ProcessLease:
    """Only one execution process may own a persisted trading book."""
    def __init__(self, path: Path):
        self.path, self.file = path, None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        self.file.seek(0)
        self.file.write(b'0')
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError('Grid Studio 已在运行。请只启动一个后台进程。') from exc

    def release(self):
        if self.file:
            self.file.close()


class StartRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    config: GridConfig
    request_id: str = Field(min_length=8, max_length=100)
    mode: Literal['live'] = 'live'


class TemplateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    config: GridConfig


class ProtectionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    price_tp: str | None = Field(default=None, max_length=80)
    price_sl: str | None = Field(default=None, max_length=80)

    @field_validator('price_tp', 'price_sl')
    @classmethod
    def valid_protection(cls, value: str | None) -> str | None:
        # None means preserve the current Gate value; only an explicit zero clears it.
        if value is None:
            return None
        amount = decimal(value, '止盈止损价')
        if amount < 0:
            raise ValueError('止盈止损价不能小于 0；传 0 表示清空')
        return format(amount, 'f')


class OrderUpdateRequest(ProtectionUpdateRequest):
    price: str = Field(min_length=1, max_length=80)

    @field_validator('price')
    @classmethod
    def valid_price(cls, value: str) -> str:
        amount = decimal(value, '委托价')
        if amount <= 0:
            raise ValueError('委托价必须大于 0')
        return format(amount, 'f')


class PositionUpdateRequest(ProtectionUpdateRequest):
    @model_validator(mode='after')
    def require_change(self):
        if self.price_tp is None and self.price_sl is None:
            raise ValueError('请填写要修改的止盈价或止损价')
        return self


class ConnectionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: SecretStr | None = None
    secret: SecretStr | None = None
    remember: bool = False


class AccessRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: SecretStr = Field(min_length=1, max_length=256)


def create_app(db_path: str | Path | None = None, auto_tick: bool = True) -> FastAPI:
    database = Path(db_path or os.getenv('GRID_DATABASE', 'data/live.sqlite3'))
    if not database.is_absolute():
        database = ROOT / database
    lease = ProcessLease(database.with_suffix('.lock'))
    store = CredentialStore(database.parent / 'gate-credentials.json')
    access = AccessControl(database.parent / 'access-control.json')

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        lease.acquire()
        engine = gate = None
        tasks = []
        try:
            engine = LiveEngine(str(database))
            key, secret, remembered, startup_error = '', '', False, None
            try:
                saved = store.load()
                if saved:
                    key, secret = saved
                    remembered = True
            except ValueError as exc:
                startup_error = str(exc)
            if not key:
                key, secret = os.getenv('GATE_API_KEY', ''), os.getenv('GATE_API_SECRET', '')
            gate = GateClient(key, secret)
            app.state.engine, app.state.gate = engine, gate
            app.state.market = LiveMarket(gate, interval=1.0)
            engine.quote_provider = app.state.market.get
            app.state.op_lock, app.state.market_lock = asyncio.Lock(), asyncio.Lock()
            app.state.watches = {'XAUUSD': time.monotonic()}
            app.state.connection = {
                'configured': gate.credentials_set, 'connected': False,
                'checked_at': None, 'error': startup_error, 'live_trading_ready': False,
                'blockers': [], 'account_id': None, 'remembered': remembered,
                'encrypted_storage_available': store.available,
            }
            app.state.engine_error = None
            app.state.diagnostics = DiagnosticsService(
                lambda: app.state.gate,
                local_probe=lambda symbol: diagnostic_local_probe(symbol),
                locks={'op_lock': app.state.op_lock, 'market_lock': app.state.market_lock},
            )

            async def market_worker():
                while True:
                    start_at = time.monotonic()
                    try:
                        async with app.state.market_lock:
                            feed = app.state.market
                            if not feed.symbols:
                                await feed.refresh_symbols()
                            active = engine.state('XAUUSD', app.state.connection.get('account_id')).get('strategies', [])
                            symbols = {s['config']['symbol'] for s in active if s['status'] not in ('stopped', 'completed')}
                            symbols.update(s for s, last in app.state.watches.items() if start_at - last < 45)
                            await feed.poll(sorted(symbols or {'XAUUSD'}))
                            if app.state.gate.credentials_set:
                                for symbol in sorted(symbols):
                                    await feed.ensure(symbol, detail=True)
                            feed.errors.pop('_global', None)
                    except (GateError, ValueError) as exc:
                        app.state.market.errors['_global'] = str(exc)
                    except Exception:
                        app.state.market.errors['_global'] = '行情更新暂时中断，后台将自动重连。'
                    # The cache schedules each endpoint from its completed read.
                    # Check its due times often; a one-second outer loop would
                    # miss a just-not-due quote and effectively poll every two.
                    await asyncio.sleep(.1)

            async def execution_worker():
                while True:
                    try:
                        if app.state.connection['configured']:
                            async with app.state.op_lock:
                                uid = app.state.connection.get('account_id')
                                if not uid:
                                    await verify_connection(app.state.gate)
                                    uid = app.state.connection.get('account_id')
                                if uid:
                                    feed = app.state.market
                                    markets = {s: value for s in feed.markets if (value := feed.get(s)) is not None}
                                    specs = {s: value for s in feed.specs if (value := feed.spec(s)) is not None}
                                    await engine.cycle(app.state.gate, markets, specs, uid)
                                    app.state.connection.update(connected=True, live_trading_ready=True, error=None)
                                    app.state.engine_error = None
                    except (GateError, ValueError) as exc:
                        app.state.connection.update(connected=False, live_trading_ready=False, error=str(exc))
                        app.state.engine_error = str(exc)
                    except Exception:
                        app.state.connection.update(live_trading_ready=False)
                        app.state.engine_error = '实盘同步中断，正在重试对账；暂不接受新的策略。'
                    await asyncio.sleep(1 if app.state.connection['connected'] else 3)

            if auto_tick:
                tasks = [asyncio.create_task(market_worker()), asyncio.create_task(execution_worker())]
            yield
        finally:
            if hasattr(app.state, 'diagnostics'):
                await app.state.diagnostics.aclose()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
            try:
                if gate is not None:
                    await app.state.gate.aclose()
            finally:
                try:
                    if engine is not None:
                        result = engine.close()
                        if inspect.isawaitable(result):
                            await result
                finally:
                    lease.release()

    app = FastAPI(title='Grid Studio · Gate CFD 实盘', version='0.6.0', lifespan=lifespan)
    app.state.access = access
    extra_hosts = [value.strip() for value in os.getenv('GRID_ALLOWED_HOSTS', '').split(',') if value.strip()]
    if any('/' in value or '*' in value or ':' in value for value in extra_hosts):
        raise ValueError('GRID_ALLOWED_HOSTS must contain explicit hostnames or IPv4 addresses')
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver', *extra_hosts])
    app.add_middleware(CORSMiddleware, allow_origins=ORIGINS,
                       allow_methods=['GET', 'POST', 'PUT', 'DELETE'], allow_headers=['Content-Type', 'X-Grid-Client'])

    @app.middleware('http')
    async def local_request_guard(request: Request, call_next):
        public = request.url.path in {'/login', '/favicon.svg', '/api/auth/session', '/api/auth/login'} or request.url.path.startswith('/assets/')
        if not public and not access.valid_session(request.cookies.get(COOKIE)):
            if request.url.path.startswith('/api/'):
                return JSONResponse({'detail': '请先输入访问 token 完成验证。', 'code': 'access_required'}, status_code=401, headers={'Cache-Control': 'no-store'})
            return RedirectResponse('/login', status_code=303, headers={'Cache-Control': 'no-store'})
        if request.url.path.startswith('/api/') and request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
            origin = request.headers.get('origin')
            same_origin = origin == str(request.base_url).rstrip('/')
            if (origin and origin not in ORIGINS and not same_origin) or request.headers.get('x-grid-client') != 'grid-studio':
                return JSONResponse({'detail': '请求来源验证失败，请通过控制台操作。'}, status_code=403)
            if request.url.path == '/api/auth/login':
                try:
                    length = int(request.headers.get('content-length') or 0)
                except ValueError:
                    return JSONResponse({'detail': '验证请求格式无效。'}, status_code=400)
                if length > 2048:
                    return JSONResponse({'detail': '验证请求过大。'}, status_code=413)
        response = await call_next(request)
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        if request.url.path in {'/', '/login'}:
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/login', include_in_schema=False)
    def access_page():
        page = ROOT / 'dist/login.html'
        if not page.is_file():
            return JSONResponse({'detail': '验证页面尚未构建，请先运行安装脚本。'}, status_code=503)
        return FileResponse(page)

    @app.get('/api/auth/session')
    def access_session(request: Request):
        return {'app': 'grid-studio', 'authenticated': access.valid_session(request.cookies.get(COOKIE)), 'configured': access.configured}

    @app.post('/api/auth/login')
    async def access_login(request: Request, body: AccessRequest):
        if not access.configured:
            raise HTTPException(503, '后台尚未配置访问 token，请先运行 configure_access.py。')
        client = request.client.host if request.client else 'unknown'
        if not access.allow_attempt(client):
            raise HTTPException(429, '验证尝试过于频繁，请稍后再试。', headers={'Retry-After': '300'})
        if not await run_in_threadpool(check_token, body.token.get_secret_value(), access.encoded):
            raise HTTPException(401, '访问 token 不正确。')
        cookie = access.create_session(client, request.cookies.get(COOKIE))
        response = JSONResponse({'authenticated': True})
        response.set_cookie(COOKIE, cookie, httponly=True, secure=request.url.scheme == 'https',
                            samesite='strict', max_age=SESSION_SECONDS, path='/')
        return response

    @app.post('/api/auth/logout')
    def access_logout(request: Request):
        access.logout(request.cookies.get(COOKIE))
        response = JSONResponse({'authenticated': False})
        response.delete_cookie(COOKIE, httponly=True, samesite='strict', path='/')
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({'detail': '参数格式不正确，请检查输入。', 'fields': [
            {'field': '.'.join(str(p) for p in e['loc']), 'message': e['msg']} for e in exc.errors()
        ]}, status_code=422)

    @app.exception_handler(ValueError)
    async def invalid_value(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.exception_handler(GateError)
    async def gate_error(request, exc):
        return JSONResponse({'detail': str(exc), 'source': 'gate', 'code': exc.code}, status_code=502)

    def watched_symbol(value: str) -> str:
        symbol = value.strip().upper()
        if not re.fullmatch(r'[A-Z0-9_.-]{1,30}', symbol):
            raise ValueError('交易品种格式无效。')
        app.state.watches[symbol] = time.monotonic()
        return symbol

    def snapshot(symbol: str) -> dict:
        feed, conn = app.state.market, app.state.connection
        result = app.state.engine.state(symbol, conn.get('account_id'))
        if not conn.get('account_id'):
            result.update(account=None, strategies=[], orders=[], positions=[], fills=[], logs=[])
        result.update(mode='live', source='gate', symbols=feed.symbols, spec=feed.spec(symbol),
                      market=feed.get(symbol), connection=dict(conn), engine_error=app.state.engine_error,
                      feed_error=feed.errors.get(symbol) or feed.errors.get('_global'))
        return result

    def diagnostic_local_probe(symbol: str) -> dict:
        """Only cached, synchronous reads; no account refresh or execution."""
        connection, feed = app.state.connection, app.state.market
        uid = connection.get('account_id')
        cached = app.state.engine.state(symbol, uid) if uid else {}
        strategies = cached.get('strategies', [])
        known_statuses = {'starting', 'running', 'paused', 'stopping', 'stopped', 'closing',
                          'completed', 'recovering', 'legacy_paused'}
        status_counts = {}
        for strategy in strategies:
            status = strategy.get('status')
            status = status if status in known_statuses else 'other'
            status_counts[status] = status_counts.get(status, 0) + 1
        orders, positions = cached.get('orders', []), cached.get('positions', [])
        summary = {
            'source': 'existing_local_cache', 'strategy_count': len(strategies),
            'strategy_status_counts': status_counts,
            'active_strategy_count': sum(count for status, count in status_counts.items() if status not in {'stopped', 'completed'}),
            'remote_pending_count': sum(row.get('source') == 'gate' and row.get('status') == 'pending' for row in orders),
            'local_waiting_count': sum(row.get('source') != 'gate' for row in orders),
            'position_count': len(positions),
            'managed_position_count': sum(row.get('managed') is True for row in positions),
            'unresolved_operation_count': len(cached.get('unresolved_operations', [])),
        }
        matching = [strategy for strategy in strategies if strategy.get('config', {}).get('symbol') == symbol]
        matching.sort(key=lambda strategy: strategy.get('created_at') or '', reverse=True)
        selected = next((strategy for strategy in matching if strategy.get('status') in {'starting', 'running', 'paused', 'recovering'}), None)
        selected = selected or next(iter(matching), None)
        intents = app.state.engine._intents(uid) if uid and selected is not None else []
        summary['historical_submission_intervals'] = submission_intervals(
            [intent for intent in intents if intent.get('strategy_id') == selected['id']]) if selected else submission_intervals([])
        quote, spec = feed.get(symbol), feed.spec(symbol)
        stage = {'name': 'pure_preview', 'label': '现有配置纯计算预览', 'duration_ms': None,
                 'status': 'not_measured', 'note': '没有该品种现有配置或完整缓存，未构造虚拟策略进行测量。'}
        if selected is not None and quote is not None and spec is not None:
            started = time.perf_counter()
            try:
                app.state.engine.preview(selected['config'], spec, quote,
                                         connection['live_trading_ready'], account_id=uid)
                stage.update(duration_ms=round((time.perf_counter() - started) * 1000, 3), status='measured',
                             note='只计算现有配置的缓存预览；未启动、对账、补单或修改任何策略。')
            except (ValueError, KeyError, TypeError):
                stage['note'] = '现有缓存不满足纯预览条件，此项未测得。'
        return {'summary': summary, 'stages': [stage]}

    def require_connection():
        connection = app.state.connection
        if not connection['live_trading_ready'] or not connection.get('account_id'):
            raise HTTPException(409, '请先在页面配置 API Key 并连接已开通 CFD 的真实账户。')
        return connection['account_id']

    def mutation_snapshot(symbol: str, result: dict) -> dict:
        operation = result.get('operation') if isinstance(result, dict) else None
        if isinstance(operation, dict) and operation.get('status') == 'rejected':
            raise HTTPException(502, operation.get('error') or 'Gate 拒绝了本次修改，原设置未确认变更。')
        response = snapshot(symbol)
        if isinstance(operation, dict):
            response['operation'] = {key: operation[key] for key in ('id', 'kind', 'status', 'result', 'error') if key in operation}
        return response

    def management_account() -> str:
        connection = app.state.connection
        uid = connection.get('account_id') or app.state.engine.current_uid
        if not uid or not app.state.gate.credentials_set:
            raise HTTPException(409, '请先连接创建该策略的 Gate 账户。')
        return uid

    def strategy_symbol(strategy_id: str, uid: str) -> str:
        strategies = app.state.engine.state('XAUUSD', uid).get('strategies', [])
        strategy = next((s for s in strategies if s['id'] == strategy_id), None)
        if strategy is None:
            raise HTTPException(404, '策略不存在或不属于当前已连接账户。')
        return strategy['config']['symbol']

    async def ensure_quote(symbol: str, detail: bool = False):
        async with app.state.market_lock:
            await app.state.market.ensure(symbol, detail=detail)
        feed = app.state.market
        quote, spec = feed.get(symbol), feed.spec(symbol)
        if quote is None:
            raise HTTPException(503, feed.errors.get(symbol) or '尚未取得 Gate 实时报价，请稍后再试。')
        if detail and spec is None:
            raise HTTPException(409, feed.errors.get(symbol + ':detail') or '请连接 Gate 账户以读取品种手数、杠杆等实盘规格。')
        return quote, spec

    async def verify_connection(client: GateClient, *, commit: bool = True) -> dict:
        account = (await client.account()).get('data') or {}
        uid = str(account.get('mt5_uid') or '')
        if account.get('status') not in (3, '3') or not uid or uid == '0':
            raise ValueError('该账户 CFD 尚未开通或未处于正常交易状态，请先在 Gate 完成开通。')
        engine = app.state.engine
        previous_uid = app.state.connection.get('account_id') or engine.current_uid
        if previous_uid and previous_uid != uid and (
            engine.has_active_strategies(previous_uid) or engine.has_unresolved_execution(previous_uid)
        ):
            raise ValueError('原账户仍有运行策略或待核对委托，请先全部取消并完成对账，再切换账户。')
        await engine.reconcile(client, uid)
        state = engine.state('XAUUSD', uid)
        assets = state.get('account') or {}
        verified = dict(app.state.connection)
        verified.update(
            configured=True, connected=True, checked_at=time.time(), error=None,
            live_trading_ready=True, account_id=uid, blockers=[],
            account={'status': account.get('status'), 'leverage': account.get('leverage'),
                     'equity': assets.get('equity'), 'free_margin': assets.get('free_margin')})
        if commit:
            app.state.connection = verified
            app.state.engine_error = None
        return verified

    @app.get('/api/health')
    def health():
        return {'ok': True, 'app': 'grid-studio', 'version': '0.6.0', 'engine': 'FastAPI',
                'mode': 'live', 'execution_mode': 'native_trigger',
                'connected': app.state.connection['connected'], 'error': app.state.engine_error}

    @app.get('/api/state')
    async def state(symbol: str = 'XAUUSD'):
        return snapshot(watched_symbol(symbol))

    @app.post('/api/diagnostics/start')
    async def diagnostics_start(body: DiagnosticsRequest):
        # No op_lock, market refresh or connection verification on this route.
        # Starting only allocates the shared bounded background GET job.
        return app.state.diagnostics.start(body)

    @app.get('/api/diagnostics/latest')
    async def diagnostics_latest():
        return {'job': app.state.diagnostics.latest()}

    @app.get('/api/diagnostics/{job_id}')
    async def diagnostics_get(job_id: str):
        try:
            return app.state.diagnostics.get(job_id)
        except KeyError:
            raise HTTPException(404, '诊断报告不存在或已超出保留数量。') from None

    @app.post('/api/diagnostics/{job_id}/cancel')
    async def diagnostics_cancel(job_id: str):
        try:
            return await app.state.diagnostics.cancel(job_id)
        except KeyError:
            raise HTTPException(404, '诊断报告不存在或已超出保留数量。') from None

    @app.get('/api/market/stream')
    async def market_stream(request: Request, symbol: str = 'XAUUSD'):
        symbol = watched_symbol(symbol)

        async def events():
            previous, previous_symbols, heartbeat = None, None, time.monotonic()
            while not await request.is_disconnected():
                if not access.valid_session(request.cookies.get(COOKIE)):
                    break
                app.state.watches[symbol] = time.monotonic()
                feed = app.state.market
                payload = {'market': feed.get(symbol), 'spec': feed.spec(symbol),
                           'error': feed.errors.get(symbol) or feed.errors.get('symbols') or feed.errors.get('_global')}
                if previous_symbols is not feed.symbols:
                    payload['symbols'] = feed.symbols
                    previous_symbols = feed.symbols
                encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
                if encoded != previous:
                    yield f'event: market\ndata: {encoded}\n\n'
                    previous, heartbeat = encoded, time.monotonic()
                elif time.monotonic() - heartbeat >= 15:
                    yield ': heartbeat\n\n'
                    heartbeat = time.monotonic()
                await asyncio.sleep(1)

        return StreamingResponse(events(), media_type='text/event-stream',
                                 headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache'})

    @app.post('/api/grid/plan')
    async def grid_plan(body: PlannerRequest):
        symbol = watched_symbol(body.symbol)
        async with app.state.op_lock:
            if not app.state.gate.credentials_set:
                raise HTTPException(409, '请先连接 Gate 账户以读取规划所需的真实品种规格。')
            quote, spec = await ensure_quote(symbol, detail=True)
            # Planning is pure: it neither creates a strategy nor reconciles or
            # changes orders. Starting a reviewed draft remains a separate action.
            return plan_grid(body, spec, quote).model_dump(mode='json')

    @app.get('/api/grid/costs')
    async def grid_costs(symbol: str = 'XAUUSD'):
        symbol = watched_symbol(symbol)
        async with app.state.op_lock:
            if not app.state.gate.credentials_set:
                raise HTTPException(409, '请先连接 Gate 账户以查询当前品种费用。')
            payload = await app.state.gate.commissions(symbols=symbol)
        data = payload.get('data') if isinstance(payload, dict) else None
        rows = data.get('list') if isinstance(data, dict) else None
        matches = [row for row in rows if isinstance(row, dict) and row.get('symbol') == symbol] if isinstance(rows, list) else []
        result = {'symbol': symbol, 'fee_per_lot': None, 'minimum_fee': None,
                  'category_code': None, 'source': 'gate', 'checked_at': time.time(),
                  'notes': ['保留 Gate 返回的每手佣金，不重复乘 2，也不另行套用 VIP 折扣。',
                            '官方费率接口未提供每单最低收费；请核对后填写，确认无最低收费时显式填 0。']}
        if len(matches) != 1:
            result['notes'].insert(0, f'Gate 未返回 {symbol} 的唯一费率记录，请手动核对成本。')
            return result
        row = matches[0]
        category = row.get('category_code')
        if isinstance(category, str) and category.strip():
            result['category_code'] = category
        try:
            fee = decimal(row.get('fee_per_lot'), '每手佣金')
            if fee < 0:
                raise ValueError('每手佣金不得为负数。')
            result['fee_per_lot'] = format(fee, 'f')
        except ValueError:
            result['notes'].insert(0, 'Gate 返回的每手佣金缺失或无效，请手动核对，不能视为 0。')
        return result

    @app.post('/api/grid/preview')
    async def preview(config: GridConfig):
        watched_symbol(config.symbol)
        async with app.state.op_lock:
            quote, spec = await ensure_quote(config.symbol, detail=True)
            connection = app.state.connection
            uid = connection.get('account_id')
            if uid and connection['live_trading_ready'] and app.state.gate.credentials_set:
                await app.state.engine.refresh_preview_account(app.state.gate, uid)
                # A quote refreshed by the market worker during account reads
                # is preferable to the snapshot captured before those reads.
                quote = app.state.market.get(config.symbol) or quote
            return app.state.engine.preview(config, spec, quote, connection['live_trading_ready'], account_id=uid)

    @app.post('/api/strategies')
    async def start(body: StartRequest):
        async with app.state.op_lock:
            uid = require_connection()
            quote, spec = await ensure_quote(body.config.symbol, detail=True)
            await app.state.engine.start(body.config, body.request_id, app.state.gate, quote, spec, uid)
            return snapshot(body.config.symbol)

    @app.post('/api/strategies/{strategy_id}/cancel')
    async def cancel(strategy_id: str):
        stop_uid = management_account()
        strategy_symbol(strategy_id, stop_uid)
        app.state.engine.request_stop(strategy_id, stop_uid)
        async with app.state.op_lock:
            uid = management_account()
            symbol = strategy_symbol(strategy_id, uid)
            await app.state.engine.cancel(strategy_id, app.state.gate, uid)
            return snapshot(symbol)

    @app.post('/api/strategies/{strategy_id}/resume')
    async def resume_strategy(strategy_id: str):
        async with app.state.op_lock:
            uid = require_connection()
            symbol = strategy_symbol(strategy_id, uid)
            quote, spec = await ensure_quote(symbol, detail=True)
            app.state.engine.set_market_context(symbol, quote, spec)
            await app.state.engine.resume_strategy(strategy_id, app.state.gate, uid)
            return snapshot(symbol)

    @app.delete('/api/strategies/{strategy_id}/orders/{order_id}')
    async def cancel_order(strategy_id: str, order_id: str):
        uid = management_account()
        strategy_symbol(strategy_id, uid)
        app.state.engine.request_order_cancel(strategy_id, order_id, uid)
        async with app.state.op_lock:
            uid = management_account()
            symbol = strategy_symbol(strategy_id, uid)
            await app.state.engine.cancel_order(strategy_id, order_id, app.state.gate, uid)
            return snapshot(symbol)

    @app.put('/api/strategies/{strategy_id}/orders/{order_id}')
    async def update_order(strategy_id: str, order_id: str, body: OrderUpdateRequest):
        async with app.state.op_lock:
            uid = management_account()
            symbol = strategy_symbol(strategy_id, uid)
            quote, spec = await ensure_quote(symbol, detail=True)
            app.state.engine.set_market_context(symbol, quote, spec)
            result = await app.state.engine.modify_order(strategy_id, order_id, body.price,
                                                        body.price_tp, body.price_sl, app.state.gate, uid)
            return mutation_snapshot(symbol, result)

    @app.put('/api/strategies/{strategy_id}/positions/{position_id}')
    async def update_position(strategy_id: str, position_id: str, body: PositionUpdateRequest):
        async with app.state.op_lock:
            uid = management_account()
            symbol = strategy_symbol(strategy_id, uid)
            quote, spec = await ensure_quote(symbol, detail=True)
            app.state.engine.set_market_context(symbol, quote, spec)
            result = await app.state.engine.modify_position(strategy_id, position_id, body.price_tp,
                                                           body.price_sl, app.state.gate, uid)
            return mutation_snapshot(symbol, result)

    @app.post('/api/strategies/{strategy_id}/close')
    async def close_strategy(strategy_id: str):
        uid = management_account()
        strategy_symbol(strategy_id, uid)
        app.state.engine.request_stop(strategy_id, uid)
        async with app.state.op_lock:
            uid = management_account()
            symbol = strategy_symbol(strategy_id, uid)
            await app.state.engine.close_strategy(strategy_id, app.state.gate, uid)
            return snapshot(symbol)

    @app.get('/api/templates')
    async def templates():
        return app.state.engine.list_templates()

    @app.post('/api/templates')
    async def save_template(body: TemplateRequest):
        async with app.state.op_lock:
            return app.state.engine.save_template(body.name, body.config)

    @app.delete('/api/templates/{template_id}')
    async def delete_template(template_id: str):
        async with app.state.op_lock:
            return app.state.engine.delete_template(template_id)

    @app.get('/api/connection')
    def connection():
        return app.state.connection

    @app.post('/api/connection/check')
    async def check_connection(body: ConnectionRequest):
        async with app.state.op_lock:
            if body.key is None and body.secret is None:
                if not app.state.gate.credentials_set:
                    raise HTTPException(400, '请同时填写 API Key 和 API Secret。')
                app.state.connection.update(connected=False, live_trading_ready=False, checked_at=time.time())
                try:
                    return await verify_connection(app.state.gate)
                except (GateError, ValueError) as exc:
                    app.state.connection['error'] = str(exc)
                    raise
            key = body.key.get_secret_value().strip() if body.key else ''
            secret = body.secret.get_secret_value().strip() if body.secret else ''
            if not key or not secret:
                raise HTTPException(400, '请同时填写 API Key 和 API Secret。')
            replacement = GateClient(key, secret)
            previous = dict(app.state.connection)
            try:
                verified = await verify_connection(replacement, commit=False)
                if body.remember:
                    store.save(key, secret)
                else:
                    store.clear()
                async with app.state.market_lock:
                    old = app.state.gate
                    await app.state.diagnostics.cancel_active(reason='connection_changed')
                    app.state.gate = replacement
                    app.state.market.set_client(replacement)
                    app.state.market.specs.clear()
                    await old.aclose()
                verified['remembered'] = body.remember
                app.state.connection = verified
                app.state.engine_error = None
                return dict(app.state.connection)
            except Exception:
                app.state.connection = previous
                await replacement.aclose()
                raise

    @app.post('/api/connection/disconnect')
    async def disconnect():
        async with app.state.op_lock:
            uid = app.state.connection.get('account_id')
            engine = app.state.engine
            if engine.has_active_strategies(uid) or engine.has_unresolved_execution(uid):
                raise HTTPException(409, '请先全部取消运行中的策略并完成待核对委托，再清除账户配置。')
            try:
                store.clear()
            except OSError:
                raise HTTPException(500, '无法清除本机密钥文件，账户配置未更改。请检查该文件是否被其他程序占用。') from None
            async with app.state.market_lock:
                await app.state.diagnostics.cancel_active(reason='connection_changed')
                await app.state.gate.aclose()
                app.state.gate = GateClient()
                app.state.market.set_client(app.state.gate)
                app.state.market.specs.clear()
            app.state.connection.update(configured=False, connected=False, error=None, checked_at=None,
                                        live_trading_ready=False, remembered=False, account_id=None)
            app.state.connection.pop('account', None)
            app.state.engine_error = None
            return dict(app.state.connection)

    @app.get('/api/gate/symbols')
    async def gate_symbols():
        return await app.state.gate.symbols()

    @app.get('/api/gate/market/{symbol}')
    async def gate_market(symbol: str):
        quote, _ = await ensure_quote(watched_symbol(symbol))
        return {'source': 'gate', 'market': quote}

    @app.get('/api/gate/symbols/{symbol}/detail')
    async def gate_detail(symbol: str):
        _, spec = await ensure_quote(watched_symbol(symbol), detail=True)
        return spec

    dist = ROOT / 'dist'
    if (dist / 'assets').is_dir():
        app.mount('/assets', StaticFiles(directory=dist / 'assets'), name='assets')

    @app.get('/favicon.svg', include_in_schema=False)
    def favicon():
        return FileResponse(ROOT / 'public/favicon.svg')

    @app.get('/', include_in_schema=False)
    def index():
        if not (dist / 'index.html').is_file():
            return JSONResponse({'message': '前端尚未构建。开发时请打开 http://127.0.0.1:5173'}, status_code=503)
        return FileResponse(dist / 'index.html')

    return app


app = create_app()
