export type GridConfig = {
  symbol: string; direction: 'long' | 'short'; lower_price: string; upper_price: string;
  volume: string; grid_count: number; spacing: 'arithmetic' | 'geometric'; repeat: boolean; stop_loss?: string | null;
}
export type SymbolSpec = { symbol: string; name: string; tick_size?: string | null; volume_min?: string | null; volume_max?: string | null; volume_step?: string | null; contract_size?: string | null; leverage?: number | string | null; price_precision?: number | null; currency?: string | null; settlement_currency?: string | null }
export type Candle = { time: string | number; open: string; high: string; low: string; close: string }
export type Market = { symbol: string; last: string | null; bid: string | null; ask: string | null; change: string | null; high: string | null; low: string | null; updated_at?: string | number; candles: Candle[]; source: string; received_at: number; quote_timestamp?: number | null; transport?: 'rest'; interval_ms?: number; stale?: boolean; status?: string; trade_mode?: string | number }
export type MarketEvent = { market: Market | null; spec?: SymbolSpec | null; symbols?: SymbolSpec[]; error?: string | null }
export type ExecutionMode = 'native_trigger' | 'legacy_local' | 'legacy' | 'legacy_paused'
export type TradeRow = { id: string; strategy_id?: string | null; symbol: string; grid_index?: number; side?: string; direction?: string; price?: string | null; entry_price?: string | null; take_profit?: string | null; stop_loss?: string | null; volume?: string | null; status?: string; unrealized_pnl?: string | null; pnl?: string | null; time?: string | number; type?: string; fee?: string; source?: string; managed?: boolean; order_id?: string | number; position_id?: string | number; real_order_id?: string | number; remote_order_id?: string | number; remote_state?: string | number; execution_mode?: ExecutionMode; exit_confirmed?: boolean; exit_order_id?: string | number | null; exit_source?: string | null; exit_reason?: string | null }
export type Strategy = { id: string; config: GridConfig; status: string; created_at: string | number; stop_reason?: string; execution_mode?: ExecutionMode; pending_count?: number; waiting_count?: number; position_count?: number; submitted_count?: number; initial_order_count?: number; placed_count?: number; planned_count?: number; placement?: { eligible: number; total: number; submitted: number; confirmed: number; waiting: number; remaining: number }; error?: string; notice?: string; recoverable?: boolean; resume_blockers?: string[]; resume_blocker_codes?: string[] }
export type Connection = { configured: boolean; connected: boolean; checked_at: number | null; error: string | null; live_trading_ready: boolean; remembered?: boolean; credential_saved?: boolean; encrypted_storage_available?: boolean; account?: { status?: number | string; leverage?: number; equity?: string; free_margin?: string } }
export type Snapshot = {
  mode: 'live'; execution_mode?: ExecutionMode; symbols: SymbolSpec[]; spec: SymbolSpec | null; market: Market | null;
  account: { balance: string | null; equity: string | null; margin: string | null; free_margin: string | null; unrealized_pnl: string | null; realized_pnl: string | null; reserved_margin: string | null; available_for_new_strategy: string | null; stale?: boolean; updated_at?: string | number | null } | null;
  strategies: Strategy[]; orders: TradeRow[]; positions: TradeRow[]; fills: TradeRow[];
  logs: { id: string; time: string | number; level: string; message: string }[];
  connection?: Connection; engine_error?: string | null; feed_error?: string | null;
  operation?: { status: string; error?: string | null; result?: string | null };
  unresolved_operations?: { id: string; strategy_id: string; kind: string; resource_id: string; status: string; error?: string | null }[];
}
export type GridCell = { index: number; entry_price: string; take_profit: string; eligible: boolean; gross_profit?: string | null }
export type Preview = {
  execution_mode?: ExecutionMode;
  can_start: boolean; sufficient_margin: boolean; estimated_max_margin?: string;
  levels: string[]; cells: GridCell[]; active_count?: number; total_volume?: string; notional?: string;
  estimated_margin?: string | null; estimated_profit_per_grid?: string | null; profit_per_grid_min?: string | null; profit_per_grid_max?: string | null;
  fee_estimate?: string; warnings: string[];
  blockers?: string[]; blocker_codes?: string[];
  account?: { stale?: boolean; updated_at?: string | number | null } | null;
  [key: string]: unknown;
}
export type Template = { id: string; name: string; config: GridConfig }
