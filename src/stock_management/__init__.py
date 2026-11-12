import argparse
from pathlib import Path
from rich.console import Console
from stock_management.chart import generate_stock_chart
from stock_management.db import init_db
from stock_management.ingest_jp import DEFAULT_JP_CONFIG_PATH, fetch_and_save_jp_prices
from stock_management.ingest_us import DEFAULT_US_CONFIG_PATH, fetch_and_save_us_prices
from stock_management.prompt_builder import generate_ai_consultation_prompt, save_prompt_to_file
from stock_management.screener import (
    print_screening_table,
    print_sector_momentum_table,
    screen_core_swing_candidates,
)

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(description="日本株・米国株 スクリーニング＆投資支援ツール")
    subparsers = parser.add_subparsers(dest="command")

    # init
    subparsers.add_parser("init", help="DuckDBのスキーマとVIEWを初期化する")

    # fetch-us
    fetch_us_parser = subparsers.add_parser("fetch-us", help="米国主力銘柄の日足データを取得・差分更新する")
    fetch_us_parser.add_argument(
        "codes",
        nargs="*",
        help="特定の銘柄コードのみ取得する場合に指定 (例: MSFT AAPL)。省略時は config 内の全銘柄",
    )
    fetch_us_parser.add_argument("--period", default="2y", help="新規銘柄の初回取得期間 (デフォルト: 2y)")
    fetch_us_parser.add_argument("--full", action="store_true", help="差分ではなく全期間(period)を再取得する")
    fetch_us_parser.add_argument(
        "--config",
        type=str,
        default=str(DEFAULT_US_CONFIG_PATH),
        help="銘柄定義TOMLファイルのパス (デフォルト: config/us_stocks.toml)",
    )

    # fetch-jp
    fetch_jp_parser = subparsers.add_parser("fetch-jp", help="J-Quants API(v2)から日本株主力銘柄の日足・財務データを取得・差分更新する")
    fetch_jp_parser.add_argument(
        "codes",
        nargs="*",
        help="特定の銘柄コード(4桁)のみ取得する場合に指定 (例: 8306 7203)。省略時は config/jp_stocks.toml の対象全銘柄",
    )
    fetch_jp_parser.add_argument("--full", action="store_true", help="差分ではなく初回遡及期間を再取得する")
    fetch_jp_parser.add_argument("--with-fins", action="store_true", help="財務サマリー(/v2/fins/summary)も併せて取得する")
    fetch_jp_parser.add_argument(
        "--config",
        type=str,
        default=str(DEFAULT_JP_CONFIG_PATH),
        help="日本株設定TOMLファイルのパス (デフォルト: config/jp_stocks.toml)",
    )

    # screen
    screen_parser = subparsers.add_parser("screen", help="テクニカル指標とスイング候補を表示する")
    screen_parser.add_argument("--market", choices=["US", "JP"], default=None, help="市場フィルター")
    screen_parser.add_argument("--only-candidates", action="store_true", help="★候補のみに絞って表示する")
    screen_parser.add_argument(
        "--lot",
        choices=["unit", "skabu"],
        default=None,
        help="日本株の購入区分フィルター (unit: 単元株[5000円以下]のみ, skabu: S株[5001〜25000円]のみ)",
    )

    # sectors
    sectors_parser = subparsers.add_parser("sectors", help="業種・セクター別の騰落率と資金流入ランキングを表示する")
    sectors_parser.add_argument("--market", choices=["JP", "US"], default="JP", help="市場 (デフォルト: JP)")

    # prompt
    prompt_parser = subparsers.add_parser("prompt", help="指定銘柄のAI壁打ち用プロンプト(Markdown)を生成する")
    prompt_parser.add_argument("code", help="銘柄コード (例: MSFT, AAPL, 8306)")
    prompt_parser.add_argument("--budget", type=int, default=500000, help="1トレードの想定予算(円) デフォルト: 500,000")
    prompt_parser.add_argument("--risk", type=float, default=6.0, help="許容最大損失率(%%) デフォルト: 6.0")
    prompt_parser.add_argument("--save", action="store_true", help="reports/prompts/ 配下にMarkdownファイルとして保存する")

    # chart
    chart_parser = subparsers.add_parser("chart", help="指定銘柄のローソク足＋移動平均＋RSIチャート画像(PNG)を生成する")
    chart_parser.add_argument("code", help="銘柄コード (例: MSFT, AAPL, 8306)")
    chart_parser.add_argument("--days", type=int, default=120, help="描画する直近営業日数 (デフォルト: 120)")
    chart_parser.add_argument("--output", type=str, default=None, help="出力先ファイルパス (省略時は reports/charts/chart_<CODE>_<YYYYMMDD>.png)")
    chart_parser.add_argument("--entry", type=float, default=None, help="エントリー目安ラインの価格")
    chart_parser.add_argument("--stop", type=float, default=None, help="損切りラインの価格")
    chart_parser.add_argument("--target", type=float, default=None, help="利確ターゲットラインの価格")

    args = parser.parse_args()

    if args.command == "init":
        db_path = init_db()
        console.print(f"[green]Initialized DuckDB at {db_path}[/green]")
    elif args.command == "fetch-us":
        fetch_and_save_us_prices(
            config_path=Path(args.config),
            period=args.period,
            target_codes=args.codes if args.codes else None,
            full_refresh=args.full,
        )
    elif args.command == "fetch-jp":
        fetch_and_save_jp_prices(
            config_path=Path(args.config),
            target_codes=args.codes if args.codes else None,
            full_refresh=args.full,
            with_fins=args.with_fins,
        )
    elif args.command == "screen":
        df = screen_core_swing_candidates(market=args.market)
        if args.lot == "unit" and not df.empty:
            df = df[df["lot_type"] == "単元(100株)"]
        elif args.lot == "skabu" and not df.empty:
            df = df[df["lot_type"] == "S株(未満株)"]
        if args.only_candidates and not df.empty:
            df = df[df["is_candidate"]]
        print_screening_table(df, title=f"Core Swing Technical Snapshot ({args.market or 'ALL'})")
    elif args.command == "sectors":
        print_sector_momentum_table(market=args.market)
    elif args.command == "prompt":
        prompt_md = generate_ai_consultation_prompt(
            code=args.code,
            budget_jpy=args.budget,
            max_risk_pct=args.risk,
        )
        print(prompt_md)
        if args.save:
            saved_path = save_prompt_to_file(prompt_md, args.code)
            console.print(f"\n[bold green]Saved prompt to: {saved_path}[/bold green]")
    elif args.command == "chart":
        out = Path(args.output) if args.output else None
        chart_path = generate_stock_chart(
            code=args.code,
            days=args.days,
            output_path=out,
            entry_price=args.entry,
            stop_price=args.stop,
            target_price=args.target,
        )
        console.print(f"[bold green]Generated chart at: {chart_path}[/bold green]")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
