# -*- coding: utf-8 -*-
"""
OKX 선물 전체 코인 일봉 차트 알림 (매일 새벽 1시 일봉 마감 직후)
- OKX 기본 일봉(1D)은 한국시간 새벽 1시에 마감됨 -> 새벽 1시 5분에 시작
- 마감된 일봉만 그림 (진행 중인 새 봉은 뺌). 순위는 방금 마감된 하루의 거래대금 기준
- 거래대금 31위부터 끝까지 (1~30위는 top30.py가 보내고 있음). 1위부터 받고 싶으면 START_RANK = 1
- 차트 모양은 top30.py의 make_chart를 가져다 쓰고, 그 위에 지지(초록)·저항(빨강) 박스, 추세선(점선), 정배열/역배열·과매도/과매수 표시를 얹음 (zones.py) (top30.py는 안 바꿈)
- 텔레그램 속도 제한 때문에 한 장씩 천천히 보내고, 막히면 쉬었다 다시 보냄. 중간에 끊겨도 이어서 보냄
- 차트 1장 테스트: python all_daily.py --test
"""
import json
import os
import sys
import time
from datetime import datetime

import cross_alert as ca
import top30            # 차트 그리는 make_chart만 가져다 씀 (top30.py는 안 바뀜)
import zones            # 지지·저항·추세·과매도/과매수 표시 (zones.py)

# ----- 설정값 -----
START_RANK = 31         # 거래대금 몇 위부터 보낼지 (1이면 전부)
HIST_BARS = 400         # 정확한 EMA200 계산에 쓰는 일봉 개수
SEND_GAP = 3.2          # 사진 한 장 보내고 쉬는 시간(초). 텔레그램은 한 방에 1분 약 20개까지
CHAT = os.environ.get("ALL_CHAT", "")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "all_daily_state.json")


def make_chart(inst, cs, rank):
    """top30의 차트 위에 추세·지지·저항·과매도/과매수를 얹음 (top30.py는 안 바꿈)"""
    orig = top30.plt.subplots

    def sub(*a, **k):
        fig, axes = orig(*a, **k)
        osave = fig.savefig

        def save(*aa, **kk):
            try:
                zones.overlay(axes[0], cs)
            except Exception as e:
                print(f"[박스 오류] {inst}: {e}")
            return osave(*aa, **kk)
        fig.savefig = save
        return fig, axes
    top30.plt.subplots = sub
    try:
        return top30.make_chart(inst, cs, rank)
    finally:
        top30.plt.subplots = orig


def parse(rows):
    """OKX 봉 목록을 바꿈. 아직 마감 안 된 봉(맨 끝 값이 1이 아닌 것)은 뺌"""
    out = []
    for c in rows:
        if len(c) > 8 and c[8] != "1":
            continue
        out.append({"ts": int(c[0]), "open": float(c[1]), "high": float(c[2]), "low": float(c[3]),
                    "close": float(c[4]), "vol": float(c[5]), "quote_vol": float(c[7])})
    return out


def get_symbols():
    r = ca.session.get(ca.OKX + "/api/v5/public/instruments", params={"instType": "SWAP"}, timeout=20)
    r.raise_for_status()
    return [d["instId"] for d in r.json()["data"]
            if d["instId"].endswith("-USDT-SWAP") and d.get("state") == "live"]


def last_closed(inst):
    """방금 마감된 일봉 하나: (마감 시각, 그날 거래대금) 없으면 None"""
    r = ca.session.get(ca.OKX + "/api/v5/market/candles",
                       params={"instId": inst, "bar": "1D", "limit": 3}, timeout=15)
    r.raise_for_status()
    cs = parse(reversed(r.json()["data"]))
    return (cs[-1]["ts"], cs[-1]["quote_vol"]) if cs else None


def rank_all(symbols):
    """전체 코인의 마감 일봉 거래대금으로 순위를 매김: ([(코인, 거래대금)...], 마감일 시각)"""
    got = {}
    for inst in symbols:
        try:
            x = last_closed(inst)
            if x:
                got[inst] = x
        except Exception as e:
            print(f"[오류] 순위 {inst}: {e}")
        time.sleep(0.06)
    if not got:
        return [], 0
    day = max(v[0] for v in got.values())
    ranking = sorted(((i, v[1]) for i, v in got.items() if v[0] == day and v[1] > 0), key=lambda t: -t[1])
    return ranking, day


def get_history(inst, bars):
    out, after = [], None
    while len(out) < bars:
        p = {"instId": inst, "bar": "1D", "limit": 100}
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
    return out


def load(inst):
    """코인 하나의 마감된 일봉만 불러옴 (오래된 것 -> 최신)"""
    r = ca.session.get(ca.OKX + "/api/v5/market/candles",
                       params={"instId": inst, "bar": "1D", "limit": 100}, timeout=15)
    r.raise_for_status()
    by_ts = {c["ts"]: c for c in get_history(inst, HIST_BARS)}
    for c in parse(r.json()["data"]):
        by_ts[c["ts"]] = c
    return [by_ts[k] for k in sorted(by_ts)]


def send_photo(buf, caption):
    """텔레그램에 사진 한 장. 속도 제한에 걸리면 알려주는 시간만큼 쉬었다 다시 보냄"""
    url = f"https://api.telegram.org/bot{ca.TOKEN}/sendPhoto"
    data = buf.getvalue()
    for _ in range(6):
        try:
            r = ca.session.post(url, data={"chat_id": CHAT, "caption": caption},
                                files={"photo": ("chart.png", data, "image/png")}, timeout=60)
            if r.status_code == 200:
                return True
            wait = 5
            if r.status_code == 429:
                try:
                    wait = int(r.json().get("parameters", {}).get("retry_after", 10)) + 1
                except Exception:
                    wait = 15
            print(f"[텔레그램] {r.status_code} -> {wait}초 쉬고 다시")
        except Exception as e:
            wait = 10
            print(f"[텔레그램 오류] {e}")
        time.sleep(wait)
    return False


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def run(test=False):
    try:
        symbols = get_symbols()
    except Exception as e:
        print(f"[오류] 코인 목록 조회 실패: {e}")
        return
    ranking, day = rank_all(symbols)
    print(f"{datetime.now(ca.KST):%H:%M:%S} 순위 계산 완료: {len(ranking)}개 코인, 마감일 {datetime.fromtimestamp(day / 1000, ca.KST):%Y-%m-%d %H:%M}")
    state = {"day": day, "done": []}
    if not test:
        old = load_state()
        if old.get("day") == day:                 # 같은 날 다시 돌면 보낸 건 건너뛰고 이어서 보냄
            state = old
    sent = 0
    for rank, (inst, _) in enumerate(ranking, 1):
        if test:
            if rank > 1:
                break
        elif rank < START_RANK or inst in state["done"]:
            continue
        sym = inst.replace("-USDT-SWAP", "")
        try:
            cs = load(inst)
            if len(cs) >= 30:
                cap = ("[테스트] " if test else "") + f"거래대금 {rank}위 {sym}"
                if send_photo(make_chart(inst, cs, rank), cap):
                    sent += 1
            if not test:
                state["done"].append(inst)
                save_state(state)
        except Exception as e:
            print(f"[오류] {inst}: {e}")
        time.sleep(SEND_GAP)
    print(f"{datetime.now(ca.KST):%H:%M:%S} 완료: {sent}개 보냄")


def main():
    ca.setup_korean_font()
    if not ca.TOKEN or not CHAT:
        print("[알림] TELEGRAM_TOKEN 또는 ALL_CHAT이 비어 있어 보낼 수 없습니다.")
        return
    run(test="--test" in sys.argv)


if __name__ == "__main__":
    main()
