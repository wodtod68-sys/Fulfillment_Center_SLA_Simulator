# -*- coding: utf-8 -*-
"""
engine.py — 이산시간 연속 FIFO 큐 시뮬레이터

모델 (제출물 인과 사슬을 하나의 대기행렬로 축약)
------------------------------------------------
· 도착   : 주문라인이 allocated_time 에 pick 큐로 진입 (실데이터 그대로)
· 서버   : 시간대별 활성 작업자 W(t) — 각 라인은 s_eff 만큼의 작업자-초를 소비
· 서비스 : s_eff(line) = overhead + pick_service(c_z(t))
           c_z(t) = 그 라인 zone 의 '현재 동시 보충 건수' (스케줄에서 계산)
           → 간섭이 강한 zone 의 라인은 더 오래 걸려 '공유 큐' 전체를 지연 (가설3)
· 규칙   : 큐 규율 선택 가능(discipline) — fifo(기본, baseline 재현) /
           edd(마감우선) / triage(구제가능 우선 + 지연 상한). 유입 일정 +
           간섭에 의한 처리량 저하 → 백로그 누적, 주말/야간 배수 → 주중 이월(가설4)

핵심: 간섭은 '개별 픽의 완만한 팽창(서비스 원시량)'으로만 들어가고,
큰 착수 대기·SLA 붕괴는 백로그에서 '창발'한다. 비용함수 형태(오목)와
백로그 타이밍의 상호작용이 시나리오 효과를 결정한다.
"""
from __future__ import annotations
import heapq
from collections import deque
import numpy as np
import pandas as pd

from config import ZONES, OPEN_HOUR, CLOSE_HOUR


ZIDX = {z: i for i, z in enumerate(ZONES)}


# ------------------------------------------------------------
# 시간 격자
# ------------------------------------------------------------
def make_grid(t0: pd.Timestamp, t1: pd.Timestamp, step_min: int):
    step = pd.Timedelta(minutes=step_min)
    n = int(np.ceil((t1 - t0) / step)) + 1
    edges = t0 + np.arange(n + 1) * step
    return t0, step, n, edges


def worker_grid(lines: pd.DataFrame, t0, step_min, n) -> np.ndarray:
    """스텝별 활성 작업자 수 W(step). 실데이터의 (date,hour)별 고유 작업자 수 사용."""
    st = lines["start_time"]
    per = (pd.DataFrame({"date": st.dt.normalize(), "hour": st.dt.hour,
                        "wid": lines["worker_id"].values})
           .groupby(["date", "hour"])["wid"].nunique())
    lut = {(d, h): int(v) for (d, h), v in per.items()}
    step = pd.Timedelta(minutes=step_min)
    W = np.zeros(n)
    for i in range(n):
        t = t0 + i * step
        W[i] = lut.get((t.normalize(), t.hour), 0)
    return W


def concurrency_grid(sched: pd.DataFrame, t0, step_min, n) -> np.ndarray:
    """스텝×zone 동시 보충 건수. sched: [zone_id, start_time, complete_time]."""
    step = pd.Timedelta(minutes=step_min)
    edges = (t0 + np.arange(n + 1) * step)
    centers = (edges[:-1] + step / 2).astype("datetime64[ns]")
    C = np.zeros((n, len(ZONES)), dtype=np.int32)
    for z, i in ZIDX.items():
        rz = sched[sched["zone_id"] == z]
        if len(rz) == 0:
            continue
        s = np.sort(rz["start_time"].values.astype("datetime64[ns]"))
        e = np.sort(rz["complete_time"].values.astype("datetime64[ns]"))
        C[:, i] = (np.searchsorted(s, centers, side="right")
                   - np.searchsorted(e, centers, side="right"))
    return C


def boost_grid(t0, step_min, n, spec: dict) -> np.ndarray:
    """스텝별 추가 작업자 수. spec: {(dow, hour): 추가인원}. (3-3 저녁 용량 보강)"""
    step = pd.Timedelta(minutes=step_min)
    times = pd.DatetimeIndex(t0 + np.arange(n) * step)
    dow = times.dayofweek.values
    hour = times.hour.values
    b = np.zeros(n)
    for (d, h), extra in spec.items():
        b[(dow == d) & (hour == h)] += extra
    return b


# ------------------------------------------------------------
# 시뮬레이션 코어
# ------------------------------------------------------------
def simulate(lines: pd.DataFrame, params: dict, sched: pd.DataFrame,
             step_min: int = 10, tail_days: int = 3,
             base_service: float | None = None,
             worker_boost: dict | None = None,
             discipline: str = "fifo",
             defer_cap_min: float | None = None,
             reactive: dict | None = None) -> dict:
    """
    서비스시간 모델:  s_eff(c) = base_service + excess(c)
      base_service : 간섭 없는 클린 유효 서비스시간(초) — 캘리브레이션 노브
                     (기본값 params['service']['base_service'])
      excess(c)    : 간섭 초과분 = pick_service(c) - pick(0)  (측정값, 클린=0)

    용량 보강 레버 두 가지
      worker_boost : 고정 스케줄 {(dow,hour): 추가인원} — 부하와 무관하게 늘 투입.
      reactive     : 임계 기반 조건부 호출(폐루프). 매일 obs_hour 정각에 그 시점의
                     '자기 백로그'를 읽어 threshold 초과면 당일 hours 시간대에만 extra명 증원.
                     {'obs_hour': 17, 'threshold': 1000, 'extra': 20, 'hours': (19,...,23)}
                     * obs_hour < min(hours) 여야 인과적(관측→같은 날 저녁 투입).
                     * baseline 백로그를 훔쳐보지 않고 시뮬 진행 중 상태로 판단한다.

    반환
    ----
    completion : 각 라인 완료시각(ns int, 미완료는 -1) — lines 순서(allocated 정렬)
    sla_viol   : 완료시각 > promise_time
    backlog_hr : 시각별(정시) 재공량(allocated-미완료) 시계열 DataFrame
    extra_worker_h : 두 레버가 추가한 총 작업자-시간(비용 지표)
    call_days      : reactive 가 실제 호출한 일수 (고정 스케줄이면 0)
    """
    pick0 = params["service"]["pick0"]
    if base_service is None:
        base_service = params["service"].get("base_service",
                                             params["service"]["s_eff0"])
    cost = params["cost_lookup"]                      # pick_service[c]
    excess = np.maximum(cost - pick0, 0.0)            # 간섭 초과분(클린=0)
    max_c = len(cost) - 1

    # 라인을 도착(allocated) 순으로 정렬
    df = lines.sort_values("allocated_time", kind="mergesort").reset_index(drop=True)
    arr = df["allocated_time"].values.astype("datetime64[ns]").astype(np.int64)
    promise = df["promise_time"].values.astype("datetime64[ns]").astype(np.int64)
    zidx = df["zone_id"].map(ZIDX).values.astype(np.int32)
    # 라인별 base 조정(초): 슬로팅(3-2)에서 이동 SKU의 픽 거리 페널티 등. 기본 0.
    base_line = base_service + (df["base_adj"].values.astype(float)
                                if "base_adj" in df.columns else 0.0)
    if np.isscalar(base_line):
        base_line = np.full(len(df), base_line)
    N = len(df)

    t0 = pd.Timestamp(df["allocated_time"].min()).normalize()
    t1 = pd.Timestamp(df["allocated_time"].max()).normalize() + pd.Timedelta(days=tail_days)
    t0, step, n, edges = make_grid(t0, t1, step_min)
    step_sec = step_min * 60

    W = worker_grid(lines, t0, step_min, n)
    boost_extra_h = 0.0
    if worker_boost:
        b = boost_grid(t0, step_min, n, worker_boost)
        boost_extra_h = float(b.sum() * step_min / 60.0)
        W = W + b
    C = concurrency_grid(sched, t0, step_min, n)
    edge_ns = (t0 + np.arange(n + 1) * step).astype("datetime64[ns]").astype(np.int64)

    # 간섭 초과분 excess(c) — 스텝×zone 사전계산. base는 라인별로 더한다.
    Cc = np.clip(C, 0, max_c)
    excess_zt = excess[Cc]                            # (n, nzone), 초

    completion = np.full(N, -1, dtype=np.int64)
    backlog_hourly = []                               # (timestamp, backlog)
    next_hour_edge = t0

    def record_backlog(t_start, in_system):
        nonlocal next_hour_edge
        ts = pd.Timestamp(t_start)
        if ts >= next_hour_edge and ts.minute == 0:
            backlog_hourly.append((ts, in_system))
            next_hour_edge = ts + pd.Timedelta(hours=1)

    # 반응형 조건부 호출 — 관측시각에 당일 호출 여부를 확정하고 지정 시간대만 증원.
    r_on = False; r_extra_sec = 0.0; r_days = 0
    if reactive is not None:
        _tm = pd.DatetimeIndex(t0 + np.arange(n) * step)
        r_obs = (_tm.hour.values == reactive["obs_hour"]) & (_tm.minute.values == 0)
        r_hrs = np.isin(_tm.hour.values, list(reactive["hours"]))
        r_thr = float(reactive["threshold"]); r_n = float(reactive["extra"])

    def react(i, backlog):
        """스텝 i에서 추가 투입할 인원(명). 폐루프: 인자 backlog 는 현재 시뮬 상태."""
        nonlocal r_on, r_extra_sec, r_days
        if reactive is None:
            return 0.0
        if r_obs[i]:
            r_on = backlog > r_thr
            if r_on:
                r_days += 1
        if r_on and r_hrs[i]:
            r_extra_sec += r_n * step_sec
            return r_n
        return 0.0

    if discipline == "fifo":
        # 도착순 소비. head==완료 라인 수(정렬이 allocated 순이므로 포인터로 충분).
        head = 0; arrived = 0; residual = 0.0
        for i in range(n):
            t_start = edge_ns[i]; t_end = edge_ns[i + 1]
            while arrived < N and arr[arrived] < t_end:
                arrived += 1
            budget = (W[i] + react(i, arrived - head)) * step_sec
            while budget > 0 and head < arrived:
                z = zidx[head]
                need = base_line[head] + excess_zt[i, z] - residual
                if budget >= need:
                    budget -= need; residual = 0.0
                    completion[head] = t_end
                    head += 1
                else:
                    residual += budget; budget = 0.0
            record_backlog(t_start, arrived - head)

    elif discipline in ("edd", "triage"):
        # 마감 기반 우선순위.
        #  edd    : 마감 빠른 순(포화 시 가장 늦은 것부터 → FIFO와 유사, 효과 미미).
        #  triage : 구제가능(마감 미도과)을 마감순 우선, 이미 늦은 라인은 후순위로 미뤄
        #           doomed 주문에 용량 낭비를 막는다. 단 무한 지연을 막기 위해
        #           defer_cap_min(분)을 넘겨 늦은 라인은 다시 우선 처리(꼬리 지각 상한).
        cap_ns = None if defer_cap_min is None else int(defer_cap_min * 60 * 1e9)
        heap = []          # 가용 라인 (promise_ns, seq, idx)
        pending = []       # triage: 지연 상한 내 '늦은' 라인 (뒤로 미룸)
        arrived = 0; completed = 0; cur = -1; residual = 0.0
        for i in range(n):
            t_start = edge_ns[i]; t_end = edge_ns[i + 1]
            while arrived < N and arr[arrived] < t_end:
                heapq.heappush(heap, (int(promise[arrived]), arrived, arrived))
                arrived += 1
            # 지연 상한 초과분은 다시 우선 처리로 승격(꼬리 지각 억제)
            if cap_ns is not None:
                while pending and pending[0][0] < t_start - cap_ns:
                    heapq.heappush(heap, heapq.heappop(pending))
            budget = (W[i] + react(i, arrived - completed)) * step_sec
            while budget > 0:
                if cur < 0:
                    if discipline == "edd":
                        if not heap:
                            break
                        cur = heapq.heappop(heap)[2]
                    else:  # triage
                        picked = -1
                        while heap:
                            pr = heap[0][0]
                            if pr < t_start:                    # 이미 늦음
                                if cap_ns is not None and pr < t_start - cap_ns:
                                    picked = heapq.heappop(heap)[2]  # 상한 초과 → 처리
                                    break
                                heapq.heappush(pending, heapq.heappop(heap))  # 미룸
                            else:
                                picked = heapq.heappop(heap)[2]  # 구제가능·마감임박
                                break
                        if picked < 0:                          # 남은 건 doomed
                            if pending:
                                picked = heapq.heappop(pending)[2]
                            else:
                                break
                        cur = picked
                    residual = 0.0
                z = zidx[cur]
                need = base_line[cur] + excess_zt[i, z] - residual
                if budget >= need:
                    budget -= need; residual = 0.0
                    completion[cur] = t_end; completed += 1; cur = -1
                else:
                    residual += budget; budget = 0.0
            record_backlog(t_start, arrived - completed)
    else:
        raise ValueError(f"unknown discipline: {discipline}")

    sla_viol = (completion > promise) | (completion < 0)
    backlog_hr = pd.DataFrame(backlog_hourly, columns=["t", "backlog"])

    return {
        "order_line_id": df["order_line_id"].values,
        "zone_id": df["zone_id"].values,
        "dow": df["dow"].values,
        "ohour": df["ohour"].values,
        "allocated_time": df["allocated_time"].values,
        "promise_time": df["promise_time"].values,
        "completion_ns": completion,
        "sla_viol": sla_viol.astype(np.int8),
        "sla_viol_obs": df["sla_viol_obs"].values,
        "backlog_hr": backlog_hr,
        "n_incomplete": int((completion < 0).sum()),
        "extra_worker_h": boost_extra_h + r_extra_sec / 3600.0,
        "call_days": r_days,
    }


def summarize(res: dict) -> dict:
    """SLA 요약: 전체 / 포커스 / 베이스."""
    from config import FOCUS_DOWS, FOCUS_HOURS
    dow = res["dow"]; oh = res["ohour"]; v = res["sla_viol"]
    focus = np.isin(dow, FOCUS_DOWS) & np.isin(oh, FOCUS_HOURS)
    return {
        "sla_overall": float(v.mean() * 100),
        "sla_focus": float(v[focus].mean() * 100),
        "sla_base": float(v[~focus].mean() * 100),
        "n_incomplete": res["n_incomplete"],
        "n": int(len(v)),
    }
