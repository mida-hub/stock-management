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


def _attach_jp_fundamentals(df: pd.DataFrame) -> pd.DataFrame:
    """日本株の財務サマリーから PER・配当利回り・営業利益増益率・次回決算目安を付与する。"""
    for col in ("per", "div_yield_pct", "op_growth_pct", "next_earnings_est"):
        df[col] = pd.NA
    df["earnings_warn"] = False
    df["is_turnaround"] = False

    if "JP" not in df["market"].values:
        return df

    query = """
    WITH ranked_fins AS (
        SELECT
            code,
            disclosed_date,
            fiscal_quarter,
            operating_profit,
            eps,
            forecast_eps,
            forecast_dividend,
            ROW_NUMBER() OVER (PARTITION BY code ORDER BY disclosed_date DESC) AS rn
        FROM financial_summaries
    ),
    latest_fins AS (
        SELECT * FROM ranked_fins WHERE rn = 1
    ),
    prev_yoy AS (
        SELECT
            l.code,
            p.operating_profit AS prev_op
        FROM latest_fins l
        JOIN financial_summaries p
          ON l.code = p.code
         AND l.fiscal_quarter = p.fiscal_quarter
         AND p.disclosed_date BETWEEN l.disclosed_date - INTERVAL '450 days'
                                  AND l.disclosed_date - INTERVAL '270 days'
        QUALIFY ROW_NUMBER() OVER (PARTITION BY l.code ORDER BY p.disclosed_date DESC) = 1
    )
    SELECT
        l.code,
        l.disclosed_date,
        l.operating_profit,
        l.eps,
        l.forecast_eps,
        l.forecast_dividend,
        y.prev_op
    FROM latest_fins l
    LEFT JOIN prev_yoy y ON l.code = y.code;
    """
    with get_connection() as con:
        fins_df = con.execute(query).df()

    if fins_df.empty:
        return df

    fins_map = fins_df.set_index("code").to_dict(orient="index")
    for idx, row in df.iterrows():
        if row["market"] != "JP":
            continue
        f = fins_map.get(row["code"])
        if not f:
            continue

        price = float(row["raw_close"])
        eps_val = f.get("forecast_eps") if pd.notna(f.get("forecast_eps")) and f.get("forecast_eps", 0) > 0 else f.get("eps")
        if pd.notna(eps_val) and eps_val > 0 and price > 0:
            df.at[idx, "per"] = round(price / float(eps_val), 1)

        div_val = f.get("forecast_dividend")
        if pd.notna(div_val) and div_val >= 0 and price > 0:
            df.at[idx, "div_yield_pct"] = round((float(div_val) / price) * 100.0, 2)

        op = f.get("operating_profit")
        prev_op = f.get("prev_op")
        if pd.notna(op) and pd.notna(prev_op) and abs(float(prev_op)) > 0:
            growth = round(((float(op) - float(prev_op)) / abs(float(prev_op))) * 100.0, 1)
            df.at[idx, "op_growth_pct"] = growth
            if (float(prev_op) <= 0 < float(op)) or growth >= 20.0:
                df.at[idx, "is_turnaround"] = True

        disc = pd.to_datetime(f.get("disclosed_date"), errors="coerce")
        price_dt = pd.to_datetime(row["date"], errors="coerce")
        if pd.notna(disc):
            next_est = disc + pd.Timedelta(days=90)
            df.at[idx, "next_earnings_est"] = next_est.strftime("%Y-%m-%d")
            if pd.notna(price_dt):
                days_left = (next_est - price_dt).days
                df.at[idx, "earnings_warn"] = 0 <= days_left <= 10

    return df


def _attach_us_fundamentals(df: pd.DataFrame) -> pd.DataFrame:
    """米国株のファンダメンタルズ（us_fundamentals）から PER・配当利回り・増益率・次回決算日を付与する。"""
    if "US" not in df["market"].values:
        return df

    with get_connection() as con:
        us_df = con.execute("SELECT * FROM us_fundamentals").df()

    if us_df.empty:
        return df

    us_map = us_df.set_index("code").to_dict(orient="index")
    for idx, row in df.iterrows():
        if row["market"] != "US":
            continue
        f = us_map.get(row["code"])
        if not f:
            continue

        pe = f.get("forward_pe") if pd.notna(f.get("forward_pe")) else f.get("trailing_pe")
        if pd.notna(pe) and float(pe) > 0:
            df.at[idx, "per"] = round(float(pe), 1)

        if pd.notna(f.get("dividend_yield_pct")):
            df.at[idx, "div_yield_pct"] = round(float(f["dividend_yield_pct"]), 2)

        if pd.notna(f.get("earnings_growth_pct")):
            df.at[idx, "op_growth_pct"] = round(float(f["earnings_growth_pct"]), 1)

        next_ed = pd.to_datetime(f.get("next_earnings_date"), errors="coerce")
        price_dt = pd.to_datetime(row["date"], errors="coerce")
        if pd.notna(next_ed):
            df.at[idx, "next_earnings_est"] = next_ed.strftime("%Y-%m-%d")
            if pd.notna(price_dt):
                days_left = (next_ed - price_dt).days
                df.at[idx, "earnings_warn"] = 0 <= days_left <= 10

    return df


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
    df = _attach_jp_fundamentals(df)
    df = _attach_us_fundamentals(df)
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
            "PER": df["per"].map(lambda x: f"{x:.1f}x" if pd.notna(x) else "-"),
            "配当%": df["div_yield_pct"].map(lambda x: f"{x:.2f}%" if pd.notna(x) else "-"),
            "営利増益%": df["op_growth_pct"].map(lambda x: f"{x:+.1f}%" if pd.notna(x) else "-"),
            "次回決算": df.apply(
                lambda r: (f"⚠️{r['next_earnings_est']}" if r["earnings_warn"] else str(r["next_earnings_est"]))
                if pd.notna(r["next_earnings_est"])
                else "-",
                axis=1,
            ),
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


def _get_drop_reason(row: pd.Series, cfg: dict) -> str:
    dev_min = float(cfg.get("sma_25_dev_min", -6.0))
    dev_max = float(cfg.get("sma_25_dev_max", 5.0))
    rsi_min = float(cfg.get("rsi_14_min", 30.0))
    rsi_max = float(cfg.get("rsi_14_max", 65.0))

    reasons: list[str] = []
    cond_trend = (pd.notna(row["sma_200"]) and row["close"] >= row["sma_200"]) or (
        pd.notna(row["sma_75_slope_pct"]) and row["sma_75_slope_pct"] > 0
    )
    if not cond_trend:
        reasons.append("トレンド外")
    if pd.isna(row["sma_25_dev_pct"]) or not (dev_min <= row["sma_25_dev_pct"] <= dev_max):
        val = f"{row['sma_25_dev_pct']:+.1f}%" if pd.notna(row["sma_25_dev_pct"]) else "N/A"
        reasons.append(f"25MA乖離({val})")
    if pd.isna(row["rsi_14"]) or not (rsi_min <= row["rsi_14"] <= rsi_max):
        val = f"{row['rsi_14']:.1f}" if pd.notna(row["rsi_14"]) else "N/A"
        reasons.append(f"RSI({val})")
    if row["lot_type"] == "除外(値嵩)":
        reasons.append("値嵩(>25,000円)")
    return ", ".join(reasons)


def print_theme_basket(
    theme: str,
    budget: float = 250000.0,
    max_stocks: int = 5,
    min_stocks: int = 3,
    relaxed: bool = False,
) -> None:
    """テーマ別S株バスケット（コアスクリーニング通過銘柄への均等配分＋落選理由表示）を出力する。"""
    jp_cfg = load_jp_config(DEFAULT_JP_CONFIG_PATH) if DEFAULT_JP_CONFIG_PATH.exists() else {}
    themes = jp_cfg.get("themes", {})
    screening_cfg = dict(jp_cfg.get("screening", {}))
    if relaxed:
        screening_cfg["sma_25_dev_max"] = max(float(screening_cfg.get("sma_25_dev_max", 5.0)), 8.0)
        screening_cfg["rsi_14_max"] = max(float(screening_cfg.get("rsi_14_max", 65.0)), 70.0)

    df = screen_core_swing_candidates(market="JP")
    if df.empty:
        console.print("[yellow]No JP stock data found in database.[/yellow]")
        return

    if theme == "業績底打ち":
        theme_df = df[df["is_turnaround"]].copy()
        source_label = "動的テーマ: 業績底打ち（黒字転換・営利+20%以上）"
    elif theme == "上昇トレンド":
        cond_up = (
            (df["close"] >= df["sma_200"])
            & (df["sma_75_slope_pct"] > 0)
            & (df["sma_25_dev_pct"].between(0.0, 8.0))
            & (df["rsi_14"].between(50.0, 70.0))
            & (df["lot_type"] != "除外(値嵩)")
        )
        theme_df = df[cond_up].sort_values("sma_75_slope_pct", ascending=False).copy()
        theme_df["is_candidate"] = True
        source_label = "動的テーマ: 上昇トレンド順張り（200SMA上・75SMA上向き・25MA乖離0〜+8%・RSI 50〜70）"
    elif theme in themes:
        target_codes = [str(c) for c in themes[theme]]
        theme_df = df[df["code"].isin(target_codes)].copy()
        source_label = f"カスタムテーマ: {theme}"
    else:
        theme_df = df[df["sector"] == theme].copy()
        source_label = f"東証33業種: {theme}"

    if theme_df.empty:
        avail = ", ".join(themes.keys())
        console.print(f"[yellow]テーマ '{theme}' に該当する銘柄がDBに見つかりません。[/yellow]")
        if avail:
            console.print(f"定義済みカスタムテーマ: {avail}")
        return

    if relaxed:
        dev_min = float(screening_cfg.get("sma_25_dev_min", -6.0))
        dev_max = float(screening_cfg["sma_25_dev_max"])
        rsi_min = float(screening_cfg.get("rsi_14_min", 30.0))
        rsi_max = float(screening_cfg["rsi_14_max"])
        cond_trend = (theme_df["close"] >= theme_df["sma_200"]) | (theme_df["sma_75_slope_pct"] > 0)
        cond_pullback = (theme_df["sma_25_dev_pct"].between(dev_min, dev_max)) & (
            theme_df["rsi_14"].between(rsi_min, rsi_max)
        )
        cond_affordable = theme_df["lot_type"] != "除外(値嵩)"
        theme_df["is_candidate"] = cond_trend & cond_pullback & cond_affordable

    passed = theme_df[theme_df["is_candidate"]].sort_values("sma_25_dev_pct").head(max_stocks).copy()
    dropped = theme_df[~theme_df["is_candidate"]].copy()

    console.print(f"\n[bold cyan]=== テーマ別S株バスケット ({source_label} / 予算: {budget:,.0f}円) ===[/bold cyan]")

    if passed.empty:
        console.print("[yellow]スクリーニング条件を通過した銘柄はありませんでした。[/yellow]")
    elif len(passed) < min_stocks:
        codes_str = ", ".join(f"{r['code']}({r['name'][:8]})" for _, r in passed.iterrows())
        console.print(
            f"[yellow]通過銘柄が {len(passed)} 銘柄（{codes_str}）のみのため、バスケット見送り（最低 {min_stocks} 銘柄必要）。[/yellow]\n"
        )
    else:
        per_budget = budget / len(passed)
        passed["shares"] = passed["raw_close"].map(lambda p: max(1, int(per_budget // p)) if p <= budget else 0)
        passed["est_amount"] = passed["shares"] * passed["raw_close"]
        total_amount = passed["est_amount"].sum()

        basket_df = pd.DataFrame(
            {
                "Code": passed["code"],
                "Name": passed["name"].astype(str).str.slice(0, 12),
                "Close": passed["raw_close"].map(lambda x: f"{x:,.0f}"),
                "株数": passed["shares"],
                "概算金額": passed["est_amount"].map(lambda x: f"{x:,.0f}円"),
                "25d_Dev%": passed["sma_25_dev_pct"].map(lambda x: f"{x:+.2f}%"),
                "RSI(14)": passed["rsi_14"].map(lambda x: f"{x:.1f}"),
                "PER": passed["per"].map(lambda x: f"{x:.1f}x" if pd.notna(x) else "-"),
                "配当%": passed["div_yield_pct"].map(lambda x: f"{x:.2f}%" if pd.notna(x) else "-"),
            }
        )
        console.print("[bold green]▼ 採用銘柄（S株バスケット配分）[/bold green]")
        print(basket_df.to_string(index=False))
        console.print(f"合計投資額: [bold]{total_amount:,.0f}円[/bold] / 残額: {budget - total_amount:,.0f}円\n")

    if not dropped.empty:
        dropped["drop_reason"] = dropped.apply(_get_drop_reason, axis=1, cfg=screening_cfg)
        drop_df = pd.DataFrame(
            {
                "Code": dropped["code"],
                "Name": dropped["name"].astype(str).str.slice(0, 12),
                "Close": dropped["raw_close"].map(lambda x: f"{x:,.0f}"),
                "25d_Dev%": dropped["sma_25_dev_pct"].map(lambda x: f"{x:+.2f}%" if pd.notna(x) else "-"),
                "RSI(14)": dropped["rsi_14"].map(lambda x: f"{x:.1f}" if pd.notna(x) else "-"),
                "落選理由": dropped["drop_reason"],
            }
        )
        console.print("[bold yellow]▼ 除外銘柄（スクリーニング落選理由）[/bold yellow]")
        print(drop_df.to_string(index=False))
