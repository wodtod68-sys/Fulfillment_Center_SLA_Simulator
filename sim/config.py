# -*- coding: utf-8 -*-
"""
config.py — 시뮬레이션 파이프라인 전역 설정

모든 경로·운영 상수·재현 목표를 한곳에 모은다.
다른 모듈은 여기서만 상수를 읽는다(하드코딩 금지).
"""
from __future__ import annotations
import os

# ------------------------------------------------------------
# 경로
# ------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # C:\coding\haksuljae
SIM_DIR = os.path.join(ROOT, "sim")
CACHE_DIR = os.path.join(SIM_DIR, "cache")     # 파싱된 DataFrame 피클 캐시
OUT_DIR = os.path.join(SIM_DIR, "outputs")     # 그림·리포트·산출물

DATA = {
    "order":     os.path.join(ROOT, "order_line_history.csv"),
    "work":      os.path.join(ROOT, "work_event_log.csv"),
    "repl":      os.path.join(ROOT, "replenishment_log.csv"),
    "inventory": os.path.join(ROOT, "inventory_snapshot.csv"),
    "master":    os.path.join(ROOT, "master_data.csv"),
    "calendar":  os.path.join(ROOT, "calendar_weather.csv"),
}

os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)

# ------------------------------------------------------------
# 운영 상수 (현장 설명 + EDA에서 확정)
# ------------------------------------------------------------
ZONES = ["ZA", "ZB", "ZC", "ZD", "ZE", "ZF"]
N_FORKLIFTS = 6                 # F01~F06
OPEN_HOUR = 8                   # 08:00 영업 시작
CLOSE_HOUR = 20                 # 20:00 영업 종료(이후 잔여 작업 존재)
PERIOD_START = "2025-01-01"
PERIOD_END = "2025-06-30"

# 문제(포커스) 구간 정의: 화·수·목(요일 1,2,3) 14~17시(hour 14,15,16,17)
FOCUS_DOWS = (1, 2, 3)
FOCUS_HOURS = (14, 15, 16, 17)

DOW_KR = ["월", "화", "수", "목", "금", "토", "일"]

# ------------------------------------------------------------
# 재현 목표 (제출물 2장 — 시뮬 baseline이 맞춰야 할 값)
# ------------------------------------------------------------
TARGETS = {
    "sla_overall": 23.0,   # 전체 지연율(%)
    "sla_focus":   37.4,   # 포커스 구간 지연율(%)
    "sla_base":    19.0,   # 그 외 구간 지연율(%)
    # 요일별 14시 진입 백로그 평균 (근거 12)
    "entry_backlog_by_dow": {0: 95, 1: 412, 2: 934, 3: 1106, 4: 1077, 5: 437, 6: 66},
    # 오후 처리량 저하 (근거 10): 오전 ~18,400 → 오후 ~14,750 (약 20%)
    "throughput_drop_pct": 20.0,
}
