# -*- coding: utf-8 -*-
"""
OKX 선물(USDT-SWAP) EMA 교차 알림
- 거래대금 상위 100개 코인을 한 번 훑고 끝남 (GitHub Actions가 5분마다 실행)
- 15분봉 기준: 횡보 뒤 EMA7/EMA20 교차 + 거래대금 폭발이면 텔레그램으로 차트 전송
- 내 PC에서 계속 돌리려면: python cross_alert.py --loop  (60초마다 반복)
"""

import io
import json
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
import matplotlib
matplotlib.use("Agg")  # 창을 띄우지 않고 이미지로만 그림
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.ticker import FuncFormatter
from matplotlib import patheffects

# 텔레그램 정보는 파일에 쓰지 않고 환경변수(GitHub Secrets)에서 읽음
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ----- 설정값 -----
OKX = "https://www.okx.com"
TOP_N = 100              # 거래대금 상위 몇 개
BAR = "15m"              # 봉 단위
CANDLE_LIMIT = 200       # 불러올 봉 개수 (EMA50 계산 여유분 포함)
SCAN_INTERVAL = 60       # --loop 모드에서 몇 초마다 훑을지
SIDEWAYS_BARS = 12       # 횡보 판단 봉 수
SIDEWAYS_PCT = 0.005     # EMA들이 가격의 0.5% 안에 붙어 있어야 횡보
VOL_AVG_BARS = 20        # 거래대금 평균 봉 수
VOL_MULT = 1.5           # 평균의 몇 배면 폭발로 볼지
MAX_DIST_PCT = 0.007     # 현재 가격이 EMA20에서 0.7% 넘게 벗어나 있으면 제외 (이미 많이 달린 경우)
READY_ENABLED = False    # 준비 알림 켜기/끄기 (True로 바꾸면 다시 켜짐)
READY_PCT = 0.0015       # EMA7이 EMA20에 가격의 0.15% 이내로 다가오면 준비 알림
TP_RR = 1.5              # 익절거리 = 손절거리의 1.5배
MAX_RISK_PCT = 0.02      # 손절거리가 진입가의 2%를 넘으면 '패스'로 표시
CHART_BARS = 80          # 차트에 보여줄 봉 개수
KST = timezone(timedelta(hours=9))
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sent_state.json")
STATE_KEEP_MS = 2 * 60 * 60 * 1000  # 2시간 지난 알림 기록은 지움

session = requests.Session()


# ----- 한글 폰트 설정 -----
def setup_korean_font():
    installed = {f.name for f in font_manager.fontManager.ttflist}
    for name in ["Malgun Gothic", "AppleGothic", "NanumGothic", "Noto Sans CJK KR", "Noto Sans KR"]:
        if name in installed:
            plt.rcParams["font.family"] = name
            break
    else:
        print("[경고] 한글 폰트를 찾지 못했습니다. 차트 한글이 깨질 수 있습니다.")
    plt.rcParams["axes.unicode_minus"] = False  # 마이너스 기호 깨짐 방지


# ----- OKX 데이터 -----
def get_top_symbols(n=TOP_N):
    """USDT-SWAP 중 24시간 거래대금(USDT) 상위 n개"""
    r = session.get(f"{OKX}/api/v5/market/tickers", params={"instType": "SWAP"}, timeout=10)
    r.raise_for_status()
    rows = []
    for t in r.json()["data"]:
        if not t["instId"].endswith("-USDT-SWAP"):
            continue
        try:
            # volCcy24h = 코인 수량 기준 거래량 → 가격을 곱해 USDT 거래대금으로 환산
            turnover = float(t["volCcy24h"]) * float(t["last"])
        except (ValueError, TypeError):
            continue
        rows.append((t["instId"], turnover))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [inst for inst, _ in rows[:n]]


def get_candles(inst_id):
    """15분봉을 오래된 것 → 최신 순으로 반환 (마지막 봉은 진행 중인 현재 봉)"""
    r = session.get(
        f"{OKX}/api/v5/market/candles",
        params={"instId": inst_id, "bar": BAR, "limit": CANDLE_LIMIT},
        timeout=10,
    )
    r.raise_for_status()
    candles = []
    for c in reversed(r.json()["data"]):  # OKX는 최신 봉이 먼저 옴
        candles.append({
            "ts": int(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "quote_vol": float(c[7]),  # 거래대금 (USDT)
        })
    return candles


# ----- 지표 / 신호 -----
def ema(values, period):
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values, period):
    """RSI (와일더 방식). 계산이 안 되는 앞부분은 nan"""
    out = [float("nan")] * len(values)
    if len(values) <= period:
        return out
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    avg_gain = sum(max(c, 0) for c in changes[:period]) / period
    avg_loss = sum(max(-c, 0) for c in changes[:period]) / period
    for i in range(period, len(values)):
        if i > period:
            c = changes[i - 1]
            avg_gain = (avg_gain * (period - 1) + max(c, 0)) / period
            avg_loss = (avg_loss * (period - 1) + max(-c, 0)) / period
        out[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def macd(values, fast=12, slow=26, signal=9):
    """MACD선, 시그널선, 히스토그램"""
    line = [a - b for a, b in zip(ema(values, fast), ema(values, slow))]
    sig = ema(line, signal)
    return line, sig, [a - b for a, b in zip(line, sig)]


def check_signal(candles):
    """'롱진입' / '숏진입' / '롱준비' / '숏준비' / None(관망)"""
    need = max(50, SIDEWAYS_BARS, VOL_AVG_BARS) + 2
    if len(candles) < need:
        return None, None

    closes = [c["close"] for c in candles]
    vols = [c["quote_vol"] for c in candles]
    e7, e20, e50 = ema(closes, 7), ema(closes, 20), ema(closes, 50)

    # (1) 횡보: 현재 봉 직전 12봉 동안 EMA7·20·50 간격이 가격의 0.5% 이내
    for i in range(-SIDEWAYS_BARS - 1, -1):
        spread = max(e7[i], e20[i], e50[i]) - min(e7[i], e20[i], e50[i])
        if spread / closes[i] > SIDEWAYS_PCT:
            return None, None

    # 가격이 EMA20에서 0.7% 넘게 벗어나 있으면 제외
    if abs(closes[-1] - e20[-1]) / e20[-1] > MAX_DIST_PCT:
        return None, None

    # (3) 거래대금 폭발: 현재 봉이 직전 20봉 평균의 1.5배 이상
    avg_vol = sum(vols[-VOL_AVG_BARS - 1:-1]) / VOL_AVG_BARS
    if avg_vol <= 0 or vols[-1] < avg_vol * VOL_MULT:
        return None, None

    # (2) 교차: 직전 봉 대비 현재 봉에서 EMA7이 EMA20을 뚫음
    if e7[-2] <= e20[-2] and e7[-1] > e20[-1]:
        signal = "롱진입"
    elif e7[-2] >= e20[-2] and e7[-1] < e20[-1]:
        signal = "숏진입"
    # 준비: 아직 교차 전인데 EMA7이 EMA20에 0.15% 이내로 다가오는 중 (간격이 줄어드는 방향)
    elif READY_ENABLED and abs(e7[-1] - e20[-1]) / closes[-1] <= READY_PCT:
        if e7[-1] < e20[-1] and e20[-1] - e7[-1] < e20[-2] - e7[-2]:
            signal = "롱준비"
        elif e7[-1] > e20[-1] and e7[-1] - e20[-1] < e7[-2] - e20[-2]:
            signal = "숏준비"
        else:
            return None, None
    else:
        return None, None

    info = {"e7": e7, "e20": e20, "e50": e50, "vol_ratio": vols[-1] / avg_vol}

    # 진입가 = 신호봉 종가, 손절가 = 횡보 구간(직전 12봉) 저점/고점, 익절가 = 손절거리 1.5배
    if signal.endswith("진입"):
        box = candles[-SIDEWAYS_BARS - 1:-1]
        entry = closes[-1]
        if signal == "롱진입":
            sl = min(c["low"] for c in box)
            risk = entry - sl
            tp = entry + risk * TP_RR
        else:
            sl = max(c["high"] for c in box)
            risk = sl - entry
            tp = entry - risk * TP_RR
        risk_pct = risk / entry
        info.update({
            "entry": entry, "sl": sl, "tp": tp, "risk_pct": risk_pct,
            # 손절가가 진입가 반대편에 있거나(risk<=0) 손절거리가 2% 넘으면 패스
            "pass": risk <= 0 or risk_pct > MAX_RISK_PCT,
        })
    return signal, info


def fmt_price(v, ref=None):
    """가격 크기(ref 기준, 없으면 v)에 맞춰 소수점 자리수 조절 → 같은 코인은 자리수 통일"""
    ref = ref or v
    if ref <= 0:
        return f"{v}"
    return f"{v:,.{max(2, 4 - math.floor(math.log10(ref)))}f}"


# ----- 차트 -----
def make_chart(inst_id, candles, signal, info):
    n = min(CHART_BARS, len(candles))
    cs = candles[-n:]
    e7, e20, e50 = info["e7"][-n:], info["e20"][-n:], info["e50"][-n:]
    x = list(range(n))
    up_color, down_color = "#26a69a", "#ef5350"

    # 칸 순서: 가격 / RSI / MACD / 거래대금
    fig, (ax1, ax_rsi, ax_macd, ax2) = plt.subplots(
        4, 1, figsize=(12, 11), sharex=True, gridspec_kw={"height_ratios": [4, 1.3, 1.3, 1.3]}
    )

    # 가격 (캔들)
    for i, c in enumerate(cs):
        color = up_color if c["close"] >= c["open"] else down_color
        ax1.vlines(i, c["low"], c["high"], color=color, linewidth=1)
        body_low = min(c["open"], c["close"])
        body_h = abs(c["close"] - c["open"]) or (c["high"] - c["low"]) * 0.01
        ax1.bar(i, body_h, bottom=body_low, color=color, width=0.6)

    ax1.plot(x, e7, label="EMA7", color="#E91E63", linewidth=1.6)   # 진한 핑크
    ax1.plot(x, e20, label="EMA20", color="#F9A825", linewidth=1.6)  # 진한 노랑
    ax1.plot(x, e50, label="EMA50", color="#43A047", linewidth=1.6)  # 초록
    # 진입가 / 손절가 / 익절가 선 (익절은 EMA50 초록과 헷갈리지 않게 더 짙은 초록 점선)
    if "entry" in info:
        for key, label, color in [("tp", "익절", "#1B5E20"), ("entry", "진입", "#212121"), ("sl", "손절", "#c62828")]:
            v = info[key]
            ax1.axhline(v, color=color, linestyle="--", linewidth=1.3)
            # 글자는 차트 오른쪽 바깥에 (x는 차트 비율, y는 가격)
            ax1.text(1.005, v, f"{label} {fmt_price(v, info['entry'])}", color=color, va="center", ha="left",
                     fontsize=10, fontweight="bold", transform=ax1.get_yaxis_transform(), clip_on=False)

    ax1.legend(loc="upper left")
    ax1.grid(alpha=0.3)
    ax1.set_ylabel("가격 (USDT)")

    # RSI (6, 12, 24)
    closes_all = [c["close"] for c in candles]
    for period, color in [(6, "#7B1FA2"), (12, "#1976D2"), (24, "#FF6F00")]:
        ax_rsi.plot(x, rsi(closes_all, period)[-n:], label=f"RSI{period}", color=color, linewidth=1.2)
    for level in (30, 70):
        ax_rsi.axhline(level, color="#9e9e9e", linestyle="--", linewidth=0.8)
    ax_rsi.set_ylim(0, 100)
    ax_rsi.set_yticks([30, 50, 70])
    ax_rsi.set_ylabel("RSI")
    ax_rsi.legend(loc="upper left", fontsize=8, ncol=3)
    ax_rsi.grid(alpha=0.3)

    # MACD (12, 26, 9)
    m_line, m_sig, m_hist = (s[-n:] for s in macd(closes_all))
    ax_macd.bar(x, m_hist, color=[up_color if h >= 0 else down_color for h in m_hist], width=0.6, alpha=0.6)
    ax_macd.plot(x, m_line, label="MACD", color="#1976D2", linewidth=1.2)
    ax_macd.plot(x, m_sig, label="Signal", color="#FF6F00", linewidth=1.2)
    ax_macd.axhline(0, color="#9e9e9e", linewidth=0.8)
    ax_macd.set_ylabel("MACD")
    ax_macd.legend(loc="upper left", fontsize=8, ncol=2)
    ax_macd.grid(alpha=0.3)

    # 거래대금 막대
    vol_colors = [up_color if c["close"] >= c["open"] else down_color for c in cs]
    ax2.bar(x, [c["quote_vol"] for c in cs], color=vol_colors, width=0.6)
    ax2.grid(alpha=0.3)
    ax2.set_ylabel("거래대금 (USDT)")
    ax2.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v / 1e6:,.0f}M"))

    # x축 시간 (한국시간)
    step = max(1, n // 8)
    ticks = x[::step]
    ax2.set_xticks(ticks)
    ax2.set_xticklabels(
        [datetime.fromtimestamp(cs[i]["ts"] / 1000, KST).strftime("%m-%d %H:%M") for i in ticks]
    )

    # 제목: 코인 이름 + 롱진입/숏진입 크게
    coin = inst_id.replace("-USDT-SWAP", "")
    if signal.endswith("준비"):
        # 준비 알림은 노란색 (흰 배경에서도 잘 보이게 검은 테두리)
        title = fig.suptitle(f"{coin}  {signal}", fontsize=28, fontweight="bold", color="#FFD600")
        title.set_path_effects([patheffects.withStroke(linewidth=3, foreground="black")])
    elif info.get("pass"):
        fig.suptitle(f"{coin}  {signal}  (패스)", fontsize=28, fontweight="bold", color="#757575")
    else:
        title_color = up_color if signal == "롱진입" else down_color
        fig.suptitle(f"{coin}  {signal}", fontsize=28, fontweight="bold", color=title_color)

    fig.tight_layout(rect=[0, 0, 0.92, 0.95])  # 오른쪽은 가격선 글자 자리
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    buf.seek(0)
    return buf


# ----- 텔레그램 -----
def send_telegram(photo, caption):
    if not TOKEN or not CHAT_ID:
        print("  (TOKEN/CHAT_ID가 비어 있어 텔레그램 전송은 건너뜀)")
        return
    r = session.post(
        f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
        data={"chat_id": CHAT_ID, "caption": caption},
        files={"photo": ("chart.png", photo, "image/png")},
        timeout=20,
    )
    if not r.ok:
        print(f"  [텔레그램 오류] {r.status_code} {r.text}")


# ----- 보낸 알림 기록 (실행이 끝나도 기억하도록 파일에 저장) -----
def load_state():
    # 코인별로 마지막 알림을 보낸 봉의 시작 시각 → 같은 봉 중복 알림 방지
    # 준비/진입을 따로 기록해서, 준비 알림 뒤 교차가 확정되면 진입 알림도 나가게 함
    sent = {"진입": {}, "준비": {}}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        for kind in sent:
            sent[kind].update(saved.get(kind, {}))
    except (FileNotFoundError, ValueError):
        pass
    return sent


def save_state(sent):
    cutoff = time.time() * 1000 - STATE_KEEP_MS
    pruned = {kind: {k: ts for k, ts in d.items() if ts >= cutoff} for kind, d in sent.items()}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(pruned, f, ensure_ascii=False, indent=1)


# ----- 텔레그램 글 -----
def make_caption(inst_id, last, signal, info):
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    # 텔레그램 캡션은 글자색을 못 바꿔서 노란 동그라미로 표시
    head = f"🟡🟡 {signal} 🟡🟡" if signal.endswith("준비") else signal
    lines = [head, inst_id]
    if "entry" in info:
        entry, sl, tp = info["entry"], info["sl"], info["tp"]
        if info["pass"]:
            lines[0] = f"⛔ 패스 · {signal}"
        lines += [
            f"진입가: {fmt_price(entry)}",
            f"손절가: {fmt_price(sl, entry)} ({(sl - entry) / entry * 100:+.2f}%)",
            f"익절가: {fmt_price(tp, entry)} ({(tp - entry) / entry * 100:+.2f}%)",
        ]
        if info["pass"]:
            reason = ("손절가가 진입가 반대편" if info["risk_pct"] <= 0
                      else f"손절거리 {info['risk_pct'] * 100:.2f}% > {MAX_RISK_PCT * 100:.0f}%")
            lines.append(f"→ 패스 ({reason})")
    else:
        lines.append(f"가격: {fmt_price(last['close'])}")
    lines += [f"거래대금: 평균의 {info['vol_ratio']:.1f}배", f"{now} (15분봉)"]
    return "\n".join(lines)


# ----- 한 번 훑기 -----
def scan(sent):
    try:
        symbols = get_top_symbols()
    except Exception as e:
        print(f"[오류] 코인 목록 조회 실패: {e}")
        return

    hits = 0
    for inst_id in symbols:
        try:
            candles = get_candles(inst_id)
            signal, info = check_signal(candles)
            kind = signal[-2:] if signal else None  # "진입" 또는 "준비"
            if signal and sent[kind].get(inst_id) != candles[-1]["ts"]:
                last = candles[-1]
                caption = make_caption(inst_id, last, signal, info)
                print(f"[신호] {caption.replace(chr(10), ' | ')}")
                send_telegram(make_chart(inst_id, candles, signal, info), caption)
                sent[kind][inst_id] = last["ts"]
                hits += 1
        except Exception as e:
            print(f"[오류] {inst_id}: {e}")
        time.sleep(0.1)  # OKX 요청 제한 보호

    print(f"{datetime.now(KST):%H:%M:%S} 스캔 완료: {len(symbols)}개 확인, 신호 {hits}개")


def main():
    setup_korean_font()
    if not TOKEN or not CHAT_ID:
        print("[알림] 환경변수 TELEGRAM_TOKEN, TELEGRAM_CHAT_ID가 없어 텔레그램 전송 없이 화면에만 표시합니다.")

    sent = load_state()
    if "--loop" not in sys.argv:
        scan(sent)
        save_state(sent)
        return

    print("감시 시작 (종료: Ctrl+C)")
    while True:
        started = time.time()
        scan(sent)
        save_state(sent)
        time.sleep(max(0, SCAN_INTERVAL - (time.time() - started)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n종료합니다.")
