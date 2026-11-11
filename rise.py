# -*- coding: utf-8 -*-
"""
OKX 선물 전일대비 알림 (밑에서 잡기)
- 한국시간 0시 기준 전일대비 +1%~+5% (상승) 또는 -1%~-5% (하락)인 코인 중
  최근 1시간 거래대금이 평소(직전 최대 4시간 평균 1시간치)의 1.5배 이상이고 3억원(약 20만 USDT) 이상인 코인만 알림
- 한 번에 튀는 코인도, 15분씩 야금야금 늘어나는 코인도 1시간 합계로 같이 잡음
- 같은 코인은 같은 방향으로 하루 한 번만 알림
- 알림이 온 뒤 1시간이 지나면 알림 방향으로 +1% 이상 갔는지 기록해서 성적을 알림에 붙임
- 매일 밤 9시(한국시간) 이후 첫 확인 때 성적표(막대 그림)를 같은 방에 한 번 올림
- cross_alert.py의 차트·텔레그램 전송을 그대로 가져다 씀 (차트는 새 모양: 박스·구름·▲▼·하이킨아시)
- 방 번호 찾기는 volt.py --find 를 쓰면 됨 / 차트 확인용 테스트: python rise.py --test
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
TOP_N = 300             # 거래대금 상위 몇 개 코인을 볼지
MIN_CHG = 0.01          # 전일대비 최소 1% (오르거나 내리거나)
MAX_CHG = 0.05          # 전일대비 최대 5% (이미 많이 오른 건 제외)
BASE_FIELD = "sodUtc8"  # 전일대비 기준: sodUtc8=한국시간 0시 시가 / open24h=24시간 전 가격
VOL_RATIO = 1.5         # 최근 1시간 거래대금이 평소 1시간의 몇 배 이상이어야 하는지
MIN_HOUR_USDT = 200000  # 최근 1시간 거래대금 최소 (USDT, 약 3억원)
BASE_MAX_MIN = 240      # 평소 = 직전 최대 몇 분(1분봉 개수)의 평균
WIN_PCT = 0.01          # 알림 1시간 뒤 알림 방향으로 이만큼 가면 승
REPORT_HOUR = 21        # 매일 이 시각(한국시간) 이후 첫 확인 때 성적표를 방에 올림
COOLDOWN_MIN = 1440     # 같은 코인·같은 방향은 이 시간(분) 안에 다시 알리지 않음
MAX_PER_SCAN = 6        # 한 번에 보내는 최대 개수
LOOP_SECONDS = 540      # 한 번 켜지면 이 시간(초) 동안 계속 확인 (--loop 일 때)
SCAN_EVERY = 60         # 몇 초마다 한 바퀴 돌지
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rise_state.json")
CHAT = os.environ.get("RISE_CHAT", "")
ca.CHAT_ID = CHAT       # cross_alert의 텔레그램 전송이 이 방으로 가게 함


def get_tickers():
    """전체 선물 시세 한 번에: {코인: (전일대비, 현재가)}"""
    r = ca.session.get("https://www.okx.com/api/v5/market/tickers",
                       params={"instType": "SWAP"}, timeout=20).json()
    out = {}
    for t in r.get("data", []):
        inst = t.get("instId", "")
        if not inst.endswith("-USDT-SWAP"):
            continue
        try:
            base = float(t.get(BASE_FIELD) or 0)
            last = float(t["last"])
        except (ValueError, KeyError):
            continue
        if base > 0 and last > 0:
            out[inst] = ((last - base) / base, last)
    return out


def measure(candles):
    """1분봉으로 최근 1시간 거래대금을 평소와 비교. 모자라면 None"""
    n = len(candles)
    if n < 180:
        return None
    prev_n = min(n - 60, BASE_MAX_MIN)
    prev = candles[-(60 + prev_n):-60]
    last60 = candles[-60:]
    prev_hour = sum(c["quote_vol"] for c in prev) / prev_n * 60
    now_hour = sum(c["quote_vol"] for c in last60)
    steps = [sum(c["quote_vol"] for c in last60[i * 15:(i + 1) * 15]) for i in range(4)]
    return {
        "now_hour": now_hour,
        "ratio": now_hour / prev_hour if prev_hour > 0 else 0,
        "steps": steps,
        "price": candles[-1]["close"],
    }


def is_hit(m):
    return m is not None and m["ratio"] >= VOL_RATIO and m["now_hour"] >= MIN_HOUR_USDT


def fmt_man(v):
    return f"{v / 10000:.1f}만"


def stat_line(state, d):
    n, w = state["stats"][d]
    name = "상승" if d == "up" else "하락"
    if n == 0:
        return f"{name} 알림 성적: 아직 데이터 모으는 중"
    tail = " (20번 넘게 쌓인 뒤부터 참고)" if n < 20 else ""
    return f"{name} 알림 성적: 1시간 뒤 {WIN_PCT * 100:.0f}%↑ {n}번 중 {w}번 ({w / n * 100:.0f}%){tail}"


def make_caption(inst, chg, m, state):
    d = "up" if chg > 0 else "down"
    head = "📈 상승 초기" if d == "up" else "📉 하락 초기"
    return "\n".join([
        f"{head} {inst.replace('-USDT-SWAP', '')}",
        f"전일대비: {chg * 100:+.2f}% (한국시간 0시 기준)",
        f"최근 1시간 거래대금: 평소의 {m['ratio']:.1f}배 ({fmt_man(m['now_hour'])} USDT)",
        "15분씩: " + " → ".join(fmt_man(v) for v in m["steps"]),
        stat_line(state, d),
        f"{datetime.now(ca.KST):%Y-%m-%d %H:%M:%S}",
    ])


def make_chart(inst, c1m):
    c15 = ca.get_candles(inst, "15m")
    fig = plt.figure(figsize=(22, 10))
    outer = fig.add_gridspec(1, 2, wspace=0.12)
    ca.draw_frame(fig, outer[0, 0], "1분봉", c1m, "%H:%M", look=True)
    ca.draw_frame(fig, outer[0, 1], "15분봉", c15, "%m-%d %H:%M", look=True)
    fig.suptitle(inst.replace("-USDT-SWAP", "") + "  전일대비", fontsize=30, fontweight="bold", y=0.99)
    fig.subplots_adjust(left=0.04, right=0.98, top=0.90, bottom=0.05)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=80)
    plt.close(fig)
    buf.seek(0)
    return buf


def make_report_chart(state):
    """성적표 막대 그림: 상승/하락 알림이 1시간 뒤 이긴 비율"""
    labels, vals, texts, colors = [], [], [], []
    for d, name in (("up", "상승 알림"), ("down", "하락 알림")):
        n, w = state["stats"][d]
        labels.append(name)
        vals.append(w / n * 100 if n else 0)
        texts.append(f"{w / n * 100:.0f}%  ({w}/{n}번)" if n else "데이터 없음")
        colors.append("#d62728" if d == "up" else "#1f77b4")
    fig, ax = plt.subplots(figsize=(8, 3.2))
    ax.barh(labels, vals, color=colors)
    for i, (v, t) in enumerate(zip(vals, texts)):
        ax.text(min(v, 100) + 1, i, t, va="center", fontsize=14)
    ax.set_xlim(0, 135)
    ax.set_xticks([0, 20, 40, 60, 80, 100])
    ax.invert_yaxis()
    ax.set_title(f"전일대비 알림 성적 (알림 1시간 뒤 {WIN_PCT * 100:.0f}%↑ 비율)", fontsize=15)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=80)
    plt.close(fig)
    buf.seek(0)
    return buf


def report_caption(state):
    return "\n".join([
        "📊 전일대비 알림 성적표",
        stat_line(state, "up"),
        stat_line(state, "down"),
        f"{datetime.now(ca.KST):%Y-%m-%d %H:%M}",
    ])


def maybe_report(state):
    """하루 한 번(밤 9시 이후 첫 확인) 성적표를 방에 올림. 쌓인 기록이 없으면 건너뜀"""
    now = datetime.now(ca.KST)
    today = f"{now:%Y-%m-%d}"
    if now.hour < REPORT_HOUR or state.get("last_report") == today:
        return
    if state["stats"]["up"][0] + state["stats"]["down"][0] == 0:
        return
    try:
        ca.send_telegram(make_report_chart(state), report_caption(state))
        state["last_report"] = today
    except Exception as e:
        print(f"[오류] 성적표 전송 실패: {e}")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, ValueError):
        s = {}
    s.setdefault("sent", {})
    s.setdefault("pending", [])
    s.setdefault("stats", {"up": [0, 0], "down": [0, 0]})
    return s


def save_state(state):
    cutoff = time.time() * 1000 - COOLDOWN_MIN * 60000
    state["sent"] = {k: v for k, v in state["sent"].items() if v >= cutoff}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def settle(state, tickers):
    """알림 1시간 뒤 성적 기록"""
    now = time.time() * 1000
    keep = []
    for p in state["pending"]:
        age = now - p["t"]
        if age < 60 * 60000:
            keep.append(p)
            continue
        t = tickers.get(p["inst"])
        if t is None:
            if age < 3 * 3600000:
                keep.append(p)
            continue
        ret = (t[1] - p["price"]) / p["price"] * (1 if p["dir"] == "up" else -1)
        s = state["stats"][p["dir"]]
        s[0] += 1
        s[1] += 1 if ret >= WIN_PCT else 0
    state["pending"] = keep


def scan(state):
    try:
        tickers = get_tickers()
        symbols = ca.get_top_symbols(TOP_N)
    except Exception as e:
        print(f"[오류] 시세 조회 실패: {e}")
        return
    settle(state, tickers)
    now = time.time() * 1000
    hits = []
    for inst in symbols:
        t = tickers.get(inst)
        if not t or not (MIN_CHG <= abs(t[0]) <= MAX_CHG):
            continue
        d = "up" if t[0] > 0 else "down"
        if now - state["sent"].get(f"{inst}:{d}", 0) < COOLDOWN_MIN * 60000:
            continue
        try:
            c1m = ca.get_candles(inst, "1m")
            m = measure(c1m)
            if is_hit(m):
                hits.append((m["ratio"], inst, d, t[0], m, c1m))
        except Exception as e:
            print(f"[오류] {inst}: {e}")
        time.sleep(0.1)
    hits.sort(key=lambda x: -x[0])
    sent = 0
    for _, inst, d, chg, m, c1m in hits[:MAX_PER_SCAN]:
        try:
            caption = make_caption(inst, chg, m, state)
            print("[전일대비]", caption.replace("\n", " | "))
            ca.send_telegram(make_chart(inst, c1m), caption)
            state["sent"][f"{inst}:{d}"] = int(time.time() * 1000)
            state["pending"].append({"t": int(time.time() * 1000), "inst": inst, "dir": d, "price": m["price"]})
            sent += 1
        except Exception as e:
            print(f"[오류] {inst} 전송 실패: {e}")
    print(f"{datetime.now(ca.KST):%H:%M:%S} 스캔 완료: 전일대비 후보 확인, 걸린 코인 {len(hits)}개, 보냄 {sent}개")
    maybe_report(state)


def send_test():
    tickers = get_tickers()
    inst = ca.get_top_symbols(1)[0]
    c1m = ca.get_candles(inst, "1m")
    m = measure(c1m)
    chg = tickers.get(inst, (0.0, 0))[0]
    state = load_state()
    ca.send_telegram(make_chart(inst, c1m), "[테스트] 실제 알림이 아닙니다\n" + make_caption(inst, chg, m, state))
    ca.send_telegram(make_report_chart(state), "[테스트] 실제 성적표가 아닙니다\n" + report_caption(state))


def main():
    ca.setup_korean_font()
    if not ca.TOKEN or not CHAT:
        print("[알림] TELEGRAM_TOKEN 또는 RISE_CHAT이 비어 있어 텔레그램 전송 없이 화면에만 표시합니다.")
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
