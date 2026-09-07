"""Actual Gate response shapes, with deterministic in-memory transport doubles."""

import asyncio
import copy

import pytest

from app.gate import GateError, GateTransportError
from app.market import LiveMarket, normalize_candles, normalize_spec, normalize_ticker


NOW = 1_788_765_034.0
SYMBOL_ROW = {"symbol": "XAUUSD", "symbol_desc": "Gold", "status": "open", "trade_mode": "4",
              "settlement_currency": "USD", "price_precision": 2, "leverages": ["20", "500"]}
DETAIL_ROW = {"symbol": "XAUUSD", "symbol_desc": "Gold", "contract_volume": "100",
              "settlement_currency": "USD", "min_order_volume": "0.01", "max_order_volume": "100",
              "leverage": "500", "price_precision": 2, "price_sl_level": "100.00", "trade_mode": "4"}
TICKER = {"data": {"last_price": "4397.66", "bid_price": "4397.78", "ask_price": "4397.88",
                   "price_change": "-0.73", "highest_price": "4435.29", "lowest_price": "4385.37",
                   "status": "open", "trade_mode": "4", "settlement_currency": "USD", "exchange_rate": "1"},
          "timestamp": 1_788_765_034_512}
CANDLES = {"data": {"list": [{"o": "4400.34", "c": "4397.66", "h": "4400.50", "l": "4397.49", "t": 1_788_765_000},
                             {"o": "4400.38", "c": "4400.34", "h": "4401.47", "l": "4399.87", "t": 1_788_764_940}]}}


class FakeGate:
    def __init__(self, credentials=True):
        self.credentials_set = credentials
        self.calls = []
        self.failures = {}
        self.symbol_rows = [copy.deepcopy(SYMBOL_ROW)]
        self.detail = copy.deepcopy(DETAIL_ROW)
        self.quote = copy.deepcopy(TICKER)

    async def _return(self, key, payload):
        self.calls.append(key)
        await asyncio.sleep(0)
        if key in self.failures:
            raise self.failures[key]
        return copy.deepcopy(payload)

    async def symbols(self):
        return await self._return("symbols", {"data": {"list": self.symbol_rows}})

    async def ticker(self, symbol):
        return await self._return(symbol, self.quote)

    async def klines(self, symbol, *args):
        return await self._return(symbol + ":klines", CANDLES)

    async def symbol_detail(self, symbol):
        return await self._return(symbol + ":detail", {"data": {"list": [self.detail]}})


def run(coro):
    return asyncio.run(coro)


def test_actual_ticker_normalizes_server_response_time_without_creating_ticks():
    quote = normalize_ticker("XAUUSD", TICKER, NOW)
    assert quote["bid"] == "4397.78"
    assert quote["ask"] == "4397.88"
    assert quote["last"] == "4397.66"  # Last trade need not be between bid and ask.
    assert quote["spread"] == "0.10"
    assert quote["quote_timestamp"] == 1_788_765_034_512
    assert quote["received_at"] == NOW
    assert quote["quote_time_kind"] == "server_response"
    assert quote["source"] == "gate" and quote["transport"] == "rest"
    assert quote["candles"] == []
    assert quote["interval_ms"] == 1000


@pytest.mark.parametrize("field,value", [("bid_price", "0"), ("ask_price", "-1"),
                                        ("last_price", "NaN"), ("last_price", None),
                                        ("bid_price", "Infinity"), ("ask_price", "4397.77")])
def test_invalid_or_crossed_quotes_are_rejected(field, value):
    payload = copy.deepcopy(TICKER)
    payload["data"][field] = value
    with pytest.raises(ValueError):
        normalize_ticker("XAUUSD", payload, NOW)


def test_real_spec_does_not_treat_minimum_as_step_or_convert_non_usd():
    detail = {**DETAIL_ROW, "settlement_currency": "JPY", "leverage": "25", "price_precision": 3}
    spec = normalize_spec(detail, SYMBOL_ROW, NOW)
    assert spec["volume_min"] == "0.01"
    assert spec["volume_max"] == "100"
    assert spec["volume_step"] is None
    assert spec["tick_size"] == "0.001"
    assert spec["leverage"] == "25"
    assert spec["contract_size"] == "100"
    assert spec["currency"] == spec["settlement_currency"] == "JPY"
    assert "estimated_margin" not in spec


def test_candles_are_sorted_exact_and_no_synthetic_candle_is_added():
    rows = normalize_candles(CANDLES)
    assert len(rows) == 2
    assert rows[0]["time"] == 1_788_764_940
    assert rows[1]["close"] == "4397.66"
    assert rows[1]["high"] == "4400.50"
    assert normalize_candles({"data": {"list": []}}) == []


def test_bad_candle_does_not_silently_replace_actual_history():
    invalid = copy.deepcopy(CANDLES)
    invalid["data"]["list"][0]["h"] = "1"
    with pytest.raises(ValueError):
        normalize_candles(invalid)


def test_ensure_concurrent_readers_share_one_ticker_history_and_detail_request():
    async def scenario():
        gate = FakeGate()
        market = LiveMarket(gate, clock=lambda: NOW)
        values = await asyncio.gather(*(market.ensure("XAUUSD", detail=True) for _ in range(8)))
        assert gate.calls == ["symbols", "XAUUSD", "XAUUSD:klines", "XAUUSD:detail"]
        assert all(value["source"] == "gate" for value in values)
        assert market.spec("XAUUSD")["volume_step"] is None
        assert market.symbols[0]["leverages"] == ["20", "500"]
        assert "contract_size" not in market.symbols[0]

    run(scenario())


def test_missing_credentials_leave_public_quotes_usable_and_specs_unknown():
    async def scenario():
        gate = FakeGate(credentials=False)
        market = LiveMarket(gate, clock=lambda: NOW)
        quote = await market.ensure("XAUUSD", detail=True)
        assert quote["stale"] is False
        assert market.spec("XAUUSD") is None
        assert "XAUUSD:detail" in market.errors
        assert "XAUUSD:detail" not in gate.calls

    run(scenario())


def test_failure_keeps_last_received_at_and_marks_old_quote_stale():
    async def scenario():
        gate, clock = FakeGate(), [NOW]
        market = LiveMarket(gate, clock=lambda: clock[0])
        first = await market.ensure("XAUUSD")
        gate.failures["XAUUSD"] = GateTransportError()
        clock[0] += 1
        await market.poll(["XAUUSD"])
        old = market.get("XAUUSD")
        assert old["received_at"] == first["received_at"]
        assert old["quote_timestamp"] == first["quote_timestamp"]
        assert old["stale"] is True
        assert old["last"] == first["last"]
        assert old["age_ms"] == 1000
        assert old["error"] is not None

    run(scenario())


def test_invalid_new_quote_cannot_overwrite_previous_freshness():
    async def scenario():
        gate, clock = FakeGate(), [NOW]
        market = LiveMarket(gate, clock=lambda: clock[0])
        await market.ensure("XAUUSD")
        gate.quote["data"]["bid_price"] = "0"
        clock[0] += 1
        await market.poll(["XAUUSD"])
        assert market.get("XAUUSD")["received_at"] == NOW
        assert market.get("XAUUSD")["stale"] is True

    run(scenario())


def test_staleness_is_dynamic_and_return_values_are_defensive_copies():
    async def scenario():
        gate, clock = FakeGate(), [NOW]
        market = LiveMarket(gate, clock=lambda: clock[0])
        await market.ensure("XAUUSD", detail=True)
        quote, spec = market.get("XAUUSD"), market.spec("XAUUSD")
        quote["candles"].clear()
        spec["leverage"] = "99999"
        assert len(market.get("XAUUSD")["candles"]) == 2
        assert market.spec("XAUUSD")["leverage"] == "500"
        clock[0] += 6
        assert market.get("XAUUSD")["stale"] is True
        clock[0] += 600
        assert market.spec("XAUUSD") is None

    run(scenario())


def test_poll_throttles_ticker_and_refreshes_history_less_often():
    async def scenario():
        gate, clock = FakeGate(), [NOW]
        market = LiveMarket(gate, clock=lambda: clock[0])
        await market.ensure("XAUUSD")
        for _ in range(4):
            await market.poll(["XAUUSD", "XAUUSD"])
        assert gate.calls.count("XAUUSD") == 1
        clock[0] += 1
        await market.poll(["XAUUSD"])
        assert gate.calls.count("XAUUSD") == 2
        assert gate.calls.count("XAUUSD:klines") == 1
        clock[0] += 30
        await market.poll(["XAUUSD"])
        assert gate.calls.count("XAUUSD:klines") == 2
        assert gate.calls.count("symbols") == 1

    run(scenario())


def test_rate_limit_cooldown_is_respected_by_cache_pollers():
    async def scenario():
        gate, clock = FakeGate(), [NOW]
        market = LiveMarket(gate, clock=lambda: clock[0])
        await market.ensure("XAUUSD")
        gate.failures["XAUUSD"] = GateError("rate_limit", "请求频率受限。", status_code=429, retry_after=90)
        clock[0] += 1
        await market.poll(["XAUUSD"])
        clock[0] += 89
        await market.poll(["XAUUSD"])
        assert gate.calls.count("XAUUSD") == 2
        del gate.failures["XAUUSD"]
        clock[0] += 1
        await market.poll(["XAUUSD"])
        assert gate.calls.count("XAUUSD") == 3
        assert market.get("XAUUSD")["stale"] is False

    run(scenario())


def test_candle_failure_does_not_invalidate_a_fresh_quote():
    async def scenario():
        gate = FakeGate()
        gate.failures["XAUUSD:klines"] = GateTransportError()
        market = LiveMarket(gate, clock=lambda: NOW)
        result = await market.ensure("XAUUSD")
        assert result["stale"] is False
        assert result["candles"] == []
        assert result["candles_error"]

    run(scenario())


def test_client_swap_clears_account_specific_specs_but_preserves_public_observations():
    async def scenario():
        gate = FakeGate()
        market = LiveMarket(gate, clock=lambda: NOW)
        await market.ensure("XAUUSD", detail=True)
        assert market.spec("XAUUSD")
        new_gate = FakeGate(credentials=False)
        market.set_client(new_gate)
        assert market.spec("XAUUSD") is None
        assert market.get("XAUUSD")["received_at"] == NOW
        await market.ensure("XAUUSD", detail=True)
        assert "XAUUSD:detail" not in new_gate.calls

    run(scenario())


def test_metadata_precision_change_invalidates_contract_details():
    async def scenario():
        gate, clock = FakeGate(), [NOW]
        market = LiveMarket(gate, clock=lambda: clock[0])
        await market.ensure("XAUUSD", detail=True)
        gate.symbol_rows[0]["price_precision"] = 3
        clock[0] += 301
        await market.refresh_symbols()
        assert market.spec("XAUUSD") is None

    run(scenario())


@pytest.mark.parametrize("interval", [0, 0.9, float("nan"), float("inf")])
def test_subsecond_or_invalid_poll_interval_is_rejected(interval):
    with pytest.raises(ValueError):
        LiveMarket(FakeGate(), interval=interval)
