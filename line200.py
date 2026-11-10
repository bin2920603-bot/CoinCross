# -*- coding: utf-8 -*-
"""
OKX 선물 일봉 200일선(EMA200) 근처 알림
- 5분마다 거래대금 상위 코인의 일봉을 훑어서, 현재가가 EMA200 위아래 0.1% 안에 들어온 코인을 차트로 알려줌
- 큰 흐름·봉 색깔·선 기울기 같은 거름 조건 없음. 선 근처에 오면 전부 알림 (근접알림과 별도)
- cross_alert.py의 텔레그램 전송·지표 함수를 그대로 가져다 씀 (cross_alert.py는 안 바꿈)
- 차트: 캔들 + EMA200 + 거래대금 + RSI + MACD + 오늘 시가·고가·저가·현재가 + 매일 01시 일봉마감 표시
- 방 번호 찾기: python line200.py --find   /   차트 확인용 테스트: python line200.py --test
"""
import io
import json
import os
import sys
import time
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import cross_alert as ca

# ----- 설정값 (며칠 써 보고 여기 숫자만 고치면 됨) -----
TOP_N = 200            # 거래대금 상위 몇 개 코인을 볼지
NEAR = 0.001           # 현재가가 EMA200 위아래 0.1% 안이면 '근처'
PRE_NEAR = 0.03        # 1차 거름: 이 안(3%)에 든 코인만 정확한 계산을 위해 봉을 더 불러옴
BOUNCE = 0.002         # 직전 마감 일봉 꼬리가 선 0.2% 안까지 닿았다가 돌아서면 '튕김' 표시
HIST_BARS = 600        # 정확한 EMA200 계산에 쓰는 일봉 개수 (있는 만큼만 가져옴)
BAR = "1D"             # 일봉 (OKX 기본 일봉 = 한국시간 새벽 1시 마감). 1시간봉으로 되돌리려면 "1H"
BAR_H = 24             # 봉 하나의 시간(시간 단위). 일봉 24 / 1시간봉 1
COOLDOWN_H = 24        # 같은 코인은 이 시간 안에 다시 알리지 않음
MAX_PER_SCAN = 8       # 한 번에 보내는 최대 개수 (나머지는 다음 5분에)
SHOW_CAPTION = False   # False = 차트만 보냄 (글자 설명은 차트 맨 위 제목에 들어감) / True = 차트 아래 글자도 같이
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "line200_state.json")
CHAT = os.environ.get("LINE200_CHAT", "")
ca.CHAT_ID = CHAT      # cross_alert의 텔레그램 전송이 이 방으로 가게 함


def get_history(inst_id, bars, bar=BAR):
    """봉을 오래된 것 -> 최신 순으로 bars개 가까이 가져옴 (맨 끝은 진행 중인 현재 봉)"""
    out, after = [], None
    while len(out) < bars:
        p = {"instId": inst_id, "bar": bar, "limit": 100}
        if after:
            p["after"] = after
        r = ca.session.get(ca.OKX + "/api/v5/market/history-candles", params=p, timeout=15)
        r.raise_for_status()
        rows = r.json()["data"]
        if not rows:
            break
        for c in rows:
            out.append({"ts": int(c[0]), "open": float(c[1]), "high": float(c[2]),
                        "low": float(c[3]), "close": float(c[4]), "quote_vol": float(c[7])})
        after = rows[-1][0]
        time.sleep(0.08)
    out.sort(key=lambda c: c["ts"])
    return out


def merge_recent(hist, recent):
    """history-candles에는 아직 진행 중인 봉이 없을 수 있어서, 최신 봉은 candles에서 덮어씀"""
    by_ts = {c["ts"]: c for c in hist}
    for c in recent:
        by_ts[c["ts"]] = c
    return [by_ts[k] for k in sorted(by_ts)]


def analyze(candles, min_len=210):
    """EMA200 대비 현재 위치를 잼. 봉이 모자라면 None"""
    if len(candles) < min_len:
        return None
    closes = [c["close"] for c in candles]
    e200 = ca.ema(closes, 200)
    line, price = e200[-1], closes[-1]
    if line <= 0:
        return None
    gap = (price - line) / line
    prev = candles[-2]                                  # 직전에 마감된 일봉
    bounce = None
    if prev["low"] <= e200[-2] * (1 + BOUNCE) and prev["close"] > e200[-2] and prev["low"] < prev["close"]:
        bounce = "롱"                                   # 선을 꼬리로 찍고 위에서 마감
    if prev["high"] >= e200[-2] * (1 - BOUNCE) and prev["close"] < e200[-2]:
        bounce = "숏"                                   # 선을 꼬리로 찍고 아래에서 마감
    came = closes[-4] if len(closes) >= 4 else price
    came_from = "위에서 내려오는 중" if came > e200[-4] else "아래에서 올라오는 중"
    return {"price": price, "line": line, "gap": gap, "bounce": bounce, "came_from": came_from,
            "e200": e200, "slope": (e200[-1] - e200[-6]) / e200[-6]}


def is_near(a):
    return a is not None and abs(a["gap"]) <= NEAR


def make_caption(inst, a):
    side = "위" if a["gap"] >= 0 else "아래"
    lines = [
        f"📍 일봉 200일선 근처 {inst.replace('-USDT-SWAP', '')}",
        f"현재가 {ca.fmt_price(a['price'])} / EMA200 {ca.fmt_price(a['line'])}",
        f"선 {side} {abs(a['gap']) * 100:.2f}% · {a['came_from']}",
        f"EMA200 방향: {'↑ 오르는 선' if a['slope'] > 0 else '↓ 내리는 선'}",
    ]
    if a["bounce"]:
        lines.append(f"직전 일봉이 선을 찍고 돌아섬 (튕김: {a['bounce']} 쪽)")
    lines.append(f"{datetime.now(ca.KST):%Y-%m-%d %H:%M:%S}")
    return "\n".join(lines)


def draw_panel(ax, candles, e200_series, line_value, n):
    """일봉 캔들 + EMA200 선 + 선 위아래 띠"""
    cs = candles[-n:]
    x = list(range(len(cs)))
    up, down = "#26a69a", "#ef5350"
    for i, c in enumerate(cs):
        color = up if c["close"] >= c["open"] else down
        ax.vlines(i, c["low"], c["high"], color=color, linewidth=1)
        body_h = abs(c["close"] - c["open"]) or (c["high"] - c["low"]) * 0.01
        ax.bar(i, body_h, bottom=min(c["open"], c["close"]), color=color, width=0.6)
    ax.plot(x, e200_series[-len(cs):], color="#e53935", linewidth=2.4, label="EMA200 (일봉)")
    ax.axhspan(line_value * (1 - NEAR), line_value * (1 + NEAR), color="#e53935", alpha=0.10)
    ax.legend(loc="upper left", fontsize=10)
    ax.grid(alpha=0.3)
    ax.set_ylabel("가격")
    step = max(1, len(cs) // 8)
    ax.set_xticks(x[::step])
    ax.set_xticklabels([datetime.fromtimestamp(c["ts"] / 1000, ca.KST).strftime("%m-%d" if BAR_H == 24 else "%m-%d %H:%M") for c in cs[::step]],
                       fontsize=9)
    ax.set_xlim(-1, len(cs) + 2)


def make_title(inst, a):
    side = "위" if a["gap"] >= 0 else "아래"
    t = (f"{inst.replace('-USDT-SWAP', '')}  200선 근처 · 선 {side} {abs(a['gap']) * 100:.2f}% · "
         f"{a['came_from']} · 선 {'↑' if a['slope'] > 0 else '↓'}")
    if a["bounce"]:
        t += f" · 튕김({a['bounce']})"
    return t


def day_stats(cs):
    """오늘(OKX 일봉 기준, 한국시간 01시 시작) 시가·고가. 현재가는 마지막 봉 종가"""
    H = 3600000
    day = 24 * H
    start = (cs[-1]["ts"] + 8 * H) // day * day - 8 * H
    today = [c for c in cs if c["ts"] >= start]
    if not today:
        return None
    return {"open": today[0]["open"], "high": max(c["high"] for c in today), "low": min(c["low"] for c in today), "price": cs[-1]["close"]}


def draw_osc(axr, axm, r, ml, sg, hs):
    """아래 두 칸: RSI(14) 와 MACD(12,26,9)"""
    x = list(range(len(r)))
    axr.plot(x, r, color="#7b1fa2", linewidth=1.6)
    axr.axhline(30, color="#26a69a", linewidth=1, linestyle="--", alpha=0.8)
    axr.axhline(70, color="#ef5350", linewidth=1, linestyle="--", alpha=0.8)
    axr.axhline(50, color="#90a4ae", linewidth=0.8, alpha=0.6)
    axr.set_ylim(0, 100)
    axr.set_yticks([30, 50, 70])
    axr.set_title(f"RSI(14)  지금 {r[-1]:.0f}  (30 아래 과매도 · 70 위 과매수)", fontsize=12, loc="left")
    axr.grid(alpha=0.3)
    axm.bar(x, hs, color=["#26a69a" if h >= 0 else "#ef5350" for h in hs], width=0.7, alpha=0.6)
    axm.plot(x, ml, color="#1e88e5", linewidth=1.4)
    axm.plot(x, sg, color="#fb8c00", linewidth=1.4)
    axm.axhline(0, color="#90a4ae", linewidth=0.8)
    axm.set_title("MACD(12,26,9)  파랑=MACD선 · 주황=시그널선 · 막대=둘의 차이", fontsize=12, loc="left")
    axm.grid(alpha=0.3)


def add_extras(ax, axv, axr, axm, candles, n):
    """거래량(아래 칸) + 오늘 시가·고가·현재가 + 매일 한국시간 01시 일봉마감선"""
    cs = candles[-n:]
    x = list(range(len(cs)))
    grey = "#90a4ae"
    closes_all = [c["close"] for c in candles]
    r_all = ca.rsi(closes_all, 14)
    ml_all, sg_all, hs_all = ca.macd(closes_all)
    draw_osc(axr, axm, r_all[-n:], ml_all[-n:], sg_all[-n:], hs_all[-n:])
    for i, c in enumerate(cs):
        col = "#26a69a" if c["close"] >= c["open"] else "#ef5350"
        axv.bar(i, c["quote_vol"], color=col, width=0.7, alpha=0.8)
    done = [c["quote_vol"] for c in candles[-21:-1]]
    avg = sum(done) / len(done) if done else 0
    el = min(1.0, max((time.time() * 1000 - candles[-1]["ts"]) / (3600000 * BAR_H), 0.1))
    pace = candles[-1]["quote_vol"] / el
    if pace > candles[-1]["quote_vol"]:
        axv.bar(len(cs) - 1, pace, color="none", edgecolor="#222", linestyle="--", linewidth=1.2, width=0.7)
    if avg > 0:
        axv.axhline(avg, color="#555", linewidth=1.2)
        axv.set_title(f"거래대금  20봉 평균(검은선)
