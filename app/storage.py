# Design Ref: DESIGN.md §3 기술 선택 — SQLite 대신 로컬 JSON 파일로 회차별 기사 데이터를 저장/조회하는 모듈
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import ARTICLES_DIR, RETENTION_DAYS


def _run_file_path(date_str: str, run_slot: str) -> Path:
    # Windows 파일명에는 ':'를 쓸 수 없어 "-"로 바꿔 저장한다 (예: "09:00" -> "09-00")
    safe_slot = run_slot.replace(":", "-")
    return ARTICLES_DIR / f"{date_str}_{safe_slot}.json"


def save_run(run_slot: str, articles: list[dict], run_at: Optional[str] = None) -> Path:
    """
    회차 하나의 기사 목록을 JSON 파일로 저장한다.

    articles의 각 항목은 다음 필드를 갖는다 (PRD.md 기능1 규칙 5·6):
      - outlet: 언론사명
      - title: 기사 제목
      - url: 기사 URL
      - summary: 네이버 API description 원문 (요약)

    run_slot: 원래 예정돼 있던 회차 시각, 예: "09:00" (헤더 "언론 모니터링 09:00 기준"에 쓰임)
    run_at: 실제로 스크랩이 실행된 시각 (놓친 회차를 나중에 보충 실행한 경우 run_slot과 달라질 수 있음)
    """
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    run_data = {
        "run_slot": run_slot,
        "run_at": run_at or now.isoformat(timespec="seconds"),
        "articles": articles,
    }
    path = _run_file_path(date_str, run_slot)
    path.write_text(json.dumps(run_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_latest_run() -> Optional[dict]:
    """가장 최근에 저장된 회차 데이터를 읽어온다. 저장된 회차가 하나도 없으면 None.

    파일명이 아니라 저장된 run_at 값으로 비교한다. 자정을 넘겨 놓친 회차를
    보충 실행하면 파일명 날짜(now())와 run_slot이 다른 날짜가 될 수 있어,
    파일명 정렬만으로는 실제로 가장 최근인 회차를 못 찾을 수 있기 때문이다.
    """
    runs = [json.loads(f.read_text(encoding="utf-8")) for f in ARTICLES_DIR.glob("*.json")]
    if not runs:
        return None
    return max(runs, key=lambda run: run["run_at"])


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

    최대 7일치까지만 남아있다 — delete_expired_runs가 그보다 오래된 파일을 지우기 때문이다.
    깨진 JSON이거나 run_at·run_slot·articles 중 하나라도 없는 파일은 건너뛴다 (화면을
    그리는 쪽에서 이 3개 키를 그대로 쓰므로, 여기서 미리 검증해 손상 파일 하나 때문에
    전체 조회가 멈추지 않도록 한다 — delete_expired_runs와 같은 방어 철학).
    """
    runs = []
    for path in ARTICLES_DIR.glob("*.json"):
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
            datetime.fromisoformat(run["run_at"])  # 정렬 키로 쓸 수 있는지 미리 검증
            run["run_slot"]
            run["articles"]
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue
        runs.append(run)
    return sorted(runs, key=lambda run: run["run_at"], reverse=True)


def load_today_runs(date_str: Optional[str] = None) -> list[dict]:
    """오늘(또는 지정한 날짜) 저장된 모든 회차를 시간순으로 돌려준다 (진입 화면 워드클라우드 당일 누적용).

    list_all_runs와 같은 방어적 파싱(깨진 파일 건너뛰기)을 쓰되, 날짜 필터만 다르다.
    파일명이 아니라 run_at의 날짜로 판단한다 (자정 넘겨 보충 실행해도 정확히 걸러진다).
    """
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    runs = []
    for path in ARTICLES_DIR.glob("*.json"):
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
            run_at = datetime.fromisoformat(run["run_at"])
            run["articles"]
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue
        if run_at.strftime("%Y-%m-%d") == date_str:
            runs.append(run)
    return sorted(runs, key=lambda run: run["run_at"])


def _find_run_file(run_at: str, run_slot: str) -> Optional[Path]:
    """(run_at, run_slot)과 내용이 일치하는 파일을 찾는다 — 파일명의 날짜(now())와 run_at의
    날짜가 다를 수 있어(위 save_run 설명 참고), 파일명만으로는 정확히 짚어낼 수 없다."""
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


def run_exists(run_slot: str, date_str: Optional[str] = None) -> bool:
    """해당 날짜의 특정 회차가 이미 수집·저장됐는지 확인한다 (기본값: 오늘).

    스케줄러가 "이 회차를 이미 실행했는가"를 판단해 중복 실행을 막는 데 쓴다.
    """
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    return _run_file_path(date_str, run_slot).exists()


def delete_expired_runs(retention_days: int = RETENTION_DAYS, now: Optional[datetime] = None) -> list[Path]:
    """저장된 지 retention_days일이 지난 회차 파일을 삭제한다 (PRD.md 7절, 기본 7일).

    파일명의 날짜가 아니라 각 파일에 저장된 run_at을 기준으로 나이를 판단한다
    (load_latest_run과 동일한 기준 — 단일 진실 공급원을 유지한다).
    깨진 JSON이나 run_at이 없는 파일은 판단할 수 없으므로 건드리지 않고 건너뛴다.

    반환: 실제로 삭제한 파일 경로 목록.
    """
    now = now or datetime.now()
    cutoff = now - timedelta(days=retention_days)

    deleted = []
    for path in ARTICLES_DIR.glob("*.json"):
        try:
            run_at = datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["run_at"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            # run_at이 없거나(KeyError), JSON이 깨졌거나(JSONDecodeError), 문자열이
            # 아니거나(TypeError) 형식이 안 맞으면(ValueError) 판단 불가 — 이 파일 하나
            # 때문에 나머지 파일 정리까지 멈추지 않도록 건드리지 않고 계속 진행한다.
            continue
        if run_at < cutoff:
            path.unlink()
            deleted.append(path)
    return deleted
