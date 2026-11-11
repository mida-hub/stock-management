from pathlib import Path
import duckdb

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "stock.duckdb"

SCHEMA_SQL = """
-- 1. 銘柄マスター
CREATE TABLE IF NOT EXISTS stocks (
    code VARCHAR PRIMARY KEY,
    market VARCHAR NOT NULL,            -- 'JP' or 'US'
    name VARCHAR NOT NULL,
    exchange_division VARCHAR,          -- 'Prime', 'Standard', 'Growth', 'NASDAQ', 'NYSE', etc.
    sector_17_code VARCHAR,
    sector_17_name VARCHAR,
    sector_33_code VARCHAR,
    sector_33_name VARCHAR,
    scale_category VARCHAR,             -- 'TOPIX Core30', 'Large70', 'Mid400', 'US Mega Tech', etc.
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. 日足株価データ
CREATE TABLE IF NOT EXISTS daily_prices (
    code VARCHAR NOT NULL,
    date DATE NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    raw_close DOUBLE,
    volume BIGINT,
    turnover_value DOUBLE,
    PRIMARY KEY (code, date)
);

-- 3. 財務・決算サマリー
CREATE TABLE IF NOT EXISTS financial_summaries (
    code VARCHAR NOT NULL,
    disclosed_date DATE NOT NULL,
    fiscal_year VARCHAR,
    fiscal_quarter VARCHAR NOT NULL,    -- '1Q', '2Q', '3Q', 'FY', etc.
    net_sales DOUBLE,
    operating_profit DOUBLE,
    ordinary_profit DOUBLE,
    net_income DOUBLE,
    eps DOUBLE,
    bps DOUBLE,
    forecast_eps DOUBLE,
    forecast_dividend DOUBLE,
    PRIMARY KEY (code, disclosed_date, fiscal_quarter)
);

-- 4. 指数・先物データ
CREATE TABLE IF NOT EXISTS indices_and_futures (
    symbol VARCHAR NOT NULL,
    date DATE NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    volume BIGINT,
    PRIMARY KEY (symbol, date)
);

-- 5. テクニカル指標 VIEW
CREATE OR REPLACE VIEW v_technical_indicators AS
WITH base AS (
    SELECT
        p.code,
        s.market,
        s.name,
        s.exchange_division,
        s.sector_33_name,
        s.scale_category,
        p.date,
        p.open,
        p.high,
        p.low,
        p.close,
        p.raw_close,
        p.volume,
        p.turnover_value,
        p.close - LAG(p.close, 1) OVER w AS price_diff,
        AVG(p.close) OVER (w ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS sma_5,
        AVG(p.close) OVER (w ROWS BETWEEN 24 PRECEDING AND CURRENT ROW) AS sma_25,
        AVG(p.close) OVER (w ROWS BETWEEN 74 PRECEDING AND CURRENT ROW) AS sma_75,
        AVG(p.close) OVER (w ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) AS sma_200,
        AVG(p.volume) OVER (w ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS vol_sma_5,
        AVG(p.volume) OVER (w ROWS BETWEEN 24 PRECEDING AND CURRENT ROW) AS vol_sma_25,
        MAX(p.high) OVER (w ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS high_60d,
        MAX(p.high) OVER (w ROWS BETWEEN 249 PRECEDING AND CURRENT ROW) AS high_250d,
        MIN(p.low) OVER (w ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS low_20d
    FROM daily_prices p
    LEFT JOIN stocks s ON p.code = s.code
    WINDOW w AS (PARTITION BY p.code ORDER BY p.date)
),
rsi_calc AS (
    SELECT
        *,
        LAG(sma_25, 5) OVER (PARTITION BY code ORDER BY date) AS sma_25_5d_ago,
        LAG(sma_75, 5) OVER (PARTITION BY code ORDER BY date) AS sma_75_5d_ago,
        LAG(sma_200, 5) OVER (PARTITION BY code ORDER BY date) AS sma_200_5d_ago,
        AVG(CASE WHEN price_diff > 0 THEN price_diff ELSE 0 END)
            OVER (PARTITION BY code ORDER BY date ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS avg_gain_14,
        AVG(CASE WHEN price_diff < 0 THEN -price_diff ELSE 0 END)
            OVER (PARTITION BY code ORDER BY date ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS avg_loss_14
    FROM base
)
SELECT
    code,
    market,
    name,
    exchange_division,
    sector_33_name,
    scale_category,
    date,
    open,
    high,
    low,
    close,
    raw_close,
    volume,
    turnover_value,
    ROUND(sma_5, 2) AS sma_5,
    ROUND(sma_25, 2) AS sma_25,
    ROUND(sma_75, 2) AS sma_75,
    ROUND(sma_200, 2) AS sma_200,
    ROUND((close - sma_25) / NULLIF(sma_25, 0) * 100, 2) AS sma_25_dev_pct,
    ROUND((close - sma_75) / NULLIF(sma_75, 0) * 100, 2) AS sma_75_dev_pct,
    ROUND((close - sma_200) / NULLIF(sma_200, 0) * 100, 2) AS sma_200_dev_pct,
    ROUND((sma_25 - sma_25_5d_ago) / NULLIF(sma_25_5d_ago, 0) * 100, 2) AS sma_25_slope_pct,
    ROUND((sma_75 - sma_75_5d_ago) / NULLIF(sma_75_5d_ago, 0) * 100, 2) AS sma_75_slope_pct,
    ROUND((sma_200 - sma_200_5d_ago) / NULLIF(sma_200_5d_ago, 0) * 100, 2) AS sma_200_slope_pct,
    ROUND(
        CASE
            WHEN avg_loss_14 = 0 THEN 100.0
            ELSE 100.0 - (100.0 / (1.0 + (avg_gain_14 / avg_loss_14)))
        END,
        2
    ) AS rsi_14,
    ROUND(volume / NULLIF(vol_sma_25, 0), 2) AS vol_ratio_25d,
    ROUND((close - high_60d) / NULLIF(high_60d, 0) * 100, 2) AS drawdown_60d_pct,
    ROUND((close - high_250d) / NULLIF(high_250d, 0) * 100, 2) AS drawdown_250d_pct,
    ROUND(high_60d, 2) AS recent_high_60d,
    ROUND(low_20d, 2) AS recent_low_20d
FROM rsi_calc;

-- 6. セクター資金流入・モメンタム VIEW
CREATE OR REPLACE VIEW v_sector_momentum AS
WITH stock_returns AS (
    SELECT
        p.code,
        s.market,
        s.sector_33_code,
        s.sector_33_name,
        p.date,
        p.close,
        p.turnover_value,
        LAG(p.close, 5) OVER (PARTITION BY p.code ORDER BY p.date) AS close_5d_ago,
        LAG(p.close, 20) OVER (PARTITION BY p.code ORDER BY p.date) AS close_20d_ago,
        AVG(p.turnover_value) OVER (PARTITION BY p.code ORDER BY p.date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS turnover_5d_avg,
        AVG(p.turnover_value) OVER (PARTITION BY p.code ORDER BY p.date ROWS BETWEEN 24 PRECEDING AND CURRENT ROW) AS turnover_25d_avg
    FROM daily_prices p
    JOIN stocks s ON p.code = s.code
    WHERE s.sector_33_name IS NOT NULL
)
SELECT
    market,
    sector_33_code,
    sector_33_name,
    date,
    COUNT(code) AS stock_count,
    ROUND(AVG((close - close_5d_ago) / NULLIF(close_5d_ago, 0) * 100), 2) AS return_5d_pct,
    ROUND(AVG((close - close_20d_ago) / NULLIF(close_20d_ago, 0) * 100), 2) AS return_20d_pct,
    ROUND(SUM(turnover_5d_avg) / NULLIF(SUM(turnover_25d_avg), 0), 2) AS sector_turnover_ratio
FROM stock_returns
GROUP BY market, sector_33_code, sector_33_name, date;
"""


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> duckdb.DuckDBPyConnection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path))


def init_db(db_path: Path = DEFAULT_DB_PATH) -> Path:
    with get_connection(db_path) as con:
        con.execute(SCHEMA_SQL)
    return db_path
