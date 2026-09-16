# Design Ref: CODING_CONVENTIONS.md §1(추측하지 말고 잰다) · §7-4(읽기-수정-쓰기는 락) · §7-5(원자적 쓰기)
#
# 회차 파일의 **가벼운 메타만** 모아두는 캐시다. "이 회차가 언제 것이고(run_at),
# 몇 시 회차이고(run_slot), 확정됐는지(confirmed), 기사가 몇 건인지"를 알기 위해
# data/articles/*.json을 통째로 파싱하던 자리를 대신한다.
#
# 왜 필요한가 — 실측(2026-08-18, 보관 1년 = 1,460개 파일 22.8MB로 복제해 측정):
#   전체 파싱 80ms / URL 수집 75ms / glob만 1ms.
# 그런데 전체 파싱을 하는 자리가 담당자의 클릭 경로 한복판에 있었다.
# 🗑️ 한 번 누를 때마다 _regenerate_screens() -> generate_history_page() -> list_all_runs()로
# 한 번, _find_run_file()로 또 한 번 — 보관 7일에선 안 보이지만 1년이면 클릭마다 45MB를 읽는다.
#
# 원칙: **이 인덱스는 캐시일 뿐 진실이 아니다.** 진실은 항상 data/articles/의 파일이다.
#   - 매 조회마다 디렉터리를 glob해(1ms) 파일별 (mtime_ns, size)를 대조한다.
#   - 서명이 다르거나 인덱스에 없는 파일만 실제로 읽어 갱신한다 — 평상시 읽기 0회.
#   - 사라진 파일의 항목은 버린다. 인덱스 파일이 깨졌으면 통째로 다시 만든다.
#   - 못 읽는 파일(깨진 JSON·필수 필드 없음)은 bad로 기록해 매번 다시 읽지 않되,
#     목록에서는 제외한다(app.storage.list_all_runs의 방어적 파싱과 같은 철학).
import json
import logging
import threading
from pathlib import Path
from typing import Optional

from app.atomic_write import atomic_write_text

logger = logging.getLogger(__name__)


def _articles_dir() -> Path:
    """회차 파일 폴더를 **app.storage에서 그때그때** 가져온다.

    app.config에서 직접 import하지 않는 이유: 통합 테스트가 실제 data/를 건드리지 않으려고
    `patch.object(app.storage, "ARTICLES_DIR", 임시폴더)`로 바꿔치기한다. 이 모듈이 config를
    직접 붙들고 있으면 그 패치를 못 따라가 테스트가 실제 폴더를 읽고 쓰게 된다(실제로
    한 번 그렇게 만들었다 — CODING_CONVENTIONS.md §1-7과 같은 종류의 사고).
    app.storage가 app.run_index를 import하므로 순환을 피하려 호출 시점에 import한다.
    """
    import app.storage

    return app.storage.ARTICLES_DIR


def _index_path() -> Path:
    """인덱스 파일도 회차 폴더를 따라간다 — 임시 폴더로 바꿔치기하면 인덱스도 그쪽에 생겨,
    테스트가 실제 data/articles_index.json을 오염시키지 않는다."""
    return _articles_dir().parent / "articles_index.json"

# 인덱스 파일 자체의 읽기-수정-쓰기를 감싼다 — 스케줄러 스레드와 설정 서버 스레드가
# 동시에 들어올 수 있다(§7-4).
_lock = threading.Lock()


def _read_index_file() -> dict:
    """저장된 인덱스를 읽는다. 없거나 깨졌으면 빈 인덱스 — 캐시라 언제든 다시 만들 수 있다."""
    try:
        data = json.loads(_index_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    return data.get("entries", {}) if isinstance(data, dict) else {}


def _extract_meta(path: Path) -> Optional[dict]:
    """회차 파일 하나를 읽어 메타만 뽑는다. 목록에 쓸 수 없는 파일이면 None.

    검증 항목은 app.storage.list_all_runs와 같다 — run_at·run_slot·articles가 있어야
    화면 쪽에서 그대로 쓸 수 있다.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        run_at = data["run_at"]
        run_slot = data["run_slot"]
        articles = data["articles"]
        if not isinstance(run_at, str) or not isinstance(articles, list):
            return None
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
        return None
    return {
        "run_at": run_at,
        "run_slot": run_slot,
        # 옛 데이터에는 confirmed 필드가 아예 없다 — 그건 확정된 것으로 본다
        # (app.storage.load_latest_confirmed_run과 같은 규칙).
        "confirmed": bool(data.get("confirmed", True)),
        "article_count": len(articles),
    }


def _refresh_locked(entries: dict) -> tuple[dict, bool]:
    """디스크 상태에 맞춰 인덱스를 갱신한다. (새 entries, 바뀌었는지)를 돌려준다."""
    fresh: dict = {}
    changed = False

    for path in _articles_dir().glob("*.json"):
        try:
            stat = path.stat()
        except OSError:
            continue
        signature = {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
        cached = entries.get(path.name)
        if (
            cached
            and cached.get("mtime_ns") == signature["mtime_ns"]
            and cached.get("size") == signature["size"]
        ):
            fresh[path.name] = cached  # 서명 그대로 — 파일을 열지 않는다
            continue

        changed = True
        meta = _extract_meta(path)
        if meta is None:
            # 못 읽는 파일도 서명을 기록해 둔다 — 안 그러면 매 조회마다 다시 읽게 된다.
            fresh[path.name] = {**signature, "bad": True}
        else:
            fresh[path.name] = {**signature, **meta}

    if len(fresh) != len(entries):
        changed = True  # 삭제된 파일의 항목이 빠졌다
    return fresh, changed


def load_entries() -> dict:
    """{파일명: 메타} — 디스크와 대조해 최신 상태로 맞춘 뒤 돌려준다.

    평상시(파일이 안 바뀐 상태)에는 glob + stat만 하고 회차 파일은 한 개도 열지 않는다.
    """
    with _lock:
        entries = _read_index_file()
        fresh, changed = _refresh_locked(entries)
        if changed:
            try:
                atomic_write_text(
                    _index_path(), json.dumps({"entries": fresh}, ensure_ascii=False, indent=2)
                )
            except OSError:
                # 인덱스를 못 써도 동작은 계속돼야 한다 — 다음 조회에서 다시 만들면 된다.
                logger.warning("회차 인덱스 저장 실패 — 이번 조회는 메모리 값으로 진행합니다", exc_info=True)
        return fresh


def list_run_meta() -> list[dict]:
    """읽을 수 있는 회차의 메타를 run_at 내림차순(최신 먼저)으로 돌려준다.

    각 항목: {"path": Path, "filename": str, "run_at", "run_slot", "confirmed", "article_count"}
    """
    metas = []
    articles_dir = _articles_dir()
    for filename, meta in load_entries().items():
        if meta.get("bad") or "run_at" not in meta:
            continue
        metas.append({**meta, "filename": filename, "path": articles_dir / filename})
    # [수정: 2026-09-04] 동점(run_at이 같은 초)일 때 run_slot으로 한 번 더 가른다.
    # run_at은 초 단위라, 놓친 회차를 여러 개 연달아 보충하면(2026-09-04부터 그렇게
    # 한다 — app.scheduler.find_slots_to_run) 두 회차가 같은 초에 저장될 수 있다.
    # 그러면 "가장 최근 회차"가 glob 순서에 따라 갈려, 확정본 화면과 자동발송
    # 대상이 늦은 회차가 아니라 이른 회차로 잡힐 수 있었다(정렬이 결정론적이어야
    # 한다는 2026-09-02 사고의 교훈과 같은 자리).
    return sorted(metas, key=lambda m: (m["run_at"], m.get("run_slot") or ""), reverse=True)


def find_path(run_at: str, run_slot: str) -> Optional[Path]:
    """(run_at, run_slot)에 해당하는 파일 경로 — 인덱스만 보고 찾는다. 없으면 None.

    파일명으로는 못 짚는다(파일명 날짜는 저장 시각, run_at은 실행 시각이라 자정을 넘겨
    보충 실행하면 서로 다르다 — app.storage.save_run 설명 참고). 그래서 인덱스에
    run_at·run_slot을 담아두고 여기서 되찾는다.
    """
    for meta in list_run_meta():
        if meta["run_at"] == run_at and meta["run_slot"] == run_slot:
            return meta["path"]
    return None
