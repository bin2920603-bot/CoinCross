# -*- coding: utf-8 -*-
"""
일봉 차트 위에 얹는 표시 (all_daily.py가 가져다 씀)
- 지지(초록)·저항(빨강) 박스, 추세선(점선), 정배열/역배열 글자
- 과매도(RSI 30 이하) 파란 ▲ / 과매수(RSI 70 이상) 빨간 ▼ 를 캔들 옆에 표시
- 하이킨아시 캔들은 메인 차트 바로 아래 작은 칸에 따로 표시 (실제 캔들·박스·▲▼는 그대로)
- 일목균형표 구름(선행스팬 A·B, 26일 앞까지): 파랑 구름 = A가 위(상승 구름) / 주황 구름 = B가 위(하락 구름)
"""
import cross_alert as ca

PIVOT = 5               # 앞뒤 5봉보다 높/낮으면 고점/저점으로 봄
ZONE_PCT = 0.015        # 가격이 이 비율 안에 모이면 같은 박스로 묶음
SHOW_BARS = 120
RSI_LOW = 30            # 이 값 이하면 과매도 ▲
RSI_HIGH = 70           # 이 값 이상이면 과매수 ▼
SHOW_CLOUD = True       # 일목 구름 표시 (끄려면 False)
SHIFT = 26              # 구름이 앞으로 나가는 봉 수
SHOW_HA = True          # 하이킨아시 작은 칸 표시 (끄려면 False)
HA_SHARE = 0.16         # 하이킨아시 칸이 차지하는 전체 높이 비율


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


def mid(cs, i, n):
    """i번째 봉까지 n봉의 (최고가+최저가)/2"""
    w = cs[max(0, i - n + 1):i + 1]
    return (max(c["high"] for c in w) + min(c["low"] for c in w)) / 2


def draw_cloud(ax, cs, n):
    """일목 구름: 선행스팬A=(전환선9+기준선26)/2, 선행스팬B=52봉 중간값. 둘 다 26봉 앞으로 밀어서 그림"""
    total = len(cs)
    first = total - n
    xs, a_vals, b_vals = [], [], []
    for j in range(n + SHIFT):
        src = first + j - SHIFT                      # 이 자리의 구름은 26봉 전 값으로 만든 것
        if src < 51 or src > total - 1:
            continue
        a_vals.append((mid(cs, src, 9) + mid(cs, src, 26)) / 2)
        b_vals.append(mid(cs, src, 52))
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


def add_ha(ax, cs, n):
    """메인 차트 바로 아래에 하이킨아시 작은 칸을 끼워 넣음 (나머지 칸은 비율대로 조금 줄임)"""
    fig = ax.figure
    axes = list(fig.axes)
    if len(axes) < 4:
        return
    pos = [a.get_position() for a in axes[:4]]
    top = pos[0].y1
    bottom = pos[3].y0
    gap = pos[0].y0 - pos[1].y1
    s = 1 - HA_SHARE
    free = (top - bottom) * (1 - s)
    # 메인은 위쪽 고정·높이만 줄이고, 나머지 칸은 아래 기준으로 비율대로 줄임
    axes[0].set_position([pos[0].x0, top - pos[0].height * s, pos[0].width, pos[0].height * s])
    for a, p in zip(axes[1:4], pos[1:4]):
        a.set_position([p.x0, bottom + (p.y0 - bottom) * s, p.width, p.height * s])
    v_top = bottom + (pos[1].y1 - bottom) * s
    ha_h = free * 0.78
    ha_ax = fig.add_axes([pos[0].x0, v_top + gap * s * 1.2, pos[0].width, ha_h], sharex=ax)
    ha = heikin(cs)[-n:]
    up, down = "#26a69a", "#ef5350"
    for i, (o, h, l, c) in enumerate(ha):
        col = up if c >= o else down
        ha_ax.vlines(i, l, h, color=col, linewidth=1)
        ha_ax.bar(i, abs(c - o) or (h - l) * 0.01, bottom=min(o, c), color=col, width=0.6)
    ha_ax.set_title("하이킨아시", fontsize=12, loc="left")
    ha_ax.grid(alpha=0.3)
    ha_ax.tick_params(labelbottom=False)


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
    if SHOW_CLOUD:
        draw_cloud(ax, cs, n)
        ax.set_xlim(-1, n + SHIFT + 2)                       # 구름이 앞으로 나가는 만큼 오른쪽 여백
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
    if SHOW_HA:
        add_ha(ax, cs, n)
