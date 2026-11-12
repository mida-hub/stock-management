---
name: stock-screener
description: >-
  Use this skill when the user asks to update stock prices, run technical screening
  for swing trade candidates, generate stock charts (candlestick + SMA + RSI), or
  propose entry, stop-loss, and take-profit plans for US or Japanese stocks.
---

# Stock Screener & Swing Trade Advisor Skill

DuckDBとPython (`stock-management` CLI) を用いて、**「① 最新データの取得 ➔ ② スクリーニング抽出 ➔ ③ ローソク足＋移動平均線＋RSIチャート画像の生成 ➔ ④ AIによるエントリー・損切り・利確プランの提示」** を一貫して実行するスキルです。

---

## 実行ステップ（ワークフロー）

### Step 1: 株価データの取得・差分更新
最新の日足データを取得して DuckDB (`data/stock.duckdb`) を更新します。
- DuckDBに既存データがある銘柄は**自動的に最終日付からの差分のみ**を高速取得し、データがない新規銘柄のみ初回期間（過去2年分）を取得します。
- ユーザーが特定の銘柄（例: 米国株 `MSFT` や日本株 `8306`）だけを指定して相談している場合は、銘柄コードを引数に渡して**その銘柄のみをピンポイントで差分更新**してください。

```bash
# 【米国株】特定銘柄のみをピンポイントで差分更新する場合（推奨・高速）
.venv/bin/stock-management fetch-us MSFT

# 【米国株】監視ユニバース全銘柄（config/us_stocks.toml）をまとめて差分更新する場合
.venv/bin/stock-management fetch-us

# 【日本株】特定銘柄（4桁コード）のみをJ-Quants API(v2)でピンポイント取得・差分更新する場合（財務サマリーも自動取得）
.venv/bin/stock-management fetch-jp 8306

# 【日本株】監視ユニバース全銘柄（config/jp_stocks.toml: Core30 + Large70 + extra_codes）をまとめて取得・差分更新する場合
.venv/bin/stock-management fetch-jp
```

### Step 2: コア戦略（主力大型スイング）スクリーニングの実行
DuckDBの `v_technical_indicators` ビューから、中長期トレンドが上向き（終値≧200日SMA または 75日SMA傾き＞0）かつ、25日SMA付近への押し目（乖離率 -6%〜+5%、RSI 30〜65）にある候補銘柄を抽出します。

```bash
# 全監視銘柄のスナップショットを確認する場合（--market US または --market JP）
.venv/bin/stock-management screen --market US
.venv/bin/stock-management screen --market JP

# ★候補（スイング候補）のみに絞り込む場合
.venv/bin/stock-management screen --market US --only-candidates
.venv/bin/stock-management screen --market JP --only-candidates

# 【日本株】購入区分でさらに絞り込む場合（--lot unit: 1単元50万円以下の単元株のみ / --lot skabu: 株価5,001〜25,000円のS株のみ）
.venv/bin/stock-management screen --market JP --only-candidates --lot unit
.venv/bin/stock-management screen --market JP --only-candidates --lot skabu

# 【日本株】東証33業種の騰落率・資金流入ランキングを確認する場合（テーマ別S株バスケット用）
.venv/bin/stock-management sectors --market JP
```

### Step 3: 個別銘柄のプロンプト情報抽出 ＆ チャート画像（PNG）生成
候補銘柄またはユーザーが指定した銘柄について、詳細な数値データの確認とチャート画像の描画を行います。

1. **数値・直近5日四本値・サポート/レジスタンスの確認**:
   ```bash
   .venv/bin/stock-management prompt <CODE> --budget 500000 --risk 6.0 --save
   ```
2. **テクニカルデータの分析と Entry / Stop / Target 価格の決定**:
   - **エントリー目安 (`--entry`)**: 現在値または25日SMA付近の指値
   - **損切りライン (`--stop`)**: 直近20日最安値 (`20d_Low`) や75日SMA割れなど、テクニカル根拠があり、かつ許容最大損失率（デフォルト `-6.0%`）以内に収まる価格
   - **利確ターゲット (`--target`)**: 直近60日高値やトレンド上限など、リスクリワード比 `1 : 2` 以上が狙える価格
3. **エントリー・損切り・利確ライン入りチャート画像の生成**:
   ```bash
   .venv/bin/stock-management chart <CODE> --days 120 --entry <ENTRY_PRICE> --stop <STOP_PRICE> --target <TARGET_PRICE>
   ```
   - 生成された画像は `reports/charts/chart_<CODE>_<YYYYMMDD>.png` に保存されます。
   - **確認**: `read_file` ツールで生成された PNG 画像を読み込み、ローソク足・移動平均線・水平ラインの位置関係を視覚的に検証してください。

### Step 4: ユーザーへの視覚的レポート・判断提示
以下のルールに従ってユーザーへ結果を提示します：
- **Markdownエディタでの閲覧用レポート出力**:
  - ユーザーがリポジトリ内のMarkdownエディタでそのまま画像付きで閲覧できるよう、レポート本体はリポジトリ配下の `reports/` ディレクトリに日付入りファイル名（例: `reports/swing_report_YYYYMMDD.md` または `reports/analysis_<CODE>_YYYYMMDD.md`）で出力してください。
  - レポート内の画像リンクは、同じ `reports/` 配下からの相対パス（例: `![MSFT Chart](./charts/chart_MSFT_YYYYMMDD.png)`）で記述してください。
- 提案には必ず以下の5点を含めてください：
  1. **環境認識とエントリー判定（Go / Wait / Pass）**
  2. **推奨エントリー価格帯**（打診買い・押し目追加の2段階など）
  3. **損切りライン（ストップロス）の価格と根拠**（買値からの下落率％も明記）
  4. **利確ターゲット（第1目標・第2目標）とリスクリワード比**
  5. **注意点・イベントリスク**（決算発表時期やセクター動向）
