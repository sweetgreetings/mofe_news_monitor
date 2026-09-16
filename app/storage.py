# Design Ref: DESIGN.md §3 기술 선택 — SQLite 대신 로컬 JSON 파일로 회차별 기사 데이터를 저장/조회하는 모듈
import json
import logging
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import ARTICLES_DIR, RETENTION_DAYS
from app.run_index import find_path as _index_find_path
from app.run_index import list_run_meta, load_entries

logger = logging.getLogger(__name__)


def _articles_dir_for_cleanup() -> Path:
    """ARTICLES_DIR을 함수 호출 시점에 읽는다 — 통합 테스트가 이 모듈의 이름을 임시 폴더로
    바꿔치기하므로(patch.object(storage, "ARTICLES_DIR", ...)) 상수를 미리 붙들면 안 된다."""
    return ARTICLES_DIR


def _run_file_path(date_str: str, run_slot: str) -> Path:
    # Windows 파일명에는 ':'를 쓸 수 없어 "-"로 바꿔 저장한다 (예: "09:00" -> "09-00")
    safe_slot = run_slot.replace(":", "-")
    return ARTICLES_DIR / f"{date_str}_{safe_slot}.json"


def _date_from_run_at(run_at_value: str, now: datetime) -> str:
    """run_at(ISO 8601)에서 파일명에 쓸 "YYYY-MM-DD"를 뽑는다.

    형식이 깨졌으면 지금 시각으로 물러선다 — 회차를 저장하지 못하는 것보다
    날짜가 하루 어긋난 채로라도 저장되는 게 낫다(기사를 잃지 않는다). 대신 조용히
    넘어가지 않고 경고를 남긴다.
    """
    try:
        return datetime.fromisoformat(run_at_value).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        fallback = now.strftime("%Y-%m-%d")
        logger.warning(
            "run_at 형식을 읽지 못해 파일명 날짜를 현재 시각(%s)으로 대체합니다 — run_at=%r",
            fallback, run_at_value,
        )
        return fallback


def save_run(
    run_slot: str,
    articles: list[dict],
    run_at: Optional[str] = None,
    confirmed: bool = False,
    classification_degraded: bool = False,
    finalize_new_subheadings: Optional[list[str]] = None,
) -> Path:
    """
    회차 하나의 기사 목록을 JSON 파일로 저장한다.

    articles의 각 항목은 다음 필드를 갖는다 (PRD.md 기능1 규칙 5·6):
      - outlet: 언론사명
      - title: 기사 제목
      - url: 기사 URL
      - summary: 네이버 API description 원문 (요약)

    run_slot: 원래 예정돼 있던 회차 시각, 예: "09:00" (헤더 "언론 모니터링 09:00 기준"에 쓰임)
    run_at: 실제로 스크랩이 실행된 시각 (놓친 회차를 나중에 보충 실행한 경우 run_slot과 달라질 수 있음)

    [수정: 2026-08-26] **파일명 날짜는 run_at에서 나온다** — 저장 시각(now)이 아니다.
    그래서 `{run_at의 날짜}_{run_slot}.json`이 항상 성립하고, 파일명만 봐도 그 회차가
    어느 날 것인지 알 수 있다. 자정을 넘겨 저장돼도 어긋나지 않는다.
    (조회 함수들은 원래부터 run_at을 기준으로 봤으므로 화면 동작은 그대로다. 다만
    2026-08-26 이전에 저장된 옛 파일은 어긋나 있을 수 있어, 조회는 계속 파일명이 아니라
    run_at/인덱스를 본다 — _find_run_file·app.run_index.find_path 참고.)

    [추가: 2026-08-10] confirmed — 확정·전송 2단계 유예(app.confirm_send) 도입으로 회차가
    수집되자마자 곧바로 완성본이 되지 않는다. 기본값 False로 저장해 "확정 대기" 상태로
    시작하고, app.confirm_send.confirm_run()이 확정 시 True로 바꿔 쓴다. confirmed_at
    (확정된 시각, 전송 유예의 기준)과 send_count(전송 횟수, 2회째부터 "(수정)" 접두어
    판단 기준)는 처음엔 없다가 확정·전송 시 각각 채워진다 — 이 함수가 직접 채우지 않는
    이유는 새로 수집된 회차는 항상 미확정 상태로 시작해야 하기 때문이다.

    classification_degraded: [추가: 2026-08-12] 이 회차의 소제목 분류가 LLM 실패로
    규칙 기반(단어 빈도)으로 떨어졌는지(app.llm_classifier.llm_attempt_failed_without_fallback).
    True면 확정본이 자동발송을 보류하고 경고를 보여준다(app.confirm_send, app.renderer).

    finalize_new_subheadings: [추가: 2026-08-20] 초안 마감 시점(app.classifier.
    classify_for_finalize)에 새로 생긴 소제목 이름 목록 — 확정본이 해당 소제목 <h2>에
    "마감 시 신규" 배지를 붙이는 근거다. 기사별 "마감 후 자동 배정" 표시(finalize_
    auto_assigned)는 articles의 각 항목에 이미 담겨 오므로 여기서 따로 받지 않는다.
    """
    now = datetime.now()
    run_at_value = run_at or now.isoformat(timespec="seconds")
    # [수정: 2026-08-26] 파일명 날짜를 저장 시각(now)이 아니라 **회차의 실제 실행
    # 시각(run_at)**에서 가져온다. 예전엔 now()를 썼는데, 자정을 넘겨 저장된 회차가
    # 엉뚱한 날짜 파일에 꽂혔다(실측: run_at이 08-25 19:33인 회차가
    # 2026-08-26_19-30.json으로 저장됨). 조회 쪽은 원래부터 run_at을 기준으로 봐서
    # (list_run_dates/list_runs_for_date/delete_expired_runs/find_path) 화면은 멀쩡했지만,
    # 파일명 자체가 키라서 두 가지가 깨졌다:
    #   1) run_exists(run_slot)이 파일명으로 판단한다 — 어제 회차가 오늘 이름으로 있으면
    #      "오늘 이 회차는 이미 돌았다"고 오판해 **오늘 그 회차를 통째로 건너뛴다**.
    #   2) 같은 이름이 다시 필요해지면 그대로 덮어쓴다 — 앞 회차 기사가 조용히 사라진다.
    # run_at에서 날짜를 뽑으면 둘 다 구조적으로 사라진다(파일명 = 그 회차의 날짜).
    date_str = _date_from_run_at(run_at_value, now)
    run_data = {
        "run_slot": run_slot,
        "run_at": run_at_value,
        "articles": articles,
        "confirmed": confirmed,
        "confirmed_at": None,
        "send_count": 0,
        "classification_degraded": classification_degraded,
        "finalize_new_subheadings": list(finalize_new_subheadings or []),
    }
    path = _run_file_path(date_str, run_slot)
    path.write_text(json.dumps(run_data, ensure_ascii=False, indent=2), encoding="utf-8")
    # [추가: 2026-08-26] 저장 사실을 파일 로그에 남긴다 — "그 회차가 언제 어디에 몇 건으로
    # 저장됐나"를 사후에 확인할 방법이 지금까지 없었다(app/logging_setup.py 참고).
    logger.info("회차 저장 — %s (run_slot=%s, run_at=%s, 기사 %d건, confirmed=%s)",
                path.name, run_slot, run_data["run_at"], len(articles), confirmed)
    return path


def load_run_file(path: Path) -> Optional[dict]:
    """회차 파일 하나를 방어적으로 읽는다 (깨졌으면 None).

    app.run_index가 "읽을 수 있는 파일"만 골라 주지만, 인덱스를 만든 뒤 파일이 지워지거나
    바뀌었을 수 있다 — 인덱스는 캐시일 뿐이므로 실제로 읽는 쪽에서 한 번 더 방어한다.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        return None


def load_latest_confirmed_run() -> Optional[dict]:
    """확정된(옛 데이터처럼 confirmed 필드 자체가 없는 것도 포함) 회차 중 가장 최근 것을
    돌려준다 — 완성본(확정본) 화면이 항상 보여줘야 할 회차다.

    [추가: 2026-08-10] 확정·전송 2단계 유예 도입 이후, load_latest_run()은 "가장 최근에
    수집된" 회차를 돌려주는데 그게 아직 확정 대기(confirmed=False) 상태일 수 있다 —
    그 경우 완성본은 그 이전에 확정된 회차를 계속 보여줘야 한다(app.renderer.generate_screen).
    이 함수가 없으면 main.py 시작 시 "가장 최근 회차가 미확정"이라는 이유만으로 회차가
    하나도 없는 것처럼 안내 화면(🐰⏱️)을 덮어씌우는 사고가 난다 — 실제로 한 번 발생해
    추가했다.
    """
    # [수정: 2026-08-18] 확정 여부·run_at은 app.run_index가 캐시해 두므로 전체 회차를
    # 파싱하지 않는다 — 실제로 여는 파일은 "가장 최근 확정 회차" 하나뿐이다.
    for meta in list_run_meta():  # 이미 run_at 내림차순
        if meta["confirmed"]:
            return load_run_file(meta["path"])
    return None


def load_latest_run() -> Optional[dict]:
    """가장 최근에 저장된 회차 데이터를 읽어온다. 저장된 회차가 하나도 없으면 None.

    파일명이 아니라 저장된 run_at 값으로 비교한다. 자정을 넘겨 놓친 회차를
    보충 실행하면 파일명 날짜(now())와 run_slot이 다른 날짜가 될 수 있어,
    파일명 정렬만으로는 실제로 가장 최근인 회차를 못 찾을 수 있기 때문이다.
    """
    # [수정: 2026-08-18] 위와 같은 이유로 인덱스에서 최신 하나만 짚어 그 파일만 읽는다.
    for meta in list_run_meta():  # 이미 run_at 내림차순
        return load_run_file(meta["path"])
    return None


def is_today(run: dict, now: Optional[datetime] = None) -> bool:
    """이 회차가 오늘(로컬 날짜) 저장됐는지 확인한다.

    [추가: 2026-07-26] 자정이 지나 어제 회차만 남아있는데도 load_latest_run()은 여전히
    그 회차를 "가장 최근"으로 돌려주므로, 화면에 그대로 쓰기 전에 이 함수로 한 번 더
    걸러야 한다(app.renderer.generate_screen 참고). now는 테스트에서 주입할 수 있다.
    """
    now = now or datetime.now()
    return run["run_at"][:10] == now.strftime("%Y-%m-%d")


def list_all_runs() -> list[dict]:
    """저장된 모든 회차를 최신순(run_at 내림차순)으로 돌려준다 (PRD.md 기능2 규칙 6, 지난 기사 조회용).

    RETENTION_DAYS(현재 1년)치까지만 남아있다 — delete_expired_runs가 그보다 오래된 파일을 지운다.
    깨진 JSON이거나 run_at·run_slot·articles 중 하나라도 없는 파일은 건너뛴다 (화면을
    그리는 쪽에서 이 3개 키를 그대로 쓰므로, 여기서 미리 검증해 손상 파일 하나 때문에
    전체 조회가 멈추지 않도록 한다 — delete_expired_runs와 같은 방어 철학).
    """
    # [수정: 2026-08-18] 유효성 검증과 정렬은 app.run_index가 이미 해둔다 — 여기선
    # 그 목록대로 파일을 읽기만 한다. 다만 **모든 회차의 기사를 다 읽는 함수라는 점은
    # 그대로**다(보관 1년이면 22.8MB). 회차 전체의 기사가 정말로 필요한 게 아니라면
    # list_run_meta()/list_runs_for_date()를 쓴다.
    runs = []
    for meta in list_run_meta():
        run = load_run_file(meta["path"])
        if run is not None:
            runs.append(run)
    return runs


def list_run_dates(metas: Optional[list] = None) -> list[str]:
    """저장된 회차의 날짜(run_at 기준 "YYYY-MM-DD")를 최신순·중복 없이 돌려준다.

    [추가: 2026-08-18] 지난 기사 화면의 날짜 목록용 — 파일을 한 개도 열지 않는다.
    파일명의 날짜를 쓰지 않는 이유는 save_run 설명 참고(파일명=저장 시각, run_at=실행 시각).
    """
    seen = []
    for meta in (list_run_meta() if metas is None else metas):  # 이미 run_at 내림차순
        date_str = meta["run_at"][:10]
        if date_str not in seen:
            seen.append(date_str)
    return seen


def list_runs_for_date(date_str: str, metas: Optional[list] = None) -> list[dict]:
    """그 날짜(run_at 기준)에 저장된 회차만 최신순으로 읽어 돌려준다.

    [추가: 2026-08-18] 하루치만 필요한 자리(지난 기사 지연 로딩, 확정 여부 확인)가
    list_all_runs()로 전체를 파싱하고 있었다 — 인덱스로 날짜를 먼저 걸러 그 날 파일만 연다.

    metas: 이미 뽑아둔 list_run_meta() 결과. 한 화면을 그리며 여러 날짜를 연달아 읽을 땐
    이걸 넘겨 인덱스 조회(glob+stat, 1,460개 기준 실측 4ms)를 한 번으로 줄인다.
    """
    runs = []
    for meta in (list_run_meta() if metas is None else metas):
        if meta["run_at"][:10] != date_str:
            continue
        run = load_run_file(meta["path"])
        if run is not None:
            runs.append(run)
    return runs


def load_today_runs(date_str: Optional[str] = None) -> list[dict]:
    """오늘(또는 지정한 날짜) 저장된 모든 회차를 시간순으로 돌려준다 (진입 화면 워드클라우드 당일 누적용).

    파일명이 아니라 run_at의 날짜로 판단한다 (자정 넘겨 보충 실행해도 정확히 걸러진다).

    [수정: 2026-08-18] 예전엔 전 회차를 파싱한 뒤 오늘 것만 골라냈다 — 진입 화면(home.html)이
    요청마다 부르는 자리라, 보관 기간을 1년으로 늘리면 워드클라우드 한 번에 22.8MB를 읽게 된다.
    list_runs_for_date(인덱스로 날짜를 먼저 거름)를 쓰고 시간 오름차순으로만 다시 세운다.
    """
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    return sorted(list_runs_for_date(date_str), key=lambda run: run["run_at"])


def _find_run_file(run_at: str, run_slot: str) -> Optional[Path]:
    """(run_at, run_slot)과 내용이 일치하는 파일을 찾는다 — 파일명의 날짜(now())와 run_at의
    날짜가 다를 수 있어(위 save_run 설명 참고), 파일명만으로는 정확히 짚어낼 수 없다."""
    # [수정: 2026-08-18] 먼저 인덱스(app.run_index)에서 찾는다 — 큐레이션 동작마다
    # (숨기기·순서변경·확정·발송) 불리는 자리라 전수 스캔이 그대로 클릭 지연이 된다.
    # 인덱스는 캐시일 뿐이므로, 못 찾으면 예전처럼 전부 열어보는 경로로 떨어진다.
    path = _index_find_path(run_at, run_slot)
    if path is not None:
        return path
    for path in ARTICLES_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError):
            continue
        if data.get("run_at") == run_at and data.get("run_slot") == run_slot:
            return path
    return None


def update_run_articles(run: dict, articles: list[dict]) -> bool:
    """이미 저장된 회차의 기사 목록만 바꿔 같은 파일에 덮어쓴다 (기사 순서 수동 조정용).

    새 회차를 만드는 save_run과 달리, run_slot·run_at은 그대로 두고 articles만 교체한다.
    해당 회차 파일을 못 찾으면(예: 그 사이 보관 기간이 지나 삭제됨) 아무 일도 하지 않고
    False를 돌려준다.
    """
    path = _find_run_file(run["run_at"], run["run_slot"])
    if path is None:
        return False
    updated = {**run, "articles": articles}
    atomic_write_text(path, json.dumps(updated, ensure_ascii=False, indent=2))
    return True


def confirm_run(run: dict, confirmed_at: Optional[str] = None) -> bool:
    """회차를 확정 처리한다(confirmed=True, confirmed_at 기록) — app.confirm_send가
    (확정) 버튼과 자동 확정 타임아웃 양쪽에서 호출한다. 파일을 못 찾으면 False."""
    path = _find_run_file(run["run_at"], run["run_slot"])
    if path is None:
        return False
    updated = {**run, "confirmed": True, "confirmed_at": confirmed_at or datetime.now().isoformat(timespec="seconds")}
    atomic_write_text(path, json.dumps(updated, ensure_ascii=False, indent=2))
    return True


def record_send(run: dict, auto: bool = False) -> Optional[dict]:
    """이 회차의 전송 횟수(send_count)를 1 늘려 저장한다 — app.confirm_send가 (전송)
    버튼과 자동 전송 타임아웃 양쪽에서, 실제 전송 전에 호출해 몇 번째 전송인지(2회째부터
    "(수정)" 접두어) 먼저 확정한다. 갱신된 run을 돌려주고, 파일을 못 찾으면 None.

    [추가: 2026-08-10] auto — 이 전송이 담당자가 (전송) 버튼을 직접 눌러 일어난 것인지,
    담당자가 시간 안에 반응하지 않아 시스템이 대신 처리한 것인지 구분해 sent_by("manual"/
    "auto")·sent_at(그 시각)로 함께 기록한다. 화면에서 "🤖 자동 전송 · HH:MM" 배지를
    보여줄지 판단하는 근거가 된다 — 담당자가 직접 눌렀을 땐 이 필드들이 있어도 화면에는
    아무것도 안 보여준다(평소·기본 상태를 조용하게 유지).
    """
    path = _find_run_file(run["run_at"], run["run_slot"])
    if path is None:
        return None
    updated = {
        **run,
        "send_count": run.get("send_count", 0) + 1,
        "sent_by": "auto" if auto else "manual",
        "sent_at": datetime.now().isoformat(timespec="seconds"),
    }
    atomic_write_text(path, json.dumps(updated, ensure_ascii=False, indent=2))
    return updated


def record_send_failure(run: dict, channels: list, auto: bool = False) -> Optional[dict]:
    """이번 발송 시도에서 실패한 채널이 있으면 회차 파일에 흔적을 남긴다(app.confirm_send).

    [추가: 2026-08-26] 사용자 요청 — "발송실패는 빨간 점으로 뜨는게 어때? 마우스
    오버하면 그 실패 원인이 뜨도록." 지금까지 app.telegram_bot/app.email_sender는
    실패 이유를 로그에만 남기고 회차 파일엔 저장하지 않았다 — 봇 토큰이 만료돼도
    담당자가 화면에서 알 방법이 없었다. 이 함수가 그 이유를 회차 파일의 별도 필드
    (send_failure)에 남긴다.

    send_count/sent_by/sent_at(성공 기록, record_send가 관리)과 완전히 별개 필드다 —
    부분 실패(예: 텔레그램 3명 중 1명 실패, 이메일은 전원 성공)처럼 record_send가
    호출돼 "발송 완료"로 확정된 경우에도 실패 사실은 같이 남아야 하기 때문이다.

    channels가 비어 있으면(이번엔 전부 성공, 또는 시도할 채널이 아예 없었음) 이전
    시도의 실패 기록을 지운다 — 재시도 끝에 결국 성공했는데 지난 실패 흔적(빨간 점)이
    화면에 남아있으면 안 되니까.

    자동 발송은 실패할 때마다 매 tick(10초)마다 재호출되는데, 원인이 지난번과 완전히
    같으면(채널·대상·이유까지) 다시 쓰지 않는다 — 시각만 갱신하자고 회차 파일 전체를
    계속 다시 쓰는 낭비를 피한다(회차 하나가 커지면 수십~수백 KB라, 원인이 안 바뀐 채
    몇 시간씩 재시도되면 그만큼 디스크에 반복해서 쓰게 된다).
    """
    path = _find_run_file(run["run_at"], run["run_slot"])
    if path is None:
        return None
    previous = run.get("send_failure") or {}
    if channels:
        if previous.get("channels") == channels:
            return run
        updated = {
            **run,
            "send_failure": {
                "at": datetime.now().isoformat(timespec="seconds"),
                "auto": auto,
                "channels": channels,
            },
        }
    else:
        if "send_failure" not in run:
            return run
        updated = {k: v for k, v in run.items() if k != "send_failure"}
    atomic_write_text(path, json.dumps(updated, ensure_ascii=False, indent=2))
    return updated


def run_exists(run_slot: str, date_str: Optional[str] = None) -> bool:
    """해당 날짜의 특정 회차가 이미 수집·저장됐는지 확인한다 (기본값: 오늘).

    스케줄러가 "이 회차를 이미 실행했는가"를 판단해 중복 실행을 막는 데 쓴다.
    """
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    return _run_file_path(date_str, run_slot).exists()


def delete_expired_runs(retention_days: int = RETENTION_DAYS, now: Optional[datetime] = None) -> list[Path]:
    """저장된 지 retention_days일이 지난 회차 파일을 삭제한다 (PRD.md 7절, 기본 RETENTION_DAYS=365일).

    파일명의 날짜가 아니라 각 파일에 저장된 run_at을 기준으로 나이를 판단한다
    (load_latest_run과 동일한 기준 — 단일 진실 공급원을 유지한다).
    깨진 JSON이나 run_at이 없는 파일은 판단할 수 없으므로 건드리지 않고 건너뛴다.

    반환: 실제로 삭제한 파일 경로 목록.
    """
    now = now or datetime.now()
    cutoff = now - timedelta(days=retention_days)

    # [수정: 2026-08-18] run_at은 app.run_index가 캐시해 둔 값을 쓴다 — 스케줄러 tick마다
    # (10초) 도는 자리라 전체 파싱을 하면 보관 기간을 늘리는 만큼 그대로 부담이 된다.
    # 판단 불가한 파일(깨진 JSON·run_at 없음)은 인덱스가 애초에 목록에서 빼므로, 예전과
    # 똑같이 건드리지 않고 넘어가는 결과가 된다.
    deleted = []
    for meta in list_run_meta():
        path = meta["path"]
        try:
            run_at = datetime.fromisoformat(meta["run_at"])
        except (ValueError, TypeError):
            continue
        if run_at < cutoff:
            path.unlink()
            deleted.append(path)

    # 인덱스가 "읽을 수 없다"고 표시한 파일(bad)은 위 목록에 안 들어온다. 예전 구현은
    # run_at만 읽히면(articles가 없더라도) 나이로 판단해 지웠으므로, 그 파일들만 따로
    # 한 번 더 확인한다 — 안 그러면 반쯤 깨진 파일이 보관 기간을 넘겨 영영 남는다.
    # 평상시 bad는 0건이라 비용도 0이다.
    for filename, entry in load_entries().items():
        if not entry.get("bad"):
            continue
        path = _articles_dir_for_cleanup() / filename
        try:
            run_at = datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["run_at"])
        except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue
        if run_at < cutoff:
            path.unlink()
            deleted.append(path)
    return deleted


# --- 정기 보관함에서 담당자가 지운 회차 [추가: 2026-09-11] ------------------------------
#
# 지우는 건 파일을 없애는 게 아니라 옆 폴더(articles_trash)로 **옮기는** 것이다 — CLAUDE.md
# 작업 규칙("파일은 바로 지우지 말고 옮겨만 둔다")이자, 정기 보관함이 그 자리에 「삭제함 ·
# 되살리기」 줄을 남길 수 있는 근거다. 1년 보관 기한(delete_expired_runs)이 지우는 파일은
# 여기 안 온다 — 그건 예전 그대로 실제 삭제이고, 이 폴더에는 담당자가 누른 것만 쌓인다.
#
# 폴더 위치는 ARTICLES_DIR 옆을 **호출 시점에** 계산한다(_articles_dir_for_cleanup과 같은
# 이유 — 통합 테스트가 ARTICLES_DIR을 임시 폴더로 바꿔치기하므로 상수로 붙들면 실제
# data/ 옆에 쓰게 된다). ARTICLES_DIR **안에** 두지 않는 건 그 폴더를 *.json으로 훑는
# 곳(app.run_index·_find_run_file)이 여럿이라 섞이면 지운 회차가 되살아나기 때문이다.
#
# 파일 이름은 원래 파일명이 아니라 `{run_at 날짜}_{HH-MM}.deleted-{지운 시각}.json`으로
# 새로 짓는다 — 목록을 그릴 때 파일을 한 개도 열지 않고 날짜·회차·지운 시각을 알 수 있고,
# 2026-08-26 이전의 어긋난 옛 파일명도 되살릴 때 올바른 이름으로 돌아간다.
_DELETED_RUN_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})\.deleted-(\d{14})\.json$")
_run_trash_lock = threading.Lock()


class RunLockedError(ValueError):
    """지우면 안 되는 회차를 지우려 했다 — reason은 "today" 또는 "latest"."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _runs_trash_dir() -> Path:
    return Path(ARTICLES_DIR).parent / "articles_trash"


def latest_run_key(metas: Optional[list] = None) -> Optional[tuple]:
    """저장된 회차 중 가장 최근 것의 (날짜, run_slot) — load_latest_run()이 고르는 그 회차다."""
    metas = list_run_meta() if metas is None else metas
    if not metas:
        return None
    return (metas[0]["run_at"][:10], metas[0].get("run_slot"))


def run_lock_reason(date_str: str, run_slot: str, metas: Optional[list] = None,
                    now: Optional[datetime] = None, latest_key: Optional[tuple] = None) -> Optional[str]:
    """이 회차를 지우면 안 되는 이유 — 지워도 되면 None.

    latest_key: 이미 구해둔 latest_run_key() — 화면 하나를 그리며 회차마다 부를 때 인덱스
    조회를 한 번으로 줄인다. 안 넘기면 metas(없으면 인덱스)에서 구한다.

    - "today": 오늘 회차. 파일이 없어지면 스케줄러가 run_exists로 "아직 안 돈 회차"라고
      보고 곧바로 다시 수집한다 — 지우는 게 아니라 다시 모으기가 된다.
    - "latest": 저장된 회차 중 가장 최근 것. 확정본 화면과 자동 발송이 load_latest_run()
      하나만 보는데, 이걸 지우면 그 앞 회차가 "가장 최근"이 된다 — 오늘 첫 회차가 오기 전
      새벽이라면 **어제 발송 안 된 회차가 자동 발송 대상**이 될 수 있다(되돌릴 수 없는 외부
      발송이라 여기서 원천 차단한다).
    """
    now = now or datetime.now()
    if date_str == now.strftime("%Y-%m-%d"):
        return "today"
    if latest_key is None:
        latest_key = latest_run_key(metas)
    if latest_key == (date_str, run_slot):
        return "latest"
    return None


def delete_run(date_str: str, run_slot: str, stamp: Optional[str] = None,
               now: Optional[datetime] = None) -> bool:
    """정기 보관함의 회차 하나를 trash로 옮긴다. 그 회차가 없으면 False.

    지우면 안 되는 회차(run_lock_reason)는 RunLockedError — 화면이 애초에 🗑를 안 그리지만,
    열어둔 옛 탭에서 눌릴 수 있으므로 서버가 한 번 더 막는다.
    stamp: 지운 시각("YYYYMMDDHHMMSS"). 여러 개를 한 번에 지울 때 같은 값을 넘기면 한 묶음이 된다.
    """
    now = now or datetime.now()
    stamp = stamp or now.strftime("%Y%m%d%H%M%S")
    with _run_trash_lock:
        metas = list_run_meta()
        reason = run_lock_reason(date_str, run_slot, metas, now)
        if reason:
            raise RunLockedError(reason)
        meta = next(
            (m for m in metas if m["run_at"][:10] == date_str and m.get("run_slot") == run_slot), None
        )
        if meta is None or not meta["path"].exists():
            return False
        trash_dir = _runs_trash_dir()
        trash_dir.mkdir(parents=True, exist_ok=True)
        dest = trash_dir / f"{date_str}_{run_slot.replace(':', '-')}.deleted-{stamp}.json"
        meta["path"].replace(dest)
    logger.info("정기 회차 삭제(trash로 이동) — %s %s → %s", date_str, run_slot, dest.name)
    return True


def list_deleted_runs() -> list[dict]:
    """담당자가 지운 회차 목록(지운 시각 최신 먼저) — 파일을 한 개도 열지 않는다.

    각 항목: {"date", "run_slot", "deleted_at": datetime, "trash_name"}
    같은 날짜·회차가 여럿이면(지웠다 되살렸다 다시 지운 경우) 가장 최근 것 하나만 남긴다.
    """
    trash_dir = _runs_trash_dir()
    if not trash_dir.exists():
        return []
    entries = {}
    for path in trash_dir.glob("*.json"):
        m = _DELETED_RUN_RE.match(path.name)
        if not m:
            continue
        date_str, hh, mm, stamp = m.groups()
        entry = {
            "date": date_str,
            "run_slot": f"{hh}:{mm}",
            "deleted_at": datetime.strptime(stamp, "%Y%m%d%H%M%S"),
            "trash_name": path.name,
        }
        key = (date_str, entry["run_slot"])
        if key not in entries or entries[key]["deleted_at"] < entry["deleted_at"]:
            entries[key] = entry
    return sorted(entries.values(), key=lambda e: e["deleted_at"], reverse=True)


def restore_run(trash_name: str) -> Optional[dict]:
    """지운 회차를 되살린다. 성공하면 {"date", "run_slot"}, 못 하면 None.

    못 하는 경우: 이름이 이 폴더의 형식이 아님(경로 조작 방어), 파일이 이미 없음, 같은
    날짜·회차가 이미 보관함에 있음(덮어쓰면 그쪽 기사가 사라진다).
    """
    m = _DELETED_RUN_RE.match(trash_name or "")
    if not m:
        return None
    date_str, hh, mm, _ = m.groups()
    run_slot = f"{hh}:{mm}"
    with _run_trash_lock:
        src = _runs_trash_dir() / trash_name
        if not src.exists():
            return None
        if any(meta["run_at"][:10] == date_str and meta.get("run_slot") == run_slot for meta in list_run_meta()):
            return None
        target = _run_file_path(date_str, run_slot)
        if target.exists():
            return None
        src.replace(target)
    logger.info("정기 회차 되살림 — %s → %s", trash_name, target.name)
    return {"date": date_str, "run_slot": run_slot}
