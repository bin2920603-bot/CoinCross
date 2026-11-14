# -*- coding: utf-8 -*-
"""
OKX 선물 전일대비(상승률) 상위 30개 코인 차트 알림
- 5분마다 OKX 24시간 등락률(OKX 앱·거래대금 장부의 '전일대비'와 같은 값) 상위 30개를 확인
- 30위 안에 새로 들어온 코인의 일봉 차트를 바로 텔레그램으로 보냄 (사진 1장, 글은 한 줄: 순위·코인·등락률)
- 이미 보낸 코인은 2시간 동안 다시 안 보냄. 단, 순위가 10계단 이상 올라가면 다시 보냄
- 차트 모양은 top30.py의 make_chart를 가져다 씀 (top30.py는 안 바꿈)
- 방 번호 찾기: python chg30.py --find   /   테스트 1장: python chg30.py --test
"""
import json
import os
import sys
import time
from datetime import datetime

import cross_alert as ca
import top30
import zones

# ----- 설정값 -----
TOP_N = 30              # 전일대비 몇 위까지 볼지 (늘리고 싶으면 숫자만 바꾸면 됨)
MIN_BARS = 30           # 일봉이 이 개수보다 적은 신규 상장 코인은 건너뜀
COOLDOWN_H = 2          # 한 번 보낸 코인을 몇 시간 동안 다시 안 보낼지
JUMP = 10               # 순위가 이만큼 이상 올라가면 쿨타임 중이라도 다시 보냄
CHAT = os.environ.get("CHG30_CHAT", "")
ca.CHAT_ID = CHAT
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chg30_state.json")


def get_ranking():
    """전일대비(24시간 등락률) 높은 순: [(코인, 등락률%, 현재가)...]"""
    r = ca.session.get(ca.OKX + "/api/v5/market/tickers", params={"instType": "SWAP"}, timeout=20)
    r.raise_for_status()
    rows = []
    for t in r.json()["data"]:
        if not t["instId"].endswith("-USDT-SWAP"):
            continue
        try:
            last, open24 = float(t["last"]), float(t["open24h"])
        except (ValueError, KeyError):
            continue
        if open24 > 0 and last > 0:
            rows.append((t["instId"], (last / open24 - 1) * 100, last))
    rows.sort(key=lambda x: -x[1])
    return rows[:TOP_N]


def add_channel(ax, cs):
    """채널: 저점끼리 이은 추세선(초록)과 평행한 선을 가장 높은 고점에, 고점끼리 이은 추세선(빨강)과 평행한 선을 가장 낮은 저점에 그어 채널 모양을 만듦"""
    n = min(zones.SHOW_BARS, len(cs))
    view = cs[-n:]
    price = view[-1]["close"]
    hi, lo = zones.pivots(view)
    y0, y1 = ax.get_ylim()
    x0, x1 = ax.get_xlim()
    for pts, upper in ((lo, True), (hi, False)):
        t = zones.trend_line(pts, n, price, y0, y1)
        if not t:
            continue
        xa, ya, end = t
        slope = (end - ya) / (n + 2 - xa)
        if upper:     # 저점선 위쪽에 평행선: 시작점 이후 가장 높은 고점까지
            off = max(view[i]["high"] - (ya + slope * (i - xa)) for i in range(int(xa), n))
        else:         # 고점선 아래쪽에 평행선: 시작점 이후 가장 낮은 저점까지
            off = min(view[i]["low"] - (ya + slope * (i - xa)) for i in range(int(xa), n))
        if abs(off) < price * 0.005:
            continue
        # 평행선이 실제로 2곳 이상 닿을 때만 채널로 인정 (한 점에만 닿으면 그리지 않음)
        opp = hi if upper else lo
        key = "high" if upper else "low"
        touch = sum(1 for i, _ in opp if i >= xa and abs(view[i][key] - (ya + off + slope * (i - xa))) <= price * 0.01)
        if touch < 2:
            continue
        xs = [xa, n + 2]
        base = [ya, end]
        par = [ya + off, end + off]
        ax.plot(xs, par, color="#8e24aa", linestyle="--", linewidth=1.6, zorder=2)
        ax.fill_between(xs, base, par, color="#8e24aa", alpha=0.07, zorder=0)
    ax.set_ylim(y0, y1)
    ax.set_xlim(x0, x1)


def make_chart(inst, cs, rank, chg):
    """top30의 차트를 그대로 쓰고 제목만 '전일대비 N위'로 바꿈"""
    orig = top30.plt.subplots

    def sub(*a, **k):
        fig, axes = orig(*a, **k)
        osave = fig.savefig

        def save(*aa, **kk):
            try:
                add_channel(axes[0], cs)
            except Exception as e:
                print(f"[채널 오류] {inst}: {e}")
            fig.suptitle(f"전일대비 {rank}위  {inst.replace('-USDT-SWAP', '')}  일봉  "
                         f"{ca.fmt_price(cs[-1]['close'])}  ({chg:+.1f}%)",
                         fontsize=20, fontweight="bold", y=0.985)
            return osave(*aa, **kk)
        fig.savefig = save
        return fig, axes
    top30.plt.subplots = sub
    try:
        return top30.make_chart(inst, cs, rank)
    finally:
        top30.plt.subplots = orig


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def send_one(inst, rank, chg):
    cs = top30.load(inst)
    if len(cs) < MIN_BARS:          # 상장한 지 너무 얼마 안 된 코인은 차트를 못 그림
        print(f"[건너뜀] {inst}: 일봉 {len(cs)}개뿐")
        return False
    sym = inst.replace("-USDT-SWAP", "")
    ca.CHAT_ID = CHAT
    ca.send_telegram(make_chart(inst, cs, rank, chg), f"전일대비 {rank}위 {sym} {chg:+.1f}%")
    return True


def scan(test=False):
    try:
        ranking = get_ranking()
    except Exception as e:
        print(f"[오류] 순위 조회 실패: {e}")
        return
    if test:
        for rank, (inst, chg, _) in enumerate(ranking, 1):
            if send_one(inst, rank, chg):
                print(f"테스트 1장 보냄: {inst}")
                return
        print("[테스트 실패] 보낼 수 있는 코인이 없음")
        return
    state = load_state()
    now = time.time()
    first_run = not state          # 처음 켠 날은 이미 30위 안에 있던 코인도 한 번씩 보냄
    sent = 0
    for rank, (inst, chg, _) in enumerate(ranking, 1):
        old = state.get(inst)
        fresh = old is None or now - old["ts"] >= COOLDOWN_H * 3600 or old["rank"] - rank >= JUMP
        if not fresh:
            continue
        try:
            if send_one(inst, rank, chg):
                state[inst] = {"ts": now, "rank": rank}
                sent += 1
        except Exception as e:
            print(f"[오류] {inst}: {e}")
        time.sleep(0.5)
    # 30위 밖으로 나간 코인은 기록에서 빼서, 다시 들어오면 바로 알림이 가게 함
    inside = {i for i, _, _ in ranking}
    for inst in list(state):
        if inst not in inside:
            del state[inst]
    save_state(state)
    print(f"{datetime.now(ca.KST):%H:%M:%S} 완료: 30개 중 {sent}개 보냄" + (" (첫 실행)" if first_run else ""))


def main():
    ca.setup_korean_font()
    if "--find" in sys.argv:
        top30.find_chat()
        return
    if not ca.TOKEN or not CHAT:
        print("[알림] TELEGRAM_TOKEN 또는 CHG30_CHAT이 비어 있어 텔레그램 전송이 안 됩니다.")
    scan(test="--test" in sys.argv)


if __name__ == "__main__":
    main()
