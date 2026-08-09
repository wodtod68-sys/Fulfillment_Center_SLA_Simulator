# -*- coding: utf-8 -*-
"""
io_load.py — CSV 로드 · 파싱 · 정제 · 캐시

대용량 CSV(work 89MB, order 38MB)를 매번 다시 읽지 않도록
파싱 결과를 sim/cache/*.pkl 로 캐시한다.

핵심 산출물
-----------
load_lines() : 주문라인 1개 = 1행. 시뮬레이터의 기본 입력.
    order_line_id, sku_id, zone_id, qty,
    order_time, promise_time, ship_time,           # order_line_history
    allocated_time, start_time,                    # work_event (착수 = pick_start)
    pick_start, pick_end, pack_start, pack_end,     # 서비스시간 관측
    worker_id, worker_type, worker_experience_level
load_repl()     : 보충작업 로그(+소요시간 파생)
load_master()   : SKU 마스터
load_calendar() : 일자·프로모션·날씨
"""
from __future__ import annotations
import os
import pandas as pd

from config import DATA, CACHE_DIR


def _cache(name: str) -> str:
    return os.path.join(CACHE_DIR, name + ".pkl")


def _load_cached(name: str, builder, rebuild: bool = False):
    path = _cache(name)
    if (not rebuild) and os.path.exists(path):
        return pd.read_pickle(path)
    df = builder()
    df.to_pickle(path)
    return df


# ------------------------------------------------------------
# 개별 로더
# ------------------------------------------------------------
def _build_order() -> pd.DataFrame:
    df = pd.read_csv(
        DATA["order"],
        parse_dates=["order_time", "promise_time", "ship_time"],
    )
    df = df[df["cancel_flag"] == 0].copy()   # 취소 제외 (유효 라인만)
    df.drop(columns=["cancel_flag"], inplace=True)
    return df


def _build_work() -> pd.DataFrame:
    cols = [
        "order_line_id", "worker_id", "zone_id", "location_id",
        "allocated_time", "start_time",
        "pick_start_time", "pick_end_time", "pack_start_time", "pack_end_time",
        "worker_type", "worker_experience_level",
    ]
    df = pd.read_csv(DATA["work"], usecols=cols)
    for c in ["allocated_time", "start_time", "pick_start_time",
              "pick_end_time", "pack_start_time", "pack_end_time"]:
        df[c] = pd.to_datetime(df[c])
    return df


def _build_repl() -> pd.DataFrame:
    df = pd.read_csv(
        DATA["repl"],
        parse_dates=["request_time", "start_time", "complete_time"],
    )
    df["dur_sec"] = (df["complete_time"] - df["start_time"]).dt.total_seconds()
    df["dur_min"] = df["dur_sec"] / 60.0
    return df


def _build_master() -> pd.DataFrame:
    return pd.read_csv(DATA["master"])


def _build_calendar() -> pd.DataFrame:
    df = pd.read_csv(DATA["calendar"], parse_dates=["date"])
    return df


def _build_lines() -> pd.DataFrame:
    """order_line + work_event 를 order_line_id 로 1:1 결합한 시뮬 기본 테이블."""
    order = _load_cached("order", _build_order)
    work = _load_cached("work", _build_work)

    work = work.rename(columns={
        "pick_start_time": "pick_start", "pick_end_time": "pick_end",
        "pack_start_time": "pack_start", "pack_end_time": "pack_end",
    })
    lines = order.merge(work, on="order_line_id", how="inner", validate="one_to_one")

    # 파생: 관측 서비스시간(초)
    lines["pick_dur"] = (lines["pick_end"] - lines["pick_start"]).dt.total_seconds()
    lines["pack_dur"] = (lines["pack_end"] - lines["pack_start"]).dt.total_seconds()
    # 작업자 점유시간(착수 pick_start ~ pack 종료): 유효 서비스시간의 관측치
    lines["handling"] = (lines["pack_end"] - lines["start_time"]).dt.total_seconds()
    # 착수 대기(할당 -> 착수): 큐에서 창발하는 값 (시뮬 결과와 대조용)
    lines["wait_sec"] = (lines["start_time"] - lines["allocated_time"]).dt.total_seconds()

    # 시간 파생 (order_time 기준 — 제출물이 채택한 버킷)
    ot = lines["order_time"]
    lines["date"] = ot.dt.normalize()
    lines["dow"] = ot.dt.dayofweek
    lines["ohour"] = ot.dt.hour
    # 실제 SLA 위반 (관측)
    lines["sla_viol_obs"] = (lines["ship_time"] > lines["promise_time"]).astype("int8")
    return lines


# ------------------------------------------------------------
# 공개 API
# ------------------------------------------------------------
def load_lines(rebuild: bool = False) -> pd.DataFrame:
    return _load_cached("lines", _build_lines, rebuild)


def load_repl(rebuild: bool = False) -> pd.DataFrame:
    return _load_cached("repl", _build_repl, rebuild)


def load_master(rebuild: bool = False) -> pd.DataFrame:
    return _load_cached("master", _build_master, rebuild)


def load_calendar(rebuild: bool = False) -> pd.DataFrame:
    return _load_cached("calendar", _build_calendar, rebuild)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    rebuild = "--rebuild" in sys.argv
    lines = load_lines(rebuild)
    repl = load_repl(rebuild)
    print(f"lines : {len(lines):,} rows, {lines.shape[1]} cols")
    print(f"repl  : {len(repl):,} rows")
    print(f"master: {len(load_master(rebuild))} SKU")
    print(f"calendar: {len(load_calendar(rebuild))} days")
    print(f"period: {lines['order_time'].min()} ~ {lines['order_time'].max()}")
    print("cache dir:", CACHE_DIR)
