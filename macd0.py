# -*- coding: utf-8 -*-
"""
OKX 선물 일봉 MACD 0선 근처 알림
- 15분마다 전체 코인의 일봉 MACD(12,26,9) 선을 확인해서
  · MACD 선이 0선 근처로 '새로 들어온' 코인을 차트로 알려줌 (0선에서 먼 코인은 안 보냄)
  · 오늘 MACD가 어제보다 높으면 롱 후보(오르는 중), 낮으면 숏 후보(내리는 중)
- '근처'는 그 코인의 최근 90일 MACD 최대 폭의 10% 이내 (코인마다 MACD 크기가 달라서 비율로 잡음)
- 차트는 근접알림·크로스알림과 같은 모양: 한 장에 4칸 (일봉·4시간봉·1시간봉·15분봉)
- 처음 켠 날은 이미 근처에 있던 코인은 기록만 하고 알리지 않음 (그 뒤에 새로 들어오는 코인만 알림)
- 같은 코인·같은 방향은 12시간 안에 다시 알리지 않음
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
COOLDOWN_H = 12          # 같은 코인·같은 방향은 몇 시간 안에 다시 안 보낼지
NEAR_RATIO = 0.10        # MACD가 최근 최대 폭의 이 비율 이내면 0선 근처 (줄이면 더 가까운 것만)
LOOKBACK = 90            # 최대 폭을 볼 일봉 개수
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


def macd_info(candles):
    """일봉 MACD 선 상태: (0선과의 거리 비율, 방향) / 방향 1=오르는 중, -1=내리는 중 / 계산 안 되면 None
    거리 비율 = |지금 MACD| ÷ 최근 90일 MACD 최대 폭 (작을수록 0선에 가까움)"""
    if len(candles) < MIN_BARS:
        return None
    closes = [c["close"] for c in candles]
    line, _, _ = ca.macd(closes)
    amp = max(abs(v) for v in line[-LOOKBACK:])
    if amp <= 0 or line[-1] == line[-2]:
        return None
    return abs(line[-1]) / amp, (1 if line[-1] > line[-2] else -1)


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
        title, color, caption = "일봉 MACD 0선 근처 · 오르는 중 (롱 후보)", "#26a69a", f"📈 {sym} · 일봉 MACD 0선 근처, 오르는 중"
    else:
        title, color, caption = "일봉 MACD 0선 근처 · 내리는 중 (숏 후보)", "#ef5350", f"📉 {sym} · 일봉 MACD 0선 근처, 내리는 중"
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
    s.setdefault("zone", {})
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
    first = not state["zone"]          # 처음 켠 날: 기록만 하고 알리지 않음
    now = time.time()
    hits = []
    for inst, _ in coins:
        try:
            info = macd_info(ca.get_candles(inst, "1D"))
        except Exception as e:
            print(f"[오류] {inst}: {e}")
            continue
        time.sleep(0.1)
        if info is None:
            continue
        ratio, d = info
        near = ratio <= NEAR_RATIO
        was = state["zone"].get(inst)
        state["zone"][inst] = 1 if near else 0
        if first or not near or was == 1 or was is None:     # 근처로 '새로 들어온' 코인만
            continue
        key = f"{inst}|{'up' if d > 0 else 'down'}"
        if now - state["sent"].get(key, 0) < COOLDOWN_H * 3600:
            continue
        hits.append((ratio, inst, d > 0, key))
    hits.sort()                                  # 0선에 가까운 것부터
    sent = 0
    for ratio, inst, up, key in hits[:MAX_PER_SCAN]:
        ok = False
        try:
            ok = send_one(inst, up)
        except Exception as e:
            print(f"[오류] {inst} 전송 실패: {e}")
        if ok:
            state["sent"][key] = now
            sent += 1
        else:
            state["zone"][inst] = 0                # 못 보냈으면 다음 훑을 때 다시 잡히게
        time.sleep(0.5)
    for ratio, inst, up, key in hits[MAX_PER_SCAN:]:
        state["zone"][inst] = 0                    # 못 보낸 코인은 다음 훑을 때 다시 잡히게
    for k in [k for k, v in state["sent"].items() if now - v > 24 * 3600]:   # 오래된 기록 정리
        del state["sent"][k]
    save_state(state)
    print(f"{datetime.now(ca.KST):%H:%M:%S} 훑기 완료: {len(coins)}개 확인, 0선 근처로 새로 온 코인 {len(hits)}개, 보냄 {sent}개" + (" (첫 실행: 기록만)" if first else ""))


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
    """거래대금 큰 코인 중 지금 실제로 0선 근처인 코인을 골라, 실제 방향대로 1장 보냄"""
    best = None
    for inst, _ in get_tickers()[:100]:
        try:
            info = macd_info(ca.get_candles(inst, "1D"))
        except Exception:
            continue
        time.sleep(0.1)
        if info is None:
            continue
        if best is None or info[0] < best[0]:
            best = (info[0], inst, info[1] > 0)
        if info[0] <= NEAR_RATIO:
            best = (info[0], inst, info[1] > 0)
            break
    if best is None:
        print("[테스트 실패] 계산할 수 있는 코인이 없음")
        return
    ratio, inst, up = best
    if send_one(inst, up):
        print(f"테스트 1장 보냄: {inst} (0선과의 거리 {ratio:.0%}, {'롱' if up else '숏'} 후보)")


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
