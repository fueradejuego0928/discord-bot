import os
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import tasks, commands
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")

if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKEN が設定されていません。")
if not CHANNEL_ID:
    raise ValueError("DISCORD_CHANNEL_ID が設定されていません。")

CHANNEL_ID = int(CHANNEL_ID)

SP500_TICKER = "^GSPC"
VIX_TICKER = "^VIX"
BTC_TICKER = "BTC-USD"

CHECK_INTERVAL_MINUTES = 15

alert_state = {
    "sp500_-10": False,
    "sp500_-20": False,
    "sp500_-30": False,
    "vix_25": False,
    "vix_30": False,
    "vix_40": False,
    "stage_1": False,
    "stage_2": False,
    "stage_3": False,
}


def get_jst_now() -> datetime:
    jst = timezone(timedelta(hours=9))
    return datetime.now(jst)


def fetch_latest_data(ticker: str, period: str = "1y", interval: str = "1d"):
    df = yf.download(
        ticker,
        period=period,
        interval=interval,
        progress=False,
        auto_adjust=False,
        threads=False,
    )
    if df.empty:
        raise ValueError(f"{ticker} のデータ取得に失敗しました。")
    return df


def extract_close_series(df):
    """
    yfinance の返り値が
    - 単純な columns
    - MultiIndex columns
    のどちらでも Close 系列を 1本の Series として返す
    """
    if df.empty:
        raise ValueError("データが空です。")

    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        close_cols = [col for col in df.columns if "Close" in col]
        if not close_cols:
            raise ValueError("Close 列が見つかりません。")
        close_data = df[close_cols[0]]
    else:
        if "Close" not in df.columns:
            raise ValueError("Close 列が見つかりません。")
        close_data = df["Close"]

    if hasattr(close_data, "ndim") and close_data.ndim == 2:
        close_data = close_data.iloc[:, 0]

    return close_data.dropna()


def calculate_sp500_drawdown() -> dict:
    df = fetch_latest_data(SP500_TICKER, period="1y", interval="1d")
    close_series = extract_close_series(df)

    latest_close = float(close_series.iloc[-1])
    rolling_high = float(close_series.max())
    drawdown_pct = ((latest_close - rolling_high) / rolling_high) * 100.0

    return {
        "latest_close": latest_close,
        "rolling_high": rolling_high,
        "drawdown_pct": drawdown_pct,
    }


def get_latest_vix() -> float:
    df = fetch_latest_data(VIX_TICKER, period="1mo", interval="1d")
    close_series = extract_close_series(df)
    return float(close_series.iloc[-1])


def get_latest_btc() -> float:
    df = fetch_latest_data(BTC_TICKER, period="5d", interval="1h")
    close_series = extract_close_series(df)
    return float(close_series.iloc[-1])


def reset_alert_if_recovered(key: str, condition: bool):
    if not condition:
        alert_state[key] = False


def build_market_summary(drawdown_info: dict, vix_value: float, btc_price: float) -> str:
    now_str = get_jst_now().strftime("%Y-%m-%d %H:%M JST")
    return (
        f"**マーケット監視レポート**\n"
        f"- 時刻: {now_str}\n"
        f"- S&P500: {drawdown_info['latest_close']:.2f}\n"
        f"- 1年高値: {drawdown_info['rolling_high']:.2f}\n"
        f"- 高値比: {drawdown_info['drawdown_pct']:.2f}%\n"
        f"- VIX: {vix_value:.2f}\n"
        f"- BTC: ${btc_price:,.0f}"
    )


def build_market_embed(
    title: str,
    description: str,
    drawdown_info: dict,
    vix_value: float,
    btc_price: float,
    color: int,
    action: str = None,
    candidates: str = None,
):
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=get_jst_now(),
    )

    embed.add_field(name="S&P500", value=f"{drawdown_info['latest_close']:.2f}", inline=True)
    embed.add_field(name="1年高値比", value=f"{drawdown_info['drawdown_pct']:.2f}%", inline=True)
    embed.add_field(name="VIX", value=f"{vix_value:.2f}", inline=True)
    embed.add_field(name="BTC", value=f"${btc_price:,.0f}", inline=True)
    embed.add_field(name="1年高値", value=f"{drawdown_info['rolling_high']:.2f}", inline=True)
    embed.add_field(name="時刻", value=get_jst_now().strftime("%Y-%m-%d %H:%M JST"), inline=True)

    if action:
        embed.add_field(name="今やること", value=action, inline=False)

    if candidates:
        embed.add_field(name="候補ETF・銘柄", value=candidates, inline=False)

    return embed

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


async def send_message(message: str = None, embed: discord.Embed = None):
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        print("Discord channel が見つかりません。")
        return

    if embed is not None:
        await channel.send(embed=embed)
    elif message is not None:
        await channel.send(message)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    if not market_check_loop.is_running():
        market_check_loop.start()


@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def market_check_loop():
    try:
        drawdown_info = calculate_sp500_drawdown()
        vix_value = get_latest_vix()
        btc_price = get_latest_btc()

        drawdown = drawdown_info["drawdown_pct"]

        cond_sp500_10 = drawdown <= -10
        cond_sp500_20 = drawdown <= -20
        cond_sp500_30 = drawdown <= -30

        cond_vix_25 = vix_value >= 25
        cond_vix_30 = vix_value >= 30
        cond_vix_40 = vix_value >= 40

        reset_alert_if_recovered("sp500_-10", cond_sp500_10)
        reset_alert_if_recovered("sp500_-20", cond_sp500_20)
        reset_alert_if_recovered("sp500_-30", cond_sp500_30)

        reset_alert_if_recovered("vix_25", cond_vix_25)
        reset_alert_if_recovered("vix_30", cond_vix_30)
        reset_alert_if_recovered("vix_40", cond_vix_40)

        # S&P500 下落アラート
        if cond_sp500_10 and not alert_state["sp500_-10"]:
            alert_state["sp500_-10"] = True

            embed = build_market_embed(
                title="⚠️ 第1警戒",
                description="S&P500 が高値から -10% に到達しました。\n候補: `VOO` / `VT` を少量検討",
                drawdown_info=drawdown_info,
                vix_value=vix_value,
                btc_price=btc_price,
                color=0xF1C40F,
            )
            await send_message(embed=embed)

        if cond_sp500_20 and not alert_state["sp500_-20"]:
            alert_state["sp500_-20"] = True

            embed = build_market_embed(
                title="📉 第2警戒",
                description="S&P500 が高値から -20% に到達しました。\n候補: `VOO` / `VT` / `QQQM` / 
`IAU`",
                drawdown_info=drawdown_info,
                vix_value=vix_value,
                btc_price=btc_price,
                color=0xE67E22,
            )
            await send_message(embed=embed)

        if cond_sp500_30 and not alert_state["sp500_-30"]:
            alert_state["sp500_-30"] = True

            embed = build_market_embed(
                title="🔥 第3警戒",
                description="S&P500 が高値から -30% に到達しました。\n候補: `VOO` / `VT` / `QQQM` / 
`SOXX` を厚めに検討",
                drawdown_info=drawdown_info,
                vix_value=vix_value,
                btc_price=btc_price,
                color=0xE74C3C,
            )
            await send_message(embed=embed)

        # VIX アラート

if cond_vix_25 and not alert_state["vix_25"]:
    alert_state["vix_25"] = True

    embed = build_market_embed(
        title="⚠️ VIX注意（監視強化）",
        description="VIX が 25 を超えました。警戒感が高まっています。",
        drawdown_info=drawdown_info,
        vix_value=vix_value,
        btc_price=btc_price,
        color=0xF1C40F,
        action="まだ本格買いはしない。S&P500 の下落率が -10% に届くか監視。",
        candidates="監視対象: `VOO`, `VT`, `QQQM`, `IAU`",
    )
    await send_message(embed=embed)

if cond_vix_30 and not alert_state["vix_30"]:
    alert_state["vix_30"] = True

    embed = build_market_embed(
        title="📈 VIX上昇（買い準備）",
        description="VIX が 30 を超えました。相場不安が強まっています。",
        drawdown_info=drawdown_info,
        vix_value=vix_value,
        btc_price=btc_price,
        color=0xE67E22,
        action="現金比率を確認。S&P500 が -10% / -20% に進む場合に備えて注文候補を整理。",
        candidates="候補: `VOO`, `VT`, `QQQM`, `IAU`",
    )
    await send_message(embed=embed)

if cond_vix_40 and not alert_state["vix_40"]:
    alert_state["vix_40"] = True

    embed = build_market_embed(
        title="🚨 VIX急騰（極端な恐怖）",
        description="VIX が 40 を超えました。極端な恐怖局面です。",
        drawdown_info=drawdown_info,
        vix_value=vix_value,
        btc_price=btc_price,
        color=0xE74C3C,
        action="あわてて飛びつかず、S&P500 の下落率と併せて分割買いを判断。",
        candidates="候補: `VOO`, `VT`, `QQQM`, `SOXX`, `IAU`",
    )
    await send_message(embed=embed)

        # 複合シグナル
        cond_stage_1 = cond_sp500_10 and cond_vix_25
        cond_stage_2 = cond_sp500_20 and cond_vix_30
        cond_stage_3 = cond_sp500_30 and cond_vix_40

        reset_alert_if_recovered("stage_1", cond_stage_1)
        reset_alert_if_recovered("stage_2", cond_stage_2)
        reset_alert_if_recovered("stage_3", cond_stage_3)

if cond_stage_1 and not alert_state["stage_1"]:
    alert_state["stage_1"] = True

    embed = build_market_embed(
        title="🟡 買いシグナル 第1段階",
        description="条件: S&P500 -10% かつ VIX 25以上",
        drawdown_info=drawdown_info,
        vix_value=vix_value,
        btc_price=btc_price,
        color=0xF1C40F,
        action="試し玉の段階。予定資金の20%までで分割開始。",
        candidates="`VOO`, `VT`",
    )
    await send_message(embed=embed)

if cond_stage_2 and not alert_state["stage_2"]:
    alert_state["stage_2"] = True

    embed = build_market_embed(
        title="🟠 買いシグナル 第2段階",
        description="条件: S&P500 -20% かつ VIX 30以上",
        drawdown_info=drawdown_info,
        vix_value=vix_value,
        btc_price=btc_price,
        color=0xE67E22,
        action="本格買いの段階。予定資金の40%を目安に投入。",
        candidates="`VOO`, `VT`, `QQQM`, `IAU`",
    )
    await send_message(embed=embed)

if cond_stage_3 and not alert_state["stage_3"]:
    alert_state["stage_3"] = True

    embed = build_market_embed(
        title="🔴 買いシグナル 第3段階",
        description="条件: S&P500 -30% かつ VIX 40以上",
        drawdown_info=drawdown_info,
        vix_value=vix_value,
        btc_price=btc_price,
        color=0xE74C3C,
        action="最大チャンス帯。残り資金を分割で投入。",
        candidates="`VOO`, `VT`, `QQQM`, `SOXX`",
    )
    await send_message(embed=embed)

    except Exception as e:
        print(f"market_check_loop error: {e}")


@bot.command(name="status")
async def status_command(ctx):
    try:
        drawdown_info = calculate_sp500_drawdown()
        vix_value = get_latest_vix()
        btc_price = get_latest_btc()

        embed = build_market_embed(
            title="📊 マーケット監視レポート",
            description="現在のマーケット状況です。",
            drawdown_info=drawdown_info,
            vix_value=vix_value,
            btc_price=btc_price,
            color=0x3498DB,
        )
        await ctx.send(embed=embed)

    except Exception as e:
        await ctx.send(f"データ取得エラー: {e}")


@bot.command(name="helpme")
async def helpme_command(ctx):
    await ctx.send(
        "**使えるコマンド**\n"
        "`!status` : 現在のマーケット状況を表示\n"
        "`!helpme` : コマンド一覧を表示"
    )


bot.run(DISCORD_TOKEN)
