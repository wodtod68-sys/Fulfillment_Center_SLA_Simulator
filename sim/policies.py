# -*- coding: utf-8 -*-
"""
policies.py — 보충 스케줄 정책

정책 = 보충작업 목록을 [zone_id, start_time, complete_time] 로 반환하는 함수.
엔진은 이 구간들로 zone×시간 동시 보충 건수를 만들어 간섭을 계산한다.

이번 단계에서는 baseline(과거 그대로)만 구현한다.
다음 단계(3-1)에서 여기에 s31_reschedule 등을 추가하면
엔진·평가 코드는 그대로 재사용된다.
"""
from __future__ import annotations
import numpy as np
import pandas as pd


def baseline(repl: pd.DataFrame) -> pd.DataFrame:
    """과거 실제 보충 시점 그대로."""
    return repl[["zone_id", "start_time", "complete_time"]].copy()


# ------------------------------------------------------------
# 3-1 보충 시점 재분배 (규칙 기반 휴리스틱)
# ------------------------------------------------------------
#  대상: 비긴급 보충(threshold·promo_prepare)이 오후에 시작하는 건.
#  긴급(stockout_risk)은 잔류.  각 작업의 소요시간(complete-start)은 보존.
#  * 지게차 6대 제약은 걸지 않음 — 데이터의 start~complete 구간은 지게차 점유가
#    아니라 작업 라이프사이클(요청→가용)이며 서로 87% 겹치기 때문.
#  * '더 일찍' 보충하는 방향이므로 재주문점·promo 선행 제약은 자동 충족(더 안전).
#
#  mode="shift"  : 오후 배치를 통째로 오전으로 평행이동(구조 보존) → 타이밍 이득만.
#  mode="spread" : 오전 창에 균일하게 펼침(문서의 '평준화' 문구 그대로)
#                  → 동시 보충 피크는 낮지만 간섭 시간대 수가 늘어 오목 비용에서 불리.

HOUR_NS = 3600 * 10**9


def s31_reschedule(repl: pd.DataFrame, mode: str = "shift",
                   keep_reasons=("stockout_risk",),
                   src_from_hour: int = 12,
                   dst_window=(8, 12), shift_hours: int = 6) -> pd.DataFrame:
    r = repl.reset_index(drop=True)
    start_ns = r["start_time"].values.astype("datetime64[ns]").astype("int64")
    comp_ns = r["complete_time"].values.astype("datetime64[ns]").astype("int64")
    dur_ns = comp_ns - start_ns
    sh = r["start_time"].dt.hour.values
    day_ns = r["start_time"].dt.normalize().values.astype("datetime64[ns]").astype("int64")

    movable = (~r["repl_reason"].isin(keep_reasons)).values & (sh >= src_from_hour)
    d0, d1 = dst_window
    new_start = start_ns.copy()

    if mode == "shift":
        # 오후 배치를 통째로 평행이동, 오전 창 하한(d0시) 아래로는 클램프
        shifted = np.maximum(start_ns - shift_hours * HOUR_NS, day_ns + d0 * HOUR_NS)
        new_start = np.where(movable, shifted, start_ns)

    elif mode == "spread":
        # 날짜별로 이동 대상을 시작시각 순서대로 오전 창 [d0,d1)에 균일 배치
        span = (d1 - d0) * HOUR_NS
        m = pd.DataFrame({"day": day_ns[movable], "start": start_ns[movable],
                          "pos": np.arange(int(movable.sum()))})
        m = m.sort_values(["day", "start"])
        m["rank"] = m.groupby("day").cumcount()
        m["n"] = m.groupby("day")["rank"].transform("size")
        frac = np.where(m["n"].values > 1, m["rank"].values / (m["n"].values - 1), 0.0)
        ns_new = m["day"].values + d0 * HOUR_NS + (frac * span).astype("int64")
        tmp = np.empty(int(movable.sum()), dtype="int64")
        tmp[m["pos"].values] = ns_new
        new_start[movable] = tmp
    else:
        raise ValueError(f"unknown mode: {mode}")

    return pd.DataFrame({
        "zone_id": r["zone_id"].values,
        "start_time": new_start.astype("datetime64[ns]"),
        "complete_time": (new_start + dur_ns).astype("datetime64[ns]"),
    })


def s31_shift(repl):
    return s31_reschedule(repl, mode="shift")


def s31_spread(repl):
    return s31_reschedule(repl, mode="spread")


# ------------------------------------------------------------
# 3-2 슬로팅 개선 (강도 감소) — 대조군/미리보기
# ------------------------------------------------------------
#  간섭의 '시점'이 아니라 '강도'를 낮추는 레버.
#  ZB·ZD의 과밀 슬로팅 → 건당 보충 소요 약 67분(타 zone의 2배).
#  재배치로 소요를 타 zone 수준(~32분)으로 낮추면 구간이 짧아져 동시 보충(겹침)이
#  줄고, 오목 비용함수에서도 '간섭받는 픽 수' 자체가 감소 → 총 간섭 감소.
#  * 여기서는 시점은 그대로 두고 ZB·ZD 소요만 단축하는 프록시로 3-2 효과를 예시.

def s32_slotting(repl, heavy_zones=("ZB", "ZD"), cap_min: int = 32) -> pd.DataFrame:
    r = repl.reset_index(drop=True)
    start_ns = r["start_time"].values.astype("datetime64[ns]").astype("int64")
    dur_ns = r["complete_time"].values.astype("datetime64[ns]").astype("int64") - start_ns
    cap = cap_min * 60 * 10**9
    heavy = r["zone_id"].isin(heavy_zones).values
    dur_ns = np.where(heavy, np.minimum(dur_ns, cap), dur_ns)
    return pd.DataFrame({
        "zone_id": r["zone_id"].values,
        "start_time": start_ns.astype("datetime64[ns]"),
        "complete_time": (start_ns + dur_ns).astype("datetime64[ns]"),
    })


POLICIES = {
    "baseline": baseline,
    "s31_shift": s31_shift,
    "s31_spread": s31_spread,
    "s32_slotting": s32_slotting,
}
