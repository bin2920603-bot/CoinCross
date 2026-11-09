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


def make_chart(inst, cs, a, name):
    fig, ax = plt.subplots(figsize=(14, 8))
    n = 130
    v = cs[-n:]
    x = list(range(len(v)))
    for i, c in enumerate(v):
        col = "#26a69a" if c["close"] >= c["open"] else "#ef5350"
        ax.vlines(i, c["low"], c["high"], color=col, linewidth=1)
        ax.bar(i, abs(c["close"] - c["open"]) or (c["high"] - c["low"]) * 0.01,
               bottom=min(c["open"], c["close"]), color=col, width=0.6)
    for ln, color in LINES:
        s = a["series"][ln][-n:]
        pts = [(i, y) for i, y in zip(x, s) if y]
        if pts:
            main = ln == name
            ax.plot([p[0] for p in pts], [p[1] for p in pts], color=color,
                    linewidth=3.2 if main else 1.2, alpha=1 if main else 0.6, label=ln)
    lv = a["lines"][name]["line"]
    ax.axhspan(lv * (1 - NEAR), lv * (1 + NEAR), color="#e53935", alpha=0.12)
    ax.legend(loc="upper left", fontsize=10)
    ax.grid(alpha=0.3)
    step = max(1, len(v) // 8)
    ax.set_xticks(x[::step])
    ax.set_xticklabels([datetime.fromtimestamp(c["ts"] / 1000, ca.KST).strftime("%m-%d %H:%M") for c in v[::step]], fontsize=9)
    ax.set_xlim(-1, len(v) + 2)
    g = a["lines"][name]
    side = "위" if g["gap"] >= 0 else "아래"
    fig.suptitle(f"{inst.replace('-USDT-SWAP', '')}  1시간봉 {name} 근처 · 선 {side} {abs(g['gap']) * 100:.2f}% · 선 {'↑' if g['slope'] > 0 else '↓'}",
                 fontsize=20, fontweight="bold", y=0.985)
    fig.subplots_adjust(left=0.07, right=0.98, top=0.92, bottom=0.07)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=80)
    plt.close(fig)
    buf.seek(0)
    return buf


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
            ca.send_telegram(make_chart(inst, cs, a, name), "")
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
