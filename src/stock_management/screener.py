import pandas as pd
from rich.console import Console
from stock_management.db import get_connection
from stock_management.ingest_jp import DEFAULT_JP_CONFIG_PATH, load_jp_config
from stock_management.ingest_us import DEFAULT_US_CONFIG_PATH, load_us_config

console = Console(width=140)


def get_latest_technical_snapshot(market: str | None = None) -> pd.DataFrame:
    query = f"""
    WITH ranked AS (
        SELECT
            *,
            ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn
        FROM v_technical_indicators
        WHERE market IS NOT NULL
        {"AND market = '" + market + "'" if market else ""}
    )
    SELECT
        code,
        market,
        name,
        sector_33_name AS sector,
        date,
        ROUND(close, 2) AS close,
        ROUND(COALESCE(raw_close, close), 2) AS raw_close,
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


def _classify_lot_type(row: pd.Series, max_unit_price_jpy: float, max_skabu_price_jpy: float) -> str:
    if row["market"] != "JP":
        return "米国(1株~)"
    price = float(row["raw_close"])
    if price <= max_unit_price_jpy:
        return "単元(100株)"
    if price <= max_skabu_price_jpy:
        return "S株(未満株)"
    return "除外(値嵩)"


def screen_core_swing_candidates(market: str | None = None) -> pd.DataFrame:
    """
    コア戦略（大型主力スイング）向けのスクリーニング:
    - 中長期トレンド: close >= sma_200 または sma_75_slope_pct > 0
    - 押し目・反発圏: 25日移動平均線乖離率とRSI(14)が設定範囲内
    - 日本株の購入単価・単位ルール:
      - 1単元50万円以下（株価5,000円以下） -> 単元(100株)
      - 株価5,001円〜25,000円以下 -> S株(未満株)
      - 株価25,000円超 -> 除外(値嵩) として候補から外す
    """
    df = get_latest_technical_snapshot(market=market)
    if df.empty:
        return df

    us_cfg = load_us_config(DEFAULT_US_CONFIG_PATH).get("screening", {})
    jp_cfg = load_jp_config(DEFAULT_JP_CONFIG_PATH).get("screening", {}) if DEFAULT_JP_CONFIG_PATH.exists() else {}

    cfg = jp_cfg if market == "JP" else us_cfg
    dev_min = float(cfg.get("sma_25_dev_min", -6.0))
    dev_max = float(cfg.get("sma_25_dev_max", 5.0))
    rsi_min = float(cfg.get("rsi_14_min", 30.0))
    rsi_max = float(cfg.get("rsi_14_max", 65.0))

    max_unit_price_jpy = float(jp_cfg.get("max_unit_price_jpy", 5000.0))
    max_skabu_price_jpy = float(jp_cfg.get("max_skabu_price_jpy", 25000.0))

    df["lot_type"] = df.apply(
        _classify_lot_type,
        axis=1,
        max_unit_price_jpy=max_unit_price_jpy,
        max_skabu_price_jpy=max_skabu_price_jpy,
    )

    cond_trend = (df["close"] >= df["sma_200"]) | (df["sma_75_slope_pct"] > 0)
    cond_pullback = (df["sma_25_dev_pct"].between(dev_min, dev_max)) & (df["rsi_14"].between(rsi_min, rsi_max))
    cond_affordable = df["lot_type"] != "除外(値嵩)"

    df["is_candidate"] = cond_trend & cond_pullback & cond_affordable
    return df


def print_screening_table(df: pd.DataFrame, title: str = "Technical Screening Snapshot") -> None:
    if df.empty:
        console.print("[yellow]No data found in database.[/yellow]")
        return

    console.print(f"\n[bold cyan]=== {title} ===[/bold cyan]")
    display_df = pd.DataFrame(
        {
            "Code": df["code"],
            "Name": df["name"].astype(str).str.slice(0, 10),
            "Date": df["date"].astype(str).str[:10],
            "Close": df["close"].map(lambda x: f"{x:.2f}"),
            "購入区分": df["lot_type"],
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


def get_latest_sector_momentum(market: str = "JP") -> pd.DataFrame:
    query = """
    WITH latest_dt AS (
        SELECT MAX(date) AS max_date
        FROM v_sector_momentum
        WHERE market = ?
    )
    SELECT
        sector_33_code AS code,
        sector_33_name AS sector,
        date,
        stock_count,
        return_5d_pct,
        return_20d_pct,
        sector_turnover_ratio
    FROM v_sector_momentum
    WHERE market = ?
      AND date = (SELECT max_date FROM latest_dt)
    ORDER BY return_5d_pct DESC, sector_turnover_ratio DESC;
    """
    with get_connection() as con:
        return con.execute(query, [market, market]).df()


def print_sector_momentum_table(market: str = "JP") -> None:
    df = get_latest_sector_momentum(market=market)
    if df.empty:
        console.print(f"[yellow]No sector momentum data found for market={market}.[/yellow]")
        return

    console.print(f"\n[bold cyan]=== Sector Momentum & Inflow Ranking ({market}) ===[/bold cyan]")
    display_df = pd.DataFrame(
        {
            "Sector": df["sector"],
            "Date": df["date"].astype(str).str[:10],
            "Count": df["stock_count"],
            "5d_Ret%": df["return_5d_pct"].map(lambda x: f"{x:+.2f}%" if pd.notna(x) else "-"),
            "20d_Ret%": df["return_20d_pct"].map(lambda x: f"{x:+.2f}%" if pd.notna(x) else "-"),
            "Turnover_Ratio(5d/25d)": df["sector_turnover_ratio"].map(lambda x: f"{x:.2f}x" if pd.notna(x) else "-"),
        }
    )
    print(display_df.to_string(index=False))
