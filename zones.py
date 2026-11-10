# -*- coding: utf-8 -*-
"""
일봉 차트 위에 얹는 표시 (all_daily.py가 가져다 씀)
- 지지(초록)·저항(빨강) 박스, 추세선(점선), 정배열/역배열 글자
- 과매도(RSI 30 이하) 파란 ▲ / 과매수(RSI 70 이상) 빨간 ▼ 를 캔들 옆에 표시
"""
import cross_alert as ca

PIVOT = 5               # 앞뒤 5봉보다 높/낮으면 고점/저점으로 봄
ZONE_PCT = 0.015        # 가격이 이 비율 안에 모이면 같은 박스로 묶음
SHOW_BARS = 120
RSI_LOW = 30            # 이 값 이하면 과매도 ▲
RSI_HIGH = 70           # 이 값 이상이면 과매수 ▼


def pivots(view):
    hi, lo = [], []
    for i in range(PIVOT, len(view) - PIVOT):
        w = view[i - PIVOT:i + PIVOT + 1]
        if view[i]["high"] >= max(c["high"] for c in w):
            hi.append((i, view[i]["high"]))
        if view[i]["low"] <= min(c["low"] for c in w):
            lo.append((i, view[i]["low"]))
    return hi, lo


def make_zones(pts, price, above):
    """비슷한 가격의 고/저점을 묶어 박스로 만듦. 현재가 위(저항)/아래(지지) 가까운 2개만"""
    groups = []
    for _, p in sorted(pts, key=lambda t: t[1]):
        if groups and p <= groups[-1][0] * (1 + ZONE_PCT):
            groups[-1][1].append(p)
            groups[-1][0] = p
        else:
            groups.append([p, [p]])
    zones = [(min(g[1]), max(g[1]), len(g[1])) for g in groups]
    if above:
        zones = [z for z in zones if z[0] > price]
        zones.sort(key=lambda z: z[0])
    else:
        zones = [z for z in zones if z[1] < price]
        zones.sort(key=lambda z: -z[1])
    return zones[:2]


def overlay(ax, cs):
    n = min(SHOW_BARS, len(cs))
    view = cs[-n:]
    price = view[-1]["close"]
    hi, lo = pivots(view)
    y0, y1 = ax.get_ylim()
    for zl, zh, k in make_zones(lo, price, False):          # 지지: 초록 박스
        ax.axhspan(zl * 0.997, zh * 1.003, color="#2e7d32", alpha=0.18, zorder=0)
    for zl, zh, k in make_zones(hi, price, True):           # 저항: 빨강 박스
        ax.axhspan(zl * 0.997, zh * 1.003, color="#c62828", alpha=0.18, zorder=0)
    if len(lo) >= 2:                                         # 추세선: 마지막 저점 2개(초록 점선)
        (xa, ya), (xb, yb) = lo[-2], lo[-1]
        ax.plot([xa, n + 2], [ya, yb + (yb - ya) / (xb - xa) * (n + 2 - xb)], color="#2e7d32", linestyle="--", linewidth=1.3, zorder=1)
    if len(hi) >= 2:                                         # 추세선: 마지막 고점 2개(빨강 점선)
        (xa, ya), (xb, yb) = hi[-2], hi[-1]
        ax.plot([xa, n + 2], [ya, yb + (yb - ya) / (xb - xa) * (n + 2 - xb)], color="#c62828", linestyle="--", linewidth=1.3, zorder=1)
    ax.set_ylim(y0, y1)                                      # 박스 때문에 차트 크기가 안 바뀌게 고정
    rs = ca.rsi([c["close"] for c in cs], 14)[-n:]           # 과매도/과매수 표시
    gap = (y1 - y0) * 0.025
    for i, c in enumerate(view):
        if rs[i] is None:
            continue
        if rs[i] <= RSI_LOW:
            ax.scatter(i, c["low"] - gap, marker="^", s=60, color="#1565c0", zorder=5)
        elif rs[i] >= RSI_HIGH:
            ax.scatter(i, c["high"] + gap, marker="v", s=60, color="#d50000", zorder=5)
    ax.set_ylim(y0, y1)
    closes = [c["close"] for c in cs]
    e20, e50, e120 = ca.ema(closes, 20)[-1], ca.ema(closes, 50)[-1], ca.ema(closes, 120)[-1]
    if e20 > e50 > e120:
        txt, col = "정배열 ▲", "#2e7d32"
    elif e20 < e50 < e120:
        txt, col = "역배열 ▼", "#c62828"
    else:
        txt, col = "혼조 ─", "#555555"
    ax.text(0.99, 0.97, txt, transform=ax.transAxes, ha="right", va="top", fontsize=14, fontweight="bold", color=col)
