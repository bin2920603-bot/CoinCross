# -*- coding: utf-8 -*-
"""
OKX 선물 1시간봉 EMA960 근처 알림
- 960개 봉이 필요한 선이라, 코인마다 EMA 값을 기록해 두고 5분마다 새 봉만 이어서 계산함
  (처음 며칠 아닌 처음 몇십 분 동안 코인을 나눠서 기록을 쌓음)
- 현재가가 EMA960 위아래 0.1% 안이면 차트로 알림. 거름 조건 없음, 같은 코인은 4시간 안에 다시 안 알림
- 차트: EMA960 + 아래 거래대금(20봉 평균, 지금 봉 속도 환산) + 오늘 시가·고가·현재가 + 매일 01시 일봉마감 표시
- 방 번호 찾기: python line960.py --find   /   테스트: python line960.py --test
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
PERIOD = 960
TOP_N = 200
NEAR = 0.001            # 선 위아래 0.1% 안이면 '근처'
COOLDOWN_H = 4
MAX_PER_SCAN = 8
SEED_BARS = 2400        # 처음 EMA를 만들 때 불러오는 봉 개수 (많을수록 정확)
SEED_PER_RUN = 25       # 한 번에 새로 기록을 만드는 코인 수 (나머지는 다음 5분에)
HOUR = 3600000
ALPHA = 2 / (PERIOD + 1)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "line960_state.json")
CHAT = os.environ.get("LINE960_CHAT", "")
ca.CHAT_ID = CHAT


def parse(rows):
    out = []
    for c in rows:
        out.append({"ts": int(c[0]), "open": float(c[1]), "high": float(c[2]), "low": float(c[3]),
                    "close": float(c[4]), "confirm": str(c[8]) == "1",
                    "vol": float(c[7]) if len(c) > 7 else 0.0})
    out.sort(key=lambda c: c["ts"])
    return out


def get_recent(inst):
    r = ca.session.get(ca.OKX + "/api/v5/market/candles",
                       params={"instId": inst, "bar": "1H", "limit": 300}, timeout=15)
    r.raise_for_status()
    return parse(r.json()["data"])


def get_history(inst, bars):
    """오래된 -> 최신 순으로 마감된 1시간봉을 bars개 가까이 가져옴"""
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
        out.extend(parse(rows))
        after = rows[-1][0]
        time.sleep(0.08)
    out = [c for c in out if c["confirm"]]
    out.sort(key=lambda c: c["ts"])
    return out


def seed(inst):
    """처음 기록 만들기: [마지막 마감봉 시각, 그때의 EMA960]. 봉이 960개 안 되면 None"""
    h = get_history(inst, SEED_BARS)
    if len(h) < PERIOD:
        return None
    e = h[0]["close"]
    for c in h[1:]:
        e = c["close"] * ALPHA + e * (1 - ALPHA)
    return [h[-1]["ts"], e]


def advance(entry, recent):
    """기록에 새로 마감된 봉만 이어서 계산. 중간이 비면 None(다시 만들어야 함)"""
    ts, e = entry
    new = [c for c in recent if c["confirm"] and c["ts"] > ts]
    if new and new[0]["ts"] != ts + HOUR:
        return None
    for c in new:
        if len(new) > 1 and c is not new[0] and c["ts"] != prev + HOUR:
            return None
        e = c["close"] * ALPHA + e * (1 - ALPHA)
        ts = c["ts"]
        prev = c["ts"]
    return [ts, e]


def live_value(entry, recent):
    """지금 진행 중인 봉까지 반영한 EMA960 값과 현재가"""
    price = recent[-1]["close"]
    return price * ALPHA + entry[1] * (1 - ALPHA), price


def chart_series(entry, recent, n=130):
    """차트용: 최근 n개 마감봉의 EMA를 현재 값에서 거꾸로 풀어서 만듦"""
    conf = [c for c in recent if c["confirm"] and c["ts"] <= entry[0]][-n:]
    vals = [0.0] * len(conf)
    e = entry[1]
    for i in range(len(conf) - 1, -1, -1):
        vals[i] = e
        e = (e - ALPHA * conf[i]["close"]) / (1 - ALPHA)
    live, price = live_value(entry, recent)
    return conf, vals, live


def day_stats(cs):
    """오늘(OKX 일봉 기준, 한국시간 01시 시작) 시가·고가. 현재가는 마지막 봉 종가"""
    day = 24 * HOUR
    start = (cs[-1]["ts"] + 8 * HOUR) // day * day - 8 * HOUR
    today = [c for c in cs if c["ts"] >= start]
    if not today:
        return None
    return {"open": today[0]["open"], "high": max(c["high"] for c in today), "price": cs[-1]["close"]}


def add_extras(ax, axv, bars):
    """거래대금(아래 칸) + 오늘 시가·고가·현재가 + 매일 한국시간 01시 일봉마감선"""
    grey = "#90a4ae"
    x = list(range(len(bars)))
    for i, c in enumerate(bars):
        col = "#26a69a" if c["close"] >= c["open"] else "#ef5350"
        axv.bar(i, c["vol"], color=col, width=0.7, alpha=0.8)
    done = [c["vol"] for c in bars[-21:-1]]
    avg = sum(done) / len(done) if done else 0
    el = min(1.0, max((time.time() * 1000 - bars[-1]["ts"]) / HOUR, 0.1))
    pace = bars[-1]["vol"] / el
    if pace > bars[-1]["vol"]:
        axv.bar(len(bars) - 1, pace, color="none", edgecolor="#222", linestyle="--", linewidth=1.2, width=0.7)
    if avg > 0:
        axv.axhline(avg, color="#555", linewidth=1.2)
        axv.set_title(f"거래대금  20봉 평균(검은선)  ·  지금 봉 속도 평균의 {pace / avg:.1f}배", fontsize=12, loc="left")
    ds = day_stats(bars)
    if ds:
        items = (("시가(=어제 종가)", ds["open"]), ("고가", ds["high"]), ("현재가(종가)", ds["price"]))
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
    for i, c in enumerate(bars):
        if datetime.fromtimestamp(c["ts"] / 1000, ca.KST).hour == 1 and i > 0:
            marks.append(i)
            for axx in (ax, axv):
                axx.axvline(i - 0.5, color=grey, linewidth=1.4, linestyle=(0, (2, 2)), alpha=0.8)
            ax.text(i - 0.5, 1.0, "01시 마감", transform=ax.get_xaxis_transform(), fontsize=12,
                    color="#607d8b", ha="center", va="bottom", fontweight="bold")
    axv.grid(alpha=0.3)
    if len(marks) >= 2:
        axv.set_xticks([i - 0.5 for i in marks])
        axv.set_xticklabels([datetime.fromtimestamp(bars[i]["ts"] / 1000, ca.KST).strftime("%m-%d 01:00") for i in marks],
                            fontsize=10, fontweight="bold", color="#607d8b")
    else:
        step = max(1, len(bars) // 8)
        axv.set_xticks(x[::step])
        axv.set_xticklabels([datetime.fromtimestamp(c["ts"] / 1000, ca.KST).strftime("%m-%d %H:%M") for c in bars[::step]], fontsize=9)


def make_chart(inst, entry, recent):
    conf, vals, live = chart_series(entry, recent)
    bars = conf + [c for c in recent if c["ts"] > entry[0]][-1:]
    ser = vals + [live]
    fig, (ax, axv) = plt.subplots(2, 1, figsize=(14, 9), sharex=True, gridspec_kw={"height_ratios": [4, 1]})
    x = list(range(len(bars)))
    for i, c in enumerate(bars):
        col = "#26a69a" if c["close"] >= c["open"] else "#ef5350"
        ax.vlines(i, c["low"], c["high"], color=col, linewidth=1)
        ax.bar(i, abs(c["close"] - c["open"]) or (c["high"] - c["low"]) * 0.01,
               bottom=min(c["open"], c["close"]), color=col, width=0.6)
    ax.plot(x, ser, color="#e53935", linewidth=2.8, label="EMA960 (1시간봉)")
    ax.axhspan(live * (1 - NEAR), live * (1 + NEAR), color="#e53935", alpha=0.12)
    ax.legend(loc="upper left", fontsize=10)
    ax.grid(alpha=0.3)
    ax.set_xlim(-1, len(bars) + 2)
    add_extras(ax, axv, bars)
    gap = (bars[-1]["close"] - live) / live
    slope = (ser[-1] - ser[-6]) / ser[-6] if len(ser) > 6 and ser[-6] else 0
    fig.suptitle(f"{inst.replace('-USDT-SWAP', '')}  1시간봉 EMA960 근처 · 선 {'위' if gap >= 0 else '아래'} {abs(gap) * 100:.2f}% · 선 {'↑' if slope > 0 else '↓'}",
                 fontsize=20, fontweight="bold", y=0.985)
    fig.subplots_adjust(left=0.07, right=0.86, top=0.93, bottom=0.06, hspace=0.12)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=80)
    plt.close(fig)
    buf.seek(0)
    return buf


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
            return {"ema": s.get("ema", {}), "sent": s.get("sent", {})}
    except (FileNotFoundError, ValueError):
        return {"ema": {}, "sent": {}}


def save_state(state):
    cutoff = time.time() * 1000 - 24 * 3600 * 1000
    state["sent"] = {k: v for k, v in state["sent"].items() if v >= cutoff}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def scan(state):
    try:
        symbols = ca.get_top_symbols(TOP_N)
    except Exception as e:
        print(f"[오류] 코인 목록 조회 실패: {e}")
        return
    hits, seeded, ready = [], 0, 0
    for inst in symbols:
        try:
            recent = get_recent(inst)
            if len(recent) < 10:
                continue
            entry = state["ema"].get(inst)
            if entry:
                entry = advance(entry, recent)
            if not entry:
                if seeded >= SEED_PER_RUN:
                    continue
                seeded += 1
                entry = seed(inst)
                if not entry:
                    state["ema"].pop(inst, None)
                    continue
                entry = advance(entry, recent) or entry
            state["ema"][inst] = entry
            ready += 1
            live, price = live_value(entry, recent)
            gap = (price - live) / live
            if abs(gap) <= NEAR and time.time() * 1000 - state["sent"].get(inst, 0) >= COOLDOWN_H * 3600000:
                hits.append((abs(gap), inst, entry, recent))
        except Exception as e:
            print(f"[오류] {inst}: {e}")
        time.sleep(0.1)
    hits.sort(key=lambda x: x[0])
    sent = 0
    for _, inst, entry, recent in hits[:MAX_PER_SCAN]:
        try:
            print("[960선]", inst)
            ca.send_telegram(make_chart(inst, entry, recent), "")
            state["sent"][inst] = int(time.time() * 1000)
            sent += 1
        except Exception as e:
            print(f"[오류] {inst} 전송 실패: {e}")
    print(f"{datetime.now(ca.KST):%H:%M:%S} 스캔 완료: {len(symbols)}개 중 기록 준비 {ready}개, 이번에 새로 기록 {seeded}개, 근처 {len(hits)}건, 보냄 {sent}건")


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
    recent = get_recent(inst)
    entry = advance(seed(inst), recent)
    ca.send_telegram(make_chart(inst, entry, recent), "[테스트]")


def main():
    ca.setup_korean_font()
    if "--find" in sys.argv:
        find_chat()
        return
    if not ca.TOKEN or not CHAT:
        print("[알림] TELEGRAM_TOKEN 또는 LINE960_CHAT이 비어 있어 텔레그램 전송 없이 화면에만 표시합니다.")
    if "--test" in sys.argv:
        send_test()
        return
    state = load_state()
    scan(state)
    save_state(state)


if __name__ == "__main__":
    main()
