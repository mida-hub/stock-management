import os
from pathlib import Path

# サンドボックス内でもMatplotlibキャッシュが高速に読み書きできるよう data/.mplconfig を指定
_MPL_DIR = Path(__file__).resolve().parents[2] / "data" / ".mplconfig"
_MPL_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", str(_MPL_DIR))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
from stock_management.db import get_connection


def generate_stock_chart(
    code: str,
    days: int = 120,
    output_path: Path | None = None,
    entry_price: float | None = None,
    stop_price: float | None = None,
    target_price: float | None = None,
) -> Path:
    """
    指定した銘柄の直近N営業日のローソク足＋移動平均線(25/75/200)＋出来高＋RSI(14)を
    1枚のPNG画像として描画・保存する。
    """
    code_upper = code.upper()
    with get_connection() as con:
        df = con.execute(
            """
            SELECT
                date,
                code,
                name,
                market,
                open AS Open,
                high AS High,
                low AS Low,
                close AS Close,
                volume AS Volume,
                sma_25,
                sma_75,
                sma_200,
                rsi_14,
                recent_low_20d,
                recent_high_60d AS high_60d
            FROM (
                SELECT *
                FROM v_technical_indicators
                WHERE UPPER(code) = ?
                ORDER BY date DESC
                LIMIT ?
            )
            ORDER BY date ASC
            """,
            [code_upper, days],
        ).df()

    if df.empty:
        raise ValueError(f"銘柄コード '{code}' のデータがDuckDBに存在しません。")

    df["Date"] = pd.to_datetime(df["date"])
    df = df.set_index("Date")

    last_row = df.iloc[-1]
    stock_name = last_row["name"]
    curr_close = float(last_row["Close"])

    # 未指定時は直近20日安値をデフォルトのサポート/損切り目安、60日高値を第1ターゲット目安として点線描画
    support_line = stop_price if stop_price is not None else float(last_row["recent_low_20d"])
    resist_line = target_price if target_price is not None else float(last_row["high_60d"])
    entry_line = entry_price if entry_price is not None else curr_close

    # Y軸の範囲を水平ライン（Target/Stop）も含めて余裕を持たせる
    data_min = float(df["Low"].min())
    data_max = float(df["High"].max())
    if df["sma_200"].notna().any():
        data_min = min(data_min, float(df["sma_200"].min()))
        data_max = max(data_max, float(df["sma_200"].max()))
    y_min = min(data_min, support_line, entry_line) * 0.96
    y_max = max(data_max, resist_line, entry_line) * 1.04

    # 追加プロット（25/75/200 SMA ＋ 下段RSIパネル）
    add_plots = [
        mpf.make_addplot(df["sma_25"], color="#2563eb", width=1.4, label=f"25d SMA ({last_row['sma_25']:.2f})"),
        mpf.make_addplot(df["sma_75"], color="#f59e0b", width=1.4, label=f"75d SMA ({last_row['sma_75']:.2f})"),
    ]
    if df["sma_200"].notna().any():
        add_plots.append(
            mpf.make_addplot(df["sma_200"], color="#9333ea", width=1.6, label=f"200d SMA ({last_row['sma_200']:.2f})")
        )

    # RSIパネル (panel=2: 0=Main, 1=Volume, 2=RSI)
    add_plots.extend(
        [
            mpf.make_addplot(
                df["rsi_14"],
                panel=2,
                color="#0d9488",
                width=1.4,
                ylabel="RSI (14)",
                ylim=(10, 90),
            ),
            mpf.make_addplot(
                pd.Series(70.0, index=df.index),
                panel=2,
                color="#ef4444",
                linestyle="--",
                width=0.8,
            ),
            mpf.make_addplot(
                pd.Series(50.0, index=df.index),
                panel=2,
                color="#6b7280",
                linestyle=":",
                width=0.8,
            ),
            mpf.make_addplot(
                pd.Series(30.0, index=df.index),
                panel=2,
                color="#3b82f6",
                linestyle="--",
                width=0.8,
            ),
        ]
    )

    # 水平ライン（現在値/エントリー、サポート/損切り、ターゲット）
    hlines_dict = dict(
        hlines=[resist_line, entry_line, support_line],
        colors=["#16a34a", "#475569", "#dc2626"],
        linestyle=["--", ":", "--"],
        linewidths=[1.2, 1.1, 1.2],
    )

    if output_path is None:
        out_dir = Path(__file__).resolve().parents[2] / "reports" / "charts"
        out_dir.mkdir(parents=True, exist_ok=True)
        date_tag = str(last_row["date"])[:10].replace("-", "")
        output_path = out_dir / f"chart_{code_upper}_{date_tag}.png"
    else:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

    mc = mpf.make_marketcolors(
        up="#16a34a",
        down="#dc2626",
        edge="inherit",
        wick="inherit",
        volume="in",
    )
    style = mpf.make_mpf_style(
        base_mpf_style="yahoo",
        marketcolors=mc,
        gridstyle="--",
        y_on_right=True,
    )

    fig, axes = mpf.plot(
        df,
        type="candle",
        style=style,
        volume=True,
        addplot=add_plots,
        hlines=hlines_dict,
        ylim=(y_min, y_max),
        panel_ratios=(5.5, 1.5, 2.0),
        figsize=(13, 8.5),
        datetime_format="%Y-%m-%d",
        returnfig=True,
    )

    ax_main = axes[0]
    # タイトルを上部にすっきり配置
    ax_main.set_title(
        f"{code_upper} ({stock_name})  |  Close: {curr_close:.2f}  |  RSI(14): {last_row['rsi_14']:.1f}  |  Last: {str(last_row['date'])[:10]}",
        fontsize=12,
        fontweight="bold",
        pad=14,
        loc="left",
    )
    ax_main.legend(loc="upper left", fontsize=9, framealpha=0.85)

    # チャート内の中央寄りにターゲット・エントリー・ストップの価格ラベルボックスを表示（左上の凡例と重ならないように配置）
    x_label_pos = int(len(df) * 0.28)
    bbox_props = dict(boxstyle="round,pad=0.2", facecolor="white", alpha=0.88, edgecolor="none")

    ax_main.text(
        x_label_pos,
        resist_line,
        f" Target / High: {resist_line:.2f} ",
        color="#15803d",
        fontsize=8.5,
        fontweight="bold",
        va="bottom",
        bbox=bbox_props,
    )
    ax_main.text(
        x_label_pos,
        entry_line,
        f" Entry / Close: {entry_line:.2f} ",
        color="#334155",
        fontsize=8.5,
        fontweight="bold",
        va="bottom",
        bbox=bbox_props,
    )
    ax_main.text(
        x_label_pos,
        support_line,
        f" Stop / Support: {support_line:.2f} ",
        color="#b91c1c",
        fontsize=8.5,
        fontweight="bold",
        va="top",
        bbox=bbox_props,
    )

    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output_path
