# -*- coding: utf-8 -*-
"""
OKX 선물 1시간봉 이평선(EMA7·EMA20·EMA50·EMA120·거래량가중100) 근처 알림
- 5분마다 거래대금 상위 코인의 1시간봉을 훑어서, 현재가가 5개 선 중 하나의 위아래 0.1% 안에 들어오면 차트로 알려줌
- 거름 조건 없음. 같은 코인·같은 선은 4시간 안에 다시 알리지 않음
- 방 번호 찾기: python line_ma.py --find   /   테스트: python line_ma.py --test
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

# ----- 설정값 -----
TOP_N = 200
NEAR = 0.001           # 선 위아래 0.1% 안이면 '근처'
COOLDOWN_H = 4
MAX_PER_SCAN = 8
SHOW_CAPTION = False
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "line_ma_state.json")
# 선마다 따로 받는 방 (방 번호는 GitHub 비밀 칸에 저장)
CHATS = {"EMA7": os.environ.get("LINEMA_CHAT_7", ""), "EMA20": os.environ.get("LINEMA_CHAT_20", ""), "EMA50": os.environ.get("LINEMA_CHAT_50", ""),
         "EMA120": os.environ.get("LINEMA_CHAT_120", ""), "거가중100": os.environ.get("LINEMA_CHAT_V100", "")}
LINES = [("EMA7", "#d81b60"), ("EMA20", "#fb8c00"), ("EMA50", "#43a047"), ("EMA120", "#1e88e5"), ("거가중100", "#8e24aa")]


def get_candles(inst, bar="1H"):
    r = ca.session.get(ca.OKX + "/api/v5/market/candles",
                       params={"instId": inst, "bar": bar, "limit": 300}, timeout=15)
    r.raise_for_status()
    out = []
    for c in r.json()["data"]:
        out.append({"ts": int(c[0]), "open": float(c[1]), "high": float(c[2]), "low": float(c[3]),
                    "close": float(c[4]), "vol": float(c[6])})
    out.sort(key=lambda c: c["ts"])
    return out


def vwap_series(cs, n=100):
    """거래량가중 100: (고+저+종)/3 x 거래량 의 100봉 합 / 거래량 100봉 합"""
    out = [None] * len(cs)
    for i in range(n - 1, len(cs)):
        w = cs[i - n + 1:i + 1]
        v = sum(c["vol"] for c in w)
        if v > 0:
            out[i] = sum((c["high"] + c["low"] + c["close"]) / 3 * c["vol"] for c in w) / v
    return out


def analyze(cs):
    """각 선의 값과 현재가와의 거리. 봉이 모자라면 None"""
    if len(cs) < 160:
        return None
    closes = [c["close"] for c in cs]
    series = {"EMA7": ca.ema(closes, 7), "EMA20": ca.ema(closes, 20), "EMA50": ca.ema(closes, 50),
              "EMA120": ca.ema(closes, 120), "거가중100": vwap_series(cs)}
    price = closes[-1]
    res = {}
    for name, s in series.items():
        v = s[-1]
        if v and v > 0:
            res[name] = {"line": v, "gap": (price - v) / v, "slope": (s[-1] - s[-6]) / s[-6] if s[-6] else 0}
    return {"price": price, "lines": res, "series": series}


def day_stats(cs):
    """오늘(OKX 일봉 기준, 한국시간 01시 시작) 시가·고가. 현재가는 마지막 봉 종가"""
    H = 3600000
    day = 24 * H
    last = cs[-1]["ts"]
    start = (last + 8 * H) // day * day - 8 * H
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


def make_chart(inst, cs, a, name):
    fig, (ax, axv, axr, axm) = plt.subplots(4, 1, figsize=(14, 12), sharex=True, gridspec_kw={"height_ratios": [4, 1, 1, 1]})
    n = 130
    v = cs[-n:]
    x = list(range(len(v)))
    closes_all = [c["close"] for c in cs]
    r_all = ca.rsi(closes_all, 14)
    ml_all, sg_all, hs_all = ca.macd(closes_all)
    draw_osc(axr, axm, r_all[-n:], ml_all[-n:], sg_all[-n:], hs_all[-n:])
    for i, c in enumerate(v):
        col = "#26a69a" if c["close"] >= c["open"] else "#ef5350"
        ax.vlines(i, c["low"], c["high"], color=col, linewidth=1)
        ax.bar(i, abs(c["close"] - c["open"]) or (c["high"] - c["low"]) * 0.01,
               bottom=min(c["open"], c["close"]), color=col, width=0.6)
        axv.bar(i, c["vol"], color=col, width=0.7, alpha=0.8)
    # 거래량: 지금 진행 중인 봉은 1시간 속도로 환산해서 점선 막대로 함께 표시
    done = [c["vol"] for c in cs[-21:-1]]
    avg = sum(done) / len(done) if done else 0
    el = (time.time() * 1000 - cs[-1]["ts"]) / 3600000
    el = min(1.0, max(el, 0.1))
    pace = cs[-1]["vol"] / el
    last_i = len(v) - 1
    if pace > cs[-1]["vol"]:
        axv.bar(last_i, pace, color="none", edgecolor="#222", linestyle="--", linewidth=1.2, width=0.7)
    if avg > 0:
        axv.axhline(avg, color="#555", linewidth=1.2)
        ratio = pace / avg
        axv.set_title(f"거래량  20봉 평균(검은선)  ·  지금 봉 속도 평균의 {ratio:.1f}배", fontsize=12, loc="left")
    for ln, color in LINES:
        s = a["series"][ln][-n:]
        pts = [(i, y) for i, y in zip(x, s) if y]
        if pts:
            main = ln == name
            ax.plot([p[0] for p in pts], [p[1] for p in pts], color=color,
                    linewidth=3.2 if main else 1.2, alpha=1 if main else 0.6, label=ln)
    lv = a["lines"][name]["line"]
    ax.axhspan(lv * (1 - NEAR), lv * (1 + NEAR), color="#e53935", alpha=0.12)
    ds = day_stats(cs)
    if ds:
        items = (("시가(=어제 종가)", ds["open"], "#90a4ae", "--"),
                 ("고가", ds["high"], "#90a4ae", "--"),
                 ("저가", ds["low"], "#90a4ae", "--"),
                 ("현재가(종가)", ds["price"], "#90a4ae", "--"))
        for label, val, col, ls in items:
            ax.axhline(val, color=col, linewidth=1.4, linestyle=ls, alpha=0.8)
        # 오른쪽 바깥 칸에 글자를 세로로 펼쳐서 서로 안 겹치게 (선과는 가는 줄로 이어줌)
        y0, y1 = ax.get_ylim()
        gap = (y1 - y0) * 0.085
        order = sorted(items, key=lambda t: -t[1])
        pos = []
        for _, val, _, _ in order:
            y = val if not pos else min(val, pos[-1] - gap)
            pos.append(y)
        for (label, val, col, ls), y in zip(order, pos):
            ax.annotate(f"{label}\n{val:.6g}", xy=(1.0, val), xytext=(1.015, y), xycoords=ax.get_yaxis_transform(),
                        textcoords=ax.get_yaxis_transform(), fontsize=11, color="#607d8b", va="center", ha="left",
                        fontweight="bold", annotation_clip=False,
                        arrowprops=dict(arrowstyle="-", color=col, linewidth=1, shrinkA=0, shrinkB=0))
    # 매일 한국시간 01시 = 일봉이 끝나는(종가) 자리에 진한 세로선 + 큰 글자
    marks = []
    for i, c in enumerate(v):
        if datetime.fromtimestamp(c["ts"] / 1000, ca.KST).hour == 1 and i > 0:
            marks.append(i)
            for axx in (ax, axv, axr, axm):
                axx.axvline(i - 0.5, color="#90a4ae", linewidth=1.4, linestyle=(0, (2, 2)), alpha=0.8)
            ax.text(i - 0.5, 1.0, "01시 마감", transform=ax.get_xaxis_transform(), fontsize=12,
                    color="#607d8b", ha="center", va="bottom", fontweight="bold")
    ax.legend(loc="upper left", fontsize=10)
    ax.grid(alpha=0.3)
    axv.grid(alpha=0.3)
    if len(marks) >= 2:
        axm.set_xticks([i - 0.5 for i in marks])
        axm.set_xticklabels([datetime.fromtimestamp(v[i]["ts"] / 1000, ca.KST).strftime("%m-%d 01:00") for i in marks],
                            fontsize=10, fontweight="bold", color="#607d8b")
    else:
        step = max(1, len(v) // 8)
        axm.set_xticks(x[::step])
        axm.set_xticklabels([datetime.fromtimestamp(c["ts"] / 1000, ca.KST).strftime("%m-%d %H:%M") for c in v[::step]], fontsize=9)
    ax.set_xlim(-1, len(v) + 2)
    g = a["lines"][name]
    side = "위" if g["gap"] >= 0 else "아래"
    fig.suptitle(f"{inst.replace('-USDT-SWAP', '')}  1시간봉 {name} 근처 · 선 {side} {abs(g['gap']) * 100:.2f}% · 선 {'↑' if g['slope'] > 0 else '↓'}",
                 fontsize=20, fontweight="bold", y=0.985)
    fig.subplots_adjust(left=0.07, right=0.86, top=0.945, bottom=0.045, hspace=0.3)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=80)
    plt.close(fig)
    buf.seek(0)
    return buf


def hint(g):
    """최근 30일 재현 승률표 기준 한 줄 (조건은 안 바꾸고 표시만). 숫자는 롱/숏 승률(%)"""
    up = g["slope"] > 0
    above = g["gap"] >= 0
    lo, sh = {(True, True): (30, 49), (False, True): (31, 48), (True, False): (33, 46), (False, False): (31, 47)}[(above, up)]
    return f"📉 숏 유리 (승률 {sh}%)\n⚠️ 롱 주의 (승률 {lo}%)\n최근 30일 재현 기준 · 손익분기 40%"


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state):
    cutoff = time.time() * 1000 - 24 * 3600 * 1000
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in state.items() if v >= cutoff}, f)


def scan(state):
    try:
        symbols = ca.get_top_symbols(TOP_N)
    except Exception as e:
        print(f"[오류] 코인 목록 조회 실패: {e}")
        return
    hits = []
    for inst in symbols:
        try:
            cs = get_candles(inst)
            a = analyze(cs)
            if not a:
                continue
            for name, g in a["lines"].items():
                key = f"{inst}|{name}"
                if CHATS.get(name) and abs(g["gap"]) <= NEAR and time.time() * 1000 - state.get(key, 0) >= COOLDOWN_H * 3600000:
                    hits.append((abs(g["gap"]), inst, name, a, cs))
        except Exception as e:
            print(f"[오류] {inst}: {e}")
        time.sleep(0.1)
    hits.sort(key=lambda x: x[0])
    sent = 0
    for _, inst, name, a, cs in hits[:MAX_PER_SCAN]:
        try:
            print("[이평선]", inst, name)
            ca.CHAT_ID = CHATS[name]
            ca.send_telegram(make_chart(inst, cs, a, name), hint(a["lines"][name]))
            state[f"{inst}|{name}"] = int(time.time() * 1000)
            sent += 1
        except Exception as e:
            print(f"[오류] {inst} 전송 실패: {e}")
    print(f"{datetime.now(ca.KST):%H:%M:%S} 스캔 완료: {len(symbols)}개 확인, 근처 {len(hits)}건, 보냄 {sent}건")


def find_chat():
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


def send_test():
    inst = ca.get_top_symbols(1)[0]
    cs = get_candles(inst)
    a = analyze(cs)
    name = min(a["lines"], key=lambda k: abs(a["lines"][k]["gap"]))
    for ln, cid in CHATS.items():      # 5개 방에 각각 테스트 차트
        if cid:
            ca.CHAT_ID = cid
            ca.send_telegram(make_chart(inst, cs, a, ln), f"[테스트] {ln}")


def main():
    ca.setup_korean_font()
    if "--find" in sys.argv:
        find_chat()
        return
    if not ca.TOKEN or not any(CHATS.values()):
        print("[알림] TELEGRAM_TOKEN 또는 방 번호가 비어 있어 텔레그램 전송 없이 화면에만 표시합니다.")
    if "--test" in sys.argv:
        send_test()
        return
    state = load_state()
    scan(state)
    save_state(state)


if __name__ == "__main__":
    main()
