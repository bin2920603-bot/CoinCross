# -*- coding: utf-8 -*-
"""
OKX 선물 거래대금 상위 30개 코인 일봉 차트 알림
- 24시간 거래대금 상위 30개 코인을 일봉 차트로 텔레그램에 보냄 (코인마다 사진 1장, 글은 한 줄)
- 차트: 일봉 캔들 + 이평선(7 핑크·10 하늘·20 진노랑·50 초록·120 회색·200 빨강·거가중100 검정) + 거래대금 + RSI + MACD
- cross_alert.py의 텔레그램 전송·지표 함수를 그대로 가져다 씀 (cross_alert.py는 안 바꿈)
- 방 번호 찾기: python top30.py --find   /   차트 1장만 테스트: python top30.py --test
"""
import io
import os
import sys
import time
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import cross_alert as ca
import zones            # 지지·저항 박스, 추세선, 과매도/과매수, 일목 구름, 하이킨아시 (zones.py)

# ----- 설정값 -----
TOP_N = 30             # 24시간 거래대금 상위 몇 개를 볼지
HIST_BARS = 400        # 정확한 EMA200 계산에 쓰는 일봉 개수
SHOW_BARS = 120        # 차트에 보여줄 일봉 개수
CHAT = os.environ.get("TOP30_CHAT", "")
ca.CHAT_ID = CHAT      # cross_alert의 텔레그램 전송이 이 방으로 가게 함

# 이평선 색: 7일 핑크, 10일 하늘, 20일 진노랑, 50일 초록, 120일 회색, 200일 빨강, 거가중100 검정
LINES = [("EMA7", 7, "#f48fb1"), ("EMA10", 10, "#4fc3f7"), ("EMA20", 20, "#f9a825"),
         ("EMA50", 50, "#43a047"), ("EMA120", 120, "#9e9e9e"), ("EMA200", 200, "#e53935")]


def get_history(inst_id, bars, bar="1D"):
    """일봉을 오래된 것 -> 최신 순으로 bars개 가까이 가져옴 (맨 끝은 진행 중인 오늘 봉)"""
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
            out.append({"ts": int(c[0]), "open": float(c[1]), "high": float(c[2]), "low": float(c[3]),
                        "close": float(c[4]), "vol": float(c[5]), "quote_vol": float(c[7])})
        after = rows[-1][0]
        time.sleep(0.08)
    out.sort(key=lambda c: c["ts"])
    return out


def merge_recent(hist, recent):
    """진행 중인 오늘 봉은 candles 쪽 값으로 덮어씀"""
    by_ts = {c["ts"]: c for c in hist}
    for c in recent:
        by_ts[c["ts"]] = c
    return [by_ts[k] for k in sorted(by_ts)]


def vwap_series(cs, n=100):
    """거래량가중 100: (고+저+종)/3 x 거래량 의 100봉 합 / 거래량 100봉 합"""
    out = [None] * len(cs)
    for i in range(n - 1, len(cs)):
        w = cs[i - n + 1:i + 1]
        v = sum(c["vol"] for c in w)
        if v > 0:
            out[i] = sum((c["high"] + c["low"] + c["close"]) / 3 * c["vol"] for c in w) / v
    return out


def load(inst):
    """코인 하나의 일봉을 불러옴 (오늘 봉 포함)"""
    recent = []
    r = ca.session.get(ca.OKX + "/api/v5/market/candles",
                       params={"instId": inst, "bar": "1D", "limit": 100}, timeout=15)
    r.raise_for_status()
    for c in reversed(r.json()["data"]):
        recent.append({"ts": int(c[0]), "open": float(c[1]), "high": float(c[2]), "low": float(c[3]),
                       "close": float(c[4]), "vol": float(c[5]), "quote_vol": float(c[7])})
    return merge_recent(get_history(inst, HIST_BARS), recent)


def make_chart(inst, cs, rank):
    n = min(SHOW_BARS, len(cs))
    view = cs[-n:]
    x = list(range(n))
    closes = [c["close"] for c in cs]
    fig, (ax, axv, axr, axm) = plt.subplots(4, 1, figsize=(14, 12), sharex=True,
                                            gridspec_kw={"height_ratios": [4, 1, 1, 1]})
    up, down = "#26a69a", "#ef5350"
    for i, c in enumerate(view):
        col = up if c["close"] >= c["open"] else down
        ax.vlines(i, c["low"], c["high"], color=col, linewidth=1)
        body = abs(c["close"] - c["open"]) or (c["high"] - c["low"]) * 0.01
        ax.bar(i, body, bottom=min(c["open"], c["close"]), color=col, width=0.6)
    for name, period, color in LINES:
        if len(cs) > period:
            ax.plot(x, ca.ema(closes, period)[-n:], color=color, linewidth=1.8, label=name)
    vw = vwap_series(cs)[-n:]
    if any(v is not None for v in vw):
        ax.plot([i for i, v in enumerate(vw) if v], [v for v in vw if v], color="#111111", linewidth=2.0, label="거가중100")
    ax.legend(loc="upper left", fontsize=9, ncol=4)
    ax.grid(alpha=0.3)
    ax.set_xlim(-1, n + 2)

    for i, c in enumerate(view):
        axv.bar(i, c["quote_vol"], color=up if c["close"] >= c["open"] else down, width=0.7, alpha=0.8)
    axv.set_title("거래대금", fontsize=12, loc="left")
    axv.grid(alpha=0.3)

    r_all = ca.rsi(closes, 14)[-n:]
    axr.plot(x, r_all, color="#7b1fa2", linewidth=1.6)
    for y, cc in ((30, up), (70, down)):
        axr.axhline(y, color=cc, linewidth=1, linestyle="--", alpha=0.8)
    axr.set_ylim(0, 100)
    axr.set_yticks([30, 50, 70])
    axr.set_title(f"RSI(14)  지금 {r_all[-1]:.0f}", fontsize=12, loc="left")
    axr.grid(alpha=0.3)

    ml, sg, hs = ca.macd(closes)
    axm.bar(x, hs[-n:], color=[up if h >= 0 else down for h in hs[-n:]], width=0.7, alpha=0.6)
    axm.plot(x, ml[-n:], color="#1e88e5", linewidth=1.4)
    axm.plot(x, sg[-n:], color="#fb8c00", linewidth=1.4)
    axm.axhline(0, color="#90a4ae", linewidth=0.8)
    axm.set_title("MACD(12,26,9)", fontsize=12, loc="left")
    axm.grid(alpha=0.3)

    step = max(1, n // 8)
    axm.set_xticks(x[::step])
    axm.set_xticklabels([datetime.fromtimestamp(c["ts"] / 1000, ca.KST).strftime("%m-%d") for c in view[::step]], fontsize=9)

    chg = (cs[-1]["close"] / cs[-2]["close"] - 1) * 100 if len(cs) >= 2 else 0
    fig.suptitle(f"거래대금 {rank}위  {inst.replace('-USDT-SWAP', '')}  일봉  {ca.fmt_price(cs[-1]['close'])}  ({chg:+.1f}%)",
                 fontsize=20, fontweight="bold", y=0.985)
    fig.subplots_adjust(left=0.07, right=0.97, top=0.945, bottom=0.045, hspace=0.3)
    if getattr(plt.subplots, "__module__", "") != "all_daily":   # all_daily.py가 이미 같은 표시를 얹어서 부를 땐 건너뜀
        try:
            zones.overlay(ax, cs)
        except Exception as e:
            print(f"[박스 오류] {inst}: {e}")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=80)
    plt.close(fig)
    buf.seek(0)
    return buf


def scan(limit=None):
    try:
        symbols = ca.get_top_symbols(TOP_N)
    except Exception as e:
        print(f"[오류] 코인 목록 조회 실패: {e}")
        return
    if limit:
        symbols = symbols[:limit]
    sent = 0
    for rank, inst in enumerate(symbols, 1):
        try:
            cs = load(inst)
            if len(cs) < 30:
                continue
            ca.send_telegram(make_chart(inst, cs, rank), f"거래대금 {rank}위 {inst.replace('-USDT-SWAP', '')}")
            sent += 1
        except Exception as e:
            print(f"[오류] {inst}: {e}")
        time.sleep(0.5)
    print(f"{datetime.now(ca.KST):%H:%M:%S} 완료: {len(symbols)}개 중 {sent}개 보냄")


def find_chat():
    """봇이 아는 방을 찾아서 번호를 보여주고, 그 방에도 번호를 직접 말해 줌"""
    r = ca.session.get(f"https://api.telegram.org/bot{ca.TOKEN}/getUpdates", timeout=20).json()
    seen = {}
    for u in r.get("result", []):
        for key in ("message", "channel_post", "my_chat_member"):
            chat = (u.get(key) or {}).get("chat")
            if chat and chat.get("type") in ("group", "supergroup", "channel"):
                seen[chat["id"]] = chat.get("title", "")
    print("봇이 아는 방:", seen)
    for cid, title in seen.items():
        print(f"[찾은 방] {cid} {title}")
        try:
            ca.session.post(f"https://api.telegram.org/bot{ca.TOKEN}/sendMessage",
                            data={"chat_id": cid, "text": f"이 방 번호: {cid}"}, timeout=20)
        except Exception as e:
            print("[오류] 방에 번호 보내기 실패:", e)
    if not seen:
        print("[못 찾음] 방에서 /start@봇이름 을 보낸 뒤 24시간 안에 다시 실행하세요")


def main():
    ca.setup_korean_font()
    if "--find" in sys.argv:
        find_chat()
        return
    if not ca.TOKEN or not CHAT:
        print("[알림] TELEGRAM_TOKEN 또는 TOP30_CHAT이 비어 있어 텔레그램 전송 없이 화면에만 표시합니다.")
    if "--test" in sys.argv:
        scan(limit=1)
        return
    scan()


if __name__ == "__main__":
    main()
