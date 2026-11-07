# -*- coding: utf-8 -*-
"""
크로스알림 승률표 (과거 재현)
- 크로스알림 코드(cross_alert.py)를 그대로 가져다 써서, 최근 며칠 동안 알림이 났을 자리를 다시 찾아냄
- 각 알림이 익절가/손절가 중 어디에 먼저 닿았는지 세어서 승률 차트 한 장을 텔레그램으로 보냄
- cross_alert.py는 하나도 안 바꿈. 이 파일은 따로 한 번 돌고 끝남
"""
import io
import os
import time
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import cross_alert as ca

DAYS = int(os.environ.get("SCORE_DAYS", "7"))   # 최근 며칠치를 볼지
MAX_WAIT = 96                                    # 이 봉 수(15분봉 96개=24시간) 안에 안 닿으면 '시간초과'(승률에서 뺌)
WINDOW = ca.CANDLE_LIMIT                         # 실제 알림이 보는 봉 개수와 같게
BE = 40                                          # 손익분기 승률(%) (익절=손절x1.5)


def get_history(inst_id, bars):
    """15분봉을 오래된 것 -> 최신 순으로, 확정된 봉만 bars개 가까이 가져옴"""
    out, after = [], None
    while len(out) < bars:
        p = {"instId": inst_id, "bar": ca.BAR, "limit": 100}
        if after:
            p["after"] = after
        r = ca.session.get(ca.OKX + "/api/v5/market/history-candles", params=p, timeout=15)
        r.raise_for_status()
        rows = r.json()["data"]
        if not rows:
            break
        for c in rows:                       # 최신 봉이 먼저 옴
            if len(c) > 8 and c[8] == "0":   # 아직 진행 중인 봉은 뺌
                continue
            out.append({"ts": int(c[0]), "open": float(c[1]), "high": float(c[2]),
                        "low": float(c[3]), "close": float(c[4]), "quote_vol": float(c[7])})
        after = rows[-1][0]
        time.sleep(0.08)
    out.sort(key=lambda c: c["ts"])
    return out


def judge(side, sl, tp, later):
    for c in later[:MAX_WAIT]:
        if side == "롱":
            hit_sl, hit_tp = c["low"] <= sl, c["high"] >= tp
        else:
            hit_sl, hit_tp = c["high"] >= sl, c["low"] <= tp
        if hit_sl:                  # 한 봉에서 둘 다 닿으면 손절로 침(보수적으로)
            return "손절"
        if hit_tp:
            return "익절"
    return None


def backtest(inst_id, candles, since_ms):
    rows = []
    for i in range(WINDOW - 1, len(candles) - 1):
        if candles[i]["ts"] < since_ms:
            continue
        signal, info = ca.check_signal(candles[i - WINDOW + 1:i + 1])
        if not signal or not signal.endswith("진입") or info.get("pass"):
            continue
        side = "롱" if signal.startswith("롱") else "숏"
        res = judge(side, info["sl"], info["tp"], candles[i + 1:])
        if res:
            rows.append({"side": side, "res": res, "vol": info["vol_ratio"],
                         "risk": info["risk_pct"] * 100, "inst": inst_id, "ts": candles[i]["ts"]})
    return rows


def bucket(rows, fn, order):
    out = []
    for name in order:
        sub = [r for r in rows if fn(r) == name]
        w = sum(r["res"] == "익절" for r in sub)
        l = sum(r["res"] == "손절" for r in sub)
        if w + l:
            out.append(("%s (%d건)" % (name, w + l), w / (w + l) * 100))
    return out


def vol_name(r):
    return "거래대금 1.5~2배" if r["vol"] < 2 else ("거래대금 2~3배" if r["vol"] < 3 else "거래대금 3배+")


def risk_name(r):
    return "손절폭 ~0.5%" if r["risk"] < 0.5 else ("손절폭 0.5~1%" if r["risk"] < 1 else "손절폭 1~2%")


def make_report(rows):
    panels = [
        ("롱·숏별 승률", bucket(rows, lambda r: r["side"] + "진입", ["롱진입", "숏진입"])),
        ("거래대금 폭발 크기별", bucket(rows, vol_name, ["거래대금 1.5~2배", "거래대금 2~3배", "거래대금 3배+"])),
        ("손절폭별", bucket(rows, risk_name, ["손절폭 ~0.5%", "손절폭 0.5~1%", "손절폭 1~2%"])),
    ]
    fig, axes = plt.subplots(3, 1, figsize=(8, 7.5))
    for ax, (title, data) in zip(axes, panels):
        ax.set_title(title, fontsize=11, loc="left")
        ax.set_xlim(0, 100)
        if data:
            names = [d[0] for d in data][::-1]
            vals = [d[1] for d in data][::-1]
            colors = ["#2E7D32" if v >= BE else "#C62828" for v in vals]
            ax.barh(names, vals, color=colors)
            for y, v in enumerate(vals):
                ax.text(min(v + 1, 88), y, "%.0f%%" % v, va="center", fontsize=10)
        ax.axvline(BE, color="gray", ls="--", lw=0.8)   # 40% 넘으면 이득
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    return buf.getvalue()


def main():
    ca.setup_korean_font()
    now_ms = int(time.time() * 1000)
    since = now_ms - DAYS * 86400000
    bars = DAYS * 96 + WINDOW + 5
    symbols = ca.get_top_symbols()
    rows = []
    for n, inst in enumerate(symbols, 1):
        try:
            rows += backtest(inst, get_history(inst, bars), since)
        except Exception as e:
            print("[오류]", inst, e)
        if n % 20 == 0:
            print(n, "개 코인 완료, 지금까지 알림 자리", len(rows))
    w = sum(r["res"] == "익절" for r in rows)
    l = sum(r["res"] == "손절" for r in rows)
    print("결과: 익절 %d / 손절 %d" % (w, l))
    if w + l == 0:
        ca.send_telegram(make_report([]), "📊 크로스알림 최근 %d일: 재현된 알림이 없어요" % DAYS)
        return
    cap = "📊 크로스알림 최근 %d일 (과거 재현) 승률 %.0f%% (익절 %d · 손절 %d) · 점선 40%% 넘으면 이득" % (
        DAYS, w / (w + l) * 100, w, l)
    ca.send_telegram(make_report(rows), cap)


if __name__ == "__main__":
    main()
