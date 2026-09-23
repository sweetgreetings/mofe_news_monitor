# 텔레그램 봇 이름 — 받는 사람의 대화방 맨 위에 보이는 이름을 앱에서 정한다.
#
# 이름은 앱이 아니라 텔레그램 쪽(봇)에 저장된다(Bot API getMyName/setMyName). 앱은
# 「이 봇에 이름을 건 적이 있는가」만 data/telegram_bot_name.json에 봇 id별로 적어 둔다 —
#   - 한 번도 건 적 없는 봇이면 앱을 켤 때 기본 이름(DEFAULT_TELEGRAM_BOT_NAME)을 한 번 건다
#     (새 봇을 붙여도 기본 이름으로 시작하게).
#   - 그 뒤로는 앱이 먼저 이름을 바꾸지 않는다(BotFather에서 바꾼 이름을 덮어쓰지 않도록).
# 텔레그램은 이름을 자주 바꾸면 한동안 막으므로(429) 바뀌었을 때만 보낸다.
import json
import logging
import time
from typing import Optional, Tuple

import requests

from app.atomic_write import atomic_write_text
from app import credentials
from app.config import DEFAULT_TELEGRAM_BOT_NAME, TELEGRAM_BOT_NAME_FILE

logger = logging.getLogger(__name__)

MAX_BOT_NAME_LEN = 64  # 텔레그램 한도
_TIMEOUT_SEC = 5


def _bot_id() -> Optional[str]:
    """토큰 앞부분(`123456:ABC…`의 123456)이 봇 id다 — 비밀이 아니라 기록 키로 쓴다."""
    token = credentials.telegram_bot_token()
    if not token or ":" not in token:
        return None
    return token.split(":", 1)[0]


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{credentials.telegram_bot_token()}/{method}"


def _load_record() -> dict:
    try:
        data = json.loads(TELEGRAM_BOT_NAME_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _remember(name: str) -> None:
    bot_id = _bot_id()
    if not bot_id:
        return
    record = _load_record()
    record[bot_id] = {"name": name, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        atomic_write_text(TELEGRAM_BOT_NAME_FILE, json.dumps(record, ensure_ascii=False, indent=2))
    except OSError:
        logger.warning("봇 이름 기록을 못 썼습니다 — 다음에 앱을 켤 때 기본 이름을 다시 걸 수 있습니다")


def _describe(resp: requests.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code == 429:
        retry = (body.get("parameters") or {}).get("retry_after")
        if isinstance(retry, int) and retry > 0:
            minutes = max(1, round(retry / 60))
            return f"텔레그램이 이름 변경을 잠시 막았습니다(429). 약 {minutes}분 뒤에 다시 저장해주세요."
        return "텔레그램이 이름 변경을 잠시 막았습니다(429). 잠시 뒤에 다시 저장해주세요."
    if resp.status_code == 401:
        return "봇 토큰이 유효하지 않습니다(401)."
    return f"텔레그램이 거절했습니다(HTTP {resp.status_code})."


def fetch_bot_name() -> Tuple[Optional[str], Optional[str]]:
    """(지금 이름, 오류 문구). 토큰이 없으면 (None, None)."""
    if not credentials.telegram_bot_token():
        return None, None
    try:
        resp = requests.get(_api("getMyName"), timeout=_TIMEOUT_SEC)
    except requests.exceptions.RequestException:
        logger.warning("봇 이름을 불러오지 못했습니다(네트워크)", exc_info=True)
        return None, "텔레그램에 연결하지 못했습니다."
    if not resp.ok:
        logger.warning("봇 이름을 불러오지 못했습니다(%s): %s", resp.status_code, resp.text)
        return None, _describe(resp)
    try:
        return str(resp.json()["result"]["name"]), None
    except (ValueError, KeyError, TypeError):
        return None, "텔레그램 응답을 읽지 못했습니다."


def set_bot_name(name: str) -> Optional[str]:
    """이름을 건다. 성공하면 None, 실패하면 담당자에게 보여줄 오류 문구."""
    if not credentials.telegram_bot_token():
        return "봇 토큰이 없습니다."
    try:
        resp = requests.post(_api("setMyName"), data={"name": name}, timeout=_TIMEOUT_SEC)
    except requests.exceptions.RequestException:
        logger.warning("봇 이름을 바꾸지 못했습니다(네트워크)", exc_info=True)
        return "텔레그램에 연결하지 못했습니다."
    if not resp.ok:
        logger.warning("봇 이름을 바꾸지 못했습니다(%s): %s", resp.status_code, resp.text)
        return _describe(resp)
    _remember(name)
    logger.info("봇 이름을 바꿨습니다: %s", name)
    return None


def normalize_bot_name(raw: str) -> str:
    """앞뒤 공백을 떼고, 비었으면 기본 이름, 64자를 넘으면 자른다."""
    name = (raw or "").strip()
    if not name:
        return DEFAULT_TELEGRAM_BOT_NAME
    return name[:MAX_BOT_NAME_LEN]


def apply_default_bot_name_once() -> None:
    """앱을 켤 때 한 번 — 이 봇에 앱이 이름을 건 적이 없으면 기본 이름을 건다.
    실패해도 기록을 안 남기므로 다음에 켤 때 다시 시도한다."""
    bot_id = _bot_id()
    if not bot_id or bot_id in _load_record():
        return
    current, error = fetch_bot_name()
    if error:
        return
    if current == DEFAULT_TELEGRAM_BOT_NAME:
        _remember(current)
        return
    set_bot_name(DEFAULT_TELEGRAM_BOT_NAME)
