# -*- coding: utf-8 -*-
"""
grid.py — 용량 정책 그리드 서치 + 제약 조회 (해커톤 당일용)

당일 제약(예산·인력 상한)이 "기존 데이터 위에 추가"되는 형태라면, 계산은 미리
끝내두고 당일에는 조회만 하면 된다. 이 모듈이 그 역할을 한다.

  build_grid() : 정적/반응형 × 인원 × 임계치 × 시간대 × 큐규율 전 조합을 돌려
                 (SLA, 비용) 표를 만들고 CSV 로 저장. 런당 ~0.8초, 전체 ~2분.
  query()      : 제약(추가노동 %, 동시 추가 인원, 호출일수)을 넣으면 실행가능
                 최선안 + 실행가능 프론티어를 반환.
  plot_frontier(): 비용-효과 프론티어 그림(정적 곡선 vs 반응형 곡선).

정책 두 종류
  정적(static)   : 지정 요일의 지정 시간대에 무조건 n명 추가. 부하와 무관.
  반응형(reactive): 매일 obs_hour 정각에 그 시점 백로그를 보고 threshold 초과면
                   당일 저녁만 n명 호출. 폐루프(시뮬 자기 상태로 판단, 예지 없음).
                   obs_hour=11 은 리드타임 8시간(잔업·대기조 편성 현실적),
                   obs_hour=17 은 리드타임 2시간(정보는 정확하나 호출이 빠듯).
"""
from __future__ import annotations
import os
import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from config import OUT_DIR
import engine
import policies
import scenarios


GRID_CSV = os.path.join(OUT_DIR, "policy_grid.csv")

EVENING = (19, 20, 21, 22, 23)
EVENING_SHORT = (19, 20, 21)


# ------------------------------------------------------------
# 그리드 실행
# ------------------------------------------------------------
def build_grid(lines, repl, params, step_min=10, save=True,
               thresholds=(200, 500, 1000, 1500, 2000),
               extras=(10, 20, 30),
               hour_sets=(EVENING, EVENING_SHORT),
               obs_hours=(11, 17),
               disciplines=(("fifo", None), ("triage", 720))) -> pd.DataFrame:
    """전 조합 실행 → 한 행 = 한 정책, 열 = SLA·비용 지표."""
    sched = policies.baseline(repl)
    bwh = scenarios.base_worker_hours(lines)
    rows = []

    def run(label, mode, disc, cap, n_extra, hours, obs_hour, thr, spec, reactive):
        res = engine.simulate(lines, params, sched, step_min=step_min,
                              discipline=disc, defer_cap_min=cap,
                              worker_boost=spec, reactive=reactive)
        m = scenarios._discipline_metrics(res)
        rows.append({
            "label": label, "mode": mode,
            "discipline": disc, "cap_min": cap,
            "n_extra": n_extra,
            "hours": "-".join(map(str, hours)) if hours else "",
            "obs_hour": obs_hour, "threshold": thr,
            "sla_overall": m["sla_overall"], "sla_focus": m["sla_focus"],
            "sla_base": m["sla_base"],
            "late_max_h": m["late_max_h"], "over_1d_pct": m["over_1d_pct"],
            "overtime_h": res["extra_worker_h"],
            "overtime_pct": res["extra_worker_h"] / bwh * 100,
            "max_extra": n_extra,              # 동시 추가 인원 피크
            "call_days": res["call_days"],
        })

    # 기준선 (용량 추가 없음)
    for disc, cap in disciplines:
        tag = "현행 FIFO" if disc == "fifo" else f"트리아지 cap={cap//60}h"
        run(tag, "기준선", disc, cap, 0, None, None, None, None, None)

    # 정적: 지정 요일 고정 투입
    for dows, dlab in [((1, 2, 3), "화~목"), ((0, 1, 2, 3, 4), "월~금")]:
        for ne in extras:
            for hrs in hour_sets:
                spec = scenarios.evening_boost_spec(ne, dows=dows, hours=hrs)
                for disc, cap in disciplines:
                    dt = "" if disc == "fifo" else "+트리아지"
                    run(f"정적 {dlab} +{ne}명 {hrs[0]}-{hrs[-1]}시{dt}",
                        "정적", disc, cap, ne, hrs, None, None, spec, None)

    # 반응형: 관측시각 백로그 > 임계치인 날만 호출
    for oh in obs_hours:
        for thr in thresholds:
            for ne in extras:
                for hrs in hour_sets:
                    if oh >= hrs[0]:          # 관측이 투입보다 늦으면 인과 위반
                        continue
                    rc = {"obs_hour": oh, "threshold": thr,
                          "extra": ne, "hours": hrs}
                    for disc, cap in disciplines:
                        dt = "" if disc == "fifo" else "+트리아지"
                        run(f"반응형 {oh}시판단 θ={thr} +{ne}명 "
                            f"{hrs[0]}-{hrs[-1]}시{dt}",
                            "반응형", disc, cap, ne, hrs, oh, thr, None, rc)

    df = pd.DataFrame(rows)
    if save:
        df.to_csv(GRID_CSV, index=False, encoding="utf-8-sig")
    return df


def load_grid(path=GRID_CSV) -> pd.DataFrame:
    return pd.read_csv(path)


# ------------------------------------------------------------
# 제약 조회 (당일 입력 → 즉답)
# ------------------------------------------------------------
def query(df: pd.DataFrame, max_overtime_pct=None, max_extra_workers=None,
          max_call_days=None, max_late_h=None, objective="sla_overall",
          top=10):
    """제약을 만족하는 정책 중 objective 최소인 순으로 반환.

    max_overtime_pct  : 추가 인건비(작업자-시간) 상한, baseline 대비 %
    max_extra_workers : 동시 추가 인원 상한(명)
    max_call_days     : 호출 가능 일수 상한(반응형에만 의미)
    max_late_h        : 최대 지각 허용치(시간) — 트리아지 꼬리 통제용
    """
    f = df.copy()
    if max_overtime_pct is not None:
        f = f[f.overtime_pct <= max_overtime_pct + 1e-9]
    if max_extra_workers is not None:
        f = f[f.max_extra <= max_extra_workers]
    if max_call_days is not None:
        f = f[f.call_days <= max_call_days]
    if max_late_h is not None:
        f = f[f.late_max_h <= max_late_h]
    return f.sort_values(objective).head(top).reset_index(drop=True)


def pareto(df: pd.DataFrame, cost="overtime_pct", obj="sla_overall") -> pd.DataFrame:
    """비용 오름차순으로 훑으며 obj 가 갱신되는 점만 남긴다(비지배 집합)."""
    d = df.sort_values([cost, obj]).reset_index(drop=True)
    keep, best = [], np.inf
    for i, r in d.iterrows():
        if r[obj] < best - 1e-9:
            best = r[obj]; keep.append(i)
    return d.loc[keep].reset_index(drop=True)


def report(df: pd.DataFrame, constraints: dict | None = None, path=None):
    """당일 제출용 텍스트 리포트: 제약 → 최선안 + 프론티어."""
    L = ["=" * 72, " 용량 정책 그리드 — 제약 조회 결과", "=" * 72]
    if constraints:
        L.append("\n[제약]")
        for k, v in constraints.items():
            L.append(f"  {k} = {v}")
    f = query(df, **(constraints or {}), top=8)
    L.append(f"\n[실행가능 최선안 상위 {len(f)}개]  (전체 {len(df)}개 정책 중)")
    L.append(f"  {'정책':<44}{'전체':>6}{'포커스':>7}{'추가노동':>9}{'호출일':>6}")
    for _, r in f.iterrows():
        L.append(f"  {r['label'][:43]:<44}{r.sla_overall:6.1f}{r.sla_focus:7.1f}"
                 f"{r.overtime_pct:8.1f}%{int(r.call_days):6d}")
    pf = pareto(f if len(f) else df)
    L.append("\n[비용-효과 프론티어] (이 비용에서 달성 가능한 최선)")
    for _, r in pf.iterrows():
        L.append(f"  +{r.overtime_pct:5.1f}% 노동 → SLA {r.sla_overall:5.1f}%"
                 f"  ({r['label'][:48]})")
    txt = "\n".join(L)
    if path is None:
        path = os.path.join(OUT_DIR, "grid_report.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(txt)
    return txt


# ------------------------------------------------------------
# 프론티어 그림
# ------------------------------------------------------------
def plot_frontier(df: pd.DataFrame, constraints: dict | None = None,
                  obj="sla_overall", path=None):
    """정적 vs 반응형 비용-효과 프론티어. 제약이 있으면 실행가능 영역을 음영."""
    from evaluate import _font
    _font()
    fig, ax = plt.subplots(figsize=(9, 5.6))

    base = df[df["mode"] == "기준선"]
    style = {"정적": ("tab:red", "o", "정적 (요일 고정 투입)"),
             "반응형": ("tab:blue", "s", "반응형 (임계 기반 조건부 호출)")}
    for mode, (c, mk, lab) in style.items():
        sub = df[df["mode"] == mode]
        ax.scatter(sub.overtime_pct, sub[obj], s=16, alpha=.22, color=c)
        pf = pareto(sub, obj=obj)
        ax.plot(pf.overtime_pct, pf[obj], "-", color=c, lw=2.2,
                marker=mk, ms=6, label=lab)

    for _, r in base.iterrows():
        ax.scatter([0], [r[obj]], marker="*", s=190, color="k", zorder=5)
        ax.annotate(r["label"], (0, r[obj]), textcoords="offset points",
                    xytext=(9, 2), fontsize=9)

    if constraints and constraints.get("max_overtime_pct") is not None:
        x = constraints["max_overtime_pct"]
        ax.axvspan(ax.get_xlim()[0], x, color="green", alpha=.06)
        ax.axvline(x, color="green", ls="--", lw=1.4)
        ax.annotate(f"예산 상한 {x}%", (x, ax.get_ylim()[1]),
                    textcoords="offset points", xytext=(4, -14),
                    fontsize=9, color="green")

    ax.set_xlabel("추가 인건비 (baseline 작업자-시간 대비 %)")
    ax.set_ylabel(f"SLA 위반율 (%)  [{obj}]")
    ax.set_title("용량 정책 비용-효과 프론티어 — 같은 SLA를 더 싸게")
    ax.grid(alpha=.3); ax.legend(loc="upper right")
    fig.tight_layout()
    if path is None:
        path = os.path.join(OUT_DIR, "fig_policy_frontier.png")
    fig.savefig(path, dpi=140); plt.close(fig)
    return path
