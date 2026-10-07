"""사용자 계정 저장소 — 이메일·비밀번호와, 비밀번호를 잊었을 때 쓰는 값들.

이 모듈은 **「자물쇠」 단계**의 것이다. 계정을 만들고 비밀번호를 맞히는 일만 하고,
데이터를 사람별로 가르는 일(`data/users/{id}/…`)은 아직 하지 않는다 —
그건 저장 한 겹을 넣은 다음 일이다(MULTIUSER_ROLLOUT_CHECKLIST.md 2단계 순서).

**외부 패키지를 쓰지 않는다.** 해시는 표준 라이브러리 `hashlib.pbkdf2_hmac`이다.
의존성이 넷뿐인 저장소라, 자물쇠 하나 때문에 설치 단계를 늘리지 않는다.
나중에 Postgres로 옮길 때 argon2로 갈아끼우려면 `hash_secret`·`verify_secret`
**두 함수만** 고치면 된다(저장 형식 앞에 알고리즘 이름이 붙어 있어 섞여 있어도 읽힌다).

**비밀번호와 「질문의 답」은 같은 물건으로 다룬다** — 둘 다 해시로만 저장하고
다시 꺼내 볼 수 없다. 답을 평문으로 두면 파일을 열어 본 사람이 곧 그 계정이다.
"""
import hashlib
import json
import secrets
import threading
import unicodedata
from datetime import datetime
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import AUTH_MIN_PASSWORD_LEN, USERS_FILE

# pbkdf2-sha256 반복 횟수. OWASP 권고값(600,000)이고, 맥에서 한 번 계산에 0.2초쯤이라
# 로그인 한 번에는 느껴지지 않으면서 사전 대입은 충분히 비싸진다.
_PBKDF2_ROUNDS = 600_000
_SALT_BYTES = 16

_LOCK = threading.Lock()


class UserError(Exception):
    """담당자에게 그대로 보여줄 수 있는 거부 사유."""


# ── 값 다듬기 ────────────────────────────────────────────────────────────
# 가입·찾기·변경 세 자리가 **같은 함수**를 봐야 한다. 한 자리만 다르게 다듬으면
# 가입할 때 적은 값과 찾을 때 적은 값이 영영 안 맞는다.

def normalize_email(raw: str) -> str:
    return unicodedata.normalize("NFKC", (raw or "")).strip().lower()


def normalize_phone(raw: str) -> str:
    """숫자만 남긴다 — `010-1234-5678`과 `01012345678`이 갈리면 안 된다."""
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def normalize_answer(raw: str) -> str:
    """질문의 답을 대조할 모양으로 — 대소문자를 가리지 않고, 공백은 떼고 본다.

    **적을 때는 띄어쓰기를 막고(`validate_answer`), 맞힐 때는 떼고 본다.**
    쓰는 쪽에 엄하고 읽는 쪽에 너그럽다 — 저장된 답에 띄어쓰기가 없으니
    맞힐 때 공백이 끼어도 본인이 잠길 이유가 없다.
    """
    text = unicodedata.normalize("NFKC", (raw or "")).lower()
    return "".join(text.split())


def validate_answer(raw: str) -> str:
    """새로 적는 답을 검사하고 다듬어 돌려준다. 띄어쓰기가 있으면 거부한다 —
    「서울 초등」과 「서울초등」 중 무엇을 적었는지 담당자가 기억해야 하는 상황을
    아예 만들지 않는다."""
    text = unicodedata.normalize("NFKC", (raw or "")).strip()
    if not text:
        raise UserError("질문의 답을 적어 주세요.")
    if any(ch.isspace() for ch in text):
        raise UserError("답은 띄어쓰기 없이 붙여서 적어 주세요.")
    return text


# ── 해시 ─────────────────────────────────────────────────────────────────

def hash_secret(raw: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_secret(raw: str, stored: str) -> bool:
    """맞으면 True. 저장값이 깨졌어도 예외를 내지 않고 False다(로그인 화면이 멈추면 안 된다)."""
    try:
        algo, rounds, salt_hex, digest_hex = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", raw.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
    except (ValueError, AttributeError):
        return False
    # 비교 시간이 값에 따라 달라지지 않게 한다.
    return secrets.compare_digest(digest.hex(), digest_hex)


# ── 파일 ─────────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"users": []}
    if not isinstance(data, dict) or not isinstance(data.get("users"), list):
        return {"users": []}
    return data


def _save(data: dict) -> None:
    atomic_write_text(USERS_FILE, json.dumps(data, ensure_ascii=False, indent=2))


def list_users() -> list[dict]:
    return _load()["users"]


def user_count() -> int:
    return len(list_users())


def get_user(user_id: str) -> Optional[dict]:
    for user in list_users():
        if user.get("id") == user_id:
            return user
    return None


def find_by_email(email: str) -> Optional[dict]:
    target = normalize_email(email)
    for user in list_users():
        if normalize_email(user.get("email", "")) == target:
            return user
    return None


# ── 만들기·고치기 ─────────────────────────────────────────────────────────

def validate_password(password: str) -> None:
    if len(password or "") < AUTH_MIN_PASSWORD_LEN:
        raise UserError(f"비밀번호는 {AUTH_MIN_PASSWORD_LEN}자 이상이어야 합니다.")


def create_user(
    email: str,
    password: str,
    display_name: str,
    phone: str,
    question: str,
    answer: str,
    department: str = "",
) -> dict:
    """새 계정을 만들어 돌려준다. **첫 사용자가 관리자**가 된다."""
    email_n = normalize_email(email)
    if "@" not in email_n or "." not in email_n.split("@")[-1]:
        raise UserError("이메일 주소를 확인해 주세요.")
    validate_password(password)
    if not (display_name or "").strip():
        raise UserError("이름을 적어 주세요.")
    if not normalize_phone(phone):
        raise UserError("핸드폰번호를 적어 주세요.")
    answer = validate_answer(answer)

    with _LOCK:
        data = _load()
        if any(normalize_email(u.get("email", "")) == email_n for u in data["users"]):
            raise UserError("이미 가입된 이메일입니다.")
        now = datetime.now().isoformat(timespec="seconds")
        user = {
            "id": secrets.token_hex(8),
            "email": email_n,
            "password_hash": hash_secret(password),
            "display_name": display_name.strip(),
            # 부서는 선택이다 — 누구인지 화면에 보이려고 받는 것이라 비워 둘 수 있다.
            "department": (department or "").strip(),
            "phone": normalize_phone(phone),
            "security_question": question,
            "security_answer_hash": hash_secret(normalize_answer(answer)),
            # 첫 사용자가 관리자다 — 아무도 없는 앱에 승인해 줄 사람이 없다.
            "role": "admin" if not data["users"] else "member",
            "created_at": now,
            "password_changed_at": now,
        }
        data["users"].append(user)
        _save(data)
    return user


def _update(user_id: str, changes: dict) -> None:
    with _LOCK:
        data = _load()
        for user in data["users"]:
            if user.get("id") == user_id:
                user.update(changes)
                _save(data)
                return
    raise UserError("계정을 찾을 수 없습니다.")


def set_password(user_id: str, new_password: str) -> None:
    validate_password(new_password)
    _update(user_id, {
        "password_hash": hash_secret(new_password),
        "password_changed_at": datetime.now().isoformat(timespec="seconds"),
    })


def update_profile(
    user_id: str,
    display_name: Optional[str] = None,
    department: Optional[str] = None,
    phone: Optional[str] = None,
    question: Optional[str] = None,
    answer: Optional[str] = None,
) -> None:
    """빈 값으로 넘긴 칸은 건드리지 않는다 — 특히 `answer`는 **비워 두면 그대로**다
    (지금 답을 보여줄 수 없으니, 화면이 매번 다시 적게 할 수는 없다).
    예외는 부서 — 선택 칸이라 비우면 비운 대로 지운다.

    **질문을 바꾸면서 답을 비워 두는 것은 막는다.** 그대로 두면 새 질문에 옛 답이
    짝지어져, 담당자가 자기 질문에 자기 답으로는 영영 못 들어간다.
    """
    user = get_user(user_id)
    if not user:
        raise UserError("계정을 찾을 수 없습니다.")

    changes: dict = {}
    if display_name is not None and display_name.strip():
        changes["display_name"] = display_name.strip()
    if department is not None:
        changes["department"] = department.strip()
    if phone is not None and normalize_phone(phone):
        changes["phone"] = normalize_phone(phone)

    answer_given = bool((answer or "").strip())
    if question and question != user.get("security_question"):
        if not answer_given:
            raise UserError("질문을 바꾸려면 새 답도 함께 적어 주세요 — 옛 답은 새 질문과 짝이 맞지 않습니다.")
        changes["security_question"] = question
    if answer_given:
        changes["security_answer_hash"] = hash_secret(normalize_answer(validate_answer(answer)))
    if changes:
        _update(user_id, changes)


# ── 맞히기 ───────────────────────────────────────────────────────────────

def verify_login(email: str, password: str) -> Optional[dict]:
    user = find_by_email(email)
    if user and verify_secret(password, user.get("password_hash", "")):
        return user
    return None


def verify_recovery(email: str, phone: str) -> Optional[dict]:
    """비밀번호 찾기 1단계 — 이메일과 핸드폰번호가 **둘 다** 맞아야 한다."""
    user = find_by_email(email)
    if user and user.get("phone") and user["phone"] == normalize_phone(phone):
        return user
    return None


def verify_answer(user: dict, answer: str) -> bool:
    return verify_secret(normalize_answer(answer), user.get("security_answer_hash", ""))
