from datetime import datetime, timezone
from pathlib import Path
import tomllib
import pandas as pd
import yfinance as yf
from rich.console import Console
from stock_management.db import get_connection, init_db

console = Console()

DEFAULT_US_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "us_stocks.toml"


def load_us_config(config_path: Path = DEFAULT_US_CONFIG_PATH) -> dict:
    if not config_path.exists():
        raise FileNotFoundError(f"設定ファイルが見つかりません: {config_path}")
    with config_path.open("rb") as f:
        return tomllib.load(f)


def load_us_universe(config_path: Path = DEFAULT_US_CONFIG_PATH) -> list[dict]:
    data = load_us_config(config_path)
    return data.get("stocks", [])


def sync_us_stocks(universe: list[dict]) -> None:
    now = datetime.now(timezone.utc)
    records = [
        (
            item["code"],
            "US",
            item.get("name", item["code"]),
            item.get("exchange", ""),
            None,
            None,
            item.get("sector", ""),
            item.get("sector", ""),
            item.get("category", "US Stock"),
            now,
        )
        for item in universe
    ]
    codes = [item["code"] for item in universe]
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
        # 設定ファイルから削除された米国株があればstocksテーブルから整理
        if codes:
            placeholders = ", ".join(["?"] * len(codes))
            con.execute(
                f"DELETE FROM stocks WHERE market = 'US' AND code NOT IN ({placeholders})",
                codes,
            )


def fetch_and_save_us_prices(
    config_path: Path = DEFAULT_US_CONFIG_PATH,
    period: str = "2y",
) -> int:
    init_db()
    universe = load_us_universe(config_path)
    if not universe:
        console.print(f"[yellow]No stocks defined in {config_path}[/yellow]")
        return 0

    sync_us_stocks(universe)

    tickers = [item["code"] for item in universe]
    console.print(
        f"[cyan]Fetching US stock data via yfinance ({len(tickers)} tickers from {config_path.name}, period={period})...[/cyan]"
    )

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
