# -*- coding: utf-8 -*-
"""
run.py — 엔드투엔드 진입점

  1) 데이터 로드/캐시
  2) 캘리브레이션 (파라미터·간섭 비용함수)
  3) baseline 시뮬레이션
  4) 재현 검증 리포트 + 그림

사용:
  python run.py                # 캐시/파라미터 재사용
  python run.py --rebuild      # 캐시부터 재생성
  python run.py --recalib      # 파라미터 재계산
  python run.py --scenarios    # + 3-1/3-2/3-3/NEW-1 시나리오 비교
  python run.py --grid         # + 용량 정책 그리드(정적 vs 반응형) + 프론티어
"""
from __future__ import annotations
import os
import sys
import pickle

from config import OUT_DIR
import io_load
import calibrate
import policies
import engine
import evaluate
import scenarios
import grid


def load_params(recalib: bool, rebuild: bool):
    path = os.path.join(OUT_DIR, "calib_params.pkl")
    if recalib or rebuild or not os.path.exists(path):
        return calibrate.run_calibration(rebuild)
    with open(path, "rb") as f:
        return pickle.load(f)


def main():
    rebuild = "--rebuild" in sys.argv
    recalib = "--recalib" in sys.argv

    lines = io_load.load_lines(rebuild)
    repl = io_load.load_repl(rebuild)
    params = load_params(recalib, rebuild)

    sched = policies.baseline(repl)
    res = engine.simulate(lines, params, sched, step_min=10)
    summ = engine.summarize(res)

    print("\n=== baseline 시뮬 요약 ===")
    print(f"  전체 SLA : {summ['sla_overall']:.1f}%   (목표 23.0)")
    print(f"  포커스    : {summ['sla_focus']:.1f}%   (목표 37.4)")
    print(f"  베이스    : {summ['sla_base']:.1f}%   (목표 19.0)")
    print(f"  미완료    : {summ['n_incomplete']:,} / {summ['n']:,}")

    evaluate.baseline_report(lines, res, summ, params)

    if "--scenarios" in sys.argv:
        # 3-1 (보충 시점 재분배) + 형태 민감도
        scen = scenarios.run_policies(lines, repl, params)
        conc = {nm: scenarios.concurrency_by_hour(scen[nm]["sched"]) for nm in scen}
        sens = scenarios.cost_shape_sensitivity(lines, repl, params)
        evaluate.compare_scenarios(scen, sens, conc, params)

        # 3-2 (슬로팅 재최적화, 거리 상충 포함)
        master = io_load.load_master()
        verdict = scenarios.slotting_verdict(lines, repl, master, params)
        sweeps = {
            "ZE/ZF": scenarios.run_slotting(lines, repl, master, params,
                                            dest_zones=("ZE", "ZF")),
            "ZC": scenarios.run_slotting(lines, repl, master, params,
                                         dest_zones=("ZC",)),
        }
        evaluate.compare_slotting(verdict, sweeps, params)

        # 3-3 (저녁 배수 용량, 용량 직접 추가)
        frontier = scenarios.evening_frontier(lines, repl, params)
        eb = {"baseline": scenarios._entry_backlog_by_dow(
                  engine.simulate(lines, params, policies.baseline(repl))),
              "Tue-Thu +20명": scenarios._entry_backlog_by_dow(
                  engine.simulate(lines, params, policies.baseline(repl),
                                  worker_boost=scenarios.evening_boost_spec(
                                      20, dows=(1, 2, 3))))}
        evaluate.compare_evening(frontier, eb, params)

        # NEW-1 (마감 기반 우선순위 피킹 — 무비용 큐 규율)
        dm = scenarios.run_disciplines(lines, repl, params)
        evaluate.compare_disciplines(dm, params)

        # 권고 패키지 (트리아지 + 저녁 용량 스태킹)
        combo = scenarios.run_combo(lines, repl, params)
        evaluate.compare_combo(combo, params)

    if "--grid" in sys.argv:
        # NEW-2 용량 정책 그리드 — 정적(요일 고정) vs 반응형(임계 기반 조건부 호출).
        # 당일 제약(예산·인원 상한)은 grid.query()/report() 로 조회만 하면 된다.
        g = grid.build_grid(lines, repl, params)
        print(grid.report(g))
        print("\n프론티어 그림:", grid.plot_frontier(g))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
