from datetime import date, datetime, timedelta, timezone
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


def sync_us_stocks(universe: list[dict], partial: bool = False) -> None:
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
        # 全件同期モード（銘柄指定なし）のときのみ、設定ファイルから削除された銘柄を整理
        if not partial and codes:
            placeholders = ", ".join(["?"] * len(codes))
            con.execute(
                f"DELETE FROM stocks WHERE market = 'US' AND code NOT IN ({placeholders})",
                codes,
            )


def get_existing_max_dates(codes: list[str]) -> dict[str, date]:
    if not codes:
        return {}
    placeholders = ", ".join(["?"] * len(codes))
    with get_connection() as con:
        rows = con.execute(
            f"""
            SELECT code, MAX(date) AS max_date
            FROM daily_prices
            WHERE code IN ({placeholders})
            GROUP BY code
            """,
            codes,
        ).fetchall()
    return {r[0]: r[1] for r in rows if r[1] is not None}


def _extract_rows_from_yf_df(df: pd.DataFrame, tickers: list[str]) -> list[tuple]:
    rows = []
    if df is None or df.empty:
        return rows

    for code in tickers:
        if isinstance(df.columns, pd.MultiIndex):
            if code not in df.columns.get_level_values(0):
                continue
            sub = df[code]
        else:
            sub = df

        if "Close" not in sub.columns:
            continue
        sub = sub.dropna(subset=["Close"]).reset_index()
        for _, r in sub.iterrows():
            dt_col = "Date" if "Date" in r else sub.columns[0]
            dt = pd.to_datetime(r[dt_col]).date()
            open_p = float(r["Open"])
            high_p = float(r["High"])
            low_p = float(r["Low"])
            close_p = float(r["Close"])
            vol = int(r["Volume"]) if pd.notna(r["Volume"]) else 0
            turnover = close_p * vol
            rows.append((code, dt, open_p, high_p, low_p, close_p, close_p, vol, turnover))
    return rows


def fetch_and_save_us_prices(
    config_path: Path = DEFAULT_US_CONFIG_PATH,
    period: str = "2y",
    target_codes: list[str] | None = None,
    full_refresh: bool = False,
) -> int:
    """
    米国株の日足データを取得してDuckDBへ保存する。
    - target_codes が指定された場合は、その銘柄のみを取得する。
    - full_refresh=False（デフォルト）の場合、DuckDBに既存データがある銘柄は
      最終日付からの差分のみを取得し、データがない新規銘柄のみ initial period (2y等) で取得する。
    """
    init_db()
    all_universe = load_us_universe(config_path)

    if target_codes:
        req_set = {c.upper() for c in target_codes}
        universe = [item for item in all_universe if item["code"].upper() in req_set]
        # configに未登録のティッカーが直接指定された場合でも取得できるように補完
        known_codes = {item["code"].upper() for item in universe}
        for c in req_set - known_codes:
            universe.append(
                {
                    "code": c,
                    "name": c,
                    "exchange": "US",
                    "sector": "Unknown",
                    "category": "Ad-hoc US Stock",
                }
            )
        sync_us_stocks(universe, partial=True)
    else:
        universe = all_universe
        if not universe:
            console.print(f"[yellow]No stocks defined in {config_path}[/yellow]")
            return 0
        sync_us_stocks(universe, partial=False)

    tickers = [item["code"] for item in universe]
    max_dates = {} if full_refresh else get_existing_max_dates(tickers)

    new_tickers = [t for t in tickers if t not in max_dates]
    existing_tickers = [t for t in tickers if t in max_dates]

    all_rows: list[tuple] = []

    # 1. DBにデータがない新規銘柄（または --full 指定時）は period (例: 2y) で一括取得
    if new_tickers:
        console.print(
            f"[cyan]Fetching full history ({period}) for {len(new_tickers)} new ticker(s): {', '.join(new_tickers)}...[/cyan]"
        )
        df_new = yf.download(
            tickers=new_tickers,
            period=period,
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        all_rows.extend(_extract_rows_from_yf_df(df_new, new_tickers))

    # 2. 既存データがある銘柄は、最も古い最終日付の少し前（2日前）から差分のみ取得
    if existing_tickers:
        min_last_date = min(max_dates[t] for t in existing_tickers)
        # 直近営業日の修正やタイムゾーン差を取りこぼさないよう、最終日の2日前から取得して INSERT OR REPLACE
        start_date = (min_last_date - timedelta(days=2)).strftime("%Y-%m-%d")
        console.print(
            f"[cyan]Fetching incremental diff (from {start_date}) for {len(existing_tickers)} existing ticker(s): "
            f"{', '.join(existing_tickers)}...[/cyan]"
        )
        df_diff = yf.download(
            tickers=existing_tickers,
            start=start_date,
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        all_rows.extend(_extract_rows_from_yf_df(df_diff, existing_tickers))

    if not all_rows:
        console.print("[yellow]No new price rows fetched (already up to date).[/yellow]")
        return 0

    with get_connection() as con:
        con.executemany(
            """
            INSERT OR REPLACE INTO daily_prices (
                code, date, open, high, low, close, raw_close, volume, turnover_value
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            all_rows,
        )

    console.print(f"[green]Upserted {len(all_rows):,} daily price row(s) into DuckDB.[/green]")
    return len(all_rows)
