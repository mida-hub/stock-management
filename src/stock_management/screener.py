import pandas as pd
from rich.console import Console
from rich.table import Table
from stock_management.db import get_connection

console = Console(width=140)


def get_latest_technical_snapshot(market: str | None = None) -> pd.DataFrame:
    where_clause = f"WHERE market = '{market}'" if market else ""
    query = f"""
    WITH ranked AS (
        SELECT
            *,
            ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn
        FROM v_technical_indicators
        {where_clause}
    )
    SELECT
        code,
        market,
        name,
        sector_33_name AS sector,
        date,
        ROUND(close, 2) AS close,
        sma_25,
        sma_75,
        sma_200,
        sma_25_dev_pct,
        sma_75_dev_pct,
        sma_75_slope_pct,
        sma_200_slope_pct,
        rsi_14,
        vol_ratio_25d,
        drawdown_60d_pct,
        recent_low_20d
    FROM ranked
    WHERE rn = 1
    ORDER BY market, code;
    """
    with get_connection() as con:
        return con.execute(query).df()


def screen_core_swing_candidates(market: str | None = None) -> pd.DataFrame:
    """
    コア戦略（大型主力スイング）向けのスクリーニング:
    - 中長期トレンド: close >= sma_200 (または sma_75 が上向き)
    - 押し目・反発圏: 25日移動平均線からの乖離が -5% 〜 +5% 以内、または RSI(14) が 35 〜 60
    """
    df = get_latest_technical_snapshot(market=market)
    if df.empty:
        return df

    cond_trend = (df["close"] >= df["sma_200"]) | (df["sma_75_slope_pct"] > 0)
    cond_pullback = (df["sma_25_dev_pct"].between(-6.0, 5.0)) & (df["rsi_14"].between(30.0, 65.0))
    df["is_candidate"] = cond_trend & cond_pullback
    return df


def print_screening_table(df: pd.DataFrame, title: str = "Technical Screening Snapshot") -> None:
    if df.empty:
        console.print("[yellow]No data found in database.[/yellow]")
        return

    console.print(f"\n[bold cyan]=== {title} ===[/bold cyan]")
    display_df = pd.DataFrame(
        {
            "Code": df["code"],
            "Date": df["date"].astype(str).str[:10],
            "Close": df["close"].map(lambda x: f"{x:.2f}"),
            "25d_Dev%": df["sma_25_dev_pct"].map(lambda x: f"{x:+.2f}%"),
            "75d_Slope%": df["sma_75_slope_pct"].map(lambda x: f"{x:+.2f}%"),
            "200d_SMA": df["sma_200"].map(lambda x: f"{x:.2f}" if pd.notna(x) else "-"),
            "RSI(14)": df["rsi_14"].map(lambda x: f"{x:.1f}" if pd.notna(x) else "-"),
            "60d_DD%": df["drawdown_60d_pct"].map(lambda x: f"{x:+.2f}%"),
            "20d_Low": df["recent_low_20d"].map(lambda x: f"{x:.2f}"),
            "Swing判定": df["is_candidate"].map(lambda x: "★候補" if x else "-"),
        }
    )
    print(display_df.to_string(index=False))
