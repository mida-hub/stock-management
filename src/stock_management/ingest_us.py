from datetime import datetime, timezone
import pandas as pd
import yfinance as yf
from rich.console import Console
from stock_management.db import get_connection, init_db

console = Console()

# 米国主要テック・主力大型監視ユニバース（コア戦略＋テーマ監視用）
DEFAULT_US_UNIVERSE = [
    # Mega Tech (Magnificent 7)
    {"code": "AAPL", "name": "Apple Inc.", "exchange": "NASDAQ", "sector": "Technology", "category": "US Mega Tech"},
    {"code": "MSFT", "name": "Microsoft Corporation", "exchange": "NASDAQ", "sector": "Technology", "category": "US Mega Tech"},
    {"code": "NVDA", "name": "NVIDIA Corporation", "exchange": "NASDAQ", "sector": "Semiconductors", "category": "US Mega Tech"},
    {"code": "AMZN", "name": "Amazon.com Inc.", "exchange": "NASDAQ", "sector": "Consumer Cyclical", "category": "US Mega Tech"},
    {"code": "GOOGL", "name": "Alphabet Inc.", "exchange": "NASDAQ", "sector": "Communication Services", "category": "US Mega Tech"},
    {"code": "META", "name": "Meta Platforms Inc.", "exchange": "NASDAQ", "sector": "Communication Services", "category": "US Mega Tech"},
    {"code": "TSLA", "name": "Tesla Inc.", "exchange": "NASDAQ", "sector": "Consumer Cyclical", "category": "US Mega Tech"},
    # Semiconductors & AI Hardware
    {"code": "AVGO", "name": "Broadcom Inc.", "exchange": "NASDAQ", "sector": "Semiconductors", "category": "US Semiconductors"},
    {"code": "AMD", "name": "Advanced Micro Devices", "exchange": "NASDAQ", "sector": "Semiconductors", "category": "US Semiconductors"},
    {"code": "TSM", "name": "Taiwan Semiconductor", "exchange": "NYSE", "sector": "Semiconductors", "category": "US Semiconductors"},
    {"code": "ARM", "name": "Arm Holdings plc", "exchange": "NASDAQ", "sector": "Semiconductors", "category": "US Semiconductors"},
    {"code": "MU", "name": "Micron Technology", "exchange": "NASDAQ", "sector": "Semiconductors", "category": "US Semiconductors"},
    # Enterprise Software, Cloud & Cybersecurity
    {"code": "CRM", "name": "Salesforce Inc.", "exchange": "NYSE", "sector": "Software", "category": "US Cloud/AI"},
    {"code": "ORCL", "name": "Oracle Corporation", "exchange": "NYSE", "sector": "Software", "category": "US Cloud/AI"},
    {"code": "NOW", "name": "ServiceNow Inc.", "exchange": "NYSE", "sector": "Software", "category": "US Cloud/AI"},
    {"code": "SNOW", "name": "Snowflake Inc.", "exchange": "NYSE", "sector": "Software", "category": "US Cloud/AI"},
    {"code": "PLTR", "name": "Palantir Technologies", "exchange": "NASDAQ", "sector": "Software", "category": "US Cloud/AI"},
    {"code": "PANW", "name": "Palo Alto Networks", "exchange": "NASDAQ", "sector": "Cybersecurity", "category": "US Cloud/AI"},
    {"code": "CRWD", "name": "CrowdStrike Holdings", "exchange": "NASDAQ", "sector": "Cybersecurity", "category": "US Cloud/AI"},
    # Digital Media, FinTech & Healthcare Blue-Chips
    {"code": "NFLX", "name": "Netflix Inc.", "exchange": "NASDAQ", "sector": "Communication Services", "category": "US Large Growth"},
    {"code": "V", "name": "Visa Inc.", "exchange": "NYSE", "sector": "Financial Services", "category": "US Quality Value"},
    {"code": "JPM", "name": "JPMorgan Chase & Co.", "exchange": "NYSE", "sector": "Financial Services", "category": "US Quality Value"},
    {"code": "LLY", "name": "Eli Lilly and Company", "exchange": "NYSE", "sector": "Healthcare", "category": "US Quality Growth"},
    {"code": "COST", "name": "Costco Wholesale", "exchange": "NASDAQ", "sector": "Consumer Defensive", "category": "US Quality Growth"},
]


def upsert_us_stocks(universe: list[dict] = DEFAULT_US_UNIVERSE) -> None:
    now = datetime.now(timezone.utc)
    records = [
        (
            item["code"],
            "US",
            item["name"],
            item["exchange"],
            None,
            None,
            item["sector"],
            item["sector"],
            item["category"],
            now,
        )
        for item in universe
    ]
    with get_connection() as con:
        con.executemany(
            """
            INSERT OR REPLACE INTO stocks (
                code, market, name, exchange_division,
                sector_17_code, sector_17_name,
                sector_33_code, sector_33_name,
                scale_category, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            records,
        )


def fetch_and_save_us_prices(
    universe: list[dict] = DEFAULT_US_UNIVERSE,
    period: str = "2y",
) -> int:
    init_db()
    upsert_us_stocks(universe)

    tickers = [item["code"] for item in universe]
    console.print(f"[cyan]Fetching US stock data via yfinance ({len(tickers)} tickers, period={period})...[/cyan]")

    # auto_adjust=True で分割・配当調整後の四本値を取得
    df = yf.download(
        tickers=tickers,
        period=period,
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=True,
    )

    rows = []
    for code in tickers:
        sub = df[code] if len(tickers) > 1 else df
        sub = sub.dropna(subset=["Close"]).reset_index()
        for _, r in sub.iterrows():
            dt = pd.to_datetime(r["Date"]).date()
            open_p = float(r["Open"])
            high_p = float(r["High"])
            low_p = float(r["Low"])
            close_p = float(r["Close"])
            vol = int(r["Volume"]) if pd.notna(r["Volume"]) else 0
            turnover = close_p * vol
            rows.append((code, dt, open_p, high_p, low_p, close_p, close_p, vol, turnover))

    if not rows:
        console.print("[yellow]No price rows fetched.[/yellow]")
        return 0

    with get_connection() as con:
        con.executemany(
            """
            INSERT OR REPLACE INTO daily_prices (
                code, date, open, high, low, close, raw_close, volume, turnover_value
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    console.print(f"[green]Saved {len(rows):,} daily price rows into DuckDB.[/green]")
    return len(rows)
