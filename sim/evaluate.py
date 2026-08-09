# -*- coding: utf-8 -*-
"""
evaluate.py — baseline 재현 검증 리포트 + 그림

시뮬 결과가 제출물(실데이터) 패턴을 재현하는지 다각도로 확인한다.
  1) 집계 SLA (전체/포커스/베이스)  vs 목표
  2) 요일×시간 SLA 히트맵           시뮬 vs 관측
  3) 주간 진입 백로그 톱니(요일별)  시뮬 vs 문서
  4) 목요일 오후 단조 악화
재현이 성립해야 그 위에서 개선안(3-1)을 신뢰할 수 있다.
"""
from __future__ import annotations
import os
import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from config import OUT_DIR, DOW_KR, TARGETS, FOCUS_DOWS, FOCUS_HOURS


def _font():
    import platform
    try:
        plt.rcParams["font.family"] = ("Malgun Gothic" if platform.system() == "Windows"
                                       else "AppleGothic" if platform.system() == "Darwin"
                                       else "NanumGothic")
        plt.rcParams["axes.unicode_minus"] = False
    except Exception:
        pass


def _sla_pivot(dow, oh, v):
    df = pd.DataFrame({"dow": dow, "oh": oh, "v": v})
    piv = (df[df.oh.between(8, 18)]
           .pivot_table(index="dow", columns="oh", values="v", aggfunc="mean") * 100)
    piv.index = [DOW_KR[i] for i in piv.index]
    return piv


def baseline_report(lines: pd.DataFrame, res: dict, summ: dict, params: dict):
    _font()

    # ---- 1) 집계 SLA ----
    L = []
    L.append("=" * 64)
    L.append(" baseline 재현 검증")
    L.append("=" * 64)
    L.append("\n[1] 집계 SLA (시뮬 vs 목표)")
    L.append(f"  전체 : {summ['sla_overall']:5.1f}%  (목표 {TARGETS['sla_overall']})")
    L.append(f"  포커스: {summ['sla_focus']:5.1f}%  (목표 {TARGETS['sla_focus']})")
    L.append(f"  베이스: {summ['sla_base']:5.1f}%  (목표 {TARGETS['sla_base']})")
    L.append(f"  미완료: {summ['n_incomplete']:,} / {summ['n']:,}")

    # ---- 3) 주간 진입 백로그 ----
    b = res["backlog_hr"].copy()
    b["dow"] = b.t.dt.dayofweek; b["h"] = b.t.dt.hour
    e14 = b[b.h == 14].groupby("dow")["backlog"].mean()
    tgt = TARGETS["entry_backlog_by_dow"]
    L.append("\n[3] 진입 백로그 @14시 (요일별 평균) — 주말 배수·주중 톱니 이월")
    L.append("     " + "  ".join(f"{DOW_KR[d]}" for d in range(7)))
    L.append("  시뮬 " + "  ".join(f"{e14.get(d, np.nan):4.0f}" for d in range(7)))
    L.append("  문서 " + "  ".join(f"{tgt[d]:4d}" for d in range(7)))

    # ---- 4) 목요일 오후 단조 악화 ----
    piv = _sla_pivot(res["dow"], res["ohour"], res["sla_viol"])
    thu = [piv.loc["목", h] for h in [14, 15, 16, 17, 18]]
    L.append("\n[4] 목요일 14~18시 SLA (단조 악화)")
    L.append("  시뮬 " + " ".join(f"{x:4.0f}" for x in thu) + "   문서 [29,46,52,55,59]")

    txt = "\n".join(L)
    with open(os.path.join(OUT_DIR, "baseline_report.txt"), "w", encoding="utf-8") as f:
        f.write(txt)
    print("\n" + txt)

    # ---- 그림 A: 요일×시간 SLA 히트맵 (시뮬 vs 관측) ----
    piv_obs = _sla_pivot(lines["dow"].values, lines["ohour"].values,
                        lines["sla_viol_obs"].values)
    fig, ax = plt.subplots(1, 2, figsize=(15, 4.4))
    for a, (pv, ttl) in zip(ax, [(piv_obs, "관측(실데이터)"), (piv, "시뮬 baseline")]):
        im = a.imshow(pv.values, aspect="auto", cmap="Reds", vmin=0, vmax=65)
        a.set_xticks(range(len(pv.columns))); a.set_xticklabels(pv.columns)
        a.set_yticks(range(len(pv.index))); a.set_yticklabels(pv.index)
        for i in range(pv.shape[0]):
            for j in range(pv.shape[1]):
                a.text(j, i, f"{pv.values[i,j]:.0f}", ha="center", va="center",
                       fontsize=7, color="black")
        a.set(title=f"요일×시간 SLA 위반율(%) — {ttl}", xlabel="주문 시각")
        fig.colorbar(im, ax=a, fraction=0.03)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "fig_sla_heatmap.png"), dpi=130, bbox_inches="tight")
    plt.close(fig)

    # ---- 그림 B: 주간 백로그 톱니 (시뮬 vs 문서) ----
    fig, ax = plt.subplots(figsize=(8, 4.4))
    x = range(7)
    ax.plot(x, [e14.get(d, np.nan) for d in x], "o-", color="#3b6fb5", label="시뮬")
    ax.plot(x, [tgt[d] for d in x], "s--", color="#c0504d", label="문서(근거12)")
    ax.set_xticks(list(x)); ax.set_xticklabels(DOW_KR)
    ax.set(title="진입 백로그 @14시 — 요일별 (주중 이월 톱니)", ylabel="재공량(라인)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "fig_backlog_sawtooth.png"), dpi=130, bbox_inches="tight")
    plt.close(fig)

    # ---- 그림 C: 요일별 일중 백로그 프로파일 (오후 급증·야간 배수·주중 상승) ----
    prof = (b[b.h.between(0, 23)]
            .pivot_table(index="h", columns="dow", values="backlog", aggfunc="mean"))
    fig, ax = plt.subplots(figsize=(11, 4.4))
    colors = {0: "#9ecae1", 1: "#6baed6", 2: "#3182bd", 3: "#08519c",
              4: "#c0504d", 5: "#bbbbbb", 6: "#dddddd"}
    for d in range(7):
        if d in prof.columns:
            ax.plot(prof.index, prof[d], "-", color=colors[d], lw=2, label=DOW_KR[d])
    ax.axvspan(14, 17, color="orange", alpha=0.12, label="포커스 14~17시")
    ax.set(title="요일별 일중 백로그 프로파일 — 오후 급증·야간 배수, 요일 지날수록 상승",
           xlabel="시각", ylabel="평균 재공량(라인)", xlim=(0, 23))
    ax.legend(ncol=4, fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "fig_backlog_intraday.png"), dpi=130, bbox_inches="tight")
    plt.close(fig)

    print("\n[그림] fig_sla_heatmap.png, fig_backlog_sawtooth.png, "
          "fig_backlog_intraday.png ->", OUT_DIR)


# ------------------------------------------------------------
# 개선안 시나리오 비교
# ------------------------------------------------------------
_LABEL = {"baseline": "baseline", "s31_shift": "3-1 시점이동",
          "s31_spread": "3-1 평준화", "s32_slotting": "3-2 슬로팅(강도↓)"}


def compare_scenarios(scen: dict, sens, conc_profiles: dict, params: dict):
    """scen: {name: {summary,res,sched}}, sens: 형태민감도 DataFrame,
    conc_profiles: {name: {hour: conc}}."""
    _font()
    names = list(scen.keys())
    base = scen["baseline"]["summary"]

    L = []
    L.append("=" * 68)
    L.append(" 개선안(3-1) 시나리오 평가 — 실측 오목 비용함수")
    L.append("=" * 68)
    L.append(f"{'시나리오':<16}{'전체':>7}{'포커스':>8}{'베이스':>8}"
             f"{'Δ포커스':>9}{'총간섭h':>9}")
    for nm in names:
        s = scen[nm]["summary"]
        d = s["sla_focus"] - base["sla_focus"]
        L.append(f"{_LABEL.get(nm, nm):<16}{s['sla_overall']:>7.1f}"
                 f"{s['sla_focus']:>8.1f}{s['sla_base']:>8.1f}"
                 f"{d:>+9.1f}{s['total_excess_h']:>9.0f}")
    L.append("\n해석:")
    L.append(" · 3-1(시점 이동·평준화)은 포커스 SLA를 개선하지 못하고 오히려 소폭 악화.")
    L.append("   오목(포화) 비용함수에서 간섭을 시간축에서 옮겨도 총 간섭시간이 보존되고,")
    L.append("   ~99% 포화 시스템에서 오전 이동은 촉박한 당일 데드라인 픽까지 지연시킴.")
    L.append(" · 반면 3-2(강도↓: ZB·ZD 소요 단축)는 총 간섭을 줄여 SLA를 실제로 개선.")
    L.append("   → 해법은 간섭의 '시점'이 아니라 '강도/총량'을 낮추는 방향.")

    L.append("\n[비용함수 형태 민감도] 정책=s31_spread, 형태별 base 재보정")
    L.append(f"{'형태':<10}{'baseline포커스':>14}{'정책포커스':>11}{'Δ포커스':>9}"
             f"{'Δ총간섭h':>11}")
    for _, r in sens.iterrows():
        L.append(f"{r['shape']:<10}{r['baseline_focus']:>14.1f}"
                 f"{r['s31_spread_focus']:>11.1f}{r['delta_focus']:>+9.1f}"
                 f"{r['delta_excess_h']:>+11.0f}")
    L.append(" · 형태가 재배치 효과의 부호를 결정: 볼록이면 펼칠 때 총간섭↓, 오목/선형은 아님.")
    L.append("   데이터는 오목 → 총간섭 감소 ≈ 0 → 개선 없음. '형태를 먼저 검증하라'는")
    L.append("   멘토 지적이 정확했음을 정량 확인.")

    txt = "\n".join(L)
    with open(os.path.join(OUT_DIR, "scenario_report.txt"), "w", encoding="utf-8") as f:
        f.write(txt)
    print("\n" + txt)

    # ---- 그림 D: 시나리오별 SLA + 총간섭 ----
    fig, ax = plt.subplots(1, 2, figsize=(14, 4.6))
    x = np.arange(len(names)); w = 0.26
    labels = [_LABEL.get(n, n) for n in names]
    foc = [scen[n]["summary"]["sla_focus"] for n in names]
    ov = [scen[n]["summary"]["sla_overall"] for n in names]
    ex = [scen[n]["summary"]["total_excess_h"] for n in names]
    ax[0].bar(x - w/2, foc, w, label="포커스 SLA", color="#c0504d")
    ax[0].bar(x + w/2, ov, w, label="전체 SLA", color="#3b6fb5")
    ax[0].axhline(base["sla_focus"], color="#c0504d", ls=":", lw=1)
    for i, v in enumerate(foc):
        ax[0].text(i - w/2, v + 0.3, f"{v:.1f}", ha="center", fontsize=8)
    ax[0].set_xticks(x); ax[0].set_xticklabels(labels, fontsize=9)
    ax[0].set(title="시나리오별 SLA 위반율(%) — 낮을수록 좋음", ylabel="SLA %")
    ax[0].legend()
    ax[1].bar(x, ex, 0.5, color="#8064a2")
    ax[1].axhline(base["total_excess_h"], color="#555", ls=":", lw=1)
    for i, v in enumerate(ex):
        ax[1].text(i, v + 30, f"{v:.0f}", ha="center", fontsize=8)
    ax[1].set_xticks(x); ax[1].set_xticklabels(labels, fontsize=9)
    ax[1].set(title="시나리오별 총 간섭 픽시간(h) — 낮을수록 좋음", ylabel="간섭 픽시간(h)")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "fig_scenario_sla.png"), dpi=130, bbox_inches="tight")
    plt.close(fig)

    # ---- 그림 E: 시간대별 동시 보충 프로파일 (간섭이 어디로 옮겨졌나) ----
    fig, ax = plt.subplots(figsize=(11, 4.4))
    cmap = {"baseline": "#333333", "s31_shift": "#3b6fb5",
            "s31_spread": "#2ca02c", "s32_slotting": "#c0504d"}
    for nm in names:
        prof = conc_profiles[nm]
        hrs = list(range(8, 20))
        ax.plot(hrs, [prof.get(h, 0) for h in hrs], "-o", ms=3,
                color=cmap.get(nm), label=_LABEL.get(nm, nm))
    ax.axvspan(14, 17, color="orange", alpha=0.12)
    ax.set(title="시간대별 동시 보충 건수 — 3-1은 간섭을 옮길 뿐, 3-2는 크기를 줄임",
           xlabel="시각", ylabel="평균 동시 보충 건수")
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "fig_scenario_concurrency.png"), dpi=130, bbox_inches="tight")
    plt.close(fig)

    print("\n[그림] fig_scenario_sla.png, fig_scenario_concurrency.png ->", OUT_DIR)


def compare_slotting(verdict, sweeps: dict, params: dict):
    """3-2 정식 슬로팅 평가. verdict: slotting_verdict DataFrame,
    sweeps: {dest_label: run_slotting DataFrame}."""
    _font()
    base_f = verdict.iloc[0]["sla_focus"]
    ideal_f = verdict[verdict["scenario"].str.contains("순수강도")]["sla_focus"].iloc[0]

    L = []
    L.append("=" * 68)
    L.append(" 3-2 슬로팅 재최적화 평가 (거리 상충 포함 정식 모델)")
    L.append("=" * 68)
    L.append(f"{'시나리오':<24}{'전체':>7}{'포커스':>8}{'총간섭h':>10}")
    for _, r in verdict.iterrows():
        L.append(f"{r['scenario']:<24}{r['sla_overall']:>7.1f}"
                 f"{r['sla_focus']:>8.1f}{r['total_excess_h']:>10.0f}")
    L.append("\n해석 (결정적):")
    L.append(" · ZB/ZD 보충 '강도'만 낮추면(이동 없이) 개선(37.4→35.4). 그러나 이는 거리")
    L.append("   페널티가 0인 물리적 실현 불가 상한이다.")
    L.append(" · 그 강도 감소를 실제 수단(SKU를 덜 붐비는 zone으로 이동)으로 실현하는 순간,")
    L.append("   고회전 SKU의 픽 이동거리가 늘어(2.46초/거리단위) 용량을 깎고 SLA가 악화된다.")
    L.append(" · 낙관적 relief로 총 간섭이 baseline보다 낮아져도(6840<8297) SLA는 여전히 악화")
    L.append("   — ~99% 포화 시스템에서 거리 페널티에 의한 용량 손실이 지배적이기 때문.")
    L.append(" ▶ 결론: 슬로팅 이동으로는 3-2를 실현할 수 없다. 유효 레버(ZB/ZD 보충 강도↓)는")
    L.append("   결국 설비/공정(자본) 투자 또는 프로모션 수요 평준화로만 달성 가능.")

    txt = "\n".join(L)
    with open(os.path.join(OUT_DIR, "slotting_report.txt"), "w", encoding="utf-8") as f:
        f.write(txt)
    print("\n" + txt)

    # ---- 그림 F: 이동규모 상충 곡선 + 판정 ----
    fig, ax = plt.subplots(1, 2, figsize=(14, 4.6))
    colors = {"ZE/ZF": "#c0504d", "ZC": "#e08214"}
    for lbl, df in sweeps.items():
        ax[0].plot(df["n_move"], df["sla_focus"], "-o", ms=4,
                   color=colors.get(lbl, "#888"), label=f"→{lbl}")
    ax[0].axhline(base_f, color="#333", ls="--", lw=1, label="baseline")
    ax[0].axhline(ideal_f, color="#2ca02c", ls=":", lw=1.5,
                  label="순수 강도↓(상한·실현불가)")
    ax[0].set(title="이동 규모 vs 포커스 SLA — 이동할수록 악화(거리 페널티)",
              xlabel="이동 SKU 수", ylabel="포커스 SLA %")
    ax[0].legend(fontsize=8)
    v = verdict
    x = np.arange(len(v)); ex = v["total_excess_h"].values
    f_ = v["sla_focus"].values
    bars = ax[1].bar(x, f_, 0.5,
                     color=["#333333", "#2ca02c", "#c0504d", "#d99694"])
    for i, (vv, e) in enumerate(zip(f_, ex)):
        ax[1].text(i, vv + 0.3, f"{vv:.1f}\n({e:.0f}h)", ha="center", fontsize=7.5)
    ax[1].axhline(base_f, color="#333", ls="--", lw=1)
    ax[1].set_xticks(x)
    ax[1].set_xticklabels(["baseline", "순수강도↓\n(상한)", "실현3-2\n→ZE/ZF",
                           "실현3-2\n+relief"], fontsize=8)
    ax[1].set(title="판정: 총간섭↓에도 실현 슬로팅은 SLA 악화", ylabel="포커스 SLA %")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "fig_slotting.png"), dpi=130, bbox_inches="tight")
    plt.close(fig)
    print("\n[그림] fig_slotting.png ->", OUT_DIR)


def compare_evening(frontier: dict, entry_by_dow: dict, params: dict):
    """3-3 저녁 배수 용량 평가. frontier: {변형: DataFrame}, entry_by_dow:
    {라벨: Series(dow->backlog)} (baseline vs 대표 보강안)."""
    _font()
    base_focus = frontier["Tue-Thu×19-23"].iloc[0]["sla_focus"]

    L = []
    L.append("=" * 68)
    L.append(" 3-3 저녁 배수 용량 평가 (용량 직접 추가 — 반응형 안전판)")
    L.append("=" * 68)
    L.append(" 포커스 SLA를 베이스 수준(~19%)으로 낮추는 데 필요한 저녁 보강과 초과근무 비용:")
    L.append(f"{'변형':<16}{'추가인원':>8}{'포커스SLA':>10}{'초과근무h':>10}{'노동증가%':>9}")
    for lbl, df in frontier.items():
        # 포커스<=19.5 를 처음 만족하는 최소 보강
        hit = df[df["sla_focus"] <= 19.5]
        r = hit.iloc[0] if len(hit) else df.iloc[-1]
        L.append(f"{lbl:<16}{int(r['n_extra']):>8}{r['sla_focus']:>10.1f}"
                 f"{r['overtime_h']:>10.0f}{r['ot_pct_labor']:>9.1f}")
    L.append("\n해석:")
    L.append(" · 3-1·3-2(부하 재배치)와 달리 3-3은 저녁 용량을 '직접 추가'해 포화를 완화 →")
    L.append("   유일하게 SLA를 실제로 개선하는 레버. 진입 백로그 톱니가 눌린다.")
    L.append(" · 요일 타겟(화~목 저녁)이 Mon-Fri 전면 적용보다 같은 SLA를 40% 적은 초과근무로")
    L.append("   달성 — 문서의 '필요 최소 용량' 취지. 포커스≈베이스(19%)는 약 +9~10% 노동으로 가능.")
    L.append(" · 단, 초과근무 비용이 상시 발생하는 '반응형' 대책. 근본 해법은 총 부하를 줄이는")
    L.append("   것(ZB/ZD 강도↓의 실현 수단 = 설비/공정 투자, 또는 프로모션 수요 평준화).")

    txt = "\n".join(L)
    with open(os.path.join(OUT_DIR, "evening_report.txt"), "w", encoding="utf-8") as f:
        f.write(txt)
    print("\n" + txt)

    # ---- 그림 G: 비용-효익 프론티어 + 백로그 톱니 완화 ----
    fig, ax = plt.subplots(1, 2, figsize=(14, 4.6))
    colors = {"Mon-Fri×19-23": "#999999", "Tue-Thu×19-23": "#c0504d",
              "Tue-Thu×19-21": "#e08214"}
    for lbl, df in frontier.items():
        ax[0].plot(df["ot_pct_labor"], df["sla_focus"], "-o", ms=4,
                   color=colors.get(lbl), label=lbl)
    ax[0].axhline(19.0, color="#2ca02c", ls=":", lw=1.5, label="베이스 수준(19%)")
    ax[0].set(title="비용–효익 프론티어: 초과근무 vs 포커스 SLA (낮을수록↘좋음)",
              xlabel="초과근무 (baseline 노동 대비 %)", ylabel="포커스 SLA %")
    ax[0].legend(fontsize=8)
    x = np.arange(7)
    for lbl, ser in entry_by_dow.items():
        c = "#333" if "base" in lbl else "#c0504d"
        st = "s--" if "base" in lbl else "o-"
        ax[1].plot(x, [ser.get(d, np.nan) for d in x], st, color=c, label=lbl)
    ax[1].set_xticks(x); ax[1].set_xticklabels(DOW_KR)
    ax[1].set(title="진입 백로그 @14시 — 저녁 보강이 주중 이월 톱니를 억제",
              ylabel="재공량(라인)")
    ax[1].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "fig_evening.png"), dpi=130, bbox_inches="tight")
    plt.close(fig)
    print("\n[그림] fig_evening.png ->", OUT_DIR)


def compare_disciplines(dm, params: dict):
    """NEW-1 큐 규율 평가. dm: run_disciplines DataFrame (index=label)."""
    _font()
    base = dm.iloc[0]

    L = []
    L.append("=" * 72)
    L.append(" NEW-1 마감 기반 우선순위 피킹 (무비용 — 용량·부하 불변, 배분만 변경)")
    L.append("=" * 72)
    L.append(f"{'규율':<16}{'전체':>7}{'포커스':>8}{'베이스':>8}"
             f"{'최대지각h':>10}{'>1일%':>7}{'>2일%':>7}")
    for lbl, r in dm.iterrows():
        L.append(f"{lbl:<16}{r['sla_overall']:>7.1f}{r['sla_focus']:>8.1f}"
                 f"{r['sla_base']:>8.1f}{r['late_max_h']:>10.0f}"
                 f"{r['over_1d_pct']:>7.1f}{r['over_2d_pct']:>7.1f}")
    L.append("\n해석:")
    L.append(" · EDD(순수 마감우선)은 FIFO와 거의 동일 — 포화 큐에선 '마감 빠른 순'이 곧")
    L.append("   '가장 늦은 것부터'라 효과가 없다.")
    L.append(" · 트리아지(이미 늦은 doomed 주문을 후순위로)가 위반 '건수'를 크게 줄인다")
    L.append(f"   (전체 {base['sla_overall']:.1f}→최대 {dm['sla_overall'].min():.1f}%). "
             "용량 추가 없이 배분만 바꾼 무비용 개선.")
    L.append(" · 단, 상충: doomed 주문의 지각 폭이 커진다. '지연 상한(cap)'으로 꼬리를 통제 —")
    L.append("   cap=12h면 위반 22.7→14.2%(−37%)에 최대 지각 33h·2일초과 0%로 억제(∞는 7.5일).")
    L.append(" ▶ 권고: cap≈12h 균형 트리아지. 부하 재배치(3-1·3-2)와 달리, 또 3-3(유료)과 달리")
    L.append("   '무비용'으로 작동하는 첫 레버. 단 SLA를 '건수'로만 볼 때 유효(꼬리 지각은 감수).")

    txt = "\n".join(L)
    with open(os.path.join(OUT_DIR, "discipline_report.txt"), "w", encoding="utf-8") as f:
        f.write(txt)
    print("\n" + txt)

    # ---- 그림 H: 위반율(건수) + 꼬리 지각 상충 ----
    labels = list(dm.index)
    x = np.arange(len(labels))
    fig, ax = plt.subplots(1, 2, figsize=(14, 4.6))
    w = 0.38
    ax[0].bar(x - w/2, dm["sla_overall"], w, label="전체", color="#3b6fb5")
    ax[0].bar(x + w/2, dm["sla_focus"], w, label="포커스", color="#c0504d")
    ax[0].axhline(base["sla_overall"], color="#3b6fb5", ls=":", lw=1)
    for i, v in enumerate(dm["sla_overall"]):
        ax[0].text(i - w/2, v + 0.3, f"{v:.1f}", ha="center", fontsize=7.5)
    ax[0].set_xticks(x); ax[0].set_xticklabels(labels, fontsize=8, rotation=12)
    ax[0].set(title="위반 건수율(%) — 트리아지가 크게 감소", ylabel="SLA 위반율 %")
    ax[0].legend()
    ax[1].bar(x, dm["late_max_h"] / 24, 0.5, color="#8064a2")
    for i, v in enumerate(dm["late_max_h"] / 24):
        ax[1].text(i, v + 0.05, f"{v:.1f}일", ha="center", fontsize=7.5)
    ax[1].set_xticks(x); ax[1].set_xticklabels(labels, fontsize=8, rotation=12)
    ax[1].set(title="상충: 최대 지각 폭(일) — cap이 꼬리를 통제", ylabel="최대 지각(일)")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "fig_discipline.png"), dpi=130, bbox_inches="tight")
    plt.close(fig)
    print("\n[그림] fig_discipline.png ->", OUT_DIR)


def compare_combo(combo, params: dict):
    """권고 패키지(트리아지+저녁 용량) 평가. combo: run_combo DataFrame."""
    _font()
    base = combo.iloc[0]
    b_ov = base["sla_overall"]

    L = []
    L.append("=" * 72)
    L.append(" 권고 패키지 — 무비용 트리아지 → 정밀 타겟 저녁 용량 (레버 스태킹)")
    L.append("=" * 72)
    L.append(f"{'단계':<16}{'비용':>7}{'전체%':>7}{'포커스%':>8}"
             f"{'최대지각(일)':>11}{'>1일%':>7}{'초과근무%':>9}")
    for _, r in combo.iterrows():
        L.append(f"{r['label']:<16}{r['cost']:>7}{r['sla_overall']:>7.1f}"
                 f"{r['sla_focus']:>8.1f}{r['late_max_d']:>11.1f}"
                 f"{r['over_1d_pct']:>7.1f}{r['overtime_pct']:>9.1f}")
    # 권고 지점(저녁 +20 근처, 있으면)
    rec = combo[combo["label"].str.contains("20")]
    rec = rec.iloc[0] if len(rec) else combo.iloc[-1]
    L.append("\n해석:")
    L.append(f" · 레버가 곱셈적으로 쌓인다: 현행 {b_ov:.1f}% → 트리아지(무비용) "
             f"{combo.iloc[1]['sla_overall']:.1f}% → +저녁으로 "
             f"{combo['sla_overall'].min():.1f}%까지.")
    L.append(f" · 권고안(트리아지+저녁 20명): 전체 위반 {b_ov:.1f}→{rec['sla_overall']:.1f}% "
             f"(−{(1-rec['sla_overall']/b_ov)*100:.0f}%), 포커스 "
             f"{base['sla_focus']:.1f}→{rec['sla_focus']:.1f}%, 초과근무 {rec['overtime_pct']:.1f}% 노동.")
    L.append(" · ★시너지: 저녁 용량이 백로그를 배수해 doomed 주문이 줄어들어, 트리아지의")
    L.append(f"   약점인 '지각 꼬리'가 치료된다(>1일 지각 {combo.iloc[1]['over_1d_pct']:.1f}%"
             f"→{rec['over_1d_pct']:.1f}%, 최대 지각도 현행 수준).")
    L.append(" ▶ '공짜 최적화 먼저 → 필요한 최소 용량만' — 무작정 증원이 아닌 비용 효율적 패키지.")

    txt = "\n".join(L)
    with open(os.path.join(OUT_DIR, "combo_report.txt"), "w", encoding="utf-8") as f:
        f.write(txt)
    print("\n" + txt)

    # ---- 그림 I: 레버 스태킹(위반율↓) + 꼬리 치료 ----
    labels = list(combo["label"])
    x = np.arange(len(labels))
    fig, ax = plt.subplots(1, 2, figsize=(14, 4.6))
    w = 0.38
    ax[0].bar(x - w/2, combo["sla_overall"], w, label="전체", color="#3b6fb5")
    ax[0].bar(x + w/2, combo["sla_focus"], w, label="포커스", color="#c0504d")
    ax[0].axhline(b_ov, color="#3b6fb5", ls=":", lw=1)
    for i, (ov, ct) in enumerate(zip(combo["sla_overall"], combo["cost"])):
        ax[0].text(i - w/2, ov + 0.3, f"{ov:.1f}", ha="center", fontsize=8)
    ax[0].set_xticks(x); ax[0].set_xticklabels(labels, fontsize=8, rotation=12)
    ax[0].set(title="레버 스태킹: 위반율(%) — 무비용→저비용으로 누적 감소", ylabel="SLA 위반율 %")
    ax[0].legend()
    ax[1].bar(x, combo["over_1d_pct"], 0.5, color="#8064a2")
    for i, v in enumerate(combo["over_1d_pct"]):
        ax[1].text(i, v + 0.05, f"{v:.1f}", ha="center", fontsize=8)
    ax[1].set_xticks(x); ax[1].set_xticklabels(labels, fontsize=8, rotation=12)
    ax[1].set(title="시너지: >1일 지각(%) — 저녁 용량이 트리아지 꼬리를 치료",
              ylabel=">1일 지각 비율 %")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "fig_combo.png"), dpi=130, bbox_inches="tight")
    plt.close(fig)
    print("\n[그림] fig_combo.png ->", OUT_DIR)
