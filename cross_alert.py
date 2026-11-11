# -*- coding: utf-8 -*-
"""
OKX 선물(USDT-SWAP) EMA 교차 알림
- 거래대금 상위 100개 코인을 한 번 훑고 끝남 (GitHub Actions가 5분마다 실행)
- 15분봉 기준: 횡보 뒤 EMA7/EMA50 교차 + 거래대금 폭발이면 텔레그램으로 차트 전송
- 차트는 일봉·4시간봉·1시간봉·15분봉 4개를 사진 한 장에 같이 보냄 (노란 추세선 포함)
- 내 PC에서 계속 돌리려면: python cross_alert.py --loop  (60초마다 반복)
- 차트/전송 확인용 [테스트] 사진 보내기: python cross_alert.py --test
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
BAR = "15m"              # 봉 단위 (신호 판단 기준)
CANDLE_LIMIT = 200       # 불러올 봉 개수 (EMA50 계산 여유분 포함)
SCAN_INTERVAL = 60       # --loop 모드에서 몇 초마다 훑을지
SIDEWAYS_BARS = 12       # 횡보 판단 봉 수
SIDEWAYS_PCT = 0.005     # EMA들이 가격의 0.5% 안에 붙어 있어야 횡보
VOL_AVG_BARS = 20        # 거래대금 평균 봉 수
VOL_MULT = 1.5           # 평균의 몇 배면 폭발로 볼지
MAX_DIST_PCT = 0.007     # 현재 가격이 EMA20에서 0.7% 넘게 벗어나 있으면 제외 (이미 많이 달린 경우)
READY_ENABLED = False    # 준비 알림 켜기/끄기 (True로 바꾸면 다시 켜짐)
SKIP_SAME_FLOW = True     # 일봉·4시간봉 큰 흐름이 알림 방향과 둘 다 같으면 알림 안 보냄 (승률 35%로 낮았음. False면 다시 보냄)
READY_PCT = 0.0015       # EMA7이 EMA50에 가격의 0.15% 이내로 다가오면 준비 알림
TP_RR = 1.5              # 익절거리 = 손절거리의 1.5배
MAX_RISK_PCT = 0.02      # 손절거리가 진입가의 2%를 넘으면 '패스'로 표시
CHART_BARS = 120         # 차트에 보여줄 봉 개수
TREND_EXT = 20           # 추세선을 오른쪽으로 몇 봉 더 늘려 그릴지
TREND_K = 2              # 추세선 고점/저점 판단: 좌우 몇 봉보다 높아야(낮아야) 고점(저점)으로 볼지
TREND_TOL = 0.03         # 꼬리가 선을 넘어도 봐주는 여유 (차트 높이의 3%)
TREND_BAD = 1            # 선을 넘은 봉이 이 개수 이하면 인정
TREND_MIN_GAP = 8        # 선을 긋는 두 점 사이 최소 봉 수
TREND_COLOR = "#FFC107"  # 추세선 색 (노란색)
# 새 모양 (거래대금30·전체 일봉과 같은 표시 — 크로스알림 4칸 카드에만 적용)
ZPIVOT = 5               # 지지·저항 박스: 앞뒤 5봉보다 높(낮)으면 고점(저점)
ZONE_PCT = 0.015         # 가격이 이 비율 안에 모이면 같은 박스로 묶음
SHIFT = 26               # 일목 구름이 앞으로 나가는 봉 수
RSI_LOW = 30             # RSI 이 값 이하면 과매도 ▲
RSI_HIGH = 70            # RSI 이 값 이상이면 과매수 ▼
# 차트 4개 (왼쪽 위 → 오른쪽 아래 순서). 마지막 15분봉이 신호 기준이라 진입·손절·익절 선이 여기에만 그려짐
CHART_FRAMES = [
    ("일봉", "1D", "%m-%d"),
    ("4시간봉", "4H", "%m-%d %H시"),
    ("1시간봉", "1H", "%m-%d %H시"),
    ("15분봉", "15m", "%m-%d %H:%M"),
]
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


def get_candles(inst_id, bar=BAR):
    """봉을 오래된 것 → 최신 순으로 반환 (마지막 봉은 진행 중인 현재 봉)"""
    r = session.get(
        f"{OKX}/api/v5/market/candles",
        params={"instId": inst_id, "bar": bar, "limit": CANDLE_LIMIT},
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

    # (2) 교차: 직전 봉 대비 현재 봉에서 EMA7이 EMA50을 뚫음 (위로 = 롱진입, 아래로 = 숏진입, 그 외 = 관망)
    if e7[-2] <= e50[-2] and e7[-1] > e50[-1]:
        signal = "롱진입"
    elif e7[-2] >= e50[-2] and e7[-1] < e50[-1]:
        signal = "숏진입"
    # 준비: 아직 교차 전인데 EMA7이 EMA50에 0.15% 이내로 다가오는 중 (간격이 줄어드는 방향)
    elif READY_ENABLED and abs(e7[-1] - e50[-1]) / closes[-1] <= READY_PCT:
        if e7[-1] < e50[-1] and e50[-1] - e7[-1] < e50[-2] - e7[-2]:
            signal = "롱준비"
        elif e7[-1] > e50[-1] and e7[-1] - e50[-1] < e7[-2] - e50[-2]:
            signal = "숏준비"
        else:
            return None, None
    else:
        return None, None

    info = {"e7": e7, "e20": e20, "e50": e50, "vol_ratio": vols[-1] / avg_vol}

    if signal.endswith("진입"):
        info.update(trade_levels(candles, signal))
    return signal, info


def trade_levels(candles, signal):
    """진입가 = 신호봉 종가, 손절가 = 횡보 구간(직전 12봉) 저점/고점, 익절가 = 손절거리 1.5배"""
    box = candles[-SIDEWAYS_BARS - 1:-1]
    entry = candles[-1]["close"]
    if signal == "롱진입":
        sl = min(c["low"] for c in box)
        risk = entry - sl
        tp = entry + risk * TP_RR
    else:
        sl = max(c["high"] for c in box)
        risk = sl - entry
        tp = entry - risk * TP_RR
    risk_pct = risk / entry
    return {
        "entry": entry, "sl": sl, "tp": tp, "risk_pct": risk_pct,
        # 손절가가 진입가 반대편에 있거나(risk<=0) 손절거리가 2% 넘으면 패스
        "pass": risk <= 0 or risk_pct > MAX_RISK_PCT,
    }


def fmt_price(v, ref=None):
    """가격 크기(ref 기준, 없으면 v)에 맞춰 소수점 자리수 조절 → 같은 코인은 자리수 통일"""
    ref = ref or v
    if ref <= 0:
        return f"{v}"
    return f"{v:,.{max(2, 4 - math.floor(math.log10(ref)))}f}"


def fmt_vol(v, _=None):
    """거래대금 축 글자: K / M / B 로 읽기 쉽게"""
    if abs(v) >= 1e9:
        return f"{v / 1e9:,.1f}B"
    if abs(v) >= 1e6:
        return f"{v / 1e6:,.1f}M"
    if abs(v) >= 1e3:
        return f"{v / 1e3:,.0f}K"
    return f"{v:,.0f}"


# ----- 추세선 -----
def find_swings(values, k, high=True):
    """좌우 k봉보다 높은(낮은) 봉의 위치 목록 = 고점(저점)"""
    out = []
    for i in range(k, len(values) - k):
        window = values[i - k:i + k + 1]
        if (high and values[i] == max(window)) or ((not high) and values[i] == min(window)):
            out.append(i)
    return out


def find_trendline(cs, kind):
    """kind='down': 고점들을 이은 하락 추세선, kind='up': 저점들을 이은 상승 추세선.
    꼬리가 선을 살짝 넘는 건 봐주고, 넘은 봉이 TREND_BAD개 이하인 선 중 가장 길게 이어진 선을 고름.
    돌려주는 값: (시작 위치, 시작 가격, 끝 위치, 끝 가격)"""
    n = len(cs)
    highs = [c["high"] for c in cs]
    lows = [c["low"] for c in cs]
    tol = TREND_TOL * (max(highs) - min(lows))
    if kind == "down":
        vals = highs
        swings = find_swings(vals, TREND_K, True)
    else:
        vals = lows
        swings = find_swings(vals, TREND_K, False)
    best = None
    for b in swings[-4:]:
        for a in swings:
            if b - a < TREND_MIN_GAP:
                break
            if kind == "down" and vals[a] <= vals[b]:
                continue
            if kind == "up" and vals[a] >= vals[b]:
                continue
            slope = (vals[b] - vals[a]) / (b - a)
            bad = 0
            for x in range(a, n - 1):    # 마지막(진행 중) 봉은 제외
                line = vals[a] + slope * (x - a)
                if kind == "down" and vals[x] > line + tol:
                    bad += 1
                if kind == "up" and vals[x] < line - tol:
                    bad += 1
            if bad > TREND_BAD:
                continue
            span = b - a
            if best is None or span > best[0]:
                best = (span, a, vals[a], n - 1, vals[a] + slope * (n - 1 - a))
    return best[1:] if best else None


def draw_trendlines(ax, cs):
    """가격 차트에 노란 추세선(하락선·상승선)을 그림. 선은 오른쪽으로 TREND_EXT봉 더 늘려 그림"""
    first = True
    n = len(cs)
    for kind in ("down", "up"):
        t = find_trendline(cs, kind)
        if t:
            x0, y0, x1, y1 = t
            slope = (y1 - y0) / (x1 - x0) if x1 != x0 else 0
            x_end = n - 1 + TREND_EXT
            ax.plot([x0, x_end], [y0, y0 + slope * (x_end - x0)], color=TREND_COLOR, linewidth=3.0,
                    label="추세선" if first else None)
            first = False


def day_stats(cs):
    """오늘(OKX 일봉 기준, 한국시간 01시 시작) 시가·고가·저가. 현재가는 마지막 봉 종가"""
    H = 3600000
    day = 24 * H
    start = (cs[-1]["ts"] + 8 * H) // day * day - 8 * H
    today = [c for c in cs if c["ts"] >= start]
    if not today:
        return None
    return {"시가": today[0]["open"], "고가": max(c["high"] for c in today),
            "저가": min(c["low"] for c in today), "현재가": cs[-1]["close"]}


def draw_day_lines(ax, candles, n):
    """오늘 시가·고가·저가·현재가를 연한 회색 점선 + 작은 글자로 (글자는 오른쪽 빈 자리에)"""
    ds = day_stats(candles)
    if not ds:
        return
    grey, txt = "#90a4ae", "#607d8b"
    items = sorted(ds.items(), key=lambda t: -t[1])
    for _, v in items:
        ax.axhline(v, color=grey, linewidth=1.0, linestyle="--", alpha=0.8)
    y0, y1 = ax.get_ylim()
    gap = (y1 - y0) * 0.07
    pos = []
    for _, v in items:
        pos.append(v if not pos else min(v, pos[-1] - gap))
    for (name, v), y in zip(items, pos):
        ax.annotate(f"{name} {fmt_price(v)}", xy=(n + 1, v), xytext=(n + 2, y), xycoords="data", textcoords="data",
                    fontsize=8, color=txt, va="center", ha="left", fontweight="bold", annotation_clip=False,
                    arrowprops=dict(arrowstyle="-", color=grey, linewidth=0.7, shrinkA=0, shrinkB=0))


# ----- 새 모양: 지지·저항 박스, 일목 구름, 하이킨아시, 과매도/과매수 -----
def make_zones(pts, price, above):
    """비슷한 가격의 고/저점을 묶어 박스로 만듦. 현재가 위(저항)/아래(지지) 가까운 2개만"""
    groups = []
    for _, p in sorted(pts, key=lambda t: t[1]):
        if groups and p <= groups[-1][0] * (1 + ZONE_PCT):
            groups[-1][1].append(p)
            groups[-1][0] = p
        else:
            groups.append([p, [p]])
    zs = [(min(g[1]), max(g[1])) for g in groups]
    if above:
        zs = [z for z in zs if z[0] > price]
        zs.sort(key=lambda z: z[0])
    else:
        zs = [z for z in zs if z[1] < price]
        zs.sort(key=lambda z: -z[1])
    return zs[:2]


def mid_hl(cs, i, n):
    """i번째 봉까지 n봉의 (최고가+최저가)/2"""
    w = cs[max(0, i - n + 1):i + 1]
    return (max(c["high"] for c in w) + min(c["low"] for c in w)) / 2


def draw_cloud(ax, cs, n):
    """일목 구름: 선행스팬A=(전환선9+기준선26)/2, 선행스팬B=52봉 중간값. 26봉 앞으로 밀어서 그림.
    파랑 구름 = A가 위(상승 구름) / 주황 구름 = B가 위(하락 구름)"""
    total = len(cs)
    first = total - n
    xs, a_vals, b_vals = [], [], []
    for j in range(n + SHIFT):
        src = first + j - SHIFT
        if src < 51 or src > total - 1:
            continue
        a_vals.append((mid_hl(cs, src, 9) + mid_hl(cs, src, 26)) / 2)
        b_vals.append(mid_hl(cs, src, 52))
        xs.append(j)
    if len(xs) < 2:
        return
    ax.fill_between(xs, a_vals, b_vals, where=[a >= b for a, b in zip(a_vals, b_vals)],
                    color="#42a5f5", alpha=0.16, linewidth=0, interpolate=True, zorder=0)
    ax.fill_between(xs, a_vals, b_vals, where=[a < b for a, b in zip(a_vals, b_vals)],
                    color="#ffa726", alpha=0.18, linewidth=0, interpolate=True, zorder=0)


def heikin(cs):
    """하이킨아시 캔들: 종가=(시+고+저+종)/4, 시가=앞 하이킨아시 봉의 (시+종)/2"""
    out = []
    for i, c in enumerate(cs):
        hc = (c["open"] + c["high"] + c["low"] + c["close"]) / 4
        ho = (c["open"] + c["close"]) / 2 if i == 0 else (out[-1][0] + out[-1][3]) / 2
        out.append((ho, max(c["high"], ho, hc), min(c["low"], ho, hc), hc))
    return out


def draw_ha(ax_ha, candles, n, up_color, down_color):
    """가격 차트 바로 아래 작은 칸에 하이킨아시 캔들을 그림"""
    for i, (o, h, l, c) in enumerate(heikin(candles)[-n:]):
        color = up_color if c >= o else down_color
        ax_ha.vlines(i, l, h, color=color, linewidth=0.9)
        ax_ha.bar(i, abs(c - o) or (h - l) * 0.01, bottom=min(o, c), color=color, width=0.6)


def draw_look(ax, candles, n):
    """가격 차트에 지지(초록)·저항(빨강) 박스, 일목 구름, 과매도▲/과매수▼, 정배열/역배열 글자를 얹음
    (진입·손절·익절 선과 노란 추세선은 그대로)"""
    view = candles[-n:]
    price = view[-1]["close"]
    y0, y1 = ax.get_ylim()
    hi, lo = [], []
    for i in range(ZPIVOT, len(view) - ZPIVOT):
        w = view[i - ZPIVOT:i + ZPIVOT + 1]
        if view[i]["high"] >= max(c["high"] for c in w):
            hi.append((i, view[i]["high"]))
        if view[i]["low"] <= min(c["low"] for c in w):
            lo.append((i, view[i]["low"]))
    for zl, zh in make_zones(lo, price, False):
        ax.axhspan(zl * 0.997, zh * 1.003, color="#2e7d32", alpha=0.18, zorder=0)
    for zl, zh in make_zones(hi, price, True):
        ax.axhspan(zl * 0.997, zh * 1.003, color="#c62828", alpha=0.18, zorder=0)
    closes = [c["close"] for c in candles]
    rs = rsi(closes, 14)[-n:]
    gap = (y1 - y0) * 0.025
    lx, ly, hx, hy = [], [], [], []
    for i, c in enumerate(view):
        if rs[i] != rs[i]:          # nan
            continue
        if rs[i] <= RSI_LOW:
            lx.append(i)
            ly.append(c["low"] - gap)
        elif rs[i] >= RSI_HIGH:
            hx.append(i)
            hy.append(c["high"] + gap)
    if lx:
        ax.scatter(lx, ly, marker="^", s=45, color="#1565c0", zorder=5)
    if hx:
        ax.scatter(hx, hy, marker="v", s=45, color="#d50000", zorder=5)
    draw_cloud(ax, candles, n)
    e20, e50, e120 = ema(closes, 20)[-1], ema(closes, 50)[-1], ema(closes, 120)[-1]
    if e20 > e50 > e120:
        txt, col = "정배열 ▲", "#2e7d32"
    elif e20 < e50 < e120:
        txt, col = "역배열 ▼", "#c62828"
    else:
        txt, col = "혼조 ─", "#555555"
    ax.text(0.99, 0.97, txt, transform=ax.transAxes, ha="right", va="top", fontsize=14,
            fontweight="bold", color=col)
    ax.set_ylim(y0, y1)                       # 박스·구름 때문에 차트 크기가 안 바뀌게 고정
    ax.set_xlim(-1, n + SHIFT + 2)            # 구름이 앞으로 나가는 만큼 오른쪽 여백


# ----- 차트 -----
def draw_frame(fig, spec, label, candles, date_fmt, info=None, look=False):
    """차트 1개(가격+EMA / RSI / MACD / 거래대금 4칸)를 spec 자리에 그림.
    info에 진입가가 있으면 진입·손절·익절 선도 같이 그림 (15분봉 칸에서만 사용)
    look=True면 새 모양(박스·구름·▲▼·하이킨아시 칸)을 얹음 (크로스알림 4칸 카드만 True, 나머지는 예전 그대로)"""
    n = min(CHART_BARS, len(candles))
    cs = candles[-n:]
    closes_all = [c["close"] for c in candles]
    e7 = ema(closes_all, 7)[-n:]
    e20 = ema(closes_all, 20)[-n:]
    e50 = ema(closes_all, 50)[-n:]
    x = list(range(n))
    up_color, down_color = "#26a69a", "#ef5350"

    ax_ha = None
    if look:
        gs = spec.subgridspec(5, 1, height_ratios=[5.2, 1.0, 1.0, 1.0, 1.0], hspace=0.12)
        ax1 = fig.add_subplot(gs[0])
        ax_ha = fig.add_subplot(gs[1], sharex=ax1)       # 하이킨아시 작은 칸
        ax_rsi = fig.add_subplot(gs[2], sharex=ax1)
        ax_macd = fig.add_subplot(gs[3], sharex=ax1)
        ax2 = fig.add_subplot(gs[4], sharex=ax1)
    else:
        gs = spec.subgridspec(4, 1, height_ratios=[4, 1.3, 1.3, 1.3], hspace=0.12)
        ax1 = fig.add_subplot(gs[0])
        ax_rsi = fig.add_subplot(gs[1], sharex=ax1)
        ax_macd = fig.add_subplot(gs[2], sharex=ax1)
        ax2 = fig.add_subplot(gs[3], sharex=ax1)
    all_axes = [ax1, ax_rsi, ax_macd, ax2] + ([ax_ha] if ax_ha is not None else [])
    for a in [ax1, ax_rsi, ax_macd] + ([ax_ha] if ax_ha is not None else []):
        plt.setp(a.get_xticklabels(), visible=False)

    # 가격 (캔들)
    for i, c in enumerate(cs):
        color = up_color if c["close"] >= c["open"] else down_color
        ax1.vlines(i, c["low"], c["high"], color=color, linewidth=1)
        body_low = min(c["open"], c["close"])
        body_h = abs(c["close"] - c["open"]) or (c["high"]
