# Design Ref: 사용자 요청(2026-08-20) — 배포본을 받는 사람마다 자기 네이버·Claude API
# 키를 .env 파일을 직접 열지 않고 설정 화면(/naver, /llm)에서 등록할 수 있게 한다.
#
# 저장 우선순위는 "설정 화면에 저장한 값 > .env"다 — 이미 .env로 쓰던 사람은 이 기능이
# 생겨도 아무것도 안 해도 그대로 돌아가고, 설정 화면에 값을 넣는 순간부터 그 값이
# 우선한다. 두 값의 출처(saved/env/none)를 always 구분해 돌려주는 이유는, 화면이
# ".env 걸 쓰는 중"이라는 사실을 배지로 보여줘야(CREDENTIALS_MOCKUP.html 참고) "설정
# 화면은 비어 있는데 왜 앱은 잘 돌지?"라는 혼란을 막을 수 있어서다.
#
# 네이버와 AI 키를 같은 파일(data/credentials.json)에 담는다 — 둘 다 저장·마스킹·연결
# 테스트·삭제 로직이 완전히 같아서 모듈을 나눌 이유가 없고, 파일을 나누면 두 화면
# 사이에 저장 시점 경쟁(atomic_write_text가 파일 단위로만 원자적)만 하나 더 생긴다.
import json
import stat
from datetime import datetime
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import (
    ANTHROPIC_API_KEY,
    CREDENTIALS_FILE,
    EMAIL_SENDER_ADDRESS,
    EMAIL_SENDER_PASSWORD,
    EMAIL_SERVICES,
    EMAIL_SMTP_HOST,
    EMAIL_SMTP_PORT,
    LLM_MODEL,
    NAVER_CLIENT_ID,
    NAVER_CLIENT_SECRET,
    TELEGRAM_BOT_TOKEN,
)


def _load() -> dict:
    if not CREDENTIALS_FILE.exists():
        return {}
    try:
        data = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    atomic_write_text(CREDENTIALS_FILE, json.dumps(data, ensure_ascii=False, indent=2))
    # [추가: 2026-08-20] 비밀 값이 담긴 파일이라 소유자만 읽고 쓸 수 있게 권한을
    # 좁힌다 — 이 저장소가 이런 값을 다루는 첫 사례다. chmod가 안 되는 환경(윈도우
    # 일부 파일시스템)에서는 그냥 건너뛴다: 이 앱의 타겟이 원래 단일 로컬 사용자라
    # 권한 좁히기가 실패해도 보안 성격이 달라지지 않는다(다른 저장 파일들도 권한을
    # 안 좁힌다).
    try:
        CREDENTIALS_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def mask(value: str, keep_prefix: int = 0, keep_suffix: int = 4) -> str:
    """비밀 값을 화면에 보여줄 때 쓰는 마스킹 — 앞 keep_prefix자·뒤 keep_suffix자만
    남기고 가운데를 점으로 채운다. 값이 짧으면(합쳐서 keep보다 짧으면) 통째로 가린다."""
    value = value or ""
    if len(value) <= keep_prefix + keep_suffix:
        return "•" * max(len(value), 4)
    return value[:keep_prefix] + "•" * 10 + value[len(value) - keep_suffix :]


# ── 네이버 검색 API ──────────────────────────────────────────────────────

def _naver_saved() -> dict:
    return _load().get("naver", {}) or {}


def naver_client_id() -> Optional[str]:
    return (_naver_saved().get("client_id") or "").strip() or NAVER_CLIENT_ID or None


def naver_client_secret() -> Optional[str]:
    return (_naver_saved().get("client_secret") or "").strip() or NAVER_CLIENT_SECRET or None


def naver_is_configured() -> bool:
    return bool(naver_client_id() and naver_client_secret())


def naver_source() -> str:
    """지금 쓰이는 값이 어디서 왔는지 — "saved"(설정 화면) / "env"(.env) / "none"."""
    saved = _naver_saved()
    if saved.get("client_id") or saved.get("client_secret"):
        return "saved"
    if NAVER_CLIENT_ID or NAVER_CLIENT_SECRET:
        return "env"
    return "none"


def naver_saved_at() -> Optional[str]:
    return _naver_saved().get("saved_at")


def naver_saved_client_secret() -> str:
    """저장소에 실제로 저장된 값만(.env 폴백 없이) — 설정 화면이 "비밀키 칸을 비워
    두면 기존 값 유지"를 판단할 때 쓴다. .env로 폴백해버리면 그 값이 저장 파일에
    그대로 옮겨 적히는데, 사용자가 타이핑하지 않은 값이 저장되는 건 이상하다."""
    return (_naver_saved().get("client_secret") or "").strip()


def save_naver_credentials(client_id: str, client_secret: str) -> None:
    """설정 화면이 넘겨준 값을 그대로 저장한다 — "비워두면 기존 값 유지" 같은 판단은
    화면 쪽(app.settings_server)이 이미 병합해서 넘겨준다(마스킹된 값을 이 모듈이
    되받을 방법이 없으므로, 그 판단은 여기서 할 수 없다)."""
    data = _load()
    data["naver"] = {
        "client_id": (client_id or "").strip(),
        "client_secret": (client_secret or "").strip(),
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }
    _save(data)


def delete_naver_credentials() -> None:
    """설정에 저장된 값만 지운다 — .env 값이 있으면 그쪽으로 자동 폴백된다."""
    data = _load()
    data.pop("naver", None)
    _save(data)


# ── AI 연동 (Claude) ─────────────────────────────────────────────────────

def _llm_saved() -> dict:
    return _load().get("llm", {}) or {}


def llm_api_key() -> Optional[str]:
    return (_llm_saved().get("api_key") or "").strip() or ANTHROPIC_API_KEY or None


def llm_model() -> str:
    """[수정: 2026-08-20] 예전엔 /llm 화면에서 사용자가 고른 값을 저장소에서 읽었다 —
    그 라디오를 뺐다(app.config.LLM_MODEL 주석 참고). 모델은 이제 항상 app.config의
    상수 하나뿐이라 이 함수는 그 값을 그대로 돌려준다(호출부 app.llm_classifier가
    이 이름을 그대로 쓰므로 함수 자체는 남겨둔다)."""
    return LLM_MODEL


def llm_is_configured() -> bool:
    return bool(llm_api_key())


def llm_source() -> str:
    saved = _llm_saved()
    if saved.get("api_key"):
        return "saved"
    if ANTHROPIC_API_KEY:
        return "env"
    return "none"


def llm_saved_at() -> Optional[str]:
    return _llm_saved().get("saved_at")


def llm_saved_api_key() -> str:
    """naver_saved_client_secret과 같은 이유 — .env 폴백 없이 저장소 원본만."""
    return (_llm_saved().get("api_key") or "").strip()


def save_llm_credentials(api_key: str) -> None:
    data = _load()
    data["llm"] = {
        "api_key": (api_key or "").strip(),
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }
    _save(data)


def delete_llm_credentials() -> None:
    data = _load()
    data.pop("llm", None)
    _save(data)


# ── 이메일 보내는 계정 ───────────────────────────────────────────────────
# [추가: 2026-09-18] 네이버·AI 키와 같은 규칙 — 설정 화면에 저장한 값 > .env. 저장값은
# 서비스(naver/gmail)·주소·앱 비밀번호·보내는 사람 이름(선택)이고, 서버·포트는 서비스에서
# 정해진다(EMAIL_SERVICES). 보내는 사람 이름은 .env에 없던 값이라 저장값에서만 읽는다.

def _email_saved() -> dict:
    return _load().get("email", {}) or {}


def email_service() -> Optional[str]:
    """지금 쓰는 메일 서비스 키 — 저장값, 없으면 .env 서버 이름으로 짐작. 모르는 서버면 None."""
    saved = _email_saved().get("service")
    if saved in EMAIL_SERVICES:
        return saved
    host = (EMAIL_SMTP_HOST or "").lower()
    for key, spec in EMAIL_SERVICES.items():
        if host == spec["host"]:
            return key
    return None


def email_smtp_host() -> Optional[str]:
    saved = _email_saved().get("service")
    if saved in EMAIL_SERVICES:
        return EMAIL_SERVICES[saved]["host"]
    return EMAIL_SMTP_HOST or None


def email_smtp_port() -> int:
    saved = _email_saved().get("service")
    if saved in EMAIL_SERVICES:
        return EMAIL_SERVICES[saved]["port"]
    return EMAIL_SMTP_PORT


def email_sender_address() -> Optional[str]:
    if _email_saved().get("service"):
        return (_email_saved().get("address") or "").strip() or None
    return EMAIL_SENDER_ADDRESS or None


def email_sender_password() -> Optional[str]:
    if _email_saved().get("service"):
        return (_email_saved().get("password") or "").strip() or None
    return EMAIL_SENDER_PASSWORD or None


def email_sender_name() -> str:
    return (_email_saved().get("name") or "").strip()


def email_is_configured() -> bool:
    return bool(email_smtp_host() and email_sender_address() and email_sender_password())


def email_source() -> str:
    """"saved"(설정 화면) / "env"(.env) / "none" — naver_source와 같은 뜻."""
    if _email_saved().get("service"):
        return "saved"
    if EMAIL_SMTP_HOST or EMAIL_SENDER_ADDRESS or EMAIL_SENDER_PASSWORD:
        return "env"
    return "none"


def email_saved_at() -> Optional[str]:
    return _email_saved().get("saved_at")


def email_saved_password() -> str:
    """naver_saved_client_secret과 같은 이유 — .env 폴백 없이 저장소 원본만."""
    return (_email_saved().get("password") or "").strip()


def save_email_credentials(service: str, address: str, password: str, name: str) -> None:
    data = _load()
    data["email"] = {
        "service": service,
        "address": (address or "").strip(),
        "password": (password or "").strip(),
        "name": (name or "").strip(),
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }
    _save(data)


def delete_email_credentials() -> None:
    data = _load()
    data.pop("email", None)
    _save(data)


# ── 텔레그램 발송 계정(봇 토큰) ──────────────────────────────────────────
# 네이버·AI 키와 같은 규칙 — 설정 화면에 저장한 값 > .env. 토큰을 읽는 곳(app.telegram_bot,
# app.telegram_bot_name)은 모듈 상수가 아니라 이 함수를 부를 때마다 읽는다(저장 즉시 반영).

def _telegram_saved() -> dict:
    return _load().get("telegram", {}) or {}


def telegram_bot_token() -> Optional[str]:
    return (_telegram_saved().get("bot_token") or "").strip() or TELEGRAM_BOT_TOKEN or None


def telegram_source() -> str:
    """"saved"(설정 화면) / "env"(.env) / "none" — naver_source와 같은 뜻."""
    if _telegram_saved().get("bot_token"):
        return "saved"
    if TELEGRAM_BOT_TOKEN:
        return "env"
    return "none"


def telegram_saved_at() -> Optional[str]:
    return _telegram_saved().get("saved_at")


def telegram_saved_token() -> str:
    """naver_saved_client_secret과 같은 이유 — .env 폴백 없이 저장소 원본만."""
    return (_telegram_saved().get("bot_token") or "").strip()


def save_telegram_token(bot_token: str) -> None:
    data = _load()
    data["telegram"] = {
        "bot_token": (bot_token or "").strip(),
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }
    _save(data)


def delete_telegram_token() -> None:
    data = _load()
    data.pop("telegram", None)
    _save(data)
