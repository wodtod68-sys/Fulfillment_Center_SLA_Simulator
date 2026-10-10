# Fulfillment Center SLA Simulator

B2C 이커머스 풀필먼트센터의 **정시 출고(SLA) 위반 문제**를 이산시간 queueing simulation으로 재현하고,
개선안의 효과를 실데이터 위에서 **정량 검증**하는 프로젝트입니다.

아이디어가 "그럴듯하다"에서 멈추지 않고, 실제로 효과가 있는지 혹은 역효과인지를 시뮬레이션으로 판정하는 것이 목표입니다.

---

## 문제 정의

- **현장:** 수도권 B2C 풀필먼트센터 (연면적 약 15,000평, 6개 Zone ZA-ZF, SKU 500종, 작업자 약 45명, 지게차 6대, 08:00-20:00 운영)
- **데이터 기간:** 2025-01-01 ~ 2025-06-30 (주문라인 약 39.7만 건, 보충 약 2.2만 건)
- **증상:** 화~목 14~17시 인입 주문의 SLA 위반율이 나머지 구간의 약 2배

| 구간 | SLA 위반율 |
|---|---|
| 포커스 (화~목 14~17시) | **37.4%** |
| 베이스 (그 외) | 19.0% |
| 전체 | 23.0% |

**진단한 인과 사슬:**
프로모션 화~목 집중 → 보충작업 오후 배치 집중 → 지게차·통로 간섭 → 오후 피킹 처리량 약 20% 저하 → 백로그 누적 → 주말에만 배수되어 주중 이월 → SLA 위반

---

## 모델 개요

인과 사슬 전체를 **하나의 공유 FIFO queue**로 축약합니다.

- **Arrival:** 주문라인이 `allocated_time`에 pick queue 진입 (실데이터 그대로)
- **Server:** 시간대별 활성 작업자 `W(t)`가 라인당 `s_eff`만큼의 작업자-초를 소비
- **Service time:** `s_eff(c) = base_service + [pick(c) − pick(0)]`
  - `pick(c)`: 해당 zone에 동시 보충이 `c`건일 때의 pick 서비스시간 (데이터에서 측정한 간섭 cost function)
- 자유 파라미터는 `base_service` 하나뿐이며 (전체 SLA 23%를 재현하도록 보정, 546초/line), 나머지는 모두 데이터에서 직접 추출합니다.

시스템이 약 99% utilization(포화 근처)이기 때문에, 오후 간섭이 처리량을 조금만 깎아도 백로그가 기계적으로 누적됩니다.

### 간섭 cost function — 오목(포화) 형태

| 동시 보충 c | 0 | 1–5 | 6–20 | 21–50 | 50+ |
|---|---|---|---|---|---|
| pick(c) [초] | 336 | 525 | 617 | 641 | 644 |
| 한계 증가분 | — | **+189** | +92 | +24 | +3 |

첫 계단(0 → 1~5건)이 전체 팽창의 **62%**를 차지합니다. 즉 "보충이 몇 건이냐"보다 "있느냐 없느냐"가 중요하며, 이 형태가 개선안 판정의 핵심 근거가 됩니다.

### Baseline 재현 검증

| 지표 | 시뮬레이션 | 실측 |
|---|---|---|
| SLA 전체 / 포커스 / 베이스 | 22.7 / 37.4 / 18.6 % | 23.0 / 37.4 / 19.0 % |
| 14시 진입 백로그 (수요일) | 925 | 934 |

캘리브레이션에 넣지 않은 **주간 백로그 톱니 패턴**(화→금 누적, 주말 배수)과 **금요일 종일 지연**이 창발적으로 재현되어 메커니즘을 독립적으로 검증합니다.

---

## 주요 결과

| 개선안 | 유형 | 결과 |
|---|---|---|
| 3-1 보충 시점 재분배 | 부하 재배치 | ✗ 무효/역효과 (오목 cost function → 총 간섭 보존) |
| 3-2 슬로팅 이동 | 부하 재배치 | ✗ 역효과 (픽 이동거리 페널티가 간섭 이득을 압도) |
| 3-3 저녁 배수 용량 | 용량 추가 | ○ 효과 (유료, 노동 +9~10%) |
| **NEW-1 마감 기반 triage** | 용량 재배분 | ○ **무비용**, 위반 −37% (cap≈12h) |
| **NEW-2 반응형 조건부 호출** | 용량 타이밍 | ◎ 3-3 대비 동일 SLA를 약 1/4 비용으로 |

**권고안: Triage + 반응형 저녁 인원 호출**

| 정책 | 전체 위반 | 포커스 | 추가 노동 |
|---|---|---|---|
| 현행 FIFO | 22.7% | 37.4% | — |
| 정적 (월~금 +30명) + triage | 4.8% | 11.8% | +23.5% |
| **반응형 (17시 판단, θ=1000, +30명) + triage** | **4.8%** | **11.8%** | **+5.6%** |

146개 정책 조합의 비용-효과 Pareto frontier가 전부 반응형 정책으로 구성됩니다.
핵심 교훈은 약 99% 포화 시스템에서 부하를 시간·공간으로 옮기는 것은 효과가 없고, 용량의 **양·배분·타이밍**을 바꿔야 한다는 점입니다.

시나리오별 상세 수치와 해석은 [`sim/README.md`](sim/README.md)를 참고하세요.

---

## 레포 구조

```
.
├── 1.EDA.ipynb                          # 탐색적 데이터 분석
├── 물류센터_SLA_개선_시뮬레이션.ipynb     # 시뮬레이션 전체 흐름 정리 (결과 해석 포함)
├── 중간_제출물_7조.docx                  # 중간 제출 보고서
├── 중간_제출물_7팀_피드백_*.docx          # 멘토 피드백
└── sim/
    ├── config.py      # 경로, 운영 상수, 재현 목표값
    ├── io_load.py     # CSV 로드·파싱·캐시
    ├── calibrate.py   # 간섭 cost function, base_service, 작업자 수 W 등 파라미터 추정
    ├── engine.py      # 이산시간 FIFO queue 시뮬레이터 (discipline / worker_boost / reactive 지원)
    ├── policies.py    # 보충 스케줄 정책 (baseline, 3-1, 3-2 프록시)
    ├── scenarios.py   # 시나리오 실행 (3-1, 3-2, 3-3, NEW-1, 조합)
    ├── grid.py        # 용량 정책 grid search + 제약 조건 조회
    ├── evaluate.py    # 리포트·그림 생성
    ├── run.py         # 엔드투엔드 진입점
    └── README.md      # 시뮬레이터 상세 문서
```

---

## 실행 방법

### 1. 환경 설정

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install pandas numpy scipy matplotlib
pip install seaborn jupyter   # 노트북 실행 시
```

### 2. 데이터 준비

원본 데이터는 레포에 포함되어 있지 않습니다. 아래 CSV 파일을 **레포 루트**에 두어야 합니다 (경로는 `sim/config.py`의 `DATA`에서 지정).

| 파일 | 내용 |
|---|---|
| `order_line_history.csv` | 주문라인 이력 |
| `work_event_log.csv` | 작업 이벤트 로그 |
| `replenishment_log.csv` | 보충 작업 로그 |
| `inventory_snapshot.csv` | 시점별 재고 + 공간 정보 |
| `master_data.csv` | SKU 마스터 |
| `calendar_weather.csv` | 캘린더·날씨 |

### 3. 시뮬레이션 실행

```bash
cd sim
python run.py                # baseline 재현 + 검증 리포트
python run.py --scenarios    # + 3-1 / 3-2 / 3-3 / NEW-1 / 조합 시나리오 비교
python run.py --grid         # + 용량 정책 grid (정적 vs 반응형) + Pareto frontier
python run.py --recalib      # 파라미터 재계산
python run.py --rebuild      # CSV 캐시부터 재생성
```

산출물(리포트, 그림, `calib_params.pkl`)은 `sim/outputs/`, 파싱 캐시는 `sim/cache/`에 생성됩니다.

### 4. 제약 조건으로 최선 정책 조회

grid를 미리 돌려두면, 예산·인원 제약만 바꿔서 실행 가능한 최선안을 바로 조회할 수 있습니다.

```python
import grid
df = grid.load_grid()                                  # outputs/policy_grid.csv
c = {"max_overtime_pct": 6, "max_extra_workers": 20}   # 제약 조건
print(grid.report(df, c))                              # 실행 가능 최선안 + frontier
grid.plot_frontier(df, c)
```

사용 가능한 제약 인자: `max_overtime_pct`, `max_extra_workers`, `max_call_days`, `max_late_h`

### 노트북

`물류센터_SLA_개선_시뮬레이션.ipynb`는 `sim/` 모듈을 import해 전체 분석 흐름을 재현합니다. 첫 셀의 `SIM` 경로를 본인 환경의 `sim/` 폴더 경로로 수정한 뒤 실행하세요.

---

## 한계 및 향후 과제

- `s32_slotting`은 ZB·ZD 소요만 단축하는 방향성 proxy이며, 정식 3-2 모델(`scenarios.apply_slotting`)은 픽 이동거리 상충을 별도로 반영합니다.
- 반응형 호출은 현실적으로 대기수당·잔업·일용직 풀로 구현되므로, 실제 절감폭은 대기 비용만큼 줄어듭니다.
- 향후: 근본 레버(ZB/ZD 설비 증설, 프로모션 평준화)의 자본비용 대비 효과 정량화, 백로그 정의의 결과지표 누수 및 cost function의 시간대 교란 통제 보강.
