# Design Ref: 사용자 요청(2026-08-06) — 이메일 전송 받는 사람 여러 명 관리(켜고 끄기 토글)
import json

from app.atomic_write import atomic_write_text
from app.config import EMAIL_RECIPIENTS_FILE


def load_email_recipients() -> list:
    """등록된 받는 사람 목록을 읽어온다(순서: 등록한 순서 그대로).

    각 항목은 {"name": str, "email": str, "enabled": bool} 형태다. enabled가 꺼진
    사람은 목록에는 남아있지만 실제 전송(active_recipient_emails) 대상에서는 빠진다 —
    삭제 없이 "잠깐 안 보내기"를 할 수 있게 하기 위해서다(예: 휴가 기간에만 다른
    사람을 켜두고 본인은 꺼두기).
    """
    if not EMAIL_RECIPIENTS_FILE.exists():
        return []
    try:
        data = json.loads(EMAIL_RECIPIENTS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return []
    return data if isinstance(data, list) else []


def save_email_recipients(recipients: list) -> None:
    """받는 사람 목록 전체를 통째로 저장한다 — 설정 화면의 다른 목록들(검색 키워드,
    형광펜 단어 등)과 같은 패턴으로, "del로 지운 칸은 저장 폼에서 아예 빠진 채로
    넘어오고, 서버는 그걸 검증 없이 그대로 반영"하는 방식이다.
    """
    cleaned = [
        {"name": (r.get("name") or r.get("email", "")).strip(), "email": r["email"].strip(), "enabled": bool(r.get("enabled"))}
        for r in recipients
        if r.get("email", "").strip()
    ]
    atomic_write_text(EMAIL_RECIPIENTS_FILE, json.dumps(cleaned, ensure_ascii=False, indent=2))


def active_recipient_emails() -> list:
    """실제로 전송해야 할 켜진 사람들의 이메일 주소만 뽑아 돌려준다."""
    return [r["email"] for r in load_email_recipients() if r.get("enabled", True)]
