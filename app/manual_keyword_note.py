# Design Ref: 사용자 요청(2026-08-05) — "+ 직접 키워드 작성하기" 버튼으로 이용자가 자유
# 서식으로 적어두는 메모/키워드 한 줄. AI가 추출한 하단 키워드 블록과는 완전히 별개다.
#
# [수정: 2026-08-07] 회차마다 키워드가 다른데 메모가 전역 값 하나로만 저장되다 보니,
# 예전 회차에 남긴 메모가 다음 회차 초안에도 그대로 남아 있는 문제가 있었다. 메모를
# "지금 진행 중인(또는 방금 끝난) 회차"의 (날짜, 회차명)과 함께 저장해두고, 조회 시
# 그 키가 안 맞으면 빈 문자열로 취급한다 — 파일을 지우진 않고 lazy하게 무시하는 방식
# (data/custom_groups.json 등 기존 자정 초기화 패턴과 같은 발상, 다만 기준이 자정이
# 아니라 회차 전환이다).
import json
from datetime import datetime
from typing import Optional, Tuple

from app.atomic_write import atomic_write_text
from app.config import MANUAL_KEYWORD_NOTE_FILE
from app.storage import load_latest_run

RoundKey = Tuple[str, str]


def _current_round_key(now: Optional[datetime] = None) -> Optional[RoundKey]:
    """지금 이 순간 "메모가 속해야 할 회차"를 (날짜, 회차명) 튜플로 돌려준다.

    오늘 아직 안 끝난 회차가 있으면 그 회차(초안 화면이 미리 보여주는, 진행 중인 회차)를
    기준으로 삼고, 오늘 회차가 모두 끝났으면 가장 최근 저장된 회차를 기준으로 삼는다.
    저장(save)과 조회(load, run_key 생략 시)가 항상 이 정의를 함께 써야 초안에서 쓴
    메모가 그 회차의 완성본까지는 자연스럽게 이어지고, 다음 회차부터는 비워진다.

    app.scheduler를 여기서 모듈 최상단에서 import하면 scheduler -> renderer ->
    manual_keyword_note로 순환 임포트가 생겨(app.renderer가 이 모듈을 가져다 쓰므로)
    함수 안에서 지연 임포트한다.
    """
    from app.scheduler import next_pending_slot

    now = now or datetime.now()
    pending = next_pending_slot(now)
    if pending is not None:
        return now.strftime("%Y-%m-%d"), pending["end"]
    latest = load_latest_run()
    if latest is not None:
        return latest["run_at"][:10], latest["run_slot"]
    return None


def load_manual_keyword_note(run_key: Optional[RoundKey] = None) -> str:
    """저장된 메모 텍스트를 읽어온다. run_key를 생략하면 "지금 진행 중인/방금 끝난 회차"
    기준으로 판단한다(초안 화면). 완성본·내보내기처럼 특정 회차를 보여줄 때는 그 회차의
    (run_at 날짜, run_slot)을 넘긴다. 저장된 메모가 다른 회차 것이면 빈 문자열(작성 전
    상태와 동일하게 취급)을 돌려준다."""
    if not MANUAL_KEYWORD_NOTE_FILE.exists():
        return ""
    try:
        data = json.loads(MANUAL_KEYWORD_NOTE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    text = data.get("text", "")
    if not isinstance(text, str) or not text:
        return ""
    stored_key = (data.get("date"), data.get("run_slot"))
    target_key = run_key if run_key is not None else _current_round_key()
    if target_key is None or stored_key != target_key:
        return ""
    return text


def save_manual_keyword_note(text: str, run_key: Optional[RoundKey] = None) -> None:
    """메모 텍스트를 저장한다. run_key를 생략하면 "지금 진행 중인/방금 끝난 회차"에
    붙여 저장한다. 빈 문자열로 저장하면 "작성 전" 상태로 되돌아간다(버튼이 다시
    "+ 직접 키워드 작성하기"로 보이고, 내보내기 텍스트에도 안 실린다)."""
    key = run_key if run_key is not None else _current_round_key()
    date_str, run_slot = key if key is not None else (None, None)
    atomic_write_text(
        MANUAL_KEYWORD_NOTE_FILE,
        json.dumps({"text": text.strip(), "date": date_str, "run_slot": run_slot}, ensure_ascii=False),
    )
