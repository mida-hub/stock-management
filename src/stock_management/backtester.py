import pandas as pd
from rich.console import Console
from stock_management.db import get_connection

console = Console(width=140)


MODEL_CONFIGS = {
    "A": {
        "name": "Model A: 王道押し目",
        "stop_pct": -6.0,
        "target_pct": 12.0,
        "max_hold": 30,
    },
    "B": {
        "name": "Model B: 出来高転換",
        "stop_pct": -6.0,
        "target_pct": 15.0,
        "max_hold": 25,
    },
    "C": {
        "name": "Model C: 堅実ディフェンシブ",
        "stop_pct": -5.0,
        "target_pct": 10.0,
        "max_hold": 40,
    },
}


def _check_signal(row: pd.Series, model: str, strict_trend: bool = False, refined_c: bool = False) -> bool:
    close = row["close"]
    open_p = row["open"]
    sma_200 = row["sma_200"]
    slope_75 = row["sma_75_slope_pct"]
    dev_25 = row["sma_25_dev_pct"]
    rsi = row["rsi_14"]
    vol_ratio = row["vol_ratio_25d"]
    atr_pct = row["atr_pct"]

    if pd.isna(close) or pd.isna(open_p) or pd.isna(dev_25) or pd.isna(rsi):
        return False

    is_bullish_candle = close > open_p
    if strict_trend:
        trend_up = (pd.notna(sma_200) and close >= sma_200) and (pd.notna(slope_75) and slope_75 > 0)
    else:
        trend_up = (pd.notna(sma_200) and close >= sma_200) or (pd.notna(slope_75) and slope_75 > 0)

    if model == "A":
        return bool(trend_up and (-5.0 <= dev_25 <= 2.0) and (35.0 <= rsi <= 55.0) and is_bullish_candle)
    if model == "B":
        return bool(
            pd.notna(vol_ratio)
            and vol_ratio >= 1.5
            and (0.0 <= dev_25 <= 5.0)
            and (40.0 <= rsi <= 65.0)
            and is_bullish_candle
        )
    if model == "C":
        dev_low = 0.0 if refined_c else -4.0
        return bool(
            pd.notna(sma_200)
            and close >= sma_200
            and (dev_low <= dev_25 <= 3.0)
            and (35.0 <= rsi <= 55.0)
            and (pd.notna(atr_pct) and atr_pct <= 3.0)
        )
    return False


def simulate_trades_on_df(
    df: pd.DataFrame,
    model: str,
    stop_mode: str = "fixed",
    cost_pct: float = 0.2,
    strict_trend: bool = False,
    allowed_dates: set | None = None,
    refined_c: bool = False,
) -> list[dict]:
    """1銘柄分の時系列DataFrameに対して指定モデルのバックテストを実行する（翌日始値エントリー）。"""
    cfg = MODEL_CONFIGS[model]
    stop_mult = 1.0 + cfg["stop_pct"] / 100.0
    target_mult = 1.0 + cfg["target_pct"] / 100.0
    max_hold = cfg["max_hold"]

    trades: list[dict] = []
    n = len(df)
    i = 0
    while i < n - 1:
        row = df.iloc[i]
        if allowed_dates is not None and row["date"] not in allowed_dates:
            i += 1
            continue
        if not _check_signal(row, model, strict_trend=strict_trend, refined_c=refined_c):
            i += 1
            continue

        entry_idx = i + 1
        entry_price = float(df.iloc[entry_idx]["open"])
        if entry_price <= 0:
            i += 1
            continue

        atr_val = row.get("atr_14")
        if stop_mode == "atr" and pd.notna(atr_val) and float(atr_val) > 0:
            stop_price = entry_price - 2.0 * float(atr_val)
            target_price = entry_price + 4.0 * float(atr_val)
        else:
            stop_price = entry_price * stop_mult
            target_price = entry_price * target_mult

        exit_idx = min(entry_idx + max_hold - 1, n - 1)
        exit_price = float(df.iloc[exit_idx]["close"])
        exit_reason = "TIME"

        for j in range(entry_idx, min(entry_idx + max_hold, n)):
            bar = df.iloc[j]
            if float(bar["low"]) <= stop_price:
                exit_idx = j
                exit_price = min(float(bar["open"]), stop_price)
                exit_reason = "STOP"
                break
            if float(bar["high"]) >= target_price:
                exit_idx = j
                exit_price = max(float(bar["open"]), target_price)
                exit_reason = "TARGET"
                break
            if (
                refined_c
                and model == "C"
                and pd.notna(bar.get("sma_25"))
                and pd.notna(bar.get("sma_75"))
                and float(bar["sma_25"]) < float(bar["sma_75"])
            ):
                exit_idx = j
                exit_price = float(bar["close"])
                exit_reason = "DC"
                break

        ret_pct = ((exit_price - entry_price) / entry_price * 100.0) - cost_pct
        trades.append(
            {
                "code": row["code"],
                "entry_date": df.iloc[entry_idx]["date"],
                "exit_date": df.iloc[exit_idx]["date"],
                "entry_price": entry_price,
                "exit_price": exit_price,
                "return_pct": ret_pct,
                "hold_days": exit_idx - entry_idx + 1,
                "exit_reason": exit_reason,
            }
        )
        i = exit_idx + 1

    return trades


def run_backtest(
    model: str = "all",
    market: str = "JP",
    days: int = 365,
    stop_mode: str = "fixed",
    strict_trend: bool = False,
    regime_min_pct: float = 0.0,
    refined_c: bool = False,
) -> pd.DataFrame:
    """指定市場・期間の過去データでモデルA/B/Cのバックテストを実行し、サマリーを返す。"""
    query = f"""
    SELECT
        code,
        date,
        open,
        high,
        low,
        close,
        sma_25,
        sma_75,
        sma_25_dev_pct,
        sma_75_slope_pct,
        sma_200,
        rsi_14,
        vol_ratio_25d
    FROM v_technical_indicators
    WHERE market = ?
      AND date >= CURRENT_DATE - INTERVAL '{int(days)} days'
    ORDER BY code, date ASC;
    """
    with get_connection() as con:
        df = con.execute(query, [market]).df()

    if df.empty:
        return pd.DataFrame()

    # ATR(14)% を銘柄ごとに計算
    df["prev_close"] = df.groupby("code")["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - df["prev_close"]).abs(),
            (df["low"] - df["prev_close"]).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["atr_14"] = tr.groupby(df["code"]).transform(lambda s: s.rolling(14).mean())
    df["atr_pct"] = (df["atr_14"] / df["close"]) * 100.0

    allowed_dates: set | None = None
    if regime_min_pct > 0.0:
        daily_breadth = df.groupby("date")["sma_25_dev_pct"].apply(lambda s: (s >= 0).mean() * 100.0)
        allowed_dates = set(daily_breadth[daily_breadth >= regime_min_pct].index)

    models_to_run = ["A", "B", "C"] if model == "all" else [model.upper()]
    summary_rows = []

    for m in models_to_run:
        all_trades: list[dict] = []
        for _, grp in df.groupby("code", sort=False):
            all_trades.extend(
                simulate_trades_on_df(
                    grp.reset_index(drop=True),
                    m,
                    stop_mode=stop_mode,
                    strict_trend=strict_trend,
                    allowed_dates=allowed_dates,
                    refined_c=refined_c,
                )
            )

        if not all_trades:
            summary_rows.append(
                {
                    "Model": MODEL_CONFIGS[m]["name"],
                    "Trades": 0,
                    "Win%": "-",
                    "AvgRet%": "-",
                    "PF": "-",
                    "AvgHold": "-",
                    "TotalRet%": "-",
                }
            )
            continue

        tdf = pd.DataFrame(all_trades)
        wins = tdf[tdf["return_pct"] > 0]
        losses = tdf[tdf["return_pct"] <= 0]
        win_rate = len(wins) / len(tdf) * 100.0
        avg_ret = tdf["return_pct"].mean()
        gross_profit = wins["return_pct"].sum()
        gross_loss = abs(losses["return_pct"].sum())
        pf = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
        avg_hold = tdf["hold_days"].mean()
        total_ret = tdf["return_pct"].sum()

        summary_rows.append(
            {
                "Model": MODEL_CONFIGS[m]["name"],
                "Trades": len(tdf),
                "Win%": f"{win_rate:.1f}%",
                "AvgRet%": f"{avg_ret:+.2f}%",
                "PF": f"{pf:.2f}",
                "AvgHold": f"{avg_hold:.1f}d",
                "TotalRet%": f"{total_ret:+.1f}%",
            }
        )

    return pd.DataFrame(summary_rows)


def print_backtest_summary(
    model: str = "all",
    market: str = "JP",
    days: int = 365,
    stop_mode: str = "fixed",
    strict_trend: bool = False,
    regime_min_pct: float = 0.0,
) -> None:
    res = run_backtest(
        model=model,
        market=market,
        days=days,
        stop_mode=stop_mode,
        strict_trend=strict_trend,
        regime_min_pct=regime_min_pct,
    )
    if res.empty:
        console.print(f"[yellow]No backtest data found for market={market}.[/yellow]")
        return
    console.print(
        f"\n[bold cyan]=== Backtest Summary ({market} / Past {days} days / stop={stop_mode} / cost=0.2%) ===[/bold cyan]"
    )
    print(res.to_string(index=False))
