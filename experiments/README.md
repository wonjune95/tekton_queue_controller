# experiments — 학위논문 실험 하니스

학위논문 「쿠버네티스 환경에서 Tekton 파이프라인의 클러스터 자원 안정성을 위한 동적 우선순위 큐 컨트롤러의 설계 및 구현」의
실험을 수행한 부하 발생기·시나리오 스크립트·집계 스크립트다. **회차별 원자료(`results*/`)는 포함하지 않는다.**

| 문서 | 내용 |
|---|---|
| `GKE_SETUP.md` | GKE 클러스터·배스천·Tekton·Gitea·Harbor·Prometheus 구성 |
| `COMPARISON_SETUP.md` | 비교군(tekton-kueue·Volcano) 설치와 상호 배제 |
| `APPENDIX.md` | 조건·회차 인벤토리와 보고 범위 |

| 디렉터리 | 역할 |
|---|---|
| `common/` | 부하 발생기(`pr_create.py`, 푸아송·버스트 도착), 지표 수집(`metrics_collect.py`·`resource_collect.py`), 그림 생성(`make_figures.py`), 정리(`cleanup.sh`) |
| `pipeline/` | 실험 파이프라인 정의(Spring PetClinic 빌드, 요청량 유무·Volcano 변형) |
| `s0_baseline/` `s1_peak_hour/` `s2_release_burst/` `s3_priority/` | 네 시나리오 |
| `a0r_*/` `a0nr_*/` `a2_no_aging/` `a3_single_fifo/` | 에이블레이션·요인 실험 |
| `lmax_sweep/` `ta_sweep/` | 파라미터 스윕 |
| `cmp_tekton_kueue/` `cmp_volcano/` | 비교군 |
| `a1p_parallel_admission/` `e1_final_parallel/` | 동시 도착 조건 (D-W / D-L) |
| `e2_tier0_urgent/` `e3_leader_failover/` | Tier 0 우회·리더 전환 탐침 |
| `d1_pending_template/` `d2_exclude_label/` | 보류 필드 이중 용도 재현 관측 |
| `param_measure/` | 단계별 워킹셋 피크 실측 |
| `run_all.sh` | 시나리오 일괄 실행 (파괴적 조건은 `ALLOW_DESTRUCTIVE=1` 필요) |

실행 순서와 주의 사항은 `GKE_SETUP.md` 를 먼저 읽는다. 파괴적 조건(A0-NR)은 노드 자원 고갈을 유발하므로 마지막에 수행한다.
