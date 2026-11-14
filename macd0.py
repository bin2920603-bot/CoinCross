# -*- coding: utf-8 -*-
"""
OKX 선물 일봉 MACD 0선 돌파 알림
- 15분마다 전체 코인의 일봉 MACD(12,26,9) 선을 확인해서
  · 0 아래 → 위로 올라선 코인(롱 후보), 0 위 → 아래로 내려선 코인(숏 후보)을 바로 차트로 알려줌
- 차트는 근접알림·크로스알림과 같은 모양: 한 장에 4칸 (일봉·4시간봉·1시간봉·15분봉)
- 처음 켠 날은 이미 넘어 있던 코인은 기록만 하고 알리지 않음 (그 뒤에 새로 넘는 코인만 알림)
- 같은 코인·같은 방향은 6시간 안에 다시 알리지 않음 (0선 근처에서 왔다갔다 할 때 도배 방지)
- 방 번호 찾기: python macd0.py --find   /   테스트 1장: python macd0.py --test
"""
import io
import json
import os
import sys
import time
from datetime import datetime

import cross_alert as ca
import top30

# ----- 설정값 -----
SCAN_SEC = 900           # 몇 초마다 전체를 훑을지 (900 = 15분)
RUN_SEC = 20400          # 한 번 켜지면 몇 초 동안 돌지 (약 5시간 40분)
MIN_TURNOVER = 500_000   # 24시간 거래대금(USDT)이 이보다 작은 코인은 건너뜀
MIN_BARS = 60            # 일봉이 이 개수보다 적은 신규 상장 코인은 건너뜀 (MACD가 아직 안정이 안 됨)
COOLDOWN_H = 6           # 같은 코인·같은 방향은 몇 시간 안에 다시 안 보낼지
MAX_PER_SCAN = 6         # 한 번 훑을 때 보내는 최대 개수 (넘치면 다음 훑을 때)
TF_BARS = 330            # 4시간·1시간·15분봉은 이평선 계산용으로 이만큼 불러옴
FRAMES = [("일봉", "1D", "%m-%d"), ("4시간봉", "4H", "%m-%d %H시"),
          ("1시간봉", "1H", "%m-%d %H시"), ("15분봉", "15m", "%m-%d %H:%M")]   # 한 장 안의 차트 4칸 (왼쪽 위 → 오른쪽 아래)
CHAT = os.environ.get("MACD0_CHAT", "")
ca.CHAT_ID = CHAT
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "macd0_state.json")


def get_tickers():
    """전체 USDT 선물: [(코인, 24시간 거래대금)] 거래대금 큰 순"""
    r = ca.session.get(ca.OKX + "/api/v5/market/tickers", params={"instType": "SWAP"}, timeout=20)
    r.raise_for_status()
    rows = []
    for t in r.json()["data"]:
        if not t["instId"].endswith("-USDT-SWAP"):
            continue
        try:
            turn = float(t["volCcy24h"]) * float(t["last"])
        except (ValueError, KeyError, TypeError):
            continue
        if turn >= MIN_TURNOVER:
            rows.append((t["instId"], turn))
    rows.sort(key=lambda x: -x[1])
    return rows


def macd_sign(candles):
    """지금(진행 중인 일봉 포함) 일봉 MACD 선이 0 위면 1, 아래면 -1. 계산이 안 되면 None"""
    if len(candles) < MIN_BARS:
        return None
    closes = [c["close"] for c in candles]
    line, _, _ = ca.macd(closes)
    v = line[-1]
    if v > 0:
        return 1
    if v < 0:
        return -1
    return None


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
    """크로스알림·근접알림과 같은 모양: 차트 4개를 한 장(2x2)으로"""
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


def send_one(inst, up):
    sym = inst.replace("-USDT-SWAP", "")
    frames = []
    for label, bar, date_fmt in FRAMES:
        cs = load_tf(inst, bar)
        if len(cs) < 30:
            print(f"[건너뜀] {inst}: {label} {len(cs)}개뿐")
            return False
        frames.append((label, cs, date_fmt))
    if up:
        title, color, caption = "일봉 MACD 0선 위로 (롱 후보)", "#26a69a", f"📈 {sym} · 일봉 MACD 0선 위로"
    else:
        title, color, caption = "일봉 MACD 0선 아래로 (숏 후보)", "#ef5350", f"📉 {sym} · 일봉 MACD 0선 아래로"
    photo = make_card(inst, frames, title, color)
    ca.CHAT_ID = CHAT
    ca.send_telegram(photo, caption)
    return True


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, ValueError):
        s = {}
    s.setdefault("sign", {})
    s.setdefault("sent", {})
    return s


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def scan(state):
    try:
        coins = get_tickers()
    except Exception as e:
        print(f"[오류] 코인 목록 조회 실패: {e}")
        return
    first = not state["sign"]          # 처음 켠 날: 기록만 하고 알리지 않음
    now = time.time()
    hits = []
    for inst, _ in coins:
        try:
            sg = macd_sign(ca.get_candles(inst, "1D"))
        except Exception as e:
            print(f"[오류] {inst}: {e}")
            continue
        time.sleep(0.1)
        if sg is None:
            continue
        old = state["sign"].get(inst)
        state["sign"][inst] = sg
        if first or old is None or old == sg:
            continue
        key = f"{inst}|{'up' if sg > 0 else 'down'}"
        if now - state["sent"].get(key, 0) < COOLDOWN_H * 3600:
            continue
        hits.append((inst, sg > 0, key))
    sent = 0
    for inst, up, key in hits[:MAX_PER_SCAN]:
        ok = False
        try:
            ok = send_one(inst, up)
        except Exception as e:
            print(f"[오류] {inst} 전송 실패: {e}")
        if ok:
            state["sent"][key] = now
            sent += 1
        else:
            state["sign"][inst] = -1 if up else 1      # 못 보냈으면 다음 훑을 때 다시 잡히게
        time.sleep(0.5)
    # 못 보낸 코인은 다음 훑을 때 다시 잡히도록 되돌림
    for inst, up, key in hits[MAX_PER_SCAN:]:
        state["sign"][inst] = -1 if up else 1
    for k in [k for k, v in state["sent"].items() if now - v > 24 * 3600]:   # 오래된 기록 정리
        del state["sent"][k]
    save_state(state)
    print(f"{datetime.now(ca.KST):%H:%M:%S} 훑기 완료: {len(coins)}개 확인, 새로 넘은 코인 {len(hits)}개, 보냄 {sent}개" + (" (첫 실행: 기록만)" if first else ""))


def run_loop():
    state = load_state()
    start = time.time()
    while time.time() - start < RUN_SEC:
        t0 = time.time()
        try:
            scan(state)
        except Exception as e:
            print(f"[오류] 훑기 실패: {e}")
        time.sleep(max(1, SCAN_SEC - (time.time() - t0)))


def send_test():
    coins = get_tickers()
    for inst, _ in coins[:20]:
        if send_one(inst, True):
            print(f"테스트 1장 보냄: {inst}")
            return
    print("[테스트 실패] 보낼 수 있는 코인이 없음")


def main():
    ca.setup_korean_font()
    if "--find" in sys.argv:
        top30.find_chat()
        return
    if not ca.TOKEN or not CHAT:
        print("[알림] TELEGRAM_TOKEN 또는 MACD0_CHAT이 비어 있어 텔레그램 전송이 안 됩니다.")
    if "--test" in sys.argv:
        send_test()
        return
    run_loop()


if __name__ == "__main__":
    main()
