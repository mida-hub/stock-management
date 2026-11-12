from datetime import date
from pathlib import Path
import pandas as pd
from stock_management.db import get_connection


def generate_ai_consultation_prompt(
    code: str,
    budget_jpy: int = 500_000,
    max_risk_pct: float = 6.0,
) -> str:
    """
    指定した銘柄コードの最新テクニカル指標・直近値動きをDuckDBから抽出し、
    生成AI（Claude, Antigravity等）にエントリー・利確・損切り判断を相談するための
    構造化Markdownプロンプトを生成する。
    """
    code_upper = code.upper()
    with get_connection() as con:
        latest_df = con.execute(
            """
            SELECT *
            FROM v_technical_indicators
            WHERE UPPER(code) = ?
            ORDER BY date DESC
            LIMIT 10
            """,
            [code_upper],
        ).df()

        fin_df = con.execute(
            """
            SELECT *
            FROM financial_summaries
            WHERE UPPER(code) = ?
            ORDER BY disclosed_date DESC
            LIMIT 4
            """,
            [code_upper],
        ).df()

    if latest_df.empty:
        raise ValueError(f"銘柄コード '{code}' のデータがDuckDBに存在しません。")

    curr = latest_df.iloc[0]
    currency = "USD ($)" if curr["market"] == "US" else "JPY (円)"
    raw_p = float(curr["raw_close"]) if pd.notna(curr.get("raw_close")) else float(curr["close"])

    if curr["market"] == "JP":
        lot_100_jpy = int(raw_p * 100)
        if raw_p <= 5000:
            execution_mode = f"単元株（100株単位：1単元 約 {lot_100_jpy:,} 円 / 指値・逆指値・PTS利用可）"
        elif raw_p <= 25000:
            est_shares = max(1, int(min(budget_jpy, 300_000) // raw_p))
            execution_mode = (
                f"S株（単元未満株：1単元 {lot_100_jpy:,} 円で50万円超のため、1株 {raw_p:,.0f} 円 × {est_shares}株前後で分割売買）"
            )
        else:
            execution_mode = f"除外対象（超値嵩株：1株 {raw_p:,.0f} 円 > 上限 25,000 円）"
    else:
        execution_mode = "米国株（1株単位：指値・逆指値利用可）"

    # 直近5営業日の推移テーブル作成
    recent_5 = latest_df.head(5).iloc[::-1]
    recent_rows = []
    for _, r in recent_5.iterrows():
        dt_str = str(r["date"])[:10]
        recent_rows.append(
            f"| {dt_str} | {r['open']:.2f} | {r['high']:.2f} | {r['low']:.2f} | **{r['close']:.2f}** | {int(r['volume']):,} | {r['rsi_14']:.1f} |"
        )
    recent_table_md = "\n".join(recent_rows)

    # 財務サマリーがあれば整形
    if not fin_df.empty:
        fin_lines = []
        for _, f in fin_df.iterrows():
            fin_lines.append(
                f"- 開示日: {str(f['disclosed_date'])[:10]} ({f['fiscal_quarter']}) | "
                f"売上: {f['net_sales']} | 営業利益: {f['operating_profit']} | EPS: {f['eps']} | 予想EPS: {f['forecast_eps']}"
            )
        fin_section = "\n".join(fin_lines)
    else:
        fin_section = "- （DB内に財務サマリー未登録：必要に応じて直近決算日・コンセンサスを確認してください）"

    max_loss_jpy = int(budget_jpy * (max_risk_pct / 100.0))

    prompt = f"""# 株式スイングトレード エントリー＆イグジット判断相談 ({curr['code']} - {curr['name']})

以下の銘柄について、私の投資スタイル・リスク許容度・現在のテクニカル指標に基づき、**今エントリーすべきか（見送るべきか）**、および **具体的な指値・損切り・利確ライン** を提案してください。

---

## 1. 投資家の前提条件・ルール（テンプレート）
* **投資スタイル**: コア戦略（主力大型株の中期スイング：数週間〜数ヶ月保有）
* **狙う局面**: 中長期（75日・200日線）の上昇トレンド中における、25日移動平均線付近への押し目・反発局面
* **想定投入資金**: 約 {budget_jpy:,} 円（1ポジションあたり）
* **売買単位・執行方式**: **{execution_mode}**
* **許容最大リスク**: 買値から **-{max_risk_pct:.1f}%**（最大許容損失額: 約 {max_loss_jpy:,} 円）
* **目標リスクリワード比**: **1 : 2 以上**（損切り幅に対して2倍以上の利幅が狙えること）

---

## 2. 対象銘柄の最新テクニカル指標 ({str(curr['date'])[:10]} 時点)
* **銘柄**: `{curr['code']}` ({curr['name']}) / 市場: `{curr['market']}` ({curr['exchange_division']}) / セクター: `{curr['sector_33_name']}`
* **現在値 (終値)**: **{curr['close']:.2f}** ({currency})
* **移動平均線と乖離率・傾き**:
  * **5日SMA**: `{curr['sma_5']:.2f}`
  * **25日SMA**: `{curr['sma_25']:.2f}` （乖離率: `{curr['sma_25_dev_pct']:+.2f}%` / 5日前比傾き: `{curr['sma_25_slope_pct']:+.2f}%`）
  * **75日SMA**: `{curr['sma_75']:.2f}` （乖離率: `{curr['sma_75_dev_pct']:+.2f}%` / 5日前比傾き: `{curr['sma_75_slope_pct']:+.2f}%`）
  * **200日SMA**: `{curr['sma_200']:.2f}` （乖離率: `{curr['sma_200_dev_pct']:+.2f}%` / 5日前比傾き: `{curr['sma_200_slope_pct']:+.2f}%`）
* **オシレーター・出来高・高値安値**:
  * **RSI (14日)**: `{curr['rsi_14']:.1f}`
  * **出来高倍率 (対25日平均)**: `{curr['vol_ratio_25d']:.2f} 倍`
  * **直近20日間の最安値 (サポート目安)**: `{curr['recent_low_20d']:.2f}`
  * **直近60日高値からの下落率**: `{curr['drawdown_60d_pct']:+.2f}%`
  * **直近250日高値からの下落率**: `{curr['drawdown_250d_pct']:+.2f}%`

### 直近5営業日の四本値・RSI推移
| 日付 | 始値 | 高値 | 安値 | 終値 | 出来高 | RSI(14) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
{recent_table_md}

### 財務・決算データ
{fin_section}

---

## 3. AIへの分析・提案リクエスト
以下の5点について、具体的かつ定量的に回答してください：

1. **環境認識とエントリー判定（Go / Wait / Pass）**:
   - 長期・中期トレンドと現在の押し目深さ（25日線乖離・RSI）から見て、今すぐ成行/指値で入るべきか、もう少し下（75日線や直近安値付近）まで待つべきか？
2. **具体的なエントリー価格帯**:
   - 推奨エントリー価格（または打診買いと本玉の2段階エントリー価格）
3. **損切りライン（ストップロス）の設定**:
   - テクニカル的な根拠（直近20日安値 `{curr['recent_low_20d']:.2f}` 割れ、移動平均線割れ等）と、許容リスク（-{max_risk_pct:.1f}%以内）を両立する損切り価格
4. **利確ターゲット（第1目標・第2目標）とリスクリワード比**:
   - 直近60日高値やトレンド上限を基準とした利確目標価格と、その場合のR/R比
5. **注意点・イベントリスク**:
   - 決算発表時期やセクター動向など、保有中に注意すべきリスク要因
"""
    return prompt


def save_prompt_to_file(prompt_text: str, code: str, output_dir: Path | None = None) -> Path:
    if output_dir is None:
        output_dir = Path(__file__).resolve().parents[2] / "reports" / "prompts"
    output_dir.mkdir(parents=True, exist_ok=True)
    date_tag = date.today().strftime("%Y%m%d")
    out_path = output_dir / f"prompt_{code.upper()}_{date_tag}.md"
    out_path.write_text(prompt_text, encoding="utf-8")
    return out_path
