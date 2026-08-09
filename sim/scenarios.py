# -*- coding: utf-8 -*-
"""
scenarios.py — 개선안(3-1) 시나리오 실행 + 비용함수 형태 민감도

핵심 질문(멘토 지적): 오후 보충을 오전으로 재배치하면 SLA가 좋아지는가?
답은 간섭 비용함수의 '형태'에 달려 있다.
  · 오목(포화, 실측): 재배치해도 총 간섭이 보존 → 효과 없음/역효과
  · 볼록(가정)     : 피크가 매우 비싸므로 펼치면 총 간섭 감소 → 효과 있음
이 모듈은 두 가지를 모두 정량화한다.
"""
from __future__ import annotations
import copy
import numpy as np
import pandas as pd

import engine
import policies
from calibrate import compute_pick_concurrency, calibrate_base_service


# ------------------------------------------------------------
# (1) 정책 시나리오 실행
# ------------------------------------------------------------
def run_policies(lines, repl, params, step_min=10, names=None):
    names = names or ["baseline", "s31_shift", "s31_spread", "s32_slotting"]
    out = {}
    for nm in names:
        sched = policies.POLICIES[nm](repl)
        res = engine.simulate(lines, params, sched, step_min=step_min)
        summ = engine.summarize(res)
        summ["total_excess_h"], summ["interfered_frac"] = _interference_load(
            lines, sched, params)
        out[nm] = {"summary": summ, "res": res, "sched": sched}
    return out


def _interference_load(lines, sched, params):
    """스케줄이 실제 픽에 얹는 총 간섭 픽시간(시간)과 간섭받은 픽 비율."""
    pick0 = params["service"]["pick0"]
    cost = params["cost_lookup"]
    excess = np.maximum(cost - pick0, 0.0)
    conc = compute_pick_concurrency(lines, sched)
    cc = np.clip(conc, 0, len(cost) - 1)
    return float(excess[cc].sum() / 3600.0), float((conc > 0).mean())


def concurrency_by_hour(sched, step_min=30):
    """스케줄의 시간대별 평균 동시 보충 건수(전 zone 합) — 간섭이 어디로 옮겨졌나."""
    t0 = pd.Timestamp("2025-01-01"); t1 = pd.Timestamp("2025-07-03")
    t0, step, n, _ = engine.make_grid(t0, t1, step_min)
    C = engine.concurrency_grid(sched, t0, step_min, n)
    hrs = np.array([(t0 + i * step).hour for i in range(n)])
    tot = C.sum(1)
    return {h: float(tot[hrs == h].mean()) for h in range(0, 24)}


# ------------------------------------------------------------
# (2) 비용함수 형태 민감도
# ------------------------------------------------------------
def _shape_lookup(shape: str, params, lines, repl):
    """형태별 간섭 초과분 곡선을 baseline 동시보충 분포에 대해 '동일 평균'으로
    정규화하여 만든다. → baseline 간섭 총량이 형태 간 동일해 공정 비교."""
    pick0 = params["service"]["pick0"]
    emp = np.maximum(params["cost_lookup"] - pick0, 0.0)      # 실측(오목) 초과분
    maxc = len(emp) - 1
    c = np.arange(maxc + 1, dtype=float)

    if shape == "concave":
        raw = emp.copy()
    elif shape == "linear":
        raw = c.copy()
    elif shape == "convex":
        raw = c ** 2
    else:
        raise ValueError(shape)

    # baseline 픽의 동시보충 히스토그램으로 가중 평균을 맞춤
    conc = compute_pick_concurrency(lines, policies.baseline(repl))
    hist = np.bincount(np.clip(conc, 0, maxc), minlength=maxc + 1).astype(float)
    denom = (hist * raw).sum()
    if denom > 0:
        raw = raw * (hist * emp).sum() / denom
    return pick0 + raw                                        # cost_lookup 형태로 반환


# ------------------------------------------------------------
# (3) 3-2 슬로팅 재최적화 (정식 모델 — 상충 포함)
# ------------------------------------------------------------
#  SKU를 ZB/ZD -> ZE/ZF 로 옮기면:
#   [비용] 픽 이동거리 증가 → clean 픽시간 += DIST_SLOPE*(dist_dest-dist_src)
#   [이득] 옮긴 SKU 픽이 과밀 ZB/ZD 간섭에서 탈출 + 보충 소요 67→DEST_REPL_MIN분
#   [상충] 옮긴 SKU 보충이 ZE/ZF 간섭을 새로 유발
DIST_SLOPE = 2.46          # clean pick_dur ~ 2.46*distance (초/거리단위, 실측)
DEST_REPL_MIN = 32         # ZE/ZF(비과밀) 보충 소요(분)


def select_moves(lines, master, source_zones=("ZB", "ZD"), n_move=40):
    """이동 후보: source zone SKU를 픽 물량(회전율) 내림차순으로 상위 n_move개."""
    cand = master[master["zone_id"].isin(source_zones)][
        ["sku_id", "zone_id", "distance_from_staging"]].copy()
    pc = lines.groupby("sku_id").size().rename("picks")
    cand = cand.merge(pc, on="sku_id", how="left").fillna({"picks": 0})
    cand = cand.sort_values("picks", ascending=False)
    return list(cand.head(n_move)["sku_id"])


def apply_slotting(lines, repl, master, moved_skus, dest_zones=("ZE", "ZF"),
                   relief_zones=("ZB", "ZD"), relief_min=None):
    """이동 SKU에 대해 lines(픽 zone+거리페널티)와 repl(보충 zone+소요)을 변경.
    relief_min: 과밀 해소로 '남은' relief_zones 보충 소요가 이 값(분)으로 단축된다는
                낙관적 가정(문서 주장). None이면 미적용(보수적)."""
    src_dist = master.set_index("sku_id")["distance_from_staging"].to_dict()
    dest_dist = {z: master[master["zone_id"] == z]["distance_from_staging"].mean()
                 for z in dest_zones}
    assign = {sku: dest_zones[i % len(dest_zones)] for i, sku in enumerate(moved_skus)}
    moved = set(moved_skus)

    L = lines.copy()
    L["base_adj"] = 0.0
    mv = L["sku_id"].isin(moved)
    dz = L.loc[mv, "sku_id"].map(assign)
    L.loc[mv, "base_adj"] = DIST_SLOPE * (
        dz.map(dest_dist).values - L.loc[mv, "sku_id"].map(src_dist).values)
    L.loc[mv, "zone_id"] = dz.values

    R = repl.copy()
    rmv = R["sku_id"].isin(moved)
    # 이동 SKU 보충: dest zone + 비과밀 소요
    R.loc[rmv, "zone_id"] = R.loc[rmv, "sku_id"].map(assign).values
    R.loc[rmv, "complete_time"] = (R.loc[rmv, "start_time"]
                                   + pd.Timedelta(minutes=DEST_REPL_MIN))
    # 남은 relief_zones 보충 소요 단축(낙관적 가정)
    if relief_min is not None:
        rem = R["zone_id"].isin(relief_zones) & (~rmv)
        R.loc[rem, "complete_time"] = (R.loc[rem, "start_time"]
                                       + pd.Timedelta(minutes=relief_min))
    sched = R[["zone_id", "start_time", "complete_time"]].copy()
    return L, sched


def run_slotting(lines, repl, master, params, n_moves=(0, 20, 40, 60, 80, 120),
                 source_zones=("ZB", "ZD"), dest_zones=("ZE", "ZF"),
                 relief_min=None, step_min=10):
    """이동 규모를 스윕하여 SLA·총간섭·평균픽거리 상충 곡선을 만든다."""
    dist_map = master.set_index("sku_id")["distance_from_staging"]
    rows = []
    for k in n_moves:
        moved = [] if k == 0 else select_moves(lines, master, source_zones, k)
        L, sched = apply_slotting(lines, repl, master, moved,
                                  dest_zones=dest_zones, relief_zones=source_zones,
                                  relief_min=relief_min)
        s = engine.summarize(engine.simulate(L, params, sched, step_min=step_min))
        ex, _ = _interference_load(L, sched, params)
        avg_dist = float((dist_map.reindex(L["sku_id"]).values
                          + L["base_adj"].values / DIST_SLOPE).mean())
        rows.append({"n_move": k, "sla_overall": s["sla_overall"],
                     "sla_focus": s["sla_focus"], "sla_base": s["sla_base"],
                     "total_excess_h": ex, "avg_pick_dist": avg_dist})
    return pd.DataFrame(rows)


def slotting_verdict(lines, repl, master, params, step_min=10):
    """3-2 판정: '순수 강도감소(이상적 상한)' vs '실현 슬로팅(거리 페널티 포함)'.
    이동 없이 ZB/ZD 소요만 줄이면 개선되지만, 그 강도감소를 SKU 이동으로 실현하면
    거리 페널티가 이득을 압도함을 대비한다."""
    def sim(L, sched):
        s = engine.summarize(engine.simulate(L, params, sched, step_min=step_min))
        ex, _ = _interference_load(L, sched, params)
        return s["sla_overall"], s["sla_focus"], ex

    out = []
    # baseline
    L0, s0 = apply_slotting(lines, repl, master, [])
    o, f, e = sim(L0, s0)
    out.append(("baseline", o, f, e))
    # 이상적 상한: 이동 없이 ZB/ZD 소요만 32분으로 (거리 페널티 0 — 물리적 실현 불가)
    _, s_ideal = apply_slotting(lines, repl, master, [], relief_min=DEST_REPL_MIN)
    o, f, e = sim(lines.assign(base_adj=0.0), s_ideal)
    out.append(("순수강도↓(상한,이동0)", o, f, e))
    # 실현 슬로팅: 40개 이동 (ZB/ZD -> ZE/ZF), relief 미적용(보수)
    mv = select_moves(lines, master, ("ZB", "ZD"), 40)
    L1, s1 = apply_slotting(lines, repl, master, mv, dest_zones=("ZE", "ZF"))
    o, f, e = sim(L1, s1)
    out.append(("실현 3-2(40이동→ZE/ZF)", o, f, e))
    # 실현 슬로팅 + 낙관적 relief(남은 ZB/ZD도 32분)
    L2, s2 = apply_slotting(lines, repl, master, mv, dest_zones=("ZE", "ZF"),
                            relief_min=DEST_REPL_MIN)
    o, f, e = sim(L2, s2)
    out.append(("실현 3-2+relief(낙관)", o, f, e))
    return pd.DataFrame(out, columns=["scenario", "sla_overall", "sla_focus",
                                      "total_excess_h"])


# ------------------------------------------------------------
# (4) 3-3 저녁 배수 용량 (전환기 안전판)
# ------------------------------------------------------------
#  3-1·3-2가 '부하 재배치'라 포화 시스템에서 실패한 것과 달리, 3-3은 저녁 시간대에
#  '용량을 직접 추가'해 당일 백로그를 소진 → 주중 이월(화→금 톱니)을 차단한다.
#  트레이드오프: 초과근무 비용(worker-hours). '필요 최소 용량 vs SLA 이득'을 산출.
EVENING_HOURS = (19, 20, 21, 22, 23)


def evening_boost_spec(n_extra, dows=(0, 1, 2, 3, 4), hours=EVENING_HOURS):
    """저녁 보강 스펙 {(dow,hour): 추가인원}."""
    return {(d, h): n_extra for d in dows for h in hours}


def _entry_backlog_by_dow(res):
    b = res["backlog_hr"].copy()
    b["dow"] = b.t.dt.dayofweek; b["h"] = b.t.dt.hour
    return b[b.h == 14].groupby("dow")["backlog"].mean()


def overtime_hours(lines, spec, step_min=10):
    """스펙이 지평 전체에 추가하는 총 초과근무 worker-hours."""
    t0 = pd.Timestamp(lines["allocated_time"].min()).normalize()
    t1 = pd.Timestamp(lines["allocated_time"].max()).normalize() + pd.Timedelta(days=3)
    t0, step, n, _ = engine.make_grid(t0, t1, step_min)
    b = engine.boost_grid(t0, step_min, n, spec)
    return float(b.sum() * step_min / 60.0)


def run_evening_capacity(lines, repl, params, n_extras=(0, 5, 10, 15, 20, 30),
                         dows=(0, 1, 2, 3, 4), step_min=10):
    """저녁 추가 인원을 스윕하여 SLA·진입백로그·초과근무비용을 산출."""
    sched = policies.baseline(repl)
    rows = []
    for ne in n_extras:
        spec = evening_boost_spec(ne, dows=dows) if ne > 0 else None
        res = engine.simulate(lines, params, sched, step_min=step_min,
                              worker_boost=spec)
        s = engine.summarize(res)
        e14 = _entry_backlog_by_dow(res)
        ot = overtime_hours(lines, spec, step_min) if spec else 0.0
        rows.append({
            "n_extra": ne, "sla_overall": s["sla_overall"],
            "sla_focus": s["sla_focus"], "sla_base": s["sla_base"],
            "entry_fri": float(e14.get(4, float("nan"))),
            "entry_peak": float(e14.max()),
            "overtime_h": ot,
        })
    return pd.DataFrame(rows)


# ------------------------------------------------------------
# (5) NEW-1 마감 기반 우선순위 피킹 (큐 규율)
# ------------------------------------------------------------
#  용량·부하 불변, '기존 용량을 마감 기준으로 재배분'하는 무비용 레버.
#  fifo(현행) vs edd(마감우선) vs triage(구제가능 우선, 지연 상한 cap).
def _discipline_metrics(res):
    comp = res["completion_ns"].astype("int64")
    prom = res["promise_time"].astype("datetime64[ns]").astype("int64")
    lm = (comp - prom) / 6e10                     # 지각(분), 양수=위반
    s = engine.summarize(res)
    return {
        "sla_overall": s["sla_overall"], "sla_focus": s["sla_focus"],
        "sla_base": s["sla_base"],
        "late_p50": float(np.percentile(lm[lm > 0], 50)) if (lm > 0).any() else 0.0,
        "late_p95": float(np.percentile(lm[lm > 0], 95)) if (lm > 0).any() else 0.0,
        "late_max_h": float(lm.max() / 60),
        "over_1d_pct": float((lm > 1440).mean() * 100),
        "over_2d_pct": float((lm > 2880).mean() * 100),
    }


def run_disciplines(lines, repl, params, configs=None, step_min=10):
    """큐 규율 비교. configs: [(label, discipline, cap_min), ...]."""
    if configs is None:
        configs = [
            ("FIFO(현행)", "fifo", None),
            ("EDD(마감우선)", "edd", None),
            ("트리아지 cap=12h", "triage", 720),
            ("트리아지 cap=24h", "triage", 1440),
            ("트리아지 cap=∞", "triage", None),
        ]
    sched = policies.baseline(repl)
    rows = []
    for lbl, disc, cap in configs:
        res = engine.simulate(lines, params, sched, step_min=step_min,
                              discipline=disc, defer_cap_min=cap)
        m = _discipline_metrics(res)
        m["label"] = lbl
        rows.append(m)
    return pd.DataFrame(rows).set_index("label")


def run_combo(lines, repl, params, cap_min=720, evening_dows=(1, 2, 3),
              n_extras=(10, 20, 30), step_min=10):
    """권고 패키지: 무비용 트리아지(NEW-1) → 정밀 타겟 저녁 용량(3-3)을 순차로 쌓는다.
    레버가 곱셈적으로 결합하고, 저녁 용량이 트리아지의 '지각 꼬리'까지 치료함을 보인다."""
    sched = policies.baseline(repl)
    bwh = base_worker_hours(lines)

    def one(disc, cap, nE, label, cost):
        spec = evening_boost_spec(nE, dows=evening_dows) if nE > 0 else None
        res = engine.simulate(lines, params, sched, step_min=step_min,
                              discipline=disc, defer_cap_min=cap, worker_boost=spec)
        m = _discipline_metrics(res)
        ot = overtime_hours(lines, spec, step_min) if spec else 0.0
        return {"label": label, "cost": cost, "sla_overall": m["sla_overall"],
                "sla_focus": m["sla_focus"], "late_max_d": m["late_max_h"] / 24,
                "over_1d_pct": m["over_1d_pct"], "overtime_pct": ot / bwh * 100}

    rows = [one("fifo", None, 0, "현행 FIFO", "—"),
            one("triage", cap_min, 0, "① 트리아지", "무비용")]
    for nE in n_extras:
        rows.append(one("triage", cap_min, nE, f"② +저녁 {nE}명", "저비용"))
    return pd.DataFrame(rows)


def base_worker_hours(lines):
    """baseline 총 작업자-시간 (초과근무 비율의 분모)."""
    st = lines["start_time"]
    per = (pd.DataFrame({"date": st.dt.normalize(), "hour": st.dt.hour,
                        "wid": lines["worker_id"].values})
           .groupby(["date", "hour"])["wid"].nunique())
    return float(per.sum())


def evening_frontier(lines, repl, params, n_extras=(0, 5, 10, 15, 20, 30),
                     step_min=10):
    """타겟팅 변형별(요일·시간 범위) 비용-효익 프론티어."""
    variants = {
        "Mon-Fri×19-23": ((0, 1, 2, 3, 4), EVENING_HOURS),
        "Tue-Thu×19-23": ((1, 2, 3), EVENING_HOURS),
        "Tue-Thu×19-21": ((1, 2, 3), (19, 20, 21)),
    }
    bwh = base_worker_hours(lines)
    out = {}
    for lbl, (dows, hours) in variants.items():
        rows = []
        for ne in n_extras:
            spec = ({(d, h): ne for d in dows for h in hours}) if ne > 0 else None
            res = engine.simulate(lines, params, policies.baseline(repl),
                                  step_min=step_min, worker_boost=spec)
            s = engine.summarize(res)
            ot = overtime_hours(lines, spec, step_min) if spec else 0.0
            rows.append({"n_extra": ne, "sla_focus": s["sla_focus"],
                         "sla_overall": s["sla_overall"], "overtime_h": ot,
                         "ot_pct_labor": ot / bwh * 100})
        out[lbl] = pd.DataFrame(rows)
    return out


def cost_shape_sensitivity(lines, repl, params, step_min=10,
                           shapes=("concave", "linear", "convex"),
                           policy="s31_spread"):
    """형태별로 base_service를 재보정(baseline SLA 23% 일치)한 뒤,
    baseline 대비 정책의 포커스 SLA 변화를 비교."""
    rows = []
    for shp in shapes:
        p = copy.deepcopy(params)
        p["cost_lookup"] = _shape_lookup(shp, params, lines, repl)
        # 형태가 바뀌면 base_service 재보정(공정 비교의 핵심)
        p["service"] = dict(p["service"])
        p["service"]["base_service"] = calibrate_base_service(lines, repl, p,
                                                              step_min=step_min)
        sched_base = policies.baseline(repl)
        sched_pol = policies.POLICIES[policy](repl)
        base = engine.summarize(engine.simulate(lines, p, sched_base, step_min=step_min))
        pol = engine.summarize(engine.simulate(lines, p, sched_pol, step_min=step_min))
        ex_base, _ = _interference_load(lines, sched_base, p)
        ex_pol, _ = _interference_load(lines, sched_pol, p)
        rows.append({
            "shape": shp,
            "base_service": p["service"]["base_service"],
            "baseline_focus": base["sla_focus"],
            f"{policy}_focus": pol["sla_focus"],
            "delta_focus": pol["sla_focus"] - base["sla_focus"],
            "excess_base_h": ex_base,
            "excess_pol_h": ex_pol,
            "delta_excess_h": ex_pol - ex_base,
        })
    return pd.DataFrame(rows)
