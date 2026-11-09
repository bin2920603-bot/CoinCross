# -*- coding: utf-8 -*-
"""
이평선알림 승률표 (과거 재현)
- 이평선알림(line_ma.py)과 같은 기준(1시간봉 종가가 EMA7·20·50·120·거래량가중100 위아래 0.1% 안, 같은 코인·선은 4시간에 1번)으로
  최근 며칠 동안 알림이 났을 자리를 다시 찾아냄
- 알림은 방향이 없어서, 각 자리에서 롱/숏을 둘 다 가정하고 익절가·손절가 중 어디에 먼저 닿는지 셈
  가정: 손절 = ATR(14)의 1.5배(진입가의 5% 넘으면 5%), 익절 = 손절거리의 1.5배, 24시간 안에 안 닿으면 '시간초과'(승률에서 뺌)
  같은 봉에서 둘 다 닿으면 손절로 침(보수적으로)
- 승률 차트 2장을 텔레그램으로 보냄. line_ma.py·cross_alert.py는 하나도 안 바꿈
"""
import io
import os
import time
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import cross_alert as ca
import line_ma as L

DAYS = int(os.environ.get("SCORE_DAYS", "30"))      # 최근 며칠치를 볼지
TOP_N = int(os.environ.get("SCORE_TOP", "100"))     # 거래대금 상위 몇 개 코인
WARM = 220                                          # 선 계산용 앞쪽 여유 봉
MAX_WAIT = 24                                       # 이 봉 수(1시간봉 24개=24시간) 안에 안 닿으면 시간초과
ATR_MULT = 1.5
MAX_RISK = 0.05
RR = 1.5
BE = 40                                             # 손익분기 승률(%) (익절=손절x1.5)
SURGE = 1.5                                         # 거래량 필터: 직전 20봉 평균의 1.5배 이상
COOLDOWN = L.COOLDOWN_H                             # 같은 코인·선은 이 봉 수(시간) 안에 한 번만
LINE_NAMES = [n for n, _ in L.LINES]


def get_hist(inst, bars):
    """마감된 1시간봉을 오래된 -> 최신 순으로 bars개 가까이"""
    out, after = [], None
    while len(out) < bars:
        p = {"instId": inst, "bar": "1H", "limit": 100}
        if after:
            p["after"] = after
        r = ca.session.get(ca.OKX + "/api/v5/market/history-candles", params=p, timeout=15)
        r.raise_for_status()
        rows = r.json()["data"]
        if not rows:
            break
        for c in rows:
            if len(c) > 8 and c[8] == "0":
                continue
            out.append({"ts": int(c[0]), "open": float(c[1]), "high": float(c[2]), "low": float(c[3]),
                        "close": float(c[4]), "vol": float(c[6])})
        after = rows[-1][0]
        time.sleep(0.08)
    out.sort(key=lambda c: c["ts"])
    return out


def atr_series(cs, n=14):
    tr = []
    for i, c in enumerate(cs):
        pc = cs[i - 1]["close"] if i else c["close"]
        tr.append(max(c["high"] - c["low"], abs(c["high"] - pc), abs(c["low"] - pc)))
    out = [None] * len(cs)
    for i in range(n - 1, len(cs)):
        out[i] = sum(tr[i - n + 1:i + 1]) / n
    return out


def judge(cs, i, side, entry, risk):
    """i번 봉에서 진입. 'win' / 'loss' / None(시간초과)"""
    if side == "롱":
        sl, tp = entry - risk, entry + RR * risk
    else:
        sl, tp = entry + risk, entry - RR * risk
    for c in cs[i + 1:i + 1 + MAX_WAIT]:
        if side == "롱":
            hit_sl, hit_tp = c["low"] <= sl, c["high"] >= tp
        else:
            hit_sl, hit_tp = c["high"] >= sl, c["low"] <= tp
        if hit_sl:
            return "loss"
        if hit_tp:
            return "win"
    return None


def replay(cs, since_ts):
    """한 코인의 과거 알림 자리를 찾고 롱/숏 결과를 돌려줌. [{line, pos, slope, side, res, surge, rsi}, ...]"""
    if len(cs) < WARM:
        return []
    closes = [c["close"] for c in cs]
    series = {"EMA7": ca.ema(closes, 7), "EMA20": ca.ema(closes, 20), "EMA50": ca.ema(closes, 50),
              "EMA120": ca.ema(closes, 120), "거가중100": L.vwap_series(cs)}
    atr = atr_series(cs)
    rsi = ca.rsi(closes, 14)
    last_hit = {}
    out = []
    for i in range(WARM, len(cs) - 1):
        if cs[i]["ts"] < since_ts or atr[i] is None or rsi[i] != rsi[i]:
            continue
        price = closes[i]
        avg = sum(c["vol"] for c in cs[i - 20:i]) / 20
        surge = cs[i]["vol"] / avg if avg > 0 else 0
        for name, s in series.items():
            v, v5 = s[i], s[i - 5]
            if not v or not v5 or v <= 0:
                continue
            if abs(price - v) / v > L.NEAR:
                continue
            if i - last_hit.get(name, -999) < COOLDOWN:
                continue
            last_hit[name] = i
            risk = min(ATR_MULT * atr[i], MAX_RISK * price)
            for side in ("롱", "숏"):
                res = judge(cs, i, side, price, risk)
                if res:
                    out.append({"line": name, "pos": "위" if price >= v else "아래", "slope": "↑" if v > v5 else "↓",
                                "side": side, "res": res, "surge": surge, "rsi": rsi[i]})
    return out


def rate(rows):
    w = sum(r["res"] == "win" for r in rows)
    n = len(rows)
    return (w / n * 100 if n else 0), n


def follows(r):
    """추세를 따라가는 자리: 오르는 선 위에서 롱, 내리는 선 아래에서 숏"""
    return (r["side"] == "롱" and r["pos"] == "위" and r["slope"] == "↑") or \
           (r["side"] == "숏" and r["pos"] == "아래" and r["slope"] == "↓")


def rsi_ok(r):
    return r["rsi"] < 60 if r["side"] == "롱" else r["rsi"] > 40


def bar_chart(ax, labels, data, title):
    ys = list(range(len(labels)))[::-1]
    pct = [d[0] for d in data]
    ax.barh(ys, pct, color=["#2e9e44" if p >= BE else "#d62728" for p in pct])
    for y, d in zip(ys, data):
        ax.text(min(d[0], 85) + 1, y, f"{d[0]:.0f}%  ({d[1]}번)", va="center", fontsize=10)
    ax.set_yticks(ys)
    ax.set_yticklabels(labels, fontsize=10)
    ax.axvline(BE, color="gray", ls="--", lw=1)
    ax.set_xlim(0, 100)
    ax.set_title(title, fontsize=13, loc="left")
    ax.grid(alpha=0.25, axis="x")


def make_charts(rows, days):
    # 표 1: 선마다, 추세를 따라가는 자리의 승률
    labels, data = [], []
    for name in LINE_NAMES:
        for side, pos, sl, tag in (("롱", "위", "↑", "오르는 선 위 → 롱"), ("숏", "아래", "↓", "내리는 선 아래 → 숏")):
            sub = [r for r in rows if r["line"] == name and r["side"] == side and r["pos"] == pos and r["slope"] == sl]
            labels.append(f"{name} {tag}")
            data.append(rate(sub))
    fig, ax = plt.subplots(figsize=(11, 0.55 * len(labels) + 2.2))
    bar_chart(ax, labels, data, f"선별 승률 (최근 {days}일 · 추세 따라가는 자리 · 점선 {BE}% 넘으면 이득)")
    fig.tight_layout()
    b1 = io.BytesIO()
    fig.savefig(b1, format="png", dpi=90)
    plt.close(fig)
    b1.seek(0)
    # 표 2: 거름 효과 (모든 선 합쳐서, 추세 따라가는 자리만)
    fl = [r for r in rows if follows(r)]
    labels2, data2 = [], []
    for side in ("롱", "숏"):
        base = [r for r in fl if r["side"] == side]
        for tag, f in (("전체", lambda r: True), ("거래량 1.5배↑", lambda r: r["surge"] >= SURGE),
                       ("RSI 안 과열", rsi_ok), ("거래량+RSI", lambda r: r["surge"] >= SURGE and rsi_ok(r))):
            labels2.append(f"{side} · {tag}")
            data2.append(rate([r for r in base if f(r)]))
    fig, ax = plt.subplots(figsize=(11, 0.55 * len(labels2) + 2.2))
    bar_chart(ax, labels2, data2, f"거름 효과 (모든 선 합침 · 최근 {days}일 · 점선 {BE}% 넘으면 이득)")
    fig.tight_layout()
    b2 = io.BytesIO()
    fig.savefig(b2, format="png", dpi=90)
    plt.close(fig)
    b2.seek(0)
    return b1, b2


def main():
    ca.setup_korean_font()
    since = (time.time() - DAYS * 86400) * 1000
    bars = DAYS * 24 + WARM + MAX_WAIT + 10
    symbols = ca.get_top_symbols(TOP_N)
    rows = []
    for k, inst in enumerate(symbols, 1):
        try:
            rows += replay(get_hist(inst, bars), since)
        except Exception as e:
            print(f"[오류] {inst}: {e}")
        if k % 10 == 0:
            print(f"{k}/{len(symbols)} 코인 완료, 자리 {len(rows)}개")
    print("총", len(rows), "개")
    if not rows:
        return
    b1, b2 = make_charts(rows, DAYS)
    cap = (f"📊 이평선알림 승률표 (최근 {DAYS}일 재현)\n"
           f"가정: 손절 ATR×1.5 · 익절 손절×1.5 · 24시간 · 점선 {BE}% 넘으면 이득")
    ca.send_telegram(b1, cap)
    ca.send_telegram(b2, "")


if __name__ == "__main__":
    main()
