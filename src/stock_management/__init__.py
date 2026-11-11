import argparse
from pathlib import Path
from rich.console import Console
from stock_management.chart import generate_stock_chart
from stock_management.db import init_db
from stock_management.ingest_us import DEFAULT_US_CONFIG_PATH, fetch_and_save_us_prices
from stock_management.prompt_builder import generate_ai_consultation_prompt, save_prompt_to_file
from stock_management.screener import print_screening_table, screen_core_swing_candidates

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(description="日本株・米国株 スクリーニング＆投資支援ツール")
    subparsers = parser.add_subparsers(dest="command")

    # init
    subparsers.add_parser("init", help="DuckDBのスキーマとVIEWを初期化する")

    # fetch-us
    fetch_us_parser = subparsers.add_parser("fetch-us", help="米国主力銘柄の日足データを取得・更新する")
    fetch_us_parser.add_argument("--period", default="2y", help="取得期間 (例: 1y, 2y, 5y, 1mo)")
    fetch_us_parser.add_argument(
        "--config",
        type=str,
        default=str(DEFAULT_US_CONFIG_PATH),
        help="銘柄定義TOMLファイルのパス (デフォルト: config/us_stocks.toml)",
    )

    # screen
    screen_parser = subparsers.add_parser("screen", help="テクニカル指標とスイング候補を表示する")
    screen_parser.add_argument("--market", choices=["US", "JP"], default=None, help="市場フィルター")
    screen_parser.add_argument("--only-candidates", action="store_true", help="★候補のみに絞って表示する")

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
        fetch_and_save_us_prices(config_path=Path(args.config), period=args.period)
    elif args.command == "screen":
        df = screen_core_swing_candidates(market=args.market)
        if args.only_candidates and not df.empty:
            df = df[df["is_candidate"]]
        print_screening_table(df, title=f"Core Swing Technical Snapshot ({args.market or 'ALL'})")
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
