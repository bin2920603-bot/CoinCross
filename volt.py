# -*- coding: utf-8 -*-
"""
OKX 선물 변동성 알림
- 5분마다 거래대금 상위 코인의 1분봉을 훑어서, 지금 크게 출렁이고 거래대금도 터진 코인을 찾아 텔레그램으로 알림
- 롱/숏 자리를 알려주는 게 아니라 "지금 움직이는 코인 목록"을 알려주는 알림 (손절·익절 자리는 안 나옴)
- cross_alert.py의 차트·텔레그램 전송을 그대로 가져다 씀 (cross_alert.py는 안 바꿈)
- 방 번호 찾기: python volt.py --find   /   차트 확인용 테스트: python volt.py --test
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
RANGE_1H = 0.08        # 최근 1시간 고저폭이 8% 이상이면 큰 변동
RANGE_15M = 0.04       # 또는 최근 15분 고저폭이 4% 이상이면 급변
VOL_RATIO = 2.0        # 최근 15분 거래대금이 직전 2시간 평균(15분당)의 몇 배 이상이어야 하는지
FAST_RANGE = 0.02      # [빠른 감지] 최근 5분 고저폭이 2% 이상이고
FAST_VOL = 3.0         # [빠른 감지] 최근 5분 거래대금이 직전 2시간 평균(5분당)의 3배 이상이면 바로 알림
LOOP_SECONDS = 540     # 한 번 켜지면 이 시간(초) 동안 계속 확인 (--loop 일 때)
SCAN_EVERY = 60        # 몇 초마다 한 바퀴 돌지
COOLDOWN_MIN = 60      # 같은 코인은 이 시간(분) 안에 다시 알리지 않음
MAX_PER_SCAN = 6       # 한 번에 보내는 최대 개수 (나머지는 다음 5분에)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "volt_state.json")
CHAT = os.environ.get("VOLT_CHAT", "")
ca.CHAT_ID = CHAT      # cross_alert의 텔레그램 전송이 이 방으로 가게 함


def measure(candles):
    """1분봉으로 변동성을 잼. 모자라면 None"""
    if len(candles) < 136:
        return None
    last60 = candles[-60:]
    last15 = candles[-15:]
    lo1, hi1 = min(c["low"] for c in last60), max(c["high"] for c in last60)
    lo15, hi15 = min(c["low"] for c in last15), max(c["high"] for c in last15)
    if lo1 <= 0 or lo15 <= 0:
        return None
    prev = sum(c["quote_vol"] for c in candles[-135:-15]) / 8      # 직전 2시간, 15분당 평균
    now15 = sum(c["quote_vol"] for c in last15)
    last5 = candles[-5:]
    lo5, hi5 = min(c["low"] for c in last5), max(c["high"] for c in last5)
    prev5 = sum(c["quote_vol"] for c in candles[-125:-5]) / 24     # 직전 2시간, 5분당 평균
    now5 = sum(c["quote_vol"] for c in last5)
    return {
        "range_1h": (hi1 - lo1) / lo1, "lo1": lo1, "hi1": hi1,
        "range_15m": (hi15 - lo15) / lo15,
        "range_5m": (hi5 - lo5) / lo5 if lo5 > 0 else 0,
        "vol_ratio5": now5 / prev5 if prev5 > 0 else 0,
        "vol_ratio": now15 / prev if prev > 0 else 0,
        "change_1h": (last60[-1]["close"] - last60[0]["open"]) / last60[0]["open"],
        "price": candles[-1]["close"],
    }


def is_hot(m):
    if m is None:
        return False
    slow = m["vol_ratio"] >= VOL_RATIO and (m["range_1h"] >= RANGE_1H or m["range_15m"] >= RANGE_15M)
    fast = m["range_5m"] >= FAST_RANGE and m["vol_ratio5"] >= FAST_VOL
    return slow or fast


def make_caption(inst, m):
    arrow = "⬆️" if m["change_1h"] >= 0 else "⬇️"
    return "\n".join([
        f"🔥 변동성 {inst.replace('-USDT-SWAP', '')}",
        f"최근 1시간 고저폭: {m['range_1h'] * 100:.1f}% ({ca.fmt_price(m['lo1'])} ~ {ca.fmt_price(m['hi1'])})",
        f"최근 5분 고저폭: {m['range_5m'] * 100:.1f}% (거래대금 {m['vol_ratio5']:.1f}배)",
        f"최근 15분 고저폭: {m['range_15m'] * 100:.1f}%",
        f"1시간 변화: {arrow} {m['change_1h'] * 100:+.1f}%",
        f"거래대금: 직전 2시간 평균의 {m['vol_ratio']:.1f}배 (최근 15분)",
        f"{datetime.now(ca.KST):%Y-%m-%d %H:%M:%S}",
    ])


def make_chart(inst, c1m):
    c15 = ca.get_candles(inst, "15m")
    fig = plt.figure(figsize=(22, 10))
    outer = fig.add_gridspec(1, 2, wspace=0.12)
    ca.draw_frame(fig, outer[0, 0], "1분봉", c1m, "%H:%M")
    ca.draw_frame(fig, outer[0, 1], "15분봉", c15, "%m-%d %H:%M")
    fig.suptitle(inst.replace("-USDT-SWAP", "") + "  변동성", fontsize=30, fontweight="bold", y=0.99)
    fig.subplots_adjust(left=0.04, right=0.98, top=0.90, bottom=0.05)
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
    cutoff = time.time() * 1000 - 6 * 3600 * 1000
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in state.items() if v >= cutoff}, f)


def scan(state):
    try:
        symbols = ca.get_top_symbols(TOP_N)
    except Exception as e:
        print(f"[오류] 코인 목록 조회 실패: {e}")
        return
    hot = []
    for inst in symbols:
        try:
            c1m = ca.get_candles(inst, "1m")
            m = measure(c1m)
            if is_hot(m):
                last = state.get(inst, 0)
                if time.time() * 1000 - last >= COOLDOWN_MIN * 60000:
                    hot.append((m["range_1h"], inst, m, c1m))
        except Exception as e:
            print(f"[오류] {inst}: {e}")
        time.sleep(0.1)
    hot.sort(key=lambda x: -max(x[0], x[2]["range_5m"]))
    sent = 0
    for _, inst, m, c1m in hot[:MAX_PER_SCAN]:
        try:
            caption = make_caption(inst, m)
            print("[변동성]", caption.replace("\n", " | "))
            ca.send_telegram(make_chart(inst, c1m), caption)
            state[inst] = int(time.time() * 1000)
            sent += 1
        except Exception as e:
            print(f"[오류] {inst} 전송 실패: {e}")
    print(f"{datetime.now(ca.KST):%H:%M:%S} 스캔 완료: {len(symbols)}개 확인, 걸린 코인 {len(hot)}개, 보냄 {sent}개")


def find_chat():
    """봇이 아는 방(그룹)을 찾아서 번호를 보여줌 (방에서 /start@봇이름 을 한 번 보낸 뒤 실행)"""
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
        try:    # 방에도 번호를 직접 말해 줌 (로그가 안 보일 때용)
            ca.session.post(f"https://api.telegram.org/bot{ca.TOKEN}/sendMessage",
                            data={"chat_id": cid, "text": f"이 방 번호: {cid}"}, timeout=20)
        except Exception as e:
            print("[오류] 방에 번호 보내기 실패:", e)
    if not seen:
        print("[못 찾음] 방에서 /start@봇이름 을 보낸 뒤 24시간 안에 다시 실행하세요")


def send_test():
    inst = ca.get_top_symbols(1)[0]
    c1m = ca.get_candles(inst, "1m")
    m = measure(c1m)
    ca.send_telegram(make_chart(inst, c1m), "[테스트] 실제 알림이 아닙니다\n" + make_caption(inst, m))


def main():
    ca.setup_korean_font()
    if "--find" in sys.argv:
        find_chat()
        return
    if not ca.TOKEN or not CHAT:
        print("[알림] TELEGRAM_TOKEN 또는 VOLT_CHAT이 비어 있어 텔레그램 전송 없이 화면에만 표시합니다.")
    if "--test" in sys.argv:
        send_test()
        return
    state = load_state()
    if "--loop" in sys.argv:
        end = time.time() + LOOP_SECONDS
        while True:
            t0 = time.time()
            scan(state)
            save_state(state)
            wait = SCAN_EVERY - (time.time() - t0)
            if time.time() + max(wait, 0) >= end:
                break
            if wait > 0:
                time.sleep(wait)
        return
    scan(state)
    save_state(state)


if __name__ == "__main__":
    main()
