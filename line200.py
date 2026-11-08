# -*- coding: utf-8 -*-
"""
OKX 선물 1시간봉 200일선(EMA200) 근처 알림
- 5분마다 거래대금 상위 코인의 1시간봉을 훑어서, 현재가가 EMA200 위아래 0.1% 안에 들어온 코인을 차트로 알려줌
- 큰 흐름·봉 색깔·선 기울기 같은 거름 조건 없음. 선 근처에 오면 전부 알림 (근접알림과 별도)
- cross_alert.py의 텔레그램 전송·지표 함수를 그대로 가져다 씀 (cross_alert.py는 안 바꿈)
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
    """1시간봉 캔들 + EMA200 선 + 선 위아래 띠"""
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


def make_chart(inst, candles, a):
    fig, ax = plt.subplots(figsize=(14, 8))
    draw_panel(ax, candles, a["e200"], a["line"], 130)
    fig.suptitle(make_title(inst, a), fontsize=20, fontweight="bold", y=0.985)
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
