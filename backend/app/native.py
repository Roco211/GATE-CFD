"""Native Gate order state machine; snapshots, persistence and DTOs live in live.py.

Every mutation has a durable intent. Missing responses never authorize a retry.
Order tickets and position tickets are different identifiers.
"""
from __future__ import annotations

import asyncio
import copy
from collections import Counter
from decimal import Decimal as D

from .core import GridConfig, identifier, now
from .gate import GateError, GateUnknownOutcome
from .live import (ACTIVE, BATCH_SIZE, CAPACITY, MATCH_TIMEOUT, UNRESOLVED,
                   LivePreflightAbort, _data, _decimal, _direction, _epoch,
                   _estimate, _id, _text, _uid)


class NativeRuntime:
    @staticmethod
    def _exit_match_key(row, *, order=False):
        """Exact settlement evidence; net PnL is not comparable to close_pnl."""
        try:
            if order:
                direction = _direction(row.get('side'))
                operation = str(row.get('order_opt_type'))
                if str(row.get('state')) != '4' or row.get('price_type') != 'market' or operation not in {'3', '4'}:
                    return None
                volume = _decimal(row.get('fill_volume'))
                if volume != _decimal(row.get('volume')):
                    return None
                price, stamp, pnl = _decimal(row.get('price')), _epoch(row.get('time_done')), _decimal(row.get('close_pnl'))
            else:
                if str(row.get('position_status')) not in {'1', '2'}:
                    return None
                position_direction = _direction(row.get('position_dir'))
                direction = 'short' if position_direction == 'long' else 'long' if position_direction == 'short' else None
                operation = '3' if position_direction == 'long' else '4'
                volume = _decimal(row.get('volume_closed'))
                if volume != _decimal(row.get('volume')):
                    return None
                price, stamp = _decimal(row.get('close_price')), _epoch(row.get('time_close'))
                detail = row.get('realized_pnl_detail')
                pnl = _decimal(detail.get('closed_pnl') if isinstance(detail, dict) else None)
            if not row.get('symbol') or direction is None or stamp is None or volume <= 0 or price <= 0:
                return None
            return row['symbol'], operation, direction, volume, price, int(stamp), pnl
        except ValueError:
            return None

    @staticmethod
    def _exit_trigger_matches(order, position):
        # sl_tp_type is an observed Gate response field, absent from the public
        # SDK schema. Type 2 is used only together with the exact known TP and
        # unique settlement evidence. Other nonzero values are not guessed.
        if str(order.get('sl_tp_type')) != '2':
            return True
        try:
            target = _decimal(position.get('price_tp'))
            return target > 0 and _decimal(order.get('trigger_price')) == target
        except ValueError:
            return False

    @staticmethod
    def _exit_trigger_possible(order, position):
        if str(order.get('sl_tp_type')) != '2':
            return True
        try:
            return _decimal(position.get('price_tp')) == _decimal(order.get('trigger_price'))
        except ValueError:
            # Missing protection is uncertainty, not evidence of a mismatch.
            return True

    @staticmethod
    def _partial_exit_key(row, *, order=False):
        def number(value):
            try:
                return _decimal(value)
            except ValueError:
                return None
        if order:
            if row.get('state') is not None and str(row['state']) != '4':
                return None
            if row.get('order_opt_type') is not None and str(row['order_opt_type']) not in {'3', '4'}:
                return None
            if row.get('price_type') is not None and row['price_type'] != 'market':
                return None
            operation = str(row['order_opt_type']) if row.get('order_opt_type') is not None else None
            direction = _direction(row.get('side'))
            volume, price = number(row.get('fill_volume')), number(row.get('price'))
            stamp, pnl = _epoch(row.get('time_done')), number(row.get('close_pnl'))
        else:
            if row.get('position_status') is not None and str(row['position_status']) not in {'1', '2'}:
                return None
            direction = _direction(row.get('position_dir'))
            operation = '3' if direction == 'long' else '4' if direction == 'short' else None
            direction = 'short' if direction == 'long' else 'long' if direction == 'short' else None
            volume, price = number(row.get('volume_closed')), number(row.get('close_price'))
            stamp = _epoch(row.get('time_close'))
            detail = row.get('realized_pnl_detail')
            pnl = number(detail.get('closed_pnl') if isinstance(detail, dict) else None)
        total = number(row.get('volume'))
        if total is not None and volume is not None and total != volume:
            return None
        return row.get('symbol') or None, operation, direction, volume, price, int(stamp) if stamp is not None else None, pnl

    def _exit_indexes(self, uid):
        snapshot = self._snapshots.get(uid, {})
        cached = getattr(self, '_exit_index_cache', None)
        if cached and cached[0] is snapshot:
            return cached[1:]
        orders, positions, incomplete_orders, incomplete_positions = {}, {}, {}, {}
        for row in {str(r.get('order_id')): r for r in snapshot.get('order_history', []) if _id(r.get('order_id'))}.values():
            key = self._exit_match_key(row, order=True)
            if key is not None:
                orders.setdefault(key, []).append(row)
            else:
                partial = self._partial_exit_key(row, order=True)
                if partial is not None:
                    incomplete_orders.setdefault((partial[0], partial[5]), []).append((partial, row))
        for row in {str(r.get('position_id')): r for r in snapshot.get('history', []) if _id(r.get('position_id'))}.values():
            key = self._exit_match_key(row)
            if key is not None:
                positions.setdefault(key, []).append(row)
            else:
                partial = self._partial_exit_key(row)
                if partial is not None:
                    incomplete_positions.setdefault((partial[0], partial[5]), []).append((partial, row))
        self._exit_index_cache = snapshot, orders, positions, incomplete_orders, incomplete_positions
        return orders, positions, incomplete_orders, incomplete_positions

    @staticmethod
    def _possible_incomplete_rows(key, index):
        if key is None:
            return []
        matches = []
        for bucket in {(key[0], key[5]), (key[0], None), (None, key[5]), (None, None)}:
            for partial, row in index.get(bucket, []):
                if all(value is None or value == expected for value, expected in zip(partial, key)):
                    matches.append(row)
        return matches

    def classify_exit(self, position, uid, expected_protection=None):
        """Read-only, bidirectionally unique order-to-closed-position evidence."""
        base = {'type': 'awaiting_confirmation', 'confirmed': False, 'order_id': None,
                'source': 'gate_order_history', 'reason': '等待可唯一关联的平仓委托记录'}
        if str(position.get('position_status')) == '2':
            return {**base, 'type': 'liquidation', 'confirmed': True, 'source': 'position_status', 'reason': 'Gate 仓位历史记录为强平'}
        if str(position.get('position_status')) != '1':
            return base
        if self._snapshots.get(uid, {}).get('history_complete') is not True:
            return {**base, 'reason': '仓位历史尚未完整同步，暂不能确认跨仓位唯一关联'}
        coverage = self.exit_history_coverage(position, uid)
        if not coverage['complete']:
            return {**base, 'reason': coverage.get('reason') or '该平仓时段订单历史尚未完整同步'}
        key = self._exit_match_key(position)
        orders, positions, incomplete_orders, incomplete_positions = self._exit_indexes(uid)
        candidates = [row for row in orders.get(key, []) if self._exit_trigger_possible(row, position)] if key is not None else []
        uncertain_orders = [row for row in self._possible_incomplete_rows(key, incomplete_orders) if self._exit_trigger_possible(row, position)]
        if uncertain_orders:
            return {**base, 'reason': '存在关键字段缺失的潜在平仓委托，不能确认唯一关联'}
        if len(candidates) != 1:
            return {**base, 'reason': '平仓委托关联存在歧义' if candidates else base['reason']}
        order = candidates[0]
        if 'order_ids' in coverage and str(order['order_id']) not in coverage['order_ids']:
            return {**base, 'reason': '该平仓委托尚未被完整时间窗口查询覆盖，等待补充历史证据'}
        consumed_by = self._state_data.get('exit_receipts', {}).get(f"{uid}:{order['order_id']}")
        if consumed_by is not None and consumed_by != str(position.get('position_id')):
            return {**base, 'reason': '该平仓委托已经关联其他仓位，禁止重复使用'}
        reverse = [row for row in positions.get(key, []) if self._exit_trigger_possible(order, row)]
        reverse.extend(row for row in self._possible_incomplete_rows(key, incomplete_positions) if self._exit_trigger_possible(order, row))
        if len(reverse) != 1 or str(reverse[0].get('position_id')) != str(position.get('position_id')):
            return {**base, 'reason': '同一平仓委托可能对应多个仓位，等待核对'}
        evidence = {**base, 'order_id': str(order['order_id']), 'sl_tp_type': order.get('sl_tp_type'),
                    'trigger_price': order.get('trigger_price'), 'fill_price': order.get('price'),
                    'fill_volume': order.get('fill_volume'), 'closed_pnl': order.get('close_pnl'),
                    'time_done': order.get('time_done'), 'position_id': str(position.get('position_id'))}
        if str(order.get('sl_tp_type')) == '0':
            return {**evidence, 'type': 'ordinary_close', 'confirmed': True, 'reason': '已匹配普通平仓记录，不属于已确认的止盈触发'}
        if str(order.get('sl_tp_type')) != '2':
            return {**evidence, 'reason': '平仓类型标识未确认，不推测为止盈或止损'}
        protection = expected_protection or position
        try:
            known = all(position.get(k) is not None and _decimal(position[k]) == _decimal(protection.get(k, '0')) for k in ('price_tp', 'price_sl'))
            target = _decimal(protection.get('price_tp', '0'))
            valid_target = target > 0 and _decimal(order.get('trigger_price')) == target
        except ValueError:
            known = valid_target = False
        if not known or not valid_target:
            return {**evidence, 'reason': '平仓触发价格或保护参数与已确认设置不一致'}
        return {**evidence, 'type': 'target_exit', 'confirmed': True,
                'reason': 'Gate 返回的止盈标识、触发价及唯一平仓记录相符，实际成交价可能含滑点'}

    def set_market_context(self, symbol, market, spec):
        if not market or not spec or market.get('symbol') != symbol or spec.get('symbol') != symbol:
            raise ValueError('行情和品种详情必须对应同一品种')
        self._markets[symbol], self._specs[symbol] = copy.deepcopy(market), copy.deepcopy(spec)

    def _strategy(self, strategy_id, uid):
        result = next((s for s in self._strategies(uid) if s['id'] == strategy_id), None)
        if result is None:
            raise ValueError('策略不存在或不属于当前已连接账户')
        return result

    def _owned(self, strategy_id, resource_id, uid, kind):
        strategy = self._strategy(strategy_id, uid)
        rid = _uid(resource_id)
        intent = next((i for i in self._intents(uid) if i['strategy_id'] == strategy_id and
                       (i.get('remote_order_id') == rid if kind == 'order' else rid in i.get('position_ids', []))), None)
        if intent is None:
            raise ValueError('该委托或仓位没有本策略已确认的归属记录，禁止操作')
        return strategy, intent, rid

    def request_order_cancel(self, strategy_id, order_id, account_id):
        strategy, intent, _ = self._owned(strategy_id, order_id, _uid(account_id), 'order')
        self._disabled_cells.add((strategy['id'], intent['grid_index']))

    def _rows(self, uid):
        snapshot = self._snapshots[uid]
        return tuple({str(row[key]): row for row in snapshot[name] if row.get(key) is not None}
                     for name, key in [('orders', 'order_id'), ('order_history', 'order_id'),
                                       ('positions', 'position_id'), ('history', 'position_id')])

    def _operations(self, uid, sid=None):
        return [op for op in self._state_data['operations'] if op['account_id'] == uid and
                (sid is None or op['strategy_id'] == sid)]

    def _operation(self, strategy, kind, rid, request, *, explicit=False):
        previous = next((op for op in reversed(self._operations(strategy['account_id'], strategy['id']))
                         if op['kind'] == kind and op['resource_id'] == rid), None)
        if previous and previous['status'] in {'prepared', 'submitted', 'unknown'}:
            if previous['request'] != request:
                raise ValueError('该资源上一次操作结果尚未确认，暂不能发送新的修改')
            return previous, False
        if previous and kind in {'cancel', 'close'} and (previous['status'] != 'rejected' or not explicit):
            return previous, False
        op = dict(id=identifier('op'), strategy_id=strategy['id'], account_id=strategy['account_id'],
                  kind=kind, resource_id=rid, request=copy.deepcopy(request), status='prepared',
                  created_at=now(), submitted_at=self._clock())
        self._state_data['operations'].append(op)
        self._persist()
        return op, True

    async def _send_operation(self, operation, call):
        try:
            await call()
            operation['status'] = 'submitted'
        except LivePreflightAbort as exc:
            operation.update(status='rejected', not_sent=True, error=str(exc))
        except asyncio.CancelledError:
            operation['status'] = 'unknown'
            self._persist()
            raise
        except GateUnknownOutcome as exc:
            operation.update(status='unknown', error='交易请求结果未知，将只读核对；不会自动重发。',
                             error_detail=exc.safe_detail())
        except GateError as exc:
            operation.update(status='rejected', error=str(exc), error_detail=exc.safe_detail())
        except Exception:
            operation.update(status='unknown', error='交易响应未能可靠确认；不会自动重发。')
        if operation.get('error'):
            strategy = self._strategy(operation['strategy_id'], operation['account_id'])
            strategy['notice'] = operation['error']
        self._persist()

    def _matches_order(self, intent, row, *, history=False, placement=False):
        request = intent['request']
        try:
            if (row.get('symbol') != request['symbol'] or row.get('price_type') != request['price_type'] or
                    _direction(row.get('side')) != _direction(request['side']) or
                    _decimal(row.get('volume')) != _decimal(request['volume'])):
                return False
            price = row.get('trigger_price', row.get('price')) if history else row.get('price')
            if _decimal(price) != _decimal(request['price']):
                return False
            if any(row.get(k) is None or _decimal(row[k]) != _decimal(request.get(k, '0')) for k in ('price_tp', 'price_sl')):
                return False
            stamp = _epoch(row.get('time_setup'))
            if placement and (stamp is None or not -1 <= stamp - intent['submitted_server_time'] <= MATCH_TIMEOUT):
                return False
            return True
        except ValueError:
            return False

    def _reconcile_operations(self, uid):
        orders, histories, positions, closed = self._rows(uid)
        for op in self._operations(uid):
            if op['status'] not in {'prepared', 'submitted', 'unknown'} and not (op['kind'] in {'cancel', 'close'} and op['status'] == 'rejected'):
                continue
            rid, kind = op['resource_id'], op['kind']
            strategy, intent, _ = self._owned(op['strategy_id'], rid, uid, 'order' if kind in {'cancel', 'modify_order'} else 'position')
            if kind == 'cancel':
                row = histories.get(rid)
                if rid not in orders and row and str(row.get('state')) in {'2', '4', '5'}:
                    op.update(status='confirmed', result='filled' if str(row.get('state')) == '4' else 'cancelled')
            elif kind == 'close':
                row = closed.get(rid)
                if rid not in positions and row and str(row.get('position_status')) in {'1', '2'}:
                    try:
                        if _decimal(row.get('volume_closed')) >= _decimal(op['request']['volume']):
                            op.update(status='confirmed', result='closed')
                    except ValueError:
                        pass
            else:
                row = orders.get(rid) if kind == 'modify_order' else positions.get(rid)
                historical = row is None
                if historical:
                    row = histories.get(rid) if kind == 'modify_order' else closed.get(rid)
                try:
                    matched = row is not None and all(row.get('trigger_price' if historical and kind == 'modify_order' and k == 'price' else k) is not None and _decimal(row['trigger_price' if historical and kind == 'modify_order' and k == 'price' else k]) == _decimal(v)
                                                     for k, v in op['request'].items())
                except ValueError:
                    matched = False
                if matched:
                    op.update(status='confirmed', result='modified')
                    if kind == 'modify_order':
                        intent['request'].update(op['request'])
                        cell = strategy['cells'][intent['grid_index']]
                        cell.update(entry_price=op['request']['price'], take_profit=op['request']['price_tp'],
                                    stop_loss=op['request']['price_sl'], overrides=True)
                    else:
                        intent.setdefault('position_protection', {})[rid] = copy.deepcopy(op['request'])
                elif historical and row is not None:
                    op.update(status='unknown', error='资源已结束但修改结果缺少一致证据，暂停该格循环并等待核对。')
                    self._pause(strategy, op['error'])

    async def _resolve_order(self, gate, strategy, intent, uid):
        if intent['status'] in {'closed', 'closed_partial', 'cancelled', 'rejected'}:
            return
        orders, histories, _, _ = self._rows(uid)
        cell = strategy['cells'][intent['grid_index']]
        oid = intent.get('remote_order_id')
        if not oid:
            if intent.get('manual_review'):
                return
            valid_log = None
            candidate = intent.get('log_id') or intent.get('queue_id')
            if candidate and self._clock() - intent.get('log_checked_at', 0) >= 2:
                intent['log_checked_at'] = self._clock()
                try:
                    log = _data(await gate.order_log(candidate))
                    valid = (str(log.get('log_id')) == str(candidate) and log.get('symbol') == intent['symbol'] and
                             log.get('price_type') == intent['request']['price_type'] and
                             _direction(log.get('side')) == _direction(intent['request']['side']) and
                             _decimal(log.get('volume')) == _decimal(intent['request']['volume']))
                    if not valid:
                        intent.update(status='unknown', manual_review=True)
                        cell['status'] = 'unknown'
                        self._pause(strategy, '队列日志字段与本次挂单不一致，已暂停，禁止猜测委托归属。')
                        return
                    valid_log = log
                    intent['queue_log'] = copy.deepcopy(log)
                except (GateError, ValueError):
                    pass
            valid_log = valid_log or intent.get('queue_log')
            if valid_log and str(valid_log.get('state')) == '5':
                intent['status'], cell['status'] = 'rejected', 'rejected'
                self._pause(strategy, 'Gate 拒绝了原生挂单，已暂停新铺单。')
                return
            claimed = {i.get('remote_order_id') for i in self._intents(uid) if i['id'] != intent['id']}
            candidates = []
            for rid, row in {**histories, **orders}.items():
                if rid in claimed or rid in intent.get('baseline_orders', []):
                    continue
                if valid_log and _id(valid_log.get('order_id')) != rid:
                    continue
                if self._matches_order(intent, row, history=rid not in orders, placement=True):
                    candidates.append(rid)
            if len(candidates) == 1:
                oid = candidates[0]
                intent.update(remote_order_id=oid, placement_confirmed=True,
                              ownership_evidence={'order': copy.deepcopy(orders.get(oid) or histories[oid]),
                                                  'log': copy.deepcopy(valid_log)})
            elif len(candidates) > 1:
                intent.update(status='unknown', manual_review=True)
                self._pause(strategy, '出现多个相同委托候选，已暂停等待人工核对，禁止猜测归属。')
                return
            else:
                if self._clock() - intent['submitted_at'] > MATCH_TIMEOUT:
                    intent['status'], cell['status'] = 'unknown', 'unknown'
                    self._pause(strategy, '提交结果尚未找到完整委托证据，已暂停；不会自动重发。')
                return
        row = orders.get(oid)
        current = row is not None
        row = row if current else histories.get(oid)
        if row is None:
            intent.setdefault('missing_since', self._clock())
            intent['status'], cell['status'] = 'reconciling', 'reconciling'
            if self._clock() - intent['missing_since'] > MATCH_TIMEOUT:
                self._pause(strategy, '已确认委托暂未出现在当前或历史快照中，等待核对，不会补发。')
            return
        if not self._matches_order(intent, row, history=not current):
            intent['status'], cell['status'] = 'unknown', 'unknown'
            self._pause(strategy, '远端委托参数发生未确认变化，保留归属以便撤单，暂停新铺单。')
            return
        intent.pop('missing_since', None)
        state = str(row.get('state', '1' if current else ''))
        intent.update(remote_state=state, remote_done=not current and state in {'2', '4', '5'})
        if current and state == '1':
            intent['status'], cell['status'] = 'pending', 'pending'
            return
        try:
            filled = _decimal(row.get('fill_volume', '0'))
        except ValueError:
            filled = D(0)
        if not current and state in {'2', '5'} and filled == 0:
            intent['status'] = 'cancelled' if state == '2' else 'rejected'
            cell['status'] = intent['status']
            if strategy['status'] in {'starting', 'running', 'recovering'} and cell.get('enabled', True):
                self._pause(strategy, '网格委托已在远端撤销或拒绝，本格不会自动补发。')
            return
        fill_time = _epoch(row.get('time_done'))
        if filled > 0 and fill_time:
            try:
                intent.update(fill_volume=_text(filled), fill_price=_text(row['price']), fill_time=fill_time)
            except ValueError:
                pass
        if current or state == '3' or (filled > 0 and filled < _decimal(intent['request']['volume'])):
            intent['status'], cell['status'] = 'partial', 'partial'
            self._pause(strategy, '原生委托部分成交，暂停补单；可撤销已确认的剩余委托。')
        elif state == '4' and filled == _decimal(intent['request']['volume']) and fill_time:
            intent['status'], cell['status'] = 'awaiting_position', 'reconciling'
        else:
            intent['status'], cell['status'] = 'unknown', 'unknown'
            self._pause(strategy, '成交记录缺少实际成交时间或手数，禁止推测持仓归属。')

    def _position_candidates(self, intent, uid):
        if not intent.get('fill_time') or not intent.get('fill_volume'):
            return []
        _, _, positions, history = self._rows(uid)
        request = intent['request']
        tick = _decimal(intent.get('tick_size', '0.00000001'))
        candidates = []
        for pid, row in {**history, **positions}.items():
            if pid in intent.get('baseline_positions', []):
                continue
            try:
                stamp = _epoch(row.get('time_create'))
                volume = row.get('volume', row.get('volume_closed'))
                if (row.get('symbol') == intent['symbol'] and _direction(row.get('position_dir')) == _direction(request['side'])
                        and stamp is not None and abs(stamp - intent['fill_time']) <= 2
                        and _decimal(volume) == _decimal(intent['fill_volume'])
                        and abs(_decimal(row.get('price_open')) - _decimal(intent['fill_price'])) <= tick
                        and all(row.get(k) is not None and _decimal(row[k]) == _decimal(request.get(k, '0'))
                                for k in ('price_tp', 'price_sl'))):
                    candidates.append(pid)
            except ValueError:
                pass
        return candidates

    def _resolve_positions(self, strategy, intent, uid):
        pids = intent.get('position_ids', [])
        if not pids:
            return
        previously_closed = intent['status'] in {'closed', 'closed_partial'}
        _, _, positions, histories = self._rows(uid)
        cell = strategy['cells'][intent['grid_index']]
        if any(pid in positions for pid in pids):
            if previously_closed:
                return
            for pid in pids:
                row = positions.get(pid)
                expected = intent.get('position_protection', {}).get(pid, intent['request'])
                if row is not None:
                    try:
                        unchanged = all(row.get(key) is not None and _decimal(row[key]) == _decimal(expected.get(key, '0'))
                                        for key in ('price_tp', 'price_sl'))
                    except ValueError:
                        unchanged = False
                    if not unchanged:
                        intent['external_protection_change'] = True
                        self._pause(strategy, '已关联仓位保护参数发生未确认变化，保留仓位管理并暂停循环。')
            if intent.get('remote_done', strategy['execution_mode'] == 'legacy_local'):
                intent['status'], cell['status'] = 'open', 'open'
            return
        closed = [histories.get(pid) for pid in pids]
        try:
            valid = all(row and str(row.get('position_status')) in {'1', '2'} and
                        _decimal(row.get('volume_closed')) == _decimal(intent.get('fill_volume', intent['request']['volume']))
                        for row in closed)
        except ValueError:
            valid = False
        if not valid:
            if not previously_closed:
                intent['status'], cell['status'] = 'awaiting_close', 'reconciling'
            return
        evidence = []
        for pid, row in zip(pids, closed):
            protection = intent.get('position_protection', {}).get(pid, intent['request'])
            result = self.classify_exit(row, uid, protection)
            if not result['confirmed'] and strategy.get('close_requested') and any(
                    op['kind'] == 'close' and op['resource_id'] == pid and op['status'] == 'confirmed'
                    for op in self._operations(uid, strategy['id'])):
                # A user-requested close is complete when its exact owned
                # position is fully closed. Its deal-type history may arrive
                # later; it is never needed to authorize TP replenishment here.
                result = {'type': 'ordinary_close', 'confirmed': True, 'order_id': None,
                          'source': 'console_close_operation', 'reason': '用户平仓请求的目标状态已确认：该仓位已全平。'}
            evidence.append({**result, 'position_id': pid})
            if result['confirmed'] and result.get('order_id'):
                self._state_data.setdefault('exit_receipts', {})[f"{uid}:{result['order_id']}"] = pid
            if pid not in intent.setdefault('accounted_position_ids', []) and not previously_closed:
                strategy['realized_pnl'] = _text(_decimal(strategy['realized_pnl']) + _decimal(row.get('realized_pnl', '0')))
                intent['accounted_position_ids'].append(pid)
        intent['exit_evidence'] = evidence
        confirmed = all(item['confirmed'] for item in evidence)
        liquidation = any(item['type'] == 'liquidation' for item in evidence)
        target_exit = confirmed and all(item['type'] == 'target_exit' for item in evidence)
        exit_type = 'liquidation' if liquidation else 'target_exit' if target_exit else 'ordinary_close' if confirmed else 'awaiting_confirmation'
        intent['exit_type'] = exit_type
        if previously_closed:
            # Historical repairs classify the evidence only. Existing paused or
            # stopped strategies never restart because a newer build was loaded.
            intent.setdefault('cycle_accounted', True)
            return
        if not confirmed:
            intent['status'], cell['status'] = 'awaiting_exit_evidence', 'reconciling'
            strategy['notice'] = '仓位已平仓，正在核对平仓委托类型；确认止盈记录后才会补单。'
            return
        if not intent.get('remote_done', strategy['execution_mode'] == 'legacy_local'):
            intent['status'], cell['status'] = 'partial', 'partial'
            return
        partial = _decimal(intent.get('fill_volume', intent['request']['volume'])) != _decimal(intent['request']['volume'])
        intent['status'] = 'closed_partial' if partial else 'closed'
        cell['status'] = 'done'
        if not intent.get('cycle_accounted'):
            cell['completed_cycles'] += 1
            strategy['completed_cycles'] += 1
            intent['cycle_accounted'] = True
        can_repeat = (strategy['execution_mode'] == 'native_trigger' and not partial and target_exit
                      and not intent.get('external_protection_change')
                      and not cell.get('repeat_disabled') and _decimal(cell['take_profit']) > 0 and strategy['config']['repeat']
                      and strategy['status'] in {'starting', 'running', 'recovering'}
                      and strategy['id'] not in self._stop_requests and not strategy.get('stop_requested')
                      and cell.get('enabled', True) and (strategy['id'], cell['index']) not in self._disabled_cells)
        protection_pending = any(op['resource_id'] in pids and op['kind'] == 'modify_position' and
                                 op['status'] in {'prepared', 'submitted', 'unknown'} for op in self._operations(uid, strategy['id']))
        can_repeat = can_repeat and not protection_pending
        if liquidation or (not target_exit and not cell.get('repeat_disabled') and not strategy.get('close_requested') and not strategy.get('stop_requested')):
            self._pause(strategy, '策略仓位发生强平或已确认的非止盈退出，已暂停新铺单，请核对仓位。')
        if can_repeat:
            self._rearm_closed_cell(strategy, cell, intent)
        strategy.pop('notice', None)
        self._log(uid, '已核对策略仓位真实平仓记录；' + ('已确认原生止盈，等待下一轮挂单。' if can_repeat else '本轮结束。'))

    def _rearm_closed_cell(self, strategy, cell, intent):
        if cell.get('intent_id') != intent['id'] or intent.get('rearmed_generation') is not None:
            return False
        generation = max(cell.get('generation', 0), intent.get('generation', 0)) + 1
        intent['rearmed_generation'] = generation
        cell.update(status='waiting', intent_id=None, generation=generation)
        return True

    def resume_eligibility(self, strategy, uid):
        """Describe recovery without mutating strategies or contacting Gate."""
        reasons, codes = [], []
        def block(code, message):
            if code not in codes:
                codes.append(code)
                reasons.append(message)
        if strategy.get('account_id') != uid:
            block('account_mismatch', '策略不属于当前账户。')
        if strategy.get('execution_mode') != 'native_trigger':
            block('legacy_strategy', '旧版策略需要停止后重新创建，不能直接恢复。')
        if strategy.get('status') != 'paused':
            block('not_paused', '仅暂停中的原生策略可以恢复。')
        if strategy.get('stop_requested') or strategy.get('close_requested') or strategy['id'] in self._stop_requests:
            block('stop_requested', '策略已请求停止或平仓，不能恢复。')
        if not strategy['config'].get('repeat'):
            block('repeat_disabled', '该策略未启用循环补单。')
        if not self._account_fresh(uid) or self.current_uid != uid:
            block('account_sync_required', '请先完成当前账户的最新对账。')
        if self.has_unresolved_execution(uid):
            block('execution_unresolved', '账户仍有执行或平仓证据待核对，暂不能恢复。')
        intents = {i['id']: i for i in self._intents(uid) if i['strategy_id'] == strategy['id']}
        histories = self._rows(uid)[3] if uid in self._snapshots else {}
        rearm, planned = [], []
        for cell in strategy['cells']:
            if not cell.get('enabled', True) or cell.get('repeat_disabled') or (strategy['id'], cell['index']) in self._disabled_cells:
                continue
            intent = intents.get(cell.get('intent_id'))
            if intent and (intent.get('manual_review') or intent.get('external_protection_change')):
                block('ownership_or_protection_unconfirmed', '存在归属或保护参数变更待核对的格位。')
            if cell['status'] in {'waiting', 'ready', 'armed'}:
                planned.append(cell)
                continue
            if intent and intent['status'] in {'rejected', 'cancelled', 'closed_partial', 'partial'}:
                block('interrupted_grid', '存在拒单、撤单或部分成交格位，需要停止后核对重建。')
            if cell['status'] != 'done':
                continue
            if not intent or intent['status'] != 'closed' or not intent.get('remote_done') or not intent.get('position_ids'):
                block('closed_grid_unconfirmed', '已结束格位缺少完整成交与平仓证据。')
                continue
            evidence = []
            for pid in intent['position_ids']:
                row = histories.get(pid)
                evidence.append(self.classify_exit(row, uid, intent.get('position_protection', {}).get(pid, intent['request'])) if row else {'confirmed': False})
            if not all(item.get('confirmed') and item.get('type') == 'target_exit' for item in evidence):
                block('non_tp_or_unconfirmed_exit', '存在普通平仓、强平或止盈证据未确认的格位，不能作为止盈循环恢复。')
                continue
            if intent.get('rearmed_generation') is None:
                rearm.append(cell)
                planned.append(cell)
        if not planned:
            block('no_resumable_grid', '没有可恢复的待挂格位。')
        capacity = self._capacity(uid, len(rearm))
        if not capacity['sufficient']:
            block('capacity_exceeded', '恢复格位后将超过账户委托及仓位容量。')
        try:
            market, spec = self._fresh_context(strategy)
            stop = strategy['config'].get('stop_loss')
            long = strategy['config']['direction'] == 'long'
            if stop is not None and (_decimal(market['bid']) <= _decimal(stop) if long else _decimal(market['ask']) >= _decimal(stop)):
                block('strategy_stop_line_reached', '当前报价已达到策略停止线，不能恢复。')
            free = _decimal(self._snapshots.get(uid, {}).get('assets', {}).get('margin_free'))
            estimates = [_estimate(_decimal(c['entry_price']), _decimal(strategy['config']['volume']), spec) for c in planned]
            if free <= 0 or (all(value is not None for value in estimates) and sum(estimates, D(0)) > free):
                block('insufficient_margin', '当前可用保证金不足以恢复待挂格位。')
        except (ValueError, KeyError):
            block('market_or_account_unavailable', '行情、品种详情或账户资金尚未完成核验。')
        return {'recoverable': not reasons, 'resume_blockers': reasons, 'resume_blocker_codes': codes}

    async def resume_strategy(self, strategy_id, gate, account_id):
        """Explicit recovery only; this call never creates a Gate order."""
        uid = _uid(account_id)
        async with self._lock:
            strategy = self._strategy(strategy_id, uid)
            if strategy['status'] in {'running', 'starting'} and strategy.get('resumed_at'):
                return self._management_state(strategy, uid)
            await self._refresh(gate, uid, force=True)
            await self._resolve_all(gate, uid)
            if not self._snapshot_fresh(uid):
                raise ValueError('完整账户对账尚未完成或已过期，暂不能恢复策略。')
            eligibility = self.resume_eligibility(strategy, uid)
            if not eligibility['recoverable']:
                raise ValueError(eligibility['resume_blockers'][0])
            intents = {i['id']: i for i in self._intents(uid) if i['strategy_id'] == strategy_id}
            restored = 0
            for cell in strategy['cells']:
                if (cell['status'] == 'done' and cell.get('enabled', True) and not cell.get('repeat_disabled')
                        and (strategy_id, cell['index']) not in self._disabled_cells):
                    intent = intents.get(cell.get('intent_id'))
                    if intent and intent.get('exit_type') == 'target_exit':
                        restored += int(self._rearm_closed_cell(strategy, cell, intent))
            strategy.update(status='running', resumed_at=now())
            strategy.pop('error', None)
            strategy.pop('notice', None)
            self._log(uid, f"已按用户请求恢复策略，{restored} 个已确认止盈格位等待下一轮原生挂单。")
            self._persist()
            return self._management_state(strategy, uid)

    async def _resolve_all(self, gate, uid):
        self._reconcile_operations(uid)
        for intent in self._intents(uid):
            strategy = self._strategy(intent['strategy_id'], uid)
            if strategy['execution_mode'] == 'native_trigger':
                await self._resolve_order(gate, strategy, intent, uid)
        claimed = {pid for intent in self._intents(uid) for pid in intent.get('position_ids', [])}
        matches = {}
        for intent in self._intents(uid):
            if intent.get('fill_time') and not intent.get('position_ids') and not intent.get('manual_review'):
                matches[intent['id']] = [pid for pid in self._position_candidates(intent, uid) if pid not in claimed]
        counts = Counter(pid for candidates in matches.values() for pid in candidates)
        for intent in self._intents(uid):
            strategy = self._strategy(intent['strategy_id'], uid)
            candidates = matches.get(intent['id'], [])
            if len(candidates) == 1 and counts[candidates[0]] == 1:
                intent.update(position_ids=candidates, position_id=candidates[0])
            elif candidates:
                intent.update(status='unknown', manual_review=True)
                self._pause(strategy, '多个成交与仓位证据存在歧义，已暂停，不会认领不确定仓位。')
            elif intent.get('fill_time') and not intent.get('position_ids') and self._clock() - intent['fill_time'] > MATCH_TIMEOUT:
                self._pause(strategy, '成交后的仓位关联仍未确认，暂停补单并等待完整快照。')
            self._resolve_positions(strategy, intent, uid)
        for strategy in self._strategies(uid):
            if strategy['status'] == 'recovering' and not any(i['strategy_id'] == strategy['id'] and i['status'] in UNRESOLVED for i in self._intents(uid)):
                strategy['status'] = 'running'
            if strategy['status'] in {'starting', 'running'} and all(c['status'] in {'done', 'cancelled', 'rejected'} for c in strategy['cells']):
                strategy['status'] = 'completed'
        self._finish_stops(uid)

    def _finish_stops(self, uid):
        orders, _, positions, _ = self._rows(uid)
        for strategy in self._strategies(uid):
            if strategy['status'] not in {'stopping', 'closing'}:
                continue
            intents = [i for i in self._intents(uid) if i['strategy_id'] == strategy['id']]
            if any(i.get('remote_order_id') in orders or (not i.get('remote_order_id') and i['status'] in UNRESOLVED)
                   or (i.get('remote_order_id') and not i.get('remote_done') and i['status'] in UNRESOLVED)
                   for i in intents):
                continue
            latest = {(op['kind'], op['resource_id']): op for op in self._operations(uid, strategy['id'])}
            if any(op['kind'] in {'cancel', 'close'} and op['status'] in {'prepared', 'submitted', 'unknown', 'rejected'} for op in latest.values()):
                continue
            if strategy.get('close_requested') and any(any(pid in positions for pid in i.get('position_ids', [])) or i['status'] in UNRESOLVED for i in intents):
                continue
            strategy.update(status='stopped', stopped_at=strategy.get('stopped_at') or now())

    def _fresh_context(self, strategy, *, opening=True):
        symbol = strategy['config']['symbol']
        market = self.quote_provider(symbol) if callable(self.quote_provider) else self._markets.get(symbol)
        spec = self._specs.get(symbol)
        if not market or not spec:
            raise LivePreflightAbort('该品种最新行情或真实详情不可用')
        check = dict(market)
        if not opening:
            check['trade_mode'] = '4'  # Protection and closing remain available in close-only mode.
        problem = self._quote_problem(GridConfig.model_validate(strategy['config']), spec, check)
        if problem:
            raise LivePreflightAbort(problem)
        return market, spec

    def _pending_valid(self, strategy, request, market):
        long = _direction(request['side']) == 'long'
        entry = _decimal(request['price'])
        quote = _decimal(market['ask'] if long else market['bid'])
        if not (entry < quote if long else entry > quote):
            raise LivePreflightAbort('报价已越过该挂单线，本轮等待恢复正确方向后再提交')
        self._protect_valid(long, entry, request)
        stop = strategy['config'].get('stop_loss')
        if stop is not None and (_decimal(market['bid']) <= _decimal(stop) if long else _decimal(market['ask']) >= _decimal(stop)):
            raise LivePreflightAbort('行情已达到策略停止线，禁止新铺单')

    @staticmethod
    def _protect_valid(long, reference, request):
        target, stop = _decimal(request['price_tp']), _decimal(request['price_sl'])
        if target > 0 and not (target > reference if long else target < reference):
            raise LivePreflightAbort('止盈价方向不正确')
        if stop > 0 and not (stop < reference if long else stop > reference):
            raise LivePreflightAbort('止损价方向不正确')

    async def _submit(self, strategy, cell, gate, market, spec):
        uid = strategy['account_id']
        await self._refresh(gate, uid, force=True)
        cfg = GridConfig.model_validate(strategy['config'])
        request = dict(symbol=cfg.symbol, side=2 if cfg.direction == 'long' else 1,
                       price_type='trigger', price=cell['entry_price'], volume=_text(cfg.volume),
                       price_tp=cell['take_profit'], price_sl=cell.get('stop_loss', _text(cfg.stop_loss) if cfg.stop_loss is not None else '0'))
        def before_send():
            if strategy['id'] in self._stop_requests or strategy.get('stop_requested') or strategy['status'] not in {'starting', 'running', 'recovering'} or (strategy['id'], cell['index']) in self._disabled_cells or not cell.get('enabled', True):
                raise LivePreflightAbort('策略或此格已停止，此次挂单未发送')
            latest, _ = self._fresh_context(strategy)
            self._pending_valid(strategy, request, latest)
            if self.current_uid != uid or not self._snapshot_fresh(uid):
                raise LivePreflightAbort('发送前账户核验已过期，此次挂单未发送')
        try:
            before_send()
        except LivePreflightAbort as exc:
            strategy['notice'] = str(exc)
            cell['status'] = 'waiting'
            return False
        snapshot = self._snapshots[uid]
        required = _estimate(_decimal(request['price']), cfg.volume, spec)
        free = _decimal(snapshot['assets']['margin_free'])
        if free <= 0 or (required is not None and free < required) or len(snapshot['orders']) + len(snapshot['positions']) >= CAPACITY:
            self._pause(strategy, '真实账户可用保证金或委托容量不足，暂停新铺单。')
            return False
        orders, histories, positions, closed = self._rows(uid)
        intent = dict(id=identifier('intent'), strategy_id=strategy['id'], account_id=uid, symbol=cfg.symbol,
                      grid_index=cell['index'], generation=cell.get('generation', 0), request=request,
                      status='prepared', submitted_at=self._clock(), submitted_server_time=snapshot['exchange_time'],
                      baseline_positions=list(set(positions) | set(closed)), baseline_orders=list(set(orders) | set(histories)),
                      tick_size=spec['tick_size'], queue_id=None, log_id=None, remote_order_id=None, position_id=None, position_ids=[])
        self._state_data['intents'].append(intent)
        cell.update(intent_id=intent['id'], status='submitting')
        self._persist()
        try:
            response = _data(await gate.create_order(**request, before_send=before_send))
            queue_id = _id(response.get('id'))
            if queue_id is None:
                raise ValueError('缺少队列任务编号')
            intent.update(status='submitted', queue_id=queue_id, log_id=_id(response.get('log_id')))
            cell['status'] = 'reconciling'
        except LivePreflightAbort as exc:
            intent.update(status='rejected', not_sent=True)
            cell.update(status='waiting', intent_id=None)
            strategy['notice'] = str(exc)
        except asyncio.CancelledError:
            intent['status'], cell['status'] = 'unknown', 'unknown'
            self._pause(strategy, '挂单提交被中断，结果未知，不会自动重发。')
            self._persist()
            raise
        except GateError as exc:
            intent['error_detail'] = exc.safe_detail()
            strategy['last_execution_error'] = copy.deepcopy(intent['error_detail'])
            if isinstance(exc, GateUnknownOutcome) and (candidate := _id(exc.pending.get('queue_id'))):
                # Contradictory acceptance responses provide a lookup candidate,
                # never proof of a placed order. _resolve_order verifies it.
                intent['queue_id'] = candidate
            intent['status'] = 'unknown' if isinstance(exc, GateUnknownOutcome) else 'rejected'
            cell['status'] = intent['status']
            self._pause(strategy, '挂单结果未知，不会自动重发。' if isinstance(exc, GateUnknownOutcome) else str(exc))
        except Exception:
            intent['status'], cell['status'] = 'unknown', 'unknown'
            self._pause(strategy, '挂单响应未能可靠确认，不会自动重发。')
        self._persist()
        await self._refresh(gate, uid, force=True)
        await self._resolve_all(gate, uid)
        self._persist()
        return not intent.get('not_sent', False)

    async def _cancel_one(self, strategy, intent, gate, *, explicit=False):
        oid = intent.get('remote_order_id')
        if oid not in self._rows(strategy['account_id'])[0]:
            return False
        operation, created = self._operation(strategy, 'cancel', oid, {}, explicit=explicit)
        if created:
            def guard():
                self._owned(strategy['id'], oid, strategy['account_id'], 'order')
                if self.current_uid != strategy['account_id']:
                    raise LivePreflightAbort('账户已变化，撤单未发送')
            await self._send_operation(operation, lambda: gate.cancel_order(oid, before_send=guard))
        return created

    async def _drive_stop(self, strategy, gate, *, explicit=False):
        uid = strategy['account_id']
        self._stop_local(strategy)
        self._persist()
        budget = BATCH_SIZE
        for intent in self._intents(uid):
            if intent['strategy_id'] == strategy['id'] and budget > 0:
                budget -= int(await self._cancel_one(strategy, intent, gate, explicit=explicit))
        await self._refresh(gate, uid, force=True)
        await self._resolve_all(gate, uid)
        orders, _, positions, _ = self._rows(uid)
        intents = [i for i in self._intents(uid) if i['strategy_id'] == strategy['id']]
        pending = any(i.get('remote_order_id') in orders or i['status'] in UNRESOLVED for i in intents)
        if strategy.get('close_requested') and not pending:
            for intent in intents:
                for pid in intent.get('position_ids', []):
                    if budget <= 0 or pid not in positions:
                        continue
                    operation, created = self._operation(strategy, 'close', pid, {'volume': positions[pid]['volume']}, explicit=explicit)
                    if created:
                        def guard(pid=pid):
                            self._owned(strategy['id'], pid, uid, 'position')
                            if self.current_uid != uid:
                                raise LivePreflightAbort('账户已变化，平仓未发送')
                        await self._send_operation(operation, lambda pid=pid, guard=guard: gate.close_position(pid, before_send=guard))
                        budget -= 1
            await self._refresh(gate, uid, force=True)
            await self._resolve_all(gate, uid)
        self._finish_stops(uid)
        self._persist()

    async def cycle(self, gate, markets, specs, account_id):
        uid = _uid(account_id)
        async with self._lock:
            for symbol, market in markets.items():
                if market and specs.get(symbol):
                    self.set_market_context(symbol, market, specs[symbol])
            await self._refresh(gate, uid)
            await self._resolve_all(gate, uid)
            budget = BATCH_SIZE
            for strategy in self._strategies(uid):
                if strategy['id'] in self._stop_requests or strategy['status'] in {'stopping', 'closing'}:
                    if strategy['status'] != 'stopped':
                        await self._drive_stop(strategy, gate)
                    continue
                if strategy['execution_mode'] != 'native_trigger' or strategy['status'] not in {'starting', 'running', 'recovering'}:
                    continue
                try:
                    market, spec = self._fresh_context(strategy)
                except LivePreflightAbort as exc:
                    strategy['notice'] = str(exc)
                    continue
                cfg = GridConfig.model_validate(strategy['config'])
                if cfg.stop_loss is not None and (_decimal(market['bid']) <= cfg.stop_loss if cfg.direction == 'long' else _decimal(market['ask']) >= cfg.stop_loss):
                    self.request_stop(strategy['id'], uid)
                    await self._drive_stop(strategy, gate)
                    continue
                for cell in strategy['cells']:
                    if strategy['id'] in self._stop_requests:
                        await self._drive_stop(strategy, gate)
                        break
                    if (strategy['id'], cell['index']) in self._disabled_cells:
                        cell['enabled'] = False
                    if not cell.get('enabled', True) or cell['status'] not in {'waiting', 'ready', 'armed'}:
                        continue
                    price = _decimal(market['ask'] if cfg.direction == 'long' else market['bid'])
                    eligible = _decimal(cell['entry_price']) < price if cfg.direction == 'long' else _decimal(cell['entry_price']) > price
                    cell['status'] = 'ready' if eligible else 'waiting'
                    if eligible and budget > 0 and not self.has_unresolved_execution(uid) and strategy['status'] in {'starting', 'running', 'recovering'}:
                        budget -= int(await self._submit(strategy, cell, gate, market, spec))
                if strategy['status'] == 'starting' and not any(c['status'] in {'ready', 'submitting', 'reconciling'} for c in strategy['cells']):
                    strategy['status'] = 'running'
            self._persist()

    def _management_state(self, strategy, uid):
        result = self.state(strategy['config']['symbol'], uid)
        result.update(strategy_id=strategy['id'], remaining_positions=sum(p['strategy_id'] == strategy['id'] for p in result['positions']),
                      remaining_orders=sum(o['strategy_id'] == strategy['id'] and o['source'] == 'gate' for o in result['orders']),
                      cancelled_count=sum(op['kind'] == 'cancel' and op['status'] == 'confirmed' for op in self._operations(uid, strategy['id'])),
                      unresolved=self.has_unresolved_execution(uid), completed=strategy['status'] == 'stopped')
        return result

    async def cancel(self, strategy_id, gate, account_id):
        uid = _uid(account_id)
        self.request_stop(strategy_id, uid)
        async with self._lock:
            strategy = self._strategy(strategy_id, uid)
            self._stop_local(strategy)
            self._persist()
            await self._refresh(gate, uid, force=True)
            await self._resolve_all(gate, uid)
            await self._drive_stop(strategy, gate, explicit=True)
            return self._management_state(strategy, uid)

    async def cancel_order(self, strategy_id, order_id, gate, account_id):
        uid = _uid(account_id)
        self.request_order_cancel(strategy_id, order_id, uid)
        async with self._lock:
            strategy, intent, _ = self._owned(strategy_id, order_id, uid, 'order')
            strategy['cells'][intent['grid_index']]['enabled'] = False
            self._persist()
            await self._refresh(gate, uid, force=True)
            await self._resolve_all(gate, uid)
            await self._cancel_one(strategy, intent, gate, explicit=True)
            await self._refresh(gate, uid, force=True)
            await self._resolve_all(gate, uid)
            self._persist()
            return self._management_state(strategy, uid)

    @staticmethod
    def _modification_values(row, updates, spec):
        values = {}
        tick = _decimal(spec['tick_size'])
        for key, value in updates.items():
            value = row.get(key) if value is None else value
            if value is None:
                raise ValueError('远端未返回需保留的保护价格，禁止覆盖未知值')
            price = _decimal(value)
            if price < 0 or (key == 'price' and price == 0) or price % tick != 0:
                raise ValueError('修改价格必须符合该品种精度，清空止盈止损请明确传 0')
            values[key] = _text(price)
        return values

    async def modify_order(self, strategy_id, order_id, price, price_tp, price_sl, gate, account_id):
        uid = _uid(account_id)
        async with self._lock:
            strategy, intent, oid = self._owned(strategy_id, order_id, uid, 'order')
            if strategy['id'] in self._stop_requests or strategy.get('stop_requested'):
                raise ValueError('策略正在停止或已停止，不能修改入场挂单')
            await self._refresh(gate, uid, force=True)
            await self._resolve_all(gate, uid)
            row = self._rows(uid)[0].get(oid)
            if row is None:
                raise ValueError('该委托已不在当前挂单中，请刷新')
            market, spec = self._fresh_context(strategy)
            values = self._modification_values(row, {'price': price, 'price_tp': price_tp, 'price_sl': price_sl}, spec)
            request = {**intent['request'], **values}
            self._pending_valid(strategy, request, market)
            if _decimal(values['price_tp']) == 0:
                strategy['cells'][intent['grid_index']]['repeat_disabled'] = True
            operation, created = self._operation(strategy, 'modify_order', oid, values, explicit=True)
            if created:
                def guard():
                    if strategy['id'] in self._stop_requests or (strategy['id'], intent['grid_index']) in self._disabled_cells or self.current_uid != uid:
                        raise LivePreflightAbort('策略或此格已停止，修改未发送')
                    latest, _ = self._fresh_context(strategy)
                    self._pending_valid(strategy, request, latest)
                await self._send_operation(operation, lambda: gate.update_order(oid, **values, before_send=guard))
            await self._refresh(gate, uid, force=True)
            await self._resolve_all(gate, uid)
            self._persist()
            return {**self._management_state(strategy, uid), 'operation': copy.deepcopy(operation)}

    async def modify_position(self, strategy_id, position_id, price_tp, price_sl, gate, account_id):
        uid = _uid(account_id)
        async with self._lock:
            strategy, intent, pid = self._owned(strategy_id, position_id, uid, 'position')
            if strategy.get('close_requested'):
                raise ValueError('策略正在平仓，不能同时修改保护价格')
            await self._refresh(gate, uid, force=True)
            await self._resolve_all(gate, uid)
            row = self._rows(uid)[2].get(pid)
            if row is None:
                raise ValueError('该仓位已不在当前持仓中，请刷新')
            market, spec = self._fresh_context(strategy, opening=False)
            values = self._modification_values(row, {'price_tp': price_tp, 'price_sl': price_sl}, spec)
            long = _direction(row.get('position_dir')) == 'long'
            self._protect_valid(long, _decimal(market['bid'] if long else market['ask']), values)
            if _decimal(values['price_tp']) == 0:
                strategy['cells'][intent['grid_index']]['repeat_disabled'] = True
            operation, created = self._operation(strategy, 'modify_position', pid, values, explicit=True)
            if created:
                def guard():
                    if strategy.get('close_requested') or self.current_uid != uid:
                        raise LivePreflightAbort('策略正在平仓或账户已变化，修改未发送')
                    latest, _ = self._fresh_context(strategy, opening=False)
                    self._protect_valid(long, _decimal(latest['bid'] if long else latest['ask']), values)
                await self._send_operation(operation, lambda: gate.update_position(pid, **values, before_send=guard))
            await self._refresh(gate, uid, force=True)
            await self._resolve_all(gate, uid)
            self._persist()
            return {**self._management_state(strategy, uid), 'operation': copy.deepcopy(operation)}

    async def close_strategy(self, strategy_id, gate, account_id):
        uid = _uid(account_id)
        self.request_stop(strategy_id, uid)
        async with self._lock:
            strategy = self._strategy(strategy_id, uid)
            strategy.update(close_requested=True, status='closing', stop_requested=True)
            self._persist()
            await self._refresh(gate, uid, force=True)
            await self._resolve_all(gate, uid)
            await self._drive_stop(strategy, gate, explicit=True)
            return self._management_state(strategy, uid)
