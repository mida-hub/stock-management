from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path
import time
import tomllib
from dotenv import load_dotenv
import pandas as pd
import requests
from rich.console import Console
from stock_management.db import get_connection, init_db
from stock_management.ingest_us import get_existing_max_dates

console = Console()

JQUANTS_BASE_URL = "https://api.jquants.com/v2"
DEFAULT_JP_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "jp_stocks.toml"


def normalize_jp_code(code: str) -> str:
    """J-Quantsの5桁コード(末尾0)を一般的な4桁コードに正規化する"""
    c = str(code).strip().upper()
    if len(c) == 5 and c.endswith("0"):
        return c[:4]
    return c


def to_jquants_code(code: str) -> str:
    """4桁コードをJ-Quants API用の5桁コード(末尾0)に変換する"""
    c = str(code).strip().upper()
    if len(c) == 4:
        return c + "0"
    return c


def get_jquants_headers() -> dict[str, str]:
    load_dotenv()
    api_key = os.getenv("JQUANTS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "JQUANTS_API_KEY が設定されていません。.env ファイルに JQUANTS_API_KEY を設定してください。"
        )
    return {"x-api-key": api_key}


def _jquants_get_all(endpoint: str, params: dict | None = None, headers: dict[str, str] | None = None) -> list[dict]:
    if headers is None:
        headers = get_jquants_headers()
    url = f"{JQUANTS_BASE_URL}{endpoint}"
    req_params = dict(params) if params else {}
    results: list[dict] = []

    while True:
        resp = requests.get(url, headers=headers, params=req_params, timeout=30)
        if resp.status_code == 429:
            console.print("[yellow]Rate limit (429) hit. Waiting 10s...[/yellow]")
            time.sleep(10)
            continue
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data", [])
        if isinstance(data, list):
            results.extend(data)
        pagination_key = payload.get("pagination_key")
        if not pagination_key:
            break
        req_params["pagination_key"] = pagination_key
        time.sleep(1.05)

    return results


def load_jp_config(config_path: Path = DEFAULT_JP_CONFIG_PATH) -> dict:
    if not config_path.exists():
        raise FileNotFoundError(f"設定ファイルが見つかりません: {config_path}")
    with config_path.open("rb") as f:
        return tomllib.load(f)


def sync_jp_master(
    config_path: Path = DEFAULT_JP_CONFIG_PATH,
    target_codes: list[str] | None = None,
) -> list[str]:
    """
    J-Quants /v2/equities/master から上場銘柄一覧を取得し、
    config/jp_stocks.toml の条件（または target_codes）に合致する銘柄を DuckDB stocks に同期する。
    戻り値は対象となった4桁銘柄コードのリスト。
    """
    cfg = load_jp_config(config_path).get("universe", {})
    allowed_markets = set(cfg.get("markets", ["プライム"]))
    allowed_scales = set(cfg.get("scale_categories", ["TOPIX Core30", "TOPIX Large70"]))
    extra_codes = {normalize_jp_code(c) for c in cfg.get("extra_codes", [])}

    target_set = {normalize_jp_code(c) for c in target_codes} if target_codes else None

    # 少数銘柄指定時は code パラメータで対象銘柄のマスターのみ軽量取得
    if target_set is not None and len(target_set) <= 5:
        master_rows = []
        for c in target_set:
            master_rows.extend(_jquants_get_all("/equities/master", params={"code": to_jquants_code(c)}))
    else:
        master_rows = _jquants_get_all("/equities/master")

    now = datetime.now(timezone.utc)

    records = []
    selected_codes = []

    for m in master_rows:
        code4 = normalize_jp_code(m.get("Code", ""))
        mkt_nm = m.get("MktNm", "")
        scale_cat = m.get("ScaleCat", "")

        if target_set is not None:
            if code4 not in target_set:
                continue
        else:
            in_scale = (mkt_nm in allowed_markets) and (scale_cat in allowed_scales)
            in_extra = code4 in extra_codes
            if not (in_scale or in_extra):
                continue

        selected_codes.append(code4)
        records.append(
            (
                code4,
                "JP",
                m.get("CoName", code4),
                mkt_nm,
                m.get("S17", None),
                m.get("S17Nm", None),
                m.get("S33", None),
                m.get("S33Nm", None),
                scale_cat if scale_cat != "-" else None,
                now,
            )
        )

    with get_connection() as con:
        if records:
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
        if target_set is None and selected_codes:
            placeholders = ", ".join(["?"] * len(selected_codes))
            con.execute(
                f"DELETE FROM stocks WHERE market = 'JP' AND code NOT IN ({placeholders})",
                selected_codes,
            )

    return sorted(set(selected_codes))


def _parse_daily_bar_rows(raw_bars: list[dict], allowed_codes: set[str] | None = None) -> list[tuple]:
    rows = []
    for b in raw_bars:
        code4 = normalize_jp_code(b.get("Code", ""))
        if allowed_codes is not None and code4 not in allowed_codes:
            continue
        adj_c = b.get("AdjC")
        raw_c = b.get("C")
        if adj_c is None and raw_c is None:
            continue
        dt_str = b.get("Date")
        if not dt_str:
            continue
        dt = pd.to_datetime(dt_str).date()
        open_p = float(b["AdjO"]) if b.get("AdjO") is not None else float(b.get("O") or 0.0)
        high_p = float(b["AdjH"]) if b.get("AdjH") is not None else float(b.get("H") or 0.0)
        low_p = float(b["AdjL"]) if b.get("AdjL") is not None else float(b.get("L") or 0.0)
        close_p = float(adj_c) if adj_c is not None else float(raw_c)
        raw_close_p = float(raw_c) if raw_c is not None else close_p
        vol = int(float(b.get("AdjVo") or b.get("Vo") or 0))
        turnover = float(b.get("Va") or (raw_close_p * vol))
        rows.append((code4, dt, open_p, high_p, low_p, close_p, raw_close_p, vol, turnover))
    return rows


def fetch_and_save_jp_financials(codes: list[str]) -> int:
    """指定された日本株銘柄リストの財務サマリー (/v2/fins/summary) を取得して保存する"""
    headers = get_jquants_headers()
    fin_rows = []

    def _to_float(val) -> float | None:
        if val is None or val == "":
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    console.print(f"[cyan]Fetching financial summaries for {len(codes)} JP stock(s)...[/cyan]")
    for idx, code4 in enumerate(codes, 1):
        raw_fins = _jquants_get_all("/fins/summary", params={"code": to_jquants_code(code4)}, headers=headers)
        for f in raw_fins:
            disc_date_str = f.get("DiscDate")
            quarter = f.get("CurPerType") or "Unknown"
            if not disc_date_str:
                continue
            disc_date = pd.to_datetime(disc_date_str).date()
            fy_end = f.get("CurFYEn", "")
            fin_rows.append(
                (
                    code4,
                    disc_date,
                    fy_end[:4] if fy_end else None,
                    quarter,
                    _to_float(f.get("Sales")),
                    _to_float(f.get("OP")),
                    _to_float(f.get("OdP")),
                    _to_float(f.get("NP")),
                    _to_float(f.get("EPS")),
                    _to_float(f.get("BPS")),
                    _to_float(f.get("FEPS")),
                    _to_float(f.get("FDivAnn")),
                )
            )
        if idx < len(codes):
            time.sleep(1.05)

    if not fin_rows:
        return 0

    with get_connection() as con:
        con.executemany(
            """
            INSERT OR REPLACE INTO financial_summaries (
                code, disclosed_date, fiscal_year, fiscal_quarter,
                net_sales, operating_profit, ordinary_profit, net_income,
                eps, bps, forecast_eps, forecast_dividend
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            fin_rows,
        )
    console.print(f"[green]Upserted {len(fin_rows):,} financial summary row(s) into DuckDB.[/green]")
    return len(fin_rows)


def fetch_and_save_jp_prices(
    config_path: Path = DEFAULT_JP_CONFIG_PATH,
    target_codes: list[str] | None = None,
    full_refresh: bool = False,
    with_fins: bool = False,
) -> int:
    """
    J-Quants API v2 から日本株の日足データを取得し DuckDB へ保存する。
    - DBに未登録の新規銘柄は code 指定で過去 initial_lookback_days (デフォルト730日) を取得
    - DBに登録済みの銘柄は:
      - 少数銘柄指定時または欠損日数が多い場合: code 指定で最終日以降を取得
      - 全体更新かつ欠損日数が少ない場合 (14日以内): date 指定で営業日ごとに全銘柄一括取得（数秒で完了）
    """
    init_db()
    headers = get_jquants_headers()
    cfg = load_jp_config(config_path).get("universe", {})
    lookback_days = int(cfg.get("initial_lookback_days", 730))
    default_from_str = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    codes = sync_jp_master(config_path=config_path, target_codes=target_codes)
    if not codes:
        console.print("[yellow]対象となる日本株銘柄が見つかりませんでした。[/yellow]")
        return 0

    console.print(f"[cyan]Synced {len(codes)} JP stock master record(s) into DuckDB.[/cyan]")

    max_dates = {} if full_refresh else get_existing_max_dates(codes)
    new_codes = [c for c in codes if c not in max_dates]
    existing_codes = [c for c in codes if c in max_dates]

    total_saved = 0

    # 1. 新規銘柄（または --full）は銘柄コードごとに過去履歴を取得（Lightプラン 60req/min 対応でウェイト制御）
    if new_codes:
        console.print(
            f"[cyan]Fetching initial history (from {default_from_str}) for {len(new_codes)} JP ticker(s)...[/cyan]"
        )
        batch_rows: list[tuple] = []
        for idx, code4 in enumerate(new_codes, 1):
            t0 = time.monotonic()
            raw_bars = _jquants_get_all(
                "/equities/bars/daily",
                params={"code": to_jquants_code(code4), "from": default_from_str},
                headers=headers,
            )
            batch_rows.extend(_parse_daily_bar_rows(raw_bars))

            if idx % 10 == 0 or idx == len(new_codes):
                console.print(f"  [{idx}/{len(new_codes)}] fetched ({len(batch_rows):,} rows buffered)")

            # 25銘柄ごとにDuckDBへフラッシュ保存（途中中断しても再開可能にするため）
            if len(batch_rows) >= 5000 or idx == len(new_codes):
                with get_connection() as con:
                    con.executemany(
                        """
                        INSERT OR REPLACE INTO daily_prices (
                            code, date, open, high, low, close, raw_close, volume, turnover_value
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        batch_rows,
                    )
                total_saved += len(batch_rows)
                batch_rows.clear()

            # Lightプラン (60 req/min = 1.0s/req) を超えないよう調整
            elapsed = time.monotonic() - t0
            if idx < len(new_codes) and elapsed < 1.05:
                time.sleep(1.05 - elapsed)

    # 2. 既存銘柄の差分更新
    if existing_codes:
        min_last_date = min(max_dates[c] for c in existing_codes)
        days_behind = (date.today() - min_last_date).days

        if days_behind <= 0:
            console.print("[green]Existing JP tickers are already up to date.[/green]")
        elif len(existing_codes) <= days_behind or target_codes is not None:
            # 個別銘柄指定時や対象銘柄数が少ない場合は code 指定で差分取得
            console.print(
                f"[cyan]Fetching incremental diff by code for {len(existing_codes)} JP ticker(s)...[/cyan]"
            )
            diff_rows: list[tuple] = []
            for idx, code4 in enumerate(existing_codes, 1):
                from_dt = (max_dates[code4] - timedelta(days=1)).strftime("%Y-%m-%d")
                raw_bars = _jquants_get_all(
                    "/equities/bars/daily",
                    params={"code": to_jquants_code(code4), "from": from_dt},
                    headers=headers,
                )
                diff_rows.extend(_parse_daily_bar_rows(raw_bars))
                if idx < len(existing_codes):
                    time.sleep(1.05)
            if diff_rows:
                with get_connection() as con:
                    con.executemany(
                        """
                        INSERT OR REPLACE INTO daily_prices (
                            code, date, open, high, low, close, raw_close, volume, turnover_value
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        diff_rows,
                    )
                total_saved += len(diff_rows)
        else:
            # 全体差分更新（100銘柄以上で直近数日分だけの場合）は date 指定で営業日ごとに全銘柄一括取得！
            console.print(
                f"[cyan]Fetching incremental diff by date (from {min_last_date + timedelta(days=1)} to {date.today()})...[/cyan]"
            )
            allowed_set = set(existing_codes)
            diff_rows = []
            cur_dt = min_last_date + timedelta(days=1)
            while cur_dt <= date.today():
                # 土日はスキップ
                if cur_dt.weekday() < 5:
                    raw_bars = _jquants_get_all(
                        "/equities/bars/daily",
                        params={"date": cur_dt.strftime("%Y-%m-%d")},
                        headers=headers,
                    )
                    diff_rows.extend(_parse_daily_bar_rows(raw_bars, allowed_codes=allowed_set))
                    time.sleep(1.05)
                cur_dt += timedelta(days=1)

            if diff_rows:
                with get_connection() as con:
                    con.executemany(
                        """
                        INSERT OR REPLACE INTO daily_prices (
                            code, date, open, high, low, close, raw_close, volume, turnover_value
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        diff_rows,
                    )
                total_saved += len(diff_rows)

    console.print(f"[green]Upserted {total_saved:,} JP daily price row(s) into DuckDB.[/green]")

    if with_fins or target_codes:
        fetch_and_save_jp_financials(codes)

    return total_saved
