# Design Ref: 초안에 한 번 보인 기사를 그 회차 동안 붙잡아 두는 저장소 [추가: 2026-09-11]
#
# 초안(app.preview_renderer)과 확정본(app.scraper.collect_run)은 원래 둘 다 "지금 네이버
# 검색 결과를 조건대로 걸러 새로 만든 목록"이라 기억이 없었다 — 새로고침 사이에 검색
# 결과가 흔들리거나(언론사가 기사를 지움, 네이버가 기사 번호를 바꿈) 같은 제목 기사에
# 대표 자리를 뺏기면, 담당자가 초안에서 보고 "이번 회차에 넣어야지" 했던 기사가 말없이
# 사라졌다(2026-09-11 제보 — 앞 회차 시간대에 볼 수 없었던 늦게 들어온 기사를 이번
# 회차에라도 넣으려 했는데 사라짐).
#
# 약속: **담당자가 숨기거나 조건을 바꾸지 않는 한, 초안에 한 번 보인 기사는 그 회차의
# 초안에서 안 빠지고 확정본에도 들어간다.** 규칙 셋(사용자 결정, 2026-09-11):
#   ① 같은 제목이 여러 건이면 **먼저 보인 쪽이 대표 자리를 지킨다**(seen_order)
#   ② 조건을 바꾸면 다시 거른다 — 선택 언론사·사진/인사 제외는 붙잡은 기사에도 그대로
#      걸리고(파이프라인을 똑같이 통과한다), 검색어가 바뀌면 이 목록을 새로 시작한다
#      (붙잡은 기사가 새 검색어에 맞는지 다시 가를 방법이 없어서 — 검색어별 원시 결과를
#      안 들고 있다)
#   ③ 네이버 검색에서 사라져도 남긴다(그래서 기사 dict 전체를 저장한다)
#
# 저장 형식: {"round_id": [날짜, 회차 끝], "condition": [...], "articles": {url: 기사},
#            "seen_order": {url: 처음 보인 순번}, "last_visible": [url, ...]}
# 순번은 시각이 아니라 1, 2, 3… 이다 — 초 단위 시각이면 일괄 동작 중 같은 초에 보인 두
# 기사가 동점이 돼 "먼저 보인 쪽"을 못 가른다(테스트에서 실제로 걸렸다).
# 한 회차 분만 담는다 — round_id가 다르면 없는 것으로 친다(회차가 바뀌면 자연히 새로 시작).
import json
import threading

from app.atomic_write import atomic_write_text
from app.config import DRAFT_SEEN_FILE

_lock = threading.Lock()

# 저장할 때 떼어내는 필드 — 초안 파이프라인이 그때그때 다시 매기는 표시용 값이라
# 붙잡아 둔 사본에 굳어 있으면 안 된다(확정본이 창 기준으로 다시 계산한다).
_TRANSIENT_KEYS = ("late_pickup", "late_pickup_slot")


def search_condition(groups: list) -> list:
    """붙잡기 목록이 유효한지 가르는 "검색 조건" 값 — 초안 캐시 서명의 keywords 부분과
    같은 식이다(app.preview_renderer._preview_keywords_signature가 이 함수를 쓴다).
    초안과 확정본이 각자 만들면 한쪽만 고쳤을 때 조용히 어긋나므로 한 곳에 둔다."""
    return [{"keywords": g["keywords"], "mode": g.get("mode", "OR")} for g in groups]


def _read() -> dict:
    if not DRAFT_SEEN_FILE.exists():
        return {}
    try:
        data = json.loads(DRAFT_SEEN_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _matches(data: dict, round_id: tuple, condition: list) -> bool:
    return tuple(data.get("round_id") or ()) == tuple(round_id) and data.get("condition") == condition


def load_draft_seen(round_id: tuple, condition: list) -> dict:
    """이 회차·이 검색 조건의 붙잡기 목록. 없거나 어긋나면 빈 목록.

    반환: {"articles": {url: 기사}, "seen_order": {url: 순번}, "last_visible": [url],
           "reset_reason": None | "condition"}
    reset_reason은 **같은 회차인데 검색 조건만 바뀐** 경우 "condition" — 호출부가 로그로
    "검색어가 바뀌어 새로 시작"을 남기는 데 쓴다(회차가 바뀐 건 정상이라 조용히 넘긴다).
    """
    data = _read()
    if _matches(data, round_id, condition):
        return {
            "articles": dict(data.get("articles") or {}),
            "seen_order": dict(data.get("seen_order") or {}),
            "last_visible": list(data.get("last_visible") or []),
            "reset_reason": None,
        }
    same_round = tuple(data.get("round_id") or ()) == tuple(round_id)
    return {
        "articles": {},
        "seen_order": {},
        "last_visible": [],
        "reset_reason": "condition" if same_round and data.get("articles") else None,
    }


def record_draft_seen(round_id: tuple, condition: list, visible: list) -> None:
    """지금 초안에 보인 기사를 붙잡기 목록에 더하고, "직전에 보인 목록"을 갱신한다.

    visible: 초안 화면에 실제로 그려지는 기사(숨김·필터를 다 거친 뒤). 한 번 들어간 기사는
    다음에 안 보여도(숨김·설정으로 빠져도) 목록에서 지우지 않는다 — 숨김을 풀거나 설정을
    되돌리면 그대로 돌아와야 하기 때문이다. 필터는 매번 파이프라인이 다시 건다.
    """
    with _lock:
        data = _read()
        if not _matches(data, round_id, condition):
            data = {"round_id": list(round_id), "condition": condition, "articles": {}, "seen_order": {}}
        articles = data.setdefault("articles", {})
        seen_order = data.setdefault("seen_order", {})
        for a in visible:
            url = a.get("url")
            if not url:
                continue
            if url not in articles:
                articles[url] = {k: v for k, v in a.items() if k not in _TRANSIENT_KEYS}
            if url not in seen_order:
                seen_order[url] = len(seen_order) + 1
        data["last_visible"] = [a["url"] for a in visible if a.get("url")]
        atomic_write_text(DRAFT_SEEN_FILE, json.dumps(data, ensure_ascii=False))


def move_round(src_round: tuple, dst_round: tuple) -> None:
    """「✂ 오늘만 여기서 끊기」 — 붙잡기 목록을 끊은 회차 이름으로 **옮긴다**(app.today_cuts).
    한 회차 분만 담는 저장소라 복사하지 않는다 — 끊은 뒤 이어지는 회차는 새 기사로 시작한다."""
    with _lock:
        data = _read()
        if tuple(data.get("round_id") or ()) != tuple(src_round):
            return
        data["round_id"] = list(dst_round)
        atomic_write_text(DRAFT_SEEN_FILE, json.dumps(data, ensure_ascii=False))
