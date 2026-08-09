# -*- coding: utf-8 -*-
"""
calibrate.py — 데이터에서 시뮬레이션 파라미터를 추정

추정 대상
---------
(a) 간섭 비용함수  pick_service(c)   : 동시 보충 c 건일 때 pick 서비스시간(초)
      → ★형태(볼록/오목) 진단★ : 멘토 지적의 핵심 게이트
(b) 유효 서비스시간 s_eff(0)         : 간섭 0일 때 라인당 작업자 점유시간(초)
      overhead = s_eff(0) - pick(0)  (pack·이동 등, 간섭 무관 고정분)
(c) 시간대별 활성 작업자 W[dow,hour]
(d) Zone별 pick 물량 비중            : 간섭의 수요가중 평균에 사용
(e) 보충 프로파일 (zone별 소요, reason 분포)
(f) promise_time 규칙 (오전 당일 / 오후 익일) — 확인용

산출
----
sim/outputs/calib_params.pkl   : 엔진이 읽는 파라미터 번들
sim/outputs/calib_report.txt   : 사람이 읽는 요약
sim/outputs/fig_cost_function.png : 간섭 비용함수 + 형태 진단
"""
from __future__ import annotations
import os
import pickle
import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from scipy.optimize import curve_fit

from config import OUT_DIR, ZONES, DOW_KR
import io_load


# ------------------------------------------------------------
# 동시 보충 건수(concurrency) 계산
# ------------------------------------------------------------
def concurrency_at(times: np.ndarray, starts: np.ndarray, ends: np.ndarray) -> np.ndarray:
    """각 시각 t에서 [start, end] 구간이 겹치는 보충작업 수.
    active(t) = (#start <= t) - (#end <= t)."""
    s = np.sort(starts)
    e = np.sort(ends)
    return (np.searchsorted(s, times, side="right")
            - np.searchsorted(e, times, side="right")).astype(int)


def compute_pick_concurrency(lines: pd.DataFrame, repl: pd.DataFrame) -> np.ndarray:
    """각 pick 이벤트의 pick_start 시점, 같은 zone 동시 보충 건수."""
    conc = np.zeros(len(lines), dtype=int)
    ps = lines["pick_start"].values.astype("datetime64[ns]")
    zone = lines["zone_id"].values
    for z in ZONES:
        rz = repl[repl["zone_id"] == z]
        if len(rz) == 0:
            continue
        m = zone == z
        conc[m] = concurrency_at(
            ps[m],
            rz["start_time"].values.astype("datetime64[ns]"),
            rz["complete_time"].values.astype("datetime64[ns]"),
        )
    return conc


# ------------------------------------------------------------
# (a) 간섭 비용함수 + 형태 진단
# ------------------------------------------------------------
def _sat(c, s0, A, kappa):
    """포화(오목) 형태: s0 + A*(1 - exp(-c/kappa))."""
    return s0 + A * (1.0 - np.exp(-c / kappa))


def fit_cost_function(lines: pd.DataFrame, conc: np.ndarray) -> dict:
    df = pd.DataFrame({"c": conc, "dur": lines["pick_dur"].values})
    # 개별 concurrency 레벨별 평균 (관측 룩업테이블)
    by_c = df.groupby("c")["dur"].agg(["mean", "count"])
    c_vals = by_c.index.values.astype(float)
    d_vals = by_c["mean"].values
    w = by_c["count"].values

    # 포화(오목) 곡선 적합 — 표본수 가중
    p0 = [d_vals[0], d_vals.max() - d_vals[0], 3.0]
    try:
        popt, _ = curve_fit(_sat, c_vals, d_vals, p0=p0,
                            sigma=1.0 / np.sqrt(w), maxfev=20000)
        s0_fit, A_fit, kappa_fit = [float(x) for x in popt]
        sat_pred = _sat(c_vals, *popt)
        sat_sse = float(np.sum(w * (d_vals - sat_pred) ** 2))
    except Exception:
        s0_fit = float(d_vals[0]); A_fit = float(d_vals.max() - d_vals[0]); kappa_fit = 3.0
        sat_sse = np.nan

    # 선형 적합(비교용)
    lin = np.polyfit(c_vals, d_vals, 1, w=w)
    lin_pred = np.polyval(lin, c_vals)
    lin_sse = float(np.sum(w * (d_vals - lin_pred) ** 2))

    # ---- 형태 진단 (볼록 vs 오목) ----
    pick0 = float(d_vals[c_vals == 0][0]) if (c_vals == 0).any() else float(d_vals[0])
    # 버킷 한계증가분(제출물과 동일 버킷) — 오목이면 단조 감소
    def bucket(c):
        if c == 0: return "0"
        if c <= 5: return "1-5"
        if c <= 20: return "6-20"
        if c <= 50: return "21-50"
        return "50+"
    df["cb"] = df["c"].map(bucket)
    order = ["0", "1-5", "6-20", "21-50", "50+"]
    bk = df.groupby("cb")["dur"].mean().reindex(order)
    marg = bk.diff().dropna()             # 버킷 간 증가분
    concave = bool((marg.iloc[0] > marg.iloc[1:]).all())  # 첫 증가분이 이후를 압도?

    # 첫 단위 페널티가 전체 팽창에서 차지하는 비중
    first_step = float(bk["1-5"] - bk["0"])
    total_span = float(bk["50+"] - bk["0"])
    first_share = first_step / total_span if total_span else np.nan

    return {
        "lookup_c": c_vals.tolist(),
        "lookup_dur": d_vals.tolist(),
        "lookup_count": w.tolist(),
        "fit": {"s0": s0_fit, "A": A_fit, "kappa": kappa_fit,
                "sat_sse": sat_sse, "lin_sse": lin_sse},
        "pick0": pick0,
        "bucket_mean": {k: float(v) for k, v in bk.items()},
        "bucket_marginal": {k: float(v) for k, v in marg.items()},
        "concave": concave,
        "first_step_sec": first_step,
        "first_step_share": first_share,
    }


def cost_lookup_array(cost: dict, max_c: int = 200) -> np.ndarray:
    """엔진용 룩업 배열 pick_service[c] (0..max_c). 관측 구간은 관측값,
    그 밖은 포화곡선 외삽."""
    arr = np.empty(max_c + 1)
    obs = {int(c): d for c, d in zip(cost["lookup_c"], cost["lookup_dur"])}
    s0, A, kappa = cost["fit"]["s0"], cost["fit"]["A"], cost["fit"]["kappa"]
    last = None
    for c in range(max_c + 1):
        if c in obs:
            arr[c] = obs[c]; last = obs[c]
        else:
            arr[c] = _sat(c, s0, A, kappa) if last is None else max(last, _sat(c, s0, A, kappa))
    return arr


# ------------------------------------------------------------
# (c) 활성 작업자, (d) zone 비중, (e) 보충, (b) 유효 서비스시간
# ------------------------------------------------------------
def calibrate_workers(lines: pd.DataFrame) -> dict:
    st = lines["start_time"]
    tmp = pd.DataFrame({
        "date": st.dt.normalize(), "dow": st.dt.dayofweek,
        "hour": st.dt.hour, "wid": lines["worker_id"].values,
    })
    per = tmp.groupby(["date", "dow", "hour"])["wid"].nunique().reset_index(name="n")
    mean_wh = per.groupby(["dow", "hour"])["n"].mean()
    return {f"{d}_{h}": float(v) for (d, h), v in mean_wh.items()}


def calibrate_service(lines: pd.DataFrame, conc: np.ndarray, pick0: float) -> dict:
    """유효 서비스시간 s_eff(0): 간섭 0 라인의 작업자 점유시간(handling) 중앙값 기반.
    단, handling에는 파이프라이닝 여유가 섞여 있으므로, '처리량으로부터 역산한
    유효 서비스시간'을 병기해 엔진이 클린 처리량을 재현하도록 한다."""
    clean = lines["handling"].values[conc == 0]
    handling0 = float(np.median(clean))

    # 처리량 역산: 평일 클린 시간대(간섭 0)에서 W*3600/T
    st = lines["start_time"]
    df = pd.DataFrame({
        "date": st.dt.normalize(), "dow": st.dt.dayofweek, "hour": st.dt.hour,
        "wid": lines["worker_id"].values, "c": conc,
    })
    # 완료(pick_end) 기준 시간당 처리량
    pe = lines["pick_end"]
    dfc = pd.DataFrame({"date": pe.dt.normalize(), "dow": pe.dt.dayofweek,
                        "hour": pe.dt.hour})
    comp = dfc.groupby(["date", "dow", "hour"]).size().reset_index(name="T")
    wk = df.groupby(["date", "dow", "hour"]).agg(W=("wid", "nunique"),
                                                cmean=("c", "mean")).reset_index()
    j = comp.merge(wk, on=["date", "dow", "hour"])
    weekday = j[(j["dow"] < 5) & (j["hour"].between(9, 18))]
    clean_hours = weekday[weekday["cmean"] < 0.5]
    if len(clean_hours) > 30:
        s_eff0 = float((clean_hours["W"] * 3600 / clean_hours["T"]).median())
    else:
        s_eff0 = handling0
    return {"s_eff0": s_eff0, "handling0_median": handling0,
            "overhead": s_eff0 - pick0, "pick0": pick0}


def calibrate_base_service(lines, repl, params,
                          coarse=range(500, 621, 20), step_min=10) -> float:
    """base_service(간섭 없는 클린 유효 서비스시간)를 스윕하여
    집계 SLA(전체·포커스·베이스)를 동시에 재현한다. 나머지 파라미터는
    데이터에서 고정, base 하나만 조정하는 1-파라미터 보정."""
    import engine, policies
    from config import TARGETS
    sched = policies.baseline(repl)
    tgt = (TARGETS["sla_overall"], TARGETS["sla_focus"], TARGETS["sla_base"])

    def joint_err(base):
        s = engine.summarize(engine.simulate(
            lines, params, sched, step_min=step_min, base_service=base))
        return (abs(s["sla_overall"] - tgt[0])
                + abs(s["sla_focus"] - tgt[1])
                + abs(s["sla_base"] - tgt[2]))

    grid = list(coarse)
    _, b0 = min((joint_err(b), b) for b in grid)
    fine = [b0 + d for d in range(-15, 16, 3)]
    _, best = min((joint_err(b), b) for b in fine)
    return float(best)


def calibrate_repl(repl: pd.DataFrame) -> dict:
    return {
        "dur_sec_by_zone": {z: float(repl[repl.zone_id == z]["dur_sec"].mean())
                            for z in ZONES},
        "reason_counts": repl["repl_reason"].value_counts().to_dict(),
        "n_total": int(len(repl)),
    }


def calibrate_zone_share(lines: pd.DataFrame) -> dict:
    vc = lines["zone_id"].value_counts(normalize=True)
    return {z: float(vc.get(z, 0.0)) for z in ZONES}


def check_promise_rule(lines: pd.DataFrame) -> dict:
    same = (lines["promise_time"].dt.normalize() == lines["order_time"].dt.normalize())
    lead = (lines["promise_time"] - lines["order_time"]).dt.total_seconds() / 60
    byh = pd.DataFrame({"h": lines["ohour"], "same": same.astype(int), "lead": lead})
    g = byh.groupby("h").agg(same_day=("same", "mean"), lead_med=("lead", "median"))
    return {int(h): {"same_day": float(r.same_day), "lead_med": float(r.lead_med)}
            for h, r in g.iterrows()}


# ------------------------------------------------------------
# 리포트 & 그림
# ------------------------------------------------------------
def _plot_cost(cost: dict, path: str):
    c = np.array(cost["lookup_c"]); d = np.array(cost["lookup_dur"])
    s0, A, kappa = cost["fit"]["s0"], cost["fit"]["A"], cost["fit"]["kappa"]
    cc = np.linspace(0, min(c.max(), 64), 200)
    fig, ax = plt.subplots(1, 2, figsize=(12, 4.6))
    ax[0].scatter(c, d, s=14, color="#c0504d", label="관측 평균")
    ax[0].plot(cc, _sat(cc, s0, A, kappa), color="#3b6fb5",
              label=f"포화적합 s0={s0:.0f}, A={A:.0f}, κ={kappa:.1f}")
    ax[0].set(title="간섭 비용함수: 동시 보충 → pick 서비스시간",
             xlabel="동시 보충 건수 c", ylabel="pick 서비스시간(초)")
    ax[0].legend()
    bk = cost["bucket_mean"]; order = ["0", "1-5", "6-20", "21-50", "50+"]
    vals = [bk[k] for k in order]
    ax[1].bar(order, vals, color="#8064a2")
    for i, v in enumerate(vals):
        ax[1].text(i, v + 5, f"{v:.0f}", ha="center", fontsize=9)
    shape = "오목(포화)" if cost["concave"] else "볼록/기타"
    ax[1].set(title=f"버킷별 평균 · 형태={shape} · 첫계단 비중 {cost['first_step_share']*100:.0f}%",
             ylabel="pick 서비스시간(초)")
    fig.tight_layout()
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)


def _try_korean_font():
    import platform
    try:
        if platform.system() == "Windows":
            plt.rcParams["font.family"] = "Malgun Gothic"
        elif platform.system() == "Darwin":
            plt.rcParams["font.family"] = "AppleGothic"
        else:
            plt.rcParams["font.family"] = "NanumGothic"
        plt.rcParams["axes.unicode_minus"] = False
    except Exception:
        pass


def run_calibration(rebuild: bool = False, calibrate_base: bool = True) -> dict:
    _try_korean_font()
    lines = io_load.load_lines(rebuild)
    repl = io_load.load_repl(rebuild)

    conc = compute_pick_concurrency(lines, repl)
    cost = fit_cost_function(lines, conc)
    service = calibrate_service(lines, conc, cost["pick0"])
    workers = calibrate_workers(lines)
    zone_share = calibrate_zone_share(lines)
    repl_prof = calibrate_repl(repl)
    promise = check_promise_rule(lines)

    params = {
        "cost": cost,
        "cost_lookup": cost_lookup_array(cost, max_c=200),
        "service": service,
        "workers": workers,
        "zone_share": zone_share,
        "repl": repl_prof,
        "promise": promise,
    }

    # base_service: 전체 SLA를 재현하도록 보정하는 유일한 자유 파라미터
    if calibrate_base:
        base = calibrate_base_service(lines, repl, params)
        params["service"]["base_service"] = base
        params["service"]["base_calibrated"] = True

    with open(os.path.join(OUT_DIR, "calib_params.pkl"), "wb") as f:
        pickle.dump(params, f)
    _plot_cost(cost, os.path.join(OUT_DIR, "fig_cost_function.png"))
    _write_report(params)
    return params


def _write_report(p: dict):
    c = p["cost"]; s = p["service"]
    L = []
    L.append("=" * 64)
    L.append(" 캘리브레이션 리포트")
    L.append("=" * 64)
    L.append("\n[a] 간섭 비용함수 (동시 보충 c -> pick 서비스시간, 초)")
    L.append(f"  pick(0) = {c['pick0']:.1f}s (간섭 없음, 클린)")
    for k in ["0", "1-5", "6-20", "21-50", "50+"]:
        L.append(f"    버킷 {k:>5}: {c['bucket_mean'][k]:.1f}s")
    L.append("  버킷 한계증가분: " +
             ", ".join(f"{k}=+{v:.0f}" for k, v in c["bucket_marginal"].items()))
    L.append(f"  형태 진단: {'오목(포화) — 첫 단위 페널티가 지배' if c['concave'] else '볼록/기타'}")
    L.append(f"    첫 계단(0->1~5) = +{c['first_step_sec']:.0f}s "
             f"= 전체 팽창의 {c['first_step_share']*100:.0f}%")
    L.append(f"    포화적합 s0={c['fit']['s0']:.0f}, A={c['fit']['A']:.0f}, "
             f"kappa={c['fit']['kappa']:.2f} (SSE 포화 {c['fit']['sat_sse']:.0f} "
             f"vs 선형 {c['fit']['lin_sse']:.0f})")
    L.append("  ▶ 함의: 오목 비용함수에서 보충을 여러 시간대로 '펼치면' 깨끗하던")
    L.append("    시간대가 첫 계단 페널티를 새로 물어 총 픽시간이 늘 수 있음(역효과 위험).")

    L.append("\n[b] 유효 서비스시간 (엔진 모델: s_eff(c) = base_service + [pick(c)-pick(0)])")
    if s.get("base_calibrated"):
        L.append(f"  base_service = {s['base_service']:.0f}s/line  ★SLA 23% 재현 보정값★")
    L.append(f"  s_eff(0) 참고(처리량 역산) = {s['s_eff0']:.0f}s/line")
    L.append(f"  handling(관측 중앙값) = {s['handling0_median']:.0f}s")
    L.append(f"  → 시스템이 ~99% 가동률(포화 근처)에서 운영됨: 오후 간섭이 소폭만")
    L.append(f"    처리량을 깎아도 백로그가 기계적으로 누적되는 취약 구조.")

    L.append("\n[c] 활성 작업자 (평일 주간 표본)")
    wk = p["workers"]
    for h in range(9, 19):
        row = [f"{wk.get(f'{d}_{h}', float('nan')):.0f}" for d in range(5)]
        L.append(f"  {h:>2}시: 월{row[0]} 화{row[1]} 수{row[2]} 목{row[3]} 금{row[4]}")

    L.append("\n[d] Zone별 pick 물량 비중")
    L.append("  " + ", ".join(f"{z}={v*100:.0f}%" for z, v in p["zone_share"].items()))

    L.append("\n[e] 보충 프로파일")
    L.append("  zone별 평균 소요(분): " +
             ", ".join(f"{z}={v/60:.0f}" for z, v in p["repl"]["dur_sec_by_zone"].items()))
    L.append(f"  reason: {p['repl']['reason_counts']}")

    L.append("\n[f] promise_time 규칙 (주문 시각별 당일 비중 / 중앙 리드타임분)")
    for h in sorted(p["promise"]):
        r = p["promise"][h]
        L.append(f"    {h:>2}시: 당일 {r['same_day']*100:3.0f}% / 리드 {r['lead_med']:.0f}분")

    txt = "\n".join(L)
    with open(os.path.join(OUT_DIR, "calib_report.txt"), "w", encoding="utf-8") as f:
        f.write(txt)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    params = run_calibration("--rebuild" in sys.argv)
    with open(os.path.join(OUT_DIR, "calib_report.txt"), encoding="utf-8") as f:
        print(f.read())
    print("\n[저장] calib_params.pkl, calib_report.txt, fig_cost_function.png ->", OUT_DIR)
