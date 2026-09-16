# Design Ref: HISTORY.md "홈 화면 재구성" — 정책 단어 추이가 지켜볼 단어는 담당자가 직접 고른다
"""홈 화면 "정책 단어 추이" 카드·전용 화면(/trend)이 함께 쓰는, 지켜볼 단어(최대
MAX_TREND_WORDS개) 저장소.

건수 자동 TOP3나 급등 자동 채택을 쓰지 않는다 — 추이 그래프는 "같은 대상을 계속
지켜본다"는 전제가 있어야 의미가 있는데, 자동 선정은 그 전제를 깬다(HISTORY.md 참고).
그래서 이 파일은 순수 저장소일 뿐이고, 무엇을 담을지는 항상 담당자의 명시적 조작
(add_trend_word/remove_trend_word)으로만 바뀐다 — 조용히 자동으로 채워지는 값이 없다.
"""
import json
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import HOME_TREND_WORDS_FILE, MAX_TREND_WORDS


def load_trend_words() -> list[str]:
    """저장된 단어 목록을 순서 그대로 돌려준다. 없거나 깨졌으면 빈 목록(=아직 안 고른 상태)."""
    if not HOME_TREND_WORDS_FILE.exists():
        return []
    try:
        data = json.loads(HOME_TREND_WORDS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return []
    words = data.get("words") if isinstance(data, dict) else None
    return [w for w in words if isinstance(w, str) and w.strip()] if isinstance(words, list) else []


def _save(words: list[str]) -> None:
    atomic_write_text(HOME_TREND_WORDS_FILE, json.dumps({"words": words}, ensure_ascii=False))


def add_trend_word(word: str) -> tuple[bool, Optional[str]]:
    """단어를 추가한다. (성공 여부, 실패 사유) — 실패해도 예외를 내지 않는다(설정 저장과 같은 관례).

    이미 MAX_TREND_WORDS개면 거절한다(가장 오래된 것을 밀어내지 않는다) — 어떤 걸
    뺄지는 화면이 마음대로 정할 일이 아니라 담당자가 ×로 직접 골라야 한다.
    """
    word = (word or "").strip()
    if not word:
        return False, "빈 단어는 추가할 수 없습니다."
    words = load_trend_words()
    if word in words:
        return False, f'이미 추가된 단어입니다: "{word}"'
    if len(words) >= MAX_TREND_WORDS:
        return False, f"최대 {MAX_TREND_WORDS}개까지만 지켜볼 수 있습니다. 하나를 먼저 빼주세요."
    words.append(word)
    _save(words)
    return True, None


def remove_trend_word(word: str) -> bool:
    """단어를 뺀다. 목록에 없었으면 아무 일도 안 하고 False."""
    words = load_trend_words()
    if word not in words:
        return False
    words.remove(word)
    _save(words)
    return True
