# Design Ref: 2026-08-20 담당자 제보 — AI 소제목 분류가 규칙 기반으로 폴백됐을 때
# "🤖 전체 기사 재분류" 버튼을 몇 번 눌러도 계속 실패하는 경우, 몇 번 실패했는지 화면이
# 알 방법이 없어서 담당자가 안 통하는 버튼을 반복해서 누르며 시간을 버렸다.
#
# 이 모듈은 초안의 **명시적** 재분류 클릭(app.settings_server._handle_regenerate_
# subheadings_draft, force_llm=True)만 기록한다 — 회차 초반 자동 첫 분류
# (app.preview_renderer._take_auto_classify_turn)는 담당자가 요청한 게 아니라서
# 대상이 아니다. 연속 2회 이상 실패하면 app.preview_renderer가 "재분류마저 실패"
# 문구로 바꿔 재시도 대신 수기 정리를 권한다.
#
# 프로세스 메모리에만 둔다(디스크 저장 없음) — 다음 회차로 넘어가면(round_id가 달라짐)
# 자연히 안 보이고, 세션 동안의 "방금 몇 번 시도했는지"만 알면 충분한 정보라
# app.llm_classifier._cache처럼 디스크에 남길 이유가 없다.
import threading
from datetime import datetime
from typing import Optional

_lock = threading.Lock()
# round_id -> [(datetime, success: bool), ...] — 최근 시도만 남기면 되므로 회차당 상한을 둔다.
_attempts: dict = {}
_MAX_ROUNDS = 20
_MAX_PER_ROUND = 5


def record_attempt(round_id: Optional[tuple], success: bool, now: Optional[datetime] = None) -> None:
    """담당자가 "전체 기사 재분류"를 직접 눌렀을 때, 그 결과(성공/실패)를 이 회차 기록에 남긴다."""
    with _lock:
        if round_id not in _attempts and len(_attempts) >= _MAX_ROUNDS:
            # app.llm_classifier._cache와 같은 방식 — 무한정 쌓이지 않도록 상한에
            # 닿으면 통째로 비운다(개별 만료 로직을 둘 만큼 중요한 데이터가 아니다).
            _attempts.clear()
        entries = _attempts.setdefault(round_id, [])
        entries.append((now or datetime.now(), success))
        del entries[:-_MAX_PER_ROUND]


def consecutive_failures(round_id: Optional[tuple]) -> int:
    """이 회차에서 마지막 성공 이후(또는 기록 시작 이후) 연속으로 실패한 횟수."""
    with _lock:
        entries = list(_attempts.get(round_id, []))
    count = 0
    for _, success in reversed(entries):
        if success:
            break
        count += 1
    return count


def recent_attempts(round_id: Optional[tuple]) -> list:
    """이 회차의 시도 기록을 오래된 순으로 돌려준다 — 화면의 재시도 이력 표시용."""
    with _lock:
        return list(_attempts.get(round_id, []))


def copy_attempts(src_round: Optional[tuple], dst_round: Optional[tuple]) -> None:
    """「✂ 오늘만 여기서 끊기」로 회차 이름이 바뀔 때 시도 기록을 새 회차로 복사한다(app.today_cuts)."""
    with _lock:
        if src_round in _attempts:
            _attempts[dst_round] = list(_attempts[src_round])
