# -*- coding: utf-8 -*-
"""
OKX 선물 선 터치 알림 (일봉·4시간봉·1시간봉)
- 거래대금 상위 코인의 일봉·4시간·1시간봉을 훑어서, 지금 봉이 아래 중 하나에 닿으면 차트(4칸)로 알려줌
  EMA7·20·50·120·200, 거래량가중100·200, 하락/상승 추세선, 지지·저항 박스
- 거름 조건 없음. 같은 코인·같은 선·같은 봉은 일봉 24시간 / 4시간봉 4시간 / 1시간봉 1시간 안에 다시 알리지 않음
- 방 번호 찾기: python line_touch.py --find   /   테스트: python line_touch.py --test
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
import zones            # 지지·저항 박스, 추세선 그리기 (zones.py)
import line_ma as lm    # 봉 받기·거래량가중 계산·방 찾기를 그대로 빌려 씀

# ----- 설정값 -----
TOP_N = 100            # 거래대금 상위 몇 개 코인을 볼지
TOUCH = 0.001          # 선 위아래 0.1% 안에 꼬리나 몸통이 닿으면 '닿음'
TOUCH_TREND = 0.002    # 추세선은 0.2%
NOW = 0.005            # 닿았더라도 지금 가격이 선에서 0.5% 넘게 멀어졌으면 알리지 않음
ZONE_PAD = 0.003       # 박스 위아래 여유 0.3%
ZPIVOT = 5             # 지지·저항: 앞뒤 5봉보다 높(낮)으면 고점(저점)
ZONE_PCT = 0.015       # 가격이 이 비율 안에 모이면 같은 박스로 묶음
TREND_K = 2
TREND_TOL = 0.03
TREND_BAD = 1
TREND_MIN_GAP = 8
SHOW = 120             # 추세선·박스를 찾을 때 보는 봉 개수
PANEL_BARS = 100       # 차트 한 칸에 보여줄 봉 개수
MAX_PER_SCAN = 10      # 한 번 훑을 때 최대 알림 개수
BARS = [("1D", "일봉", 24), ("4H", "4시간봉", 4), ("1H", "1시간봉", 1)]   # (봉, 이름, 다시 알림까지 쉬는 시간)
CARD = [("1D", "일봉"), ("4H", "4시간봉"), ("1H", "1시간봉"), ("15m", "15분봉")]
CHAT = os.environ.get("LINETOUCH_CHAT", "")    # 새 방 번호 (GitHub 비밀 칸)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "line_touch_state.json")
COLORS = {"EMA7": "#d81b60", "EMA20": "#fb8c00", "EMA50": "#43a047", "EMA120": "#1e88e5",
          "EMA200": "#e53935", "거가중100": "#8e24aa", "거가중200": "#00897b"}


# ---------------- 선 계산 ----------------
def line_values(cs):
    """봉이 충분한 선만 계산해서 {이름: 값 목록}으로 돌려줌"""
    closes = [c["close"] for c in cs]
    out = {}
    for n in (7, 20, 50, 120, 200):
        if len(cs) >= n + 10:
            out[f"EMA{n}"] = ca.ema(closes, n)
    for n in (100, 200):
        if len(cs) >= n + 10:
            out[f"거가중{n}"] = lm.vwap_series(cs, n)
    return out


def find_swings(vals, k, high):
    out = []
    for i in range(k, len(vals) - k):
        w = vals[i - k:i + k + 1]
        if (high and vals[i] == max(w)) or ((not high) and vals[i] == min(w)):
            out.append(i)
    return out


def find_trendline(d, kind):
    """kind='down': 고점을 이은 하락 추세선 / 'up': 저점을 이은 상승 추세선.
    돌려주는 값: (시작 위치, 시작 가격, 끝 위치, 끝 가격). 못 찾으면 None"""
    n = len(d)
    highs = [c["high"] for c in d]
    lows = [c["low"] for c in d]
    if n < 30:
        return None
    tol = TREND_TOL * (max(highs) - min(lows))
    vals = highs if kind == "down" else lows
    swings = find_swings(vals, TREND_K, kind == "down")
    best = None
    for b in swings[-4:]:
        for a in swings:
            if b - a < TREND_MIN_GAP:
                break
            if kind == "down" and vals[a] <= vals[b]:
                continue
            if kind == "up" and vals[a] >= vals[b]:
                continue
            slope = (vals[b] - vals[a]) / (b - a)
            bad = 0
            for x in range(a, n - 1):
                line = vals[a] + slope * (x - a)
                if kind == "down" and vals[x] > line + tol:
                    bad += 1
                if kind == "up" and vals[x] < line - tol:
                    bad += 1
            if bad > TREND_BAD:
                continue
            span = b - a
            if best is None or span > best[0]:
                best = (span, a, vals[a], n - 1, vals[a] + slope * (n - 1 - a))
    return best[1:] if best else None


def find_zones(d):
    """비슷한 가격의 고점·저점을 묶어 (아래, 위) 박스 목록으로 돌려줌"""
    n = len(d)
    hv = [c["high"] for c in d]
    lv = [c["low"] for c in d]
    pts = []
    for i in range(ZPIVOT, n - ZPIVOT):
        if hv[i] >= max(hv[i - ZPIVOT:i + ZPIVOT + 1]):
            pts.append(hv[i])
        if lv[i] <= min(lv[i - ZPIVOT:i + ZPIVOT + 1]):
            pts.append(lv[i])
    pts.sort()
    groups = []
    for p in pts:
        if groups and p <= groups[-1][-1] * (1 + ZONE_PCT):
            groups[-1].append(p)
        else:
            groups.append([p])
    return [(min(g), max(g)) for g in groups]


def find_touches(cs):
    """지금(진행 중인) 봉이 닿은 것들: [(이름, 선값)]"""
    last = cs[-1]
    lo, hi, cl = last["low"], last["high"], last["close"]
    hits = []
    for name, s in line_values(cs).items():
        v = s[-1]
        if v and v > 0 and lo <= v * (1 + TOUCH) and hi >= v * (1 - TOUCH) and abs(cl - v) / v <= NOW:
            hits.append((name, v))
    d = cs[-SHOW:]
    for kind, label in (("down", "하락추세선"), ("up", "상승추세선")):
        t = find_trendline(d, kind)
        if t:
            v = t[3]
            if v > 0 and lo <= v * (1 + TOUCH_TREND) and hi >= v * (1 - TOUCH_TREND) and abs(cl - v) / v <= NOW:
                hits.append((label, v))
    if len(d) >= 30:
        for zl, zh in find_zones(d):
            if lo <= zh * (1 + ZONE_PAD) and hi >= zl * (1 - ZONE_PAD):
                if cl > zh * (1 + NOW) or cl < zl * (1 - NOW):
                    continue
                label = "지지박스" if cl >= zh else "저항박스" if cl <= zl else "박스 안"
                hits.append((f"{label} {zl:.4g}", (zl + zh) / 2))
    return hits


# ---------------- 그림 ----------------
def draw_panel(ax, cs, bn, hit_name, hit_val):
    n = min(PANEL_BARS, len(cs))
    v = cs[-n:]
    for i, c in enumerate(v):
        col = "#26a69a" if c["close"] >= c["open"] else "#ef5350"
        ax.vlines(i, c["low"], c["high"], color=col, linewidth=1)
        ax.bar(i, abs(c["close"] - c["open"]) or (c["high"] - c["low"]) * 0.01,
               bottom=min(c["open"], c["close"]), color=col, width=0.6)
    for name, s in line_values(cs).items():
        seg = s[-n:]
        pts = [(i, y) for i, y in enumerate(seg) if y]
        if pts:
            main = name == hit_name
            ax.plot([p[0] for p in pts], [p[1] for p in pts], color=COLORS.get(name, "#888"),
                    linewidth=3.2 if main else 0.9, alpha=1 if main else 0.55, label=name if main else None)
    lo = min(c["low"] for c in v)
    hi = max(c["high"] for c in v)
    pad = (hi - lo) * 0.06
    ax.set_ylim(lo - pad, hi + pad)
    if hit_val:
        ax.axhline(hit_val, color="#FFC107", linestyle="--", linewidth=1.4)
    ax.set_xlim(-1, len(v) + 2)
    try:
        zones.overlay(ax, cs, n)
    except Exception as e:
        print(f"[박스 오류] {e}")
    ax.set_title(f"{bn}" + (f"  ★ {hit_name}" if hit_name else ""), fontsize=14, loc="left",
                 fontweight="bold" if hit_name else "normal")
    if hit_name in COLORS:
        ax.legend(loc="upper left", fontsize=10)
    ax.grid(alpha=0.3)
    ax.tick_params(labelsize=8)


def make_card(data, hit_bar, hit_name, hit_val, title):
    fig, axes = plt.subplots(2, 2, figsize=(14, 11))
    for ax, (bar, bn) in zip(axes.flatten(), CARD):
        cs = data.get(bar)
        if not cs or len(cs) < 30:
            ax.set_title(f"{bn} (데이터 없음)", fontsize=12, loc="left")
            continue
        star = bar == hit_bar
        draw_panel(ax, cs, bn, hit_name if star else None, hit_val if star else None)
    fig.suptitle(title, fontsize=18, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=80)
    plt.close(fig)
    buf.seek(0)
    return buf


# ---------------- 상태 · 전송 ----------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state):
    cutoff = time.time() * 1000 - 48 * 3600 * 1000
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in state.items() if v >= cutoff}, f)


def scan(state):
    try:
        symbols = ca.get_top_symbols(TOP_N)
    except Exception as e:
        print(f"[오류] 코인 목록 조회 실패: {e}")
        return
    now = time.time() * 1000
    groups = {}
    for inst in symbols:
        for bar, bn, cool_h in BARS:
            try:
                cs = lm.get_candles(inst, bar)
                if len(cs) < 60:
                    continue
                for name, val in find_touches(cs):
                    key = f"{inst}|{bar}|{name}"
                    if now - state.get(key, 0) >= cool_h * 3600000:
                        gap = abs(cs[-1]["close"] - val) / val
                        groups.setdefault((inst, bar, bn), []).append((gap, name, val, key, cs[-1]["close"]))
            except Exception as e:
                print(f"[오류] {inst} {bar}: {e}")
            time.sleep(0.1)
    order = sorted(groups.items(), key=lambda kv: min(h[0] for h in kv[1]))
    sent = 0
    for (inst, bar, bn), hs in order[:MAX_PER_SCAN]:
        try:
            hs.sort(key=lambda h: h[0])
            gap, name, val, key, price = hs[0]
            coin = inst.replace("-USDT-SWAP", "")
            names = " · ".join(h[1] for h in hs)
            print("[선터치]", inst, bn, names)
            data = {}
            for b, _ in CARD:
                data[b] = lm.get_candles(inst, b)
                time.sleep(0.1)
            title = f"{coin}  {bn} 터치 · {names}"
            buf = make_card(data, bar, name, val, title)
            ca.CHAT_ID = CHAT
            ca.send_telegram(buf, f"{coin} · {bn} · {names} · 현재가 {price:.6g}")
            for h in hs:
                state[h[3]] = int(time.time() * 1000)
            sent += 1
        except Exception as e:
            print(f"[오류] {inst} 전송 실패: {e}")
    print(f"{datetime.now(ca.KST):%H:%M:%S} 스캔 완료: {len(symbols)}개 확인, 닿음 {len(order)}건, 보냄 {sent}건")


def send_test():
    inst = ca.get_top_symbols(1)[0]
    data = {b: lm.get_candles(inst, b) for b, _ in CARD}
    buf = make_card(data, "1D", None, None, f"[테스트] {inst.replace('-USDT-SWAP', '')} 선터치 알림")
    ca.CHAT_ID = CHAT
    ca.send_telegram(buf, "[테스트] 선터치 알림 방 확인")


def main():
    ca.setup_korean_font()
    if "--find" in sys.argv:
        lm.find_chat()
        return
    if not ca.TOKEN or not CHAT:
        print("[알림] TELEGRAM_TOKEN 또는 방 번호(LINETOUCH_CHAT)가 비어 있어 텔레그램 전송이 안 됩니다.")
    if "--test" in sys.argv:
        send_test()
        return
    state = load_state()
    scan(state)
    save_state(state)


if __name__ == "__main__":
    main()
