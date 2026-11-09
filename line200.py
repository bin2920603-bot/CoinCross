# -*- coding: utf-8 -*-
"""
OKX 선물 1시간봉 200일선(EMA200) 근처 알림
- 5분마다 거래대금 상위 코인의 1시간봉을 훑어서, 현재가가 EMA200 위아래 0.1% 안에 들어온 코인을 차트로 알려줌
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
BOUNCE = 0.002         # 직전 마감 1시간봉 꼬리가 선 0.2% 안까지 닿았다가 돌아서면 '튕김' 표시
HIST_BARS = 600        # 정확한 EMA200 계산에 쓰는 1시간봉 개수
COOLDOWN_H = 4         # 같은 코인은 이 시간 안에 다시 알리지 않음
MAX_PER_SCAN = 8       # 한 번에 보내는 최대 개수 (나머지는 다음 5분에)
SHOW_CAPTION = False   # False = 차트만 보냄 (글자 설명은 차트 맨 위 제목에 들어감) / True = 차트 아래 글자도 같이
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "line200_state.json")
CHAT = os.environ.get("LINE200_CHAT", "")
ca.CHAT_ID = CHAT      # cross_alert의 텔레그램 전송이 이 방으로 가게 함


def get_history(inst_id, bars, bar="1H"):
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
    prev = candles[-2]                                  # 직전에 마감된 1시간봉
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
        f"📍 1시간봉 200일선 근처 {inst.replace('-USDT-SWAP', '')}",
        f"현재가 {ca.fmt_price(a['price'])} / EMA200 {ca.fmt_price(a['line'])}",
        f"선 {side} {abs(a['gap']) * 100:.2f}% · {a['came_from']}",
        f"EMA200 방향: {'↑ 오르는 선' if a['slope'] > 0 else '↓ 내리는 선'}",
    ]
    if a["bounce"]:
        lines.append(f"직전 1시간봉이 선을 찍고 돌아섬 (튕김: {a['bounce']} 쪽)")
    lines.append(f"{datetime.now(ca.KST):%Y-%m-%d %H:%M:%S}")
    return "\n".join(lines)


def draw_panel(ax, candles, e200_series, line_value, n):
    """1시간봉 캔들 + EMA200 선 + 선 위아래 0.5% 띠"""
    cs = candles[-n:]
    x = list(range(len(cs)))
    up, down = "#26a69a", "#ef5350"
    for i, c in enumerate(cs):
        color = up if c["close"] >= c["open"] else down
        ax.vlines(i, c["low"], c["high"], color=color, linewidth=1)
        body_h = abs(c["close"] - c["open"]) or (c["high"] - c["low"]) * 0.01
        ax.bar(i, body_h, bottom=min(c["open"], c["close"]), color=color, width=0.6)
    ax.plot(x, e200_series[-len(cs):], color="#e53935", linewidth=2.4, label="EMA200 (1시간봉)")
    ax.axhspan(line_value * (1 - NEAR), line_value * (1 + NEAR), color="#e53935", alpha=0.10)
    ax.legend(loc="upper left", fontsize=10)
    ax.grid(alpha=0.3)
    ax.set_ylabel("가격")
    step = max(1, len(cs) // 8)
    ax.set_xticks(x[::step])
    ax.set_xticklabels([datetime.fromtimestamp(c["ts"] / 1000, ca.KST).strftime("%m-%d %H:%M") for c in cs[::step]],
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
    el = min(1.0, max((time.time() * 1000 - candles[-1]["ts"]) / 3600000, 0.1))
    pace = candles[-1]["quote_vol"] / el
    if pace > candles[-1]["quote_vol"]:
        axv.bar(len(cs) - 1, pace, color="none", edgecolor="#222", linestyle="--", linewidth=1.2, width=0.7)
    if avg > 0:
        axv.axhline(avg, color="#555", linewidth=1.2)
        axv.set_title(f"거래대금  20봉 평균(검은선)  ·  지금 봉 속도 평균의 {pace / avg:.1f}배", fontsize=12, loc="left")
    ds = day_stats(candles)
    if ds:
        items = (("시가(=어제 종가)", ds["open"]), ("고가", ds["high"]), ("저가", ds["low"]), ("현재가(종가)", ds["price"]))
        for label, val in items:
            ax.axhline(val, color=grey, linewidth=1.4, linestyle="--", alpha=0.8)
        y0, y1 = ax.get_ylim()
        gap = (y1 - y0) * 0.085
        pos = []
        order = sorted(items, key=lambda t: -t[1])
        for _, val in order:
            pos.append(val if not pos else min(val, pos[-1] - gap))
        for (label, val), y in zip(order, pos):
            ax.annotate(f"{label}\n{val:.6g}", xy=(1.0, val), xytext=(1.015, y), xycoords=ax.get_yaxis_transform(),
                        textcoords=ax.get_yaxis_transform(), fontsize=11, color="#607d8b", va="center", ha="left",
                        fontweight="bold", annotation_clip=False,
                        arrowprops=dict(arrowstyle="-", color=grey, linewidth=1, shrinkA=0, shrinkB=0))
    marks = []
    for i, c in enumerate(cs):
        if datetime.fromtimestamp(c["ts"] / 1000, ca.KST).hour == 1 and i > 0:
            marks.append(i)
            for axx in (ax, axv, axr, axm):
                axx.axvline(i - 0.5, color=grey, linewidth=1.4, linestyle=(0, (2, 2)), alpha=0.8)
            ax.text(i - 0.5, 1.0, "01시 마감", transform=ax.get_xaxis_transform(), fontsize=12,
                    color="#607d8b", ha="center", va="bottom", fontweight="bold")
    axv.grid(alpha=0.3)
    if len(marks) >= 2:
        axm.set_xticks([i - 0.5 for i in marks])
        axm.set_xticklabels([datetime.fromtimestamp(cs[i]["ts"] / 1000, ca.KST).strftime("%m-%d 01:00") for i in marks],
                            fontsize=10, fontweight="bold", color="#607d8b")
    else:
        step = max(1, len(cs) // 8)
        axm.set_xticks(x[::step])
        axm.set_xticklabels([datetime.fromtimestamp(c["ts"] / 1000, ca.KST).strftime("%m-%d %H:%M") for c in cs[::step]], fontsize=9)


def make_chart(inst, candles, a):
    fig, (ax, axv, axr, axm) = plt.subplots(4, 1, figsize=(14, 12), sharex=True, gridspec_kw={"height_ratios": [4, 1, 1, 1]})
    draw_panel(ax, candles, a["e200"], a["line"], 130)
    add_extras(ax, axv, axr, axm, candles, 130)
    fig.suptitle(make_title(inst, a), fontsize=20, fontweight="bold", y=0.985)
    fig.subplots_adjust(left=0.07, right=0.86, top=0.945, bottom=0.045, hspace=0.3)
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
            if time.time() * 1000 - state.get(inst, 0) < COOLDOWN_H * 3600000:
                continue
            quick = analyze(ca.get_candles(inst, "1H"), 150)       # 200봉짜리 어림 계산
            if quick is None or abs(quick["gap"]) > PRE_NEAR:
                continue
            full = merge_recent(get_history(inst, HIST_BARS), ca.get_candles(inst, "1H"))
            a = analyze(full)                                      # 정확한 계산
            if is_near(a):
                hits.append((abs(a["gap"]), inst, a, full))
        except Exception as e:
            print(f"[오류] {inst}: {e}")
        time.sleep(0.1)
    hits.sort(key=lambda x: x[0])
    sent = 0
    for _, inst, a, full in hits[:MAX_PER_SCAN]:
        try:
            caption = make_caption(inst, a)
            print("[200선]", caption.replace("\n", " | "))
            ca.send_telegram(make_chart(inst, full, a), caption if SHOW_CAPTION else "")
            state[inst] = int(time.time() * 1000)
            sent += 1
        except Exception as e:
            print(f"[오류] {inst} 전송 실패: {e}")
    print(f"{datetime.now(ca.KST):%H:%M:%S} 스캔 완료: {len(symbols)}개 확인, 근처 코인 {len(hits)}개, 보냄 {sent}개")


def find_chat():
    """봇이 아는 방(그룹)을 찾아서 번호를 보여주고, 그 방에도 번호를 직접 말해 줌"""
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
    full = merge_recent(get_history(inst, HIST_BARS), ca.get_candles(inst, "1H"))
    a = analyze(full)
    ca.send_telegram(make_chart(inst, full, a), ("[테스트] 실제 알림이 아닙니다\n" + make_caption(inst, a)) if SHOW_CAPTION else "[테스트]")


def main():
    ca.setup_korean_font()
    if "--find" in sys.argv:
        find_chat()
        return
    if not ca.TOKEN or not CHAT:
        print("[알림] TELEGRAM_TOKEN 또는 LINE200_CHAT이 비어 있어 텔레그램 전송 없이 화면에만 표시합니다.")
    if "--test" in sys.argv:
        send_test()
        return
    state = load_state()
    scan(state)
    save_state(state)


if __name__ == "__main__":
    main()
