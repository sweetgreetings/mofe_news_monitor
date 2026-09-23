# Design Ref: 사용자 요청(2026-08-05) — "+ 직접 키워드 작성하기" 버튼으로 이용자가 자유
# 서식으로 적어두는 메모/키워드 한 줄. 하단 💬 AI 요약 블록과는 완전히 별개다
# (예전엔 그 위에 있던 "🤖 AI가 추출한 주요 키워드"와 대비해 설명했는데,
#  그 블록은 2026-09-10에 없앴다 — HISTORY.md 참고).
#
# [수정: 2026-08-07] 회차마다 키워드가 다른데 메모가 전역 값 하나로만 저장되다 보니,
# 예전 회차에 남긴 메모가 다음 회차 초안에도 그대로 남아 있는 문제가 있었다. 메모를
# "지금 진행 중인(또는 방금 끝난) 회차"의 (날짜, 회차명)과 함께 저장해두고, 조회 시
# 그 키가 안 맞으면 빈 문자열로 취급한다 — 파일을 지우진 않고 lazy하게 무시하는 방식
# (data/custom_groups.json 등 기존 자정 초기화 패턴과 같은 발상, 다만 기준이 자정이
# 아니라 회차 전환이다).
#
# [수정: 2026-08-26] 그 회차 키를 **메모 1건에** 달아두다 보니, 회차가 바뀌어 새 메모를
# 저장하는 순간 이전 회차 메모가 파일에서 통째로 덮어써져 영영 사라졌다. 게다가 초안이
# 보는 회차는 "아직 안 끝난 회차"(11:00 수집 직후면 벌써 14:00)라, 초안에 메모를 쓰고
# 확정본(=직전 11:00 회차)으로 넘어가면 키가 안 맞아 화면에서 사라진 것처럼 보였다.
# 그래서 저장 형식을 **회차별 딕셔너리**로 바꿨다(덮어쓰기 없음, 여기까지는 유지).
#
# [수정: 2026-08-27] 같은 커밋에서 "그 회차 메모가 없으면 가장 가까운 회차 메모를
# 이어받아 화면에 미리 채운다"도 같이 넣었었는데, 되돌렸다(사용자 판단) — 이전 회차
# 메모를 다음 회차까지 끌고 오는 게 오히려 헷갈리고, 회차별 메모는 담당자가 그때그때
# 직접 판단해 쓰는 게 원칙이라는 이유. 부작용으로 "취소" 버튼도 먹통이었다: 이어받은
# 상태의 취소는 저장 없이 새로고침만 했는데, 이어받기 자체가 매 렌더링마다 다시
# 계산되는 값이라 새로고침해도 똑같은 이어받은 메모가 그대로 되살아났다(화면상 취소가
# 아무 일도 안 하는 것처럼 보임). 이제 그 회차에 저장된 메모가 없으면 화면도 빈 칸으로
# 시작한다 — load_manual_keyword_note_entry는 없앴고, 화면과 내보내기 텍스트 둘 다
# load_manual_keyword_note() 하나만 쓴다.
import json
from datetime import datetime
from typing import Optional, Tuple

from app.atomic_write import atomic_write_text
from app.config import MANUAL_KEYWORD_NOTE_FILE
from app.storage import load_latest_run

RoundKey = Tuple[str, str]

# 보관할 회차 메모 개수 상한. 하루 4~6회차이므로 60이면 최근 두 주 남짓 — 파일이 무한정
# 늘지 않게만 막는 값이고, 화면이 실제로 쓰는 건 "이번 회차"와 "직전 회차" 둘뿐이다.
_MAX_NOTES = 60


def _key_str(key: Optional[RoundKey]) -> str:
    """(날짜, 회차명)을 파일에 쓸 문자열 키로. 판단할 회차 자체가 없으면 빈 문자열 —
    ISO 날짜 + HH:MM이라 문자열 정렬이 곧 시간 정렬이다(직전 회차 찾기가 이 성질에 기댄다)."""
    if key is None or not key[0] or not key[1]:
        return ""
    return f"{key[0]}|{key[1]}"


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


def _read_notes() -> dict:
    """저장 파일을 {회차키 문자열: 메모} 형태로 읽는다.

    옛 형식({"text", "date", "run_slot"} 1건)도 그대로 읽어 같은 모양으로 바꿔준다 —
    다음 저장 때 새 형식으로 옮겨 적히고, 그때까지 화면 동작은 동일하다.
    """
    if not MANUAL_KEYWORD_NOTE_FILE.exists():
        return {}
    try:
        data = json.loads(MANUAL_KEYWORD_NOTE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    notes = data.get("notes")
    if isinstance(notes, dict):
        return {k: v for k, v in notes.items() if isinstance(k, str) and isinstance(v, str) and v}
    text = data.get("text", "")
    if isinstance(text, str) and text:
        return {_key_str((data.get("date"), data.get("run_slot"))): text}
    return {}


def load_manual_keyword_note(run_key: Optional[RoundKey] = None) -> str:
    """그 회차에 **실제로 저장된** 메모를 돌려준다. run_key를 생략하면 "지금 진행 중인/
    방금 끝난 회차" 기준으로 판단한다(초안 화면). 저장된 게 없으면 빈 문자열.

    화면 입력칸도 이 값을 그대로 쓴다(다른 회차 메모를 끌어와 미리 채우지 않는다 —
    2026-08-27 되돌림, 위 모듈 설명 참고). 이 함수의 반환값이 복사·txt·텔레그램·이메일
    텍스트에도 "- {메모}" 줄로 그대로 실려 나가므로, 화면과 보고서가 항상 같은 값을 본다.
    """
    notes = _read_notes()
    if not notes:
        return ""
    target = run_key if run_key is not None else _current_round_key()
    return notes.get(_key_str(target), "")


def past_notes(run_key: Optional[RoundKey] = None, prev_days: int = 1) -> list:
    """메모 칸 아래 「지난 회차 메모」 참고 목록 — 이 회차보다 **앞** 회차들의 저장된 메모.

    범위는 **오늘 앞 회차 전부 + 메모가 있는 직전 `prev_days`개 날짜**다. 오늘은 날짜
    수로 세지 않는다 — 14시 회차를 쓸 때 가장 가까운 참고는 오늘 9:30·11시 메모라서
    아침 첫 회차든 오후든 「오늘 + 하루」가 같은 뜻이어야 한다. **달력상 어제로 세지
    않는 것**도 같은 이유다: 월요일·연휴 다음 날은 어제 메모가 없어 칸이 빈다(메모가
    있는 가장 최근 하루를 가져온다).

    최신 회차부터, 같은 날 이어진 회차가 같은 문구면 한 줄로 합친다.
    돌려주는 값: [(날짜, [회차…(이른 것부터)], 문구)].
    **칸을 채우는 재료가 아니다** — 화면은 읽기만 한다(2026-08-27 이어받기 되돌림과 같은 원칙).
    """
    notes = _read_notes()
    target = _key_str(run_key if run_key is not None else _current_round_key())
    today = target.partition("|")[0]
    past = sorted((k for k in notes if k and (not target or k < target)), reverse=True)
    prev_dates: list = []
    rows: list = []
    for key in past:
        date, _, slot = key.partition("|")
        if date != today:
            if date not in prev_dates:
                if len(prev_dates) >= prev_days:
                    break
                prev_dates.append(date)
        text = notes[key]
        if rows and rows[-1][0] == date and rows[-1][2] == text:
            rows[-1][1].insert(0, slot)
        else:
            rows.append((date, [slot], text))
    return rows


def save_manual_keyword_note(text: str, run_key: Optional[RoundKey] = None) -> None:
    """메모 텍스트를 그 회차 자리에 저장한다. run_key를 생략하면 "지금 진행 중인/방금
    끝난 회차"에 붙여 저장한다. 빈 문자열로 저장하면 그 회차 메모만 지운다("작성 전"
    상태로 되돌아가 버튼이 다시 "+ 직접 키워드 작성하기"로 보이고, 내보내기 텍스트에도
    안 실린다) — 다른 회차 메모는 건드리지 않는다.
    """
    key = run_key if run_key is not None else _current_round_key()
    notes = _read_notes()
    key_str = _key_str(key)
    cleaned = text.strip()
    if cleaned:
        notes[key_str] = cleaned
    else:
        notes.pop(key_str, None)
    if len(notes) > _MAX_NOTES:
        for stale in sorted(notes)[: len(notes) - _MAX_NOTES]:
            notes.pop(stale)
    atomic_write_text(
        MANUAL_KEYWORD_NOTE_FILE,
        json.dumps({"notes": notes}, ensure_ascii=False),
    )


def copy_note(src_key: RoundKey, dst_key: RoundKey) -> None:
    """「✂ 오늘만 여기서 끊기」 — 원래 회차에 쓴 메모를 끊은 회차에도 복사한다(app.today_cuts).
    원래 한 회차에 쓴 메모라 나뉜 두 회차 모두에 싣는다(사용자 결정, 2026-09-21)."""
    notes = _read_notes()
    text = notes.get(_key_str(src_key), "")
    if not text:
        return
    save_manual_keyword_note(text, dst_key)
