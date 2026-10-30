"""로그인 세션과 잠금, 그리고 「로그인 사용」 스위치.

**스위치는 기본 꺼짐이다.** 꺼져 있으면 서버는 예전과 똑같이 아무나 들여보낸다 —
자물쇠를 다는 공사가 본업(수집·발송)을 멈추면 안 된다. 스케줄러·발송은 이 모듈을
아예 거치지 않으므로, 켜든 끄든 수집과 자동발송은 그대로 돈다.

**쿠키엔 원본 토큰, 파일엔 해시만 적는다.** `data/`를 열어 본 사람이 남의 세션을
그대로 쓸 수 있으면 자물쇠가 아니다. 사용자 기록(`users.json`)의 비밀번호·답과 같은 원칙이다.
"""
import hashlib
import secrets
import threading
from datetime import datetime, timedelta
from typing import Optional

from app import store
from app.config import (
    AUTH_ATTEMPTS_FILE,
    AUTH_CONFIG_FILE,
    AUTH_SESSION_DAYS_KEEP,
    AUTH_SESSION_HOURS,
    AUTH_SESSIONS_FILE,
)

_LOCK = threading.Lock()


def _read(path, empty: dict) -> dict:
    try:
        data = store.read_json(path)
    except (OSError, ValueError):
        return dict(empty)
    return data if isinstance(data, dict) else dict(empty)


def _write(path, data: dict) -> None:
    store.write_json(path, data, indent=2)


# ── 「로그인 사용」 스위치 ────────────────────────────────────────────────

def is_login_required() -> bool:
    return bool(_read(AUTH_CONFIG_FILE, {}).get("login_required", False))


def set_login_required(value: bool) -> None:
    with _LOCK:
        data = _read(AUTH_CONFIG_FILE, {})
        data["login_required"] = bool(value)
        _write(AUTH_CONFIG_FILE, data)


# ── 세션 ─────────────────────────────────────────────────────────────────

def _token_key(raw_token: str) -> str:
    return hashlib.sha256((raw_token or "").encode("utf-8")).hexdigest()


def create_session(user_id: str, keep: bool) -> tuple[str, int]:
    """세션을 만들고 `(쿠키에 담을 원본 토큰, 쿠키 수명 초)`를 돌려준다."""
    raw = secrets.token_urlsafe(32)
    span = timedelta(days=AUTH_SESSION_DAYS_KEEP) if keep else timedelta(hours=AUTH_SESSION_HOURS)
    now = datetime.now()
    with _LOCK:
        data = _read(AUTH_SESSIONS_FILE, {"sessions": {}})
        sessions = data.setdefault("sessions", {})
        _drop_expired(sessions, now)
        sessions[_token_key(raw)] = {
            "user_id": user_id,
            "expires_at": (now + span).isoformat(timespec="seconds"),
            "last_seen_at": now.isoformat(timespec="seconds"),
        }
        _write(AUTH_SESSIONS_FILE, data)
    return raw, int(span.total_seconds())


def _drop_expired(sessions: dict, now: datetime) -> None:
    for key in [k for k, v in sessions.items() if _expired(v, now)]:
        del sessions[key]


def _expired(entry: dict, now: datetime) -> bool:
    try:
        return datetime.fromisoformat(entry.get("expires_at", "")) <= now
    except ValueError:
        return True  # 읽을 수 없는 기록은 만료로 본다(틀려도 "못 들어감" 쪽으로 틀린다).


def user_id_for_token(raw_token: str) -> Optional[str]:
    if not raw_token:
        return None
    entry = _read(AUTH_SESSIONS_FILE, {"sessions": {}}).get("sessions", {}).get(_token_key(raw_token))
    if not entry or _expired(entry, datetime.now()):
        return None
    return entry.get("user_id")


def destroy_session(raw_token: str) -> None:
    with _LOCK:
        data = _read(AUTH_SESSIONS_FILE, {"sessions": {}})
        if data.get("sessions", {}).pop(_token_key(raw_token), None) is not None:
            _write(AUTH_SESSIONS_FILE, data)


def destroy_user_sessions(user_id: str) -> None:
    """그 사람의 로그인을 전부 끊는다 — 비밀번호가 바뀌면 옛 비밀번호로 열어 둔
    다른 기기의 창도 함께 닫혀야 한다(화면이 그렇게 약속한다)."""
    with _LOCK:
        data = _read(AUTH_SESSIONS_FILE, {"sessions": {}})
        sessions = data.setdefault("sessions", {})
        for key in [k for k, v in sessions.items() if v.get("user_id") == user_id]:
            del sessions[key]
        _write(AUTH_SESSIONS_FILE, data)


# ── 잠금 ─────────────────────────────────────────────────────────────────
# key는 `"login:{이메일}"` / `"recover:{이메일}"` 처럼 **무엇을 하다 틀렸는지**까지
# 담는다. 로그인에서 틀린 횟수가 찾기 쪽 잠금을 당기면 안 된다.

def lock_remaining_sec(key: str) -> int:
    entry = _read(AUTH_ATTEMPTS_FILE, {"attempts": {}}).get("attempts", {}).get(key)
    if not entry or not entry.get("locked_until"):
        return 0
    try:
        left = datetime.fromisoformat(entry["locked_until"]) - datetime.now()
    except ValueError:
        return 0
    return max(0, int(left.total_seconds()))


def record_fail(key: str, max_fails: int, lock_min: int) -> int:
    """한 번 틀린 것을 적고 **남은 횟수**를 돌려준다. 0이면 방금 잠겼다는 뜻이다."""
    with _LOCK:
        data = _read(AUTH_ATTEMPTS_FILE, {"attempts": {}})
        attempts = data.setdefault("attempts", {})
        entry = attempts.setdefault(key, {"fails": 0, "locked_until": None})
        entry["fails"] = int(entry.get("fails", 0)) + 1
        remaining = max(0, max_fails - entry["fails"])
        if remaining == 0:
            entry["locked_until"] = (
                datetime.now() + timedelta(minutes=lock_min)
            ).isoformat(timespec="seconds")
            entry["fails"] = 0
        _write(AUTH_ATTEMPTS_FILE, data)
    return remaining


def clear_fails(key: str) -> None:
    with _LOCK:
        data = _read(AUTH_ATTEMPTS_FILE, {"attempts": {}})
        if data.get("attempts", {}).pop(key, None) is not None:
            _write(AUTH_ATTEMPTS_FILE, data)


# ── 비밀번호 찾기 표 ──────────────────────────────────────────────────────
# 1단계를 통과한 사람에게만 질문을 보여주고, 2단계를 통과한 사람에게만 새 비밀번호
# 칸을 준다. 그 「통과했다」를 화면이 들고 다니게 하면 안 되므로(숨은 칸에 이메일을
# 적어 보내면 누구나 3단계로 바로 갈 수 있다) 서버가 표를 끊어 주고 단계를 적어 둔다.
# 10분이면 끊긴다 — 중간에 멈춘 찾기가 오래 열려 있을 이유가 없다.
_TICKET_MIN = 10


def issue_ticket(user_id: str, stage: str) -> str:
    raw = secrets.token_urlsafe(24)
    with _LOCK:
        data = _read(AUTH_SESSIONS_FILE, {"sessions": {}})
        tickets = data.setdefault("tickets", {})
        now = datetime.now()
        for key in [k for k, v in tickets.items() if _expired(v, now)]:
            del tickets[key]
        tickets[_token_key(raw)] = {
            "user_id": user_id,
            "stage": stage,
            "expires_at": (now + timedelta(minutes=_TICKET_MIN)).isoformat(timespec="seconds"),
        }
        _write(AUTH_SESSIONS_FILE, data)
    return raw


def ticket_user_id(raw_ticket: str, stage: str) -> Optional[str]:
    """그 단계의 표가 맞으면 사용자 id, 아니면 None."""
    if not raw_ticket:
        return None
    entry = _read(AUTH_SESSIONS_FILE, {"sessions": {}}).get("tickets", {}).get(_token_key(raw_ticket))
    if not entry or entry.get("stage") != stage or _expired(entry, datetime.now()):
        return None
    return entry.get("user_id")


def drop_ticket(raw_ticket: str) -> None:
    with _LOCK:
        data = _read(AUTH_SESSIONS_FILE, {"sessions": {}})
        if data.get("tickets", {}).pop(_token_key(raw_ticket), None) is not None:
            _write(AUTH_SESSIONS_FILE, data)
