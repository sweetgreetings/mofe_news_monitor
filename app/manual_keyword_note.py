# Design Ref: 사용자 요청(2026-08-05) — "+ 직접 키워드 작성하기" 버튼으로 이용자가 자유
# 서식으로 적어두는 메모/키워드 한 줄. AI가 추출한 하단 키워드 블록과는 완전히 별개다.
import json

from app.atomic_write import atomic_write_text
from app.config import MANUAL_KEYWORD_NOTE_FILE


def load_manual_keyword_note() -> str:
    """저장된 메모 텍스트를 읽어온다. 파일이 없거나 형식이 이상하면 빈 문자열(작성 전 상태)."""
    if not MANUAL_KEYWORD_NOTE_FILE.exists():
        return ""
    try:
        data = json.loads(MANUAL_KEYWORD_NOTE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return ""
    text = data.get("text", "") if isinstance(data, dict) else ""
    return text if isinstance(text, str) else ""


def save_manual_keyword_note(text: str) -> None:
    """메모 텍스트를 저장한다. 빈 문자열로 저장하면 "작성 전" 상태로 되돌아간다(버튼이
    다시 "+ 직접 키워드 작성하기"로 보이고, 내보내기 텍스트에도 안 실린다)."""
    atomic_write_text(MANUAL_KEYWORD_NOTE_FILE, json.dumps({"text": text.strip()}, ensure_ascii=False))
