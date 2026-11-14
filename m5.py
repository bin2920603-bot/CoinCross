# -*- coding: utf-8 -*-
"""
OKX 선물 5분 급등·급락 차트 알림 (OKX 앱 Futures > Gainers/Losers 의 "5 minutes" 순위와 같은 방식)
- 1분마다 전체 코인 가격을 기록해 두고, 지금 가격을 5분 전 가격과 비교해 5분 등락률을 계산
- 5분 상승률 상위 10개 중 +1% 이상(📈), 5분 하락률 상위 10개 중 -1% 이하(📉)인 코인의 차트 4개(일봉·1시간봉·15분봉·5분봉)를 한 장(2x2)으로 바로 텔레그램으로 보냄 (크로스알림·근접알림과 같은 모양)
- 같은 코인은 30분 안에 다시 안 보냄. 24시간 거래대금이 너무 작은 코인(잔돈 코인)은 건너뜀
- 한 번 켜지면 약 5시간 40분 동안 1분마다 직접 반복 (GitHub 자동 실행이 자주 빠져서)
- 방 번호 찾기: python m5.py --find   /   테스트 1장: python m5.py --test
"""
import io
import json
import os
import sys
import time
from collections import deque
from datetime import datetime

import cross_alert as ca
import top30

# ----- 설정값 -----
TOP_N = 10               # 급등·급락 각각 몇 위까지 볼지
MIN_PCT = 1.0            # 5분에 이만큼(%) 이상 움직인 코인만 보냄 (줄이면 알림이 많아짐)
WINDOW_SEC = 300         # 5분
COOLDOWN_MIN = 30        # 한 번 보낸 코인을 몇 분 동안 다시 안 보낼지
MIN_TURNOVER = 1_000_000 # 24시간 거래대금(USDT)이 이보다 작은 코인은 건너뜀
MIN_BARS = 30            # 일봉이 이 개수보다 적은 신규 상장 코인은 건너뜀
FRAMES = [("일봉", "1D", "%m-%d"), ("1시간봉", "1H", "%m-%d %H시"), ("15분봉", "15m", "%m-%d %H:%M"), ("5분봉", "5m", "%m-%d %H:%M")]   # 한 장 안에 들어갈 차트 4개 (왼쪽 위 → 오른쪽 아래)
TF_BARS = 330            # 1시간·15분·5분봉은 이평선 계산용으로 이만큼 불러옴
TICK_SEC = 60            # 몇 초마다 확인할지
RUN_SEC = 20400          # 한 번 켜지면 몇 초 동안 돌지 (약 5시간 40분)
CHAT = os.environ.get("M5_CHAT", "")
ca.CHAT_ID = CHAT
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "m5_state.json")


def get_tickers():
    """전체 USDT 선물의 (가격, 24시간 거래대금): {코인: (last, turnover)}"""
    r = ca.session.get(ca.OKX + "/api/v5/market/tickers", params={"instType": "SWAP"}, timeout=20)
    r.raise_for_status()
    out = {}
    for t in r.json()["data"]:
        if not t["instId"].endswith("-USDT-SWAP"):
            continue
        try:
            last = float(t["last"])
            turn = float(t["volCcy24h"]) * last
        except (ValueError, KeyError, TypeError):
            continue
        if last > 0:
            out[t["instId"]] = (last, turn)
    return out


def five_min_change(hist, now):
    """5분 전(가장 가까운 기록) 대비 지금 등락률(%). 기록이 아직 5분어치가 안 되면 None"""
    if not hist or now - hist[0][0] < WINDOW_SEC - 20:
        return None
    base = None
    for ts, px in hist:
        if now - ts >= WINDOW_SEC - 20:
            base = px            # 5분보다 조금 더 오래된 것 중 가장 최근 기록
        else:
            break
    if not base:
        return None
    return (hist[-1][1] / base - 1) * 100


def load_tf(inst, bar):
    """봉 종류별 캔들 불러오기 (일봉은 top30 방식 그대로)"""
    if bar == "1D":
        return top30.load(inst)
    hist = top30.get_history(inst, TF_BARS, bar)
    r = ca.session.get(ca.OKX + "/api/v5/market/candles",
                       params={"instId": inst, "bar": bar, "limit": 100}, timeout=15)
    r.raise_for_status()
    recent = [{"ts": int(c[0]), "open": float(c[1]), "high": float(c[2]), "low": float(c[3]),
               "close": float(c[4]), "vol": float(c[5]), "quote_vol": float(c[7])} for c in reversed(r.json()["data"])]
    return top30.merge_recent(hist, recent)


def make_card(inst, frames, title, color):
    """크로스알림·근접알림과 같은 모양: 차트 4개를 한 장(2x2)으로 (왼쪽 위 → 오른쪽 아래 순서)"""
    fig = ca.plt.figure(figsize=(20, 20))
    outer = fig.add_gridspec(2, 2, wspace=0.22, hspace=0.14)
    for idx, (label, cs, date_fmt) in enumerate(frames):
        ca.draw_frame(fig, outer[idx // 2, idx % 2], label, cs, date_fmt, None, look=True)
    sym = inst.replace("-USDT-SWAP", "")
    fig.suptitle(f"{sym}  {title}", fontsize=34, fontweight="bold", color=color, y=0.985)
    fig.subplots_adjust(left=0.05, right=0.90, top=0.94, bottom=0.03)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=80)
    ca.plt.close(fig)
    buf.seek(0)
    return buf


def send_one(inst, up, rank, pct):
    daily = top30.load(inst)
    if len(daily) < MIN_BARS:
        print(f"[건너뜀] {inst}: 일봉 {len(daily)}개뿐")
        return False
    sym = inst.replace("-USDT-SWAP", "")
    icon, word = ("📈", "상승") if up else ("📉", "하락")
    frames = []
    for label, bar, date_fmt in FRAMES:
        cs = daily if bar == "1D" else load_tf(inst, bar)
        if len(cs) < MIN_BARS:
            print(f"[건너뜀] {inst}: {label} {len(cs)}개뿐")
            return False
        frames.append((label, cs, date_fmt))
    color = "#26a69a" if up else "#ef5350"
    photo = make_card(inst, frames, f"5분 {pct:+.1f}% {word} {rank}위", color)
    ca.CHAT_ID = CHAT
    ca.send_telegram(photo, f"{icon} 5분 {pct:+.1f}% {sym} · {word} {rank}위")
    return True


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def tick(history, state):
    now = time.time()
    tk = get_tickers()
    for inst, (last, _) in tk.items():
        h = history.setdefault(inst, deque())
        h.append((now, last))
        while len(h) > 1 and now - h[0][0] > WINDOW_SEC + 120:
            h.popleft()
    rows = []
    for inst, (last, turn) in tk.items():
        if turn < MIN_TURNOVER:
            continue
        pct = five_min_change(history[inst], now)
        if pct is not None:
            rows.append((inst, pct))
    ups = sorted([r for r in rows if r[1] > 0], key=lambda r: -r[1])[:TOP_N]
    downs = sorted([r for r in rows if r[1] < 0], key=lambda r: r[1])[:TOP_N]
    sent = 0
    for up, lst in ((True, ups), (False, downs)):
        for rank, (inst, pct) in enumerate(lst, 1):
            if abs(pct) < MIN_PCT:
                continue
            key = f"{inst}|{'up' if up else 'down'}"
            if now - state.get(key, 0) < COOLDOWN_MIN * 60:
                continue
            try:
                if send_one(inst, up, rank, pct):
                    state[key] = now
                    sent += 1
            except Exception as e:
                print(f"[오류] {inst}: {e}")
            time.sleep(0.5)
    for k in [k for k, v in state.items() if now - v > 6 * 3600]:   # 오래된 기록 정리
        del state[k]
    save_state(state)
    print(f"{datetime.now(ca.KST):%H:%M:%S} 확인: 대상 {len(rows)}개, 급등 후보 {len(ups)}, 급락 후보 {len(downs)}, 보냄 {sent}")


def run_loop():
    history, state = {}, load_state()
    start = time.time()
    while time.time() - start < RUN_SEC:
        t0 = time.time()
        try:
            tick(history, state)
        except Exception as e:
            print(f"[오류] 확인 실패: {e}")
        time.sleep(max(1, TICK_SEC - (time.time() - t0)))


def send_test():
    tk = get_tickers()
    for inst, (last, turn) in sorted(tk.items(), key=lambda x: -x[1][1]):
        if send_one(inst, True, 1, 1.0):
            print(f"테스트 1장 보냄: {inst}")
            return
    print("[테스트 실패] 보낼 수 있는 코인이 없음")


def main():
    ca.setup_korean_font()
    if "--find" in sys.argv:
        top30.find_chat()
        return
    if not ca.TOKEN or not CHAT:
        print("[알림] TELEGRAM_TOKEN 또는 M5_CHAT이 비어 있어 텔레그램 전송이 안 됩니다.")
    if "--test" in sys.argv:
        send_test()
        return
    run_loop()


if __name__ == "__main__":
    main()
