# Design Ref: 사용자 요청(2026-08-20) — 배포본을 받는 사람마다 자기 네이버·Claude API
# 키를 .env 파일을 직접 열지 않고 설정 화면(/naver, /llm)에서 등록할 수 있게 한다.
#
# 저장 우선순위는 "설정 화면에 저장한 값 > .env"다 — 이미 .env로 쓰던 사람은 이 기능이
# 생겨도 아무것도 안 해도 그대로 돌아가고, 설정 화면에 값을 넣는 순간부터 그 값이
# 우선한다. 두 값의 출처(saved/env/none)를 always 구분해 돌려주는 이유는, 화면이
# ".env 걸 쓰는 중"이라는 사실을 배지로 보여줘야 "설정 화면은 비어 있는데
# 왜 앱은 잘 돌지?"라는 혼란을 막을 수 있어서다.
#
# 네이버와 AI 키를 같은 파일(data/credentials.json)에 담는다 — 둘 다 저장·마스킹·연결
# 테스트·삭제 로직이 완전히 같아서 모듈을 나눌 이유가 없고, 파일을 나누면 두 화면
# 사이에 저장 시점 경쟁(atomic_write_text가 파일 단위로만 원자적)만 하나 더 생긴다.
import json
import logging
import re
import shutil
import stat
from datetime import datetime
from pathlib import Path
from typing import Optional

from app import store
from app.config import (
    ANTHROPIC_API_KEY,
    BASE_DIR,
    CREDENTIALS_FILE,
    EMAIL_SENDER_ADDRESS,
    EMAIL_SENDER_PASSWORD,
    EMAIL_SERVICES,
    EMAIL_SMTP_HOST,
    EMAIL_SMTP_PORT,
    ENV_BACKUP_RETENTION_DAYS,
    LLM_MODEL,
    NAVER_CLIENT_ID,
    NAVER_CLIENT_SECRET,
    TELEGRAM_BOT_TOKEN,
)

logger = logging.getLogger(__name__)

ENV_FILE = BASE_DIR / ".env"


def _load() -> dict:
    if not CREDENTIALS_FILE.exists():
        return {}
    try:
        data = store.read_json(CREDENTIALS_FILE)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    store.write_json(CREDENTIALS_FILE, data, indent=2)
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


# ── .env 씨앗 옮겨심기 ───────────────────────────────────────────────────
# [추가: 2026-09-28] 키가 사는 자리를 한 곳(data/credentials.json)으로 모은다.
#
# 왜 필요한가: 읽는 우선순위가 "설정 화면 > .env"라, 설정 화면에서 키를 바꿔도 .env의
# 옛 키는 그대로 남는다. 평소엔 앱이 안 보니 문제가 없지만, 나중에 설정 화면에서 키를
# 지우거나 비우면 앱이 .env로 내려가 **이미 폐기한 죽은 키를 집는다.** 그러면 네이버
# 수집이 401로 멈추거나 AI 분류가 조용히 규칙 기반으로 떨어진다 — 몇 달 뒤에 터지면
# 원인을 찾기가 어렵다. 다섯 키(네이버 둘·Claude·텔레그램·이메일) 전부 같은 구조다.
#
# .env의 역할은 **처음 설치할 때 키를 한 번 넣어 주는 씨앗**이다. 그래서 앱을 켤 때 한 번,
# .env에 남은 키를 설정 파일로 옮겨 심고 .env의 그 줄은 지운다. 새 컴퓨터로 옮길 때
# .env에 키를 적어 두면 여전히 씨앗 역할을 한다 — 첫 실행에 옮겨 심고 비워진다.
#
# 읽는 우선순위(설정 > .env)는 그대로다. 옮겨심기가 실패해도 앱은 예전과 똑같이 돈다.

_ENV_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")

# 옮겨 심을 키 이름 — TELEGRAM_CHAT_ID는 여기 없다. 그건 키가 아니라 "받는 사람" 목록의
# 씨앗이고(app.telegram_recipients가 최초 1회 "나"로 심는다), 이 모듈이 읽지도 않는다.
_ENV_NAVER = ("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET")
_ENV_LLM = ("ANTHROPIC_API_KEY",)
_ENV_TELEGRAM = ("TELEGRAM_BOT_TOKEN",)
_ENV_EMAIL = ("EMAIL_SMTP_HOST", "EMAIL_SMTP_PORT", "EMAIL_SENDER_ADDRESS", "EMAIL_SENDER_PASSWORD")
ENV_KEY_NAMES = _ENV_NAVER + _ENV_LLM + _ENV_TELEGRAM + _ENV_EMAIL


def _unquote(raw: str) -> str:
    """.env 값에서 감싼 따옴표를 뗀다 — python-dotenv가 읽는 방식과 맞춘다."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _read_env_lines() -> tuple[list[str], dict[str, tuple[int, str]]]:
    """.env 원문 줄들과 {키 이름: (줄 번호, 값)}. 값이 빈 줄은 담지 않는다(옮길 게 없다)."""
    try:
        lines = store.read_text(ENV_FILE).splitlines()
    except OSError:
        return [], {}
    found: dict[str, tuple[int, str]] = {}
    for i, line in enumerate(lines):
        match = _ENV_ASSIGN_RE.match(line)
        if not match:
            continue
        name, raw = match.group(1), match.group(2)
        value = _unquote(raw)
        if name in ENV_KEY_NAMES and value:
            found[name] = (i, value)
    return lines, found


def env_leftover_keys() -> list[str]:
    """.env에 아직 값이 남아 있는 키 이름 — 「키 보관 상태」 화면이 읽는다.
    옮겨심기가 정상으로 끝났으면 빈 목록이다."""
    return [name for name in ENV_KEY_NAMES if name in _read_env_lines()[1]]


def env_backup_files() -> list:
    """옮겨심기가 남긴 .env 백업 파일들(최신 먼저) — 그 안엔 옛 키가 그대로 있다."""
    try:
        return sorted(BASE_DIR.glob(".env.backup-*"), reverse=True)
    except OSError:
        return []


def cleanup_env_backups() -> None:
    """목적을 다한 .env 백업을 치운다 — 옮기다 잘못됐을 때 되돌리는 자리라 며칠이면 끝나는데,
    안에는 아직 살아 있는 키가 들어 있어 남겨 두면 백업·공유에 딸려 갈 위험만 남는다.
    이 앱의 다른 저장물(숨김 7일·로그 14일·회차 365일)과 같은 방식이다."""
    cutoff = datetime.now().timestamp() - ENV_BACKUP_RETENTION_DAYS * 86400
    for path in env_backup_files():
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                logger.info("보관 기간이 지난 .env 백업을 지웠습니다 — %s", path.name)
        except OSError:
            logger.exception(".env 백업을 지우지 못했습니다 — %s", path.name)


def _backup_env() -> Optional[Path]:
    """.env를 고치기 전에 한 벌 복사해 둔다 — 앱이 .env를 건드리는 첫 사례라
    되돌릴 자리를 반드시 남긴다. 실패하면 None을 돌려 옮겨심기 자체를 취소시킨다."""
    stamp = datetime.now()
    target = BASE_DIR / f".env.backup-{stamp:%Y-%m-%d}"
    if target.exists():
        # 같은 날 두 번째 — 먼저 뜬 백업을 덮지 않는다.
        target = BASE_DIR / f".env.backup-{stamp:%Y-%m-%d_%H%M%S}"
    try:
        # copy2가 아니라 copy — copy2는 원본 .env의 수정 시각까지 복사해, 보관 기간
        # 시계(cleanup_env_backups)가 「백업을 뜬 때」가 아니라 「.env를 마지막에 고친 때」부터
        # 돌게 된다(몇 년 전 .env면 뜨자마자 지워질 수 있다).
        shutil.copy(ENV_FILE, target)
    except OSError:
        logger.exception(".env 백업에 실패해 키 옮겨심기를 건너뜁니다 — 앱은 예전대로 .env를 씁니다")
        return None
    # 옛 키가 그대로 든 파일이라 credentials.json과 같은 권한으로 잠근다(.env는 644일 수 있다).
    try:
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return target


def transplant_env_keys() -> dict:
    """앱을 켤 때 한 번 — .env에 남은 키를 설정 파일(data/credentials.json)로 모은다.

    세 갈래로 움직인다(키 묶음마다 따로 판단):
      - 설정에 없고 .env에만 있다  -> **옮겨 심는다**(설정에 저장 + .env 줄 삭제)
      - 설정에도 있고 .env에도 있다 -> **.env 줄만 지운다**(옛 값이다. 설정값을 덮지 않는다)
      - 설정에 있고 .env엔 없다     -> 할 일 없음

    쓰는 순서가 계약이다 — **설정 파일을 먼저 쓰고 그다음에 .env를 고친다.** 거꾸로 하면
    중간에 멈췄을 때 키가 어디에도 없게 된다. 이 순서면 중간에 멈춰도 .env에 키가 남아
    있어(우선순위상 설정값이 이기므로) 앱은 어느 쪽이든 정상으로 돈다.

    반환: {"moved": [묶음 이름], "cleared": [묶음 이름], "kept": [묶음 이름], "backup": Path|None}
    """
    result = {"moved": [], "cleared": [], "kept": [], "backup": None}
    if not ENV_FILE.exists():
        return result

    lines, found = _read_env_lines()
    if not found:
        return result

    data = _load()
    drop: set = set()          # 지울 .env 줄 번호
    changed_groups = False

    def env_get(name: str) -> str:
        return found[name][1] if name in found else ""

    def mark(names: tuple) -> None:
        for name in names:
            if name in found:
                drop.add(found[name][0])

    now = datetime.now().isoformat(timespec="seconds")

    # 네이버 — 둘 중 하나만 .env에 있어도 그 값만 옮긴다(없는 쪽은 빈 문자열).
    if any(name in found for name in _ENV_NAVER):
        saved = data.get("naver") or {}
        if saved.get("client_id") or saved.get("client_secret"):
            result["cleared"].append("네이버 검색 API")
        else:
            data["naver"] = {
                "client_id": env_get("NAVER_CLIENT_ID"),
                "client_secret": env_get("NAVER_CLIENT_SECRET"),
                "saved_at": now,
                "from_env": True,
            }
            changed_groups = True
            result["moved"].append("네이버 검색 API")
        mark(_ENV_NAVER)

    # Claude API 키
    if "ANTHROPIC_API_KEY" in found:
        if (data.get("llm") or {}).get("api_key"):
            result["cleared"].append("Claude API 키")
        else:
            data["llm"] = {"api_key": env_get("ANTHROPIC_API_KEY"), "saved_at": now, "from_env": True}
            changed_groups = True
            result["moved"].append("Claude API 키")
        mark(_ENV_LLM)

    # 텔레그램 봇 토큰
    if "TELEGRAM_BOT_TOKEN" in found:
        if (data.get("telegram") or {}).get("bot_token"):
            result["cleared"].append("텔레그램 봇 토큰")
        else:
            data["telegram"] = {"bot_token": env_get("TELEGRAM_BOT_TOKEN"), "saved_at": now, "from_env": True}
            changed_groups = True
            result["moved"].append("텔레그램 봇 토큰")
        mark(_ENV_TELEGRAM)

    # 이메일 보내는 계정 — 저장 형식이 서버 주소가 아니라 서비스 이름(naver/gmail)이라,
    # .env의 EMAIL_SMTP_HOST가 그 둘 중 하나가 아니면 옮길 수가 없다. 그때는 .env 줄을
    # 그대로 둔다(옮기지도 지우지도 않는다) — 지우면 그 사람은 메일을 못 보내게 된다.
    if any(name in found for name in _ENV_EMAIL):
        if (data.get("email") or {}).get("service"):
            result["cleared"].append("이메일 보내는 계정")
            mark(_ENV_EMAIL)
        else:
            host = env_get("EMAIL_SMTP_HOST").lower()
            service = next((key for key, spec in EMAIL_SERVICES.items() if host == spec["host"]), None)
            if service:
                data["email"] = {
                    "service": service,
                    "address": env_get("EMAIL_SENDER_ADDRESS"),
                    "password": env_get("EMAIL_SENDER_PASSWORD"),
                    "name": "",
                    "saved_at": now,
                    "from_env": True,
                }
                changed_groups = True
                result["moved"].append("이메일 보내는 계정")
                mark(_ENV_EMAIL)
            else:
                result["kept"].append("이메일 보내는 계정")
                logger.warning(
                    ".env의 메일 서버(%s)가 설정 화면이 아는 서비스가 아니라 옮기지 않았습니다 — "
                    "설정 › 연동 › 이메일 발송 계정에서 네이버·Gmail 중 하나로 다시 등록해주세요",
                    host or "(빈 값)",
                )

    if not drop:
        return result

    backup = _backup_env()
    if backup is None:
        return {"moved": [], "cleared": [], "kept": result["kept"], "backup": None}
    result["backup"] = backup

    # 1) 설정 파일 먼저 (실패하면 .env는 건드리지 않는다 — 키가 사라지지 않게)
    if changed_groups:
        try:
            _save(data)
        except OSError:
            logger.exception("설정 파일 저장에 실패해 키 옮겨심기를 취소합니다 — .env는 그대로 둡니다")
            return {"moved": [], "cleared": [], "kept": result["kept"], "backup": backup}

    # 2) 그다음에 .env에서 그 줄들을 지운다. 줄이 빠진 자리에 빈 줄만 겹쳐 남으므로
    # 연달아 빈 줄은 하나로 줄인다(주석은 손대지 않는다 — 담당자가 적어둔 글이다).
    kept_lines = []
    for i, line in enumerate(lines):
        if i in drop:
            continue
        if not line.strip() and (not kept_lines or not kept_lines[-1].strip()):
            continue
        kept_lines.append(line)
    while kept_lines and not kept_lines[-1].strip():
        kept_lines.pop()
    try:
        store.write_text(ENV_FILE, "\n".join(kept_lines) + ("\n" if kept_lines else ""))
    except OSError:
        logger.exception(
            ".env에서 옛 키 줄을 지우지 못했습니다 — 설정 파일에는 이미 옮겨졌으니 앱 동작은 정상입니다 "
            "(설정 › 연동 › 키 보관 상태에서 다시 확인하세요)"
        )
        return result

    logger.info(
        "키 옮겨심기 — 설정으로 옮김: %s / .env의 옛 줄 지움: %s / 그대로 둠: %s / 백업: %s",
        ", ".join(result["moved"]) or "없음",
        ", ".join(result["cleared"]) or "없음",
        ", ".join(result["kept"]) or "없음",
        backup.name,
    )
    return result


def moved_from_env() -> set:
    """.env에서 옮겨 심은 뒤로 담당자가 아직 직접 저장하지 않은 묶음 이름
    ({"naver","llm","telegram","email"} 중) — 「키 보관 상태」 화면이 그 사실을 적는다.
    담당자가 그 화면에서 저장하면 묶음이 통째로 새로 써지므로 표시가 저절로 사라진다."""
    data = _load()
    return {key for key in ("naver", "llm", "telegram", "email") if (data.get(key) or {}).get("from_env")}
