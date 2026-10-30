"""[단독]·[속보] 알림 감시 대상을 검색어 그룹 스위치로 한 번 옮겨 심는다.

예전에는 감시 대상이 `data/breaking_alert.json`의 `group_names`(그룹 **이름 문자열**
목록)였다. 두 가지가 문제였다:

1. **담당자가 끌 수 없었다.** 회차 수집의 「바닥」 경로(app.scraper.collect_run)가 그
   설정을 통째로 무시하고 회차 기사 전부를 검사했다. 그래서 설정 화면에서 그룹 체크를
   풀어도 그 그룹이 초안·확정본에 들어가는 한 알림은 그대로 나갔다.
2. **그룹명을 바꾸면 감시가 조용히 풀렸다.** 이름 문자열로 기억하기 때문이다.

이제 범위를 정하는 자리는 검색어 화면의 그룹 스위치(`include_in_alert`) 하나다. 이
모듈은 옛 설정을 그 스위치로 한 번 옮기고 `group_names`를 지운다 — 같은 설정이 두
자리에 남으면 어느 쪽이 진짜인지 묻게 된다.

**쓰는 순서가 계약이다 — 검색어 설정을 먼저 쓰고 그다음에 breaking_alert.json을
고친다.** 거꾸로 하면 중간에 멈췄을 때 감시 범위가 어디에도 없다(app.credentials의
`.env` 옮겨심기와 같은 규칙).
"""
import json
import logging

from app import store
from app.breaking_alert import load_breaking_alert_settings
from app.config import BREAKING_ALERT_FILE
from app.settings import load_settings, save_keyword_groups

logger = logging.getLogger(__name__)


def transplant_alert_groups() -> bool:
    """옛 group_names를 그룹의 include_in_alert로 옮긴다. 옮겼으면 True.

    main.py가 중복 실행 자물쇠 뒤에서 한 번만 부른다. 이미 옮겼거나 옛 설정이 없으면
    아무것도 하지 않는다. 실패해도 앱은 멈추지 않는다 — app.settings.group_in_alert가
    키 없는 그룹을 "켜짐"으로 읽으므로 알림은 예전처럼 계속 나간다.
    """
    try:
        config = load_breaking_alert_settings()
        if "group_names" not in _raw_config():
            return False  # 이미 옮겼다
        selected = {str(name) for name in config.get("group_names", [])}
        settings = load_settings()
        groups = settings.get("keyword_groups", [])
        if not groups:
            return False
        for group in groups:
            # 옛 설정에서 고르지 않은 그룹은 끈다. 다만 **그 목록이 아예 비어 있었으면**
            # 전부 켠다 — 빈 목록은 "감시 대상이 없다"가 아니라 설정을 한 번도 건드리지
            # 않았다는 뜻이고, 그 동안에도 「바닥」 경로가 회차 기사 전부를 알렸다.
            group["include_in_alert"] = (group.get("name") in selected) if selected else True
        save_keyword_groups(groups)                  # ① 먼저 쓴다
        _drop_group_names()                          # ② 그다음에 지운다
        logger.info(
            "[단독]·[속보] 감시 대상을 검색어 그룹 스위치로 옮겼습니다 — 켠 그룹: %s",
            ", ".join(g["name"] for g in groups if g.get("include_in_alert")) or "(없음)",
        )
        return True
    except Exception:
        logger.exception("[단독]·[속보] 감시 대상 옮겨심기 실패 — 알림은 예전 범위로 계속 돕니다")
        return False


def _raw_config() -> dict:
    """파일에 실제로 적힌 값만 — load_breaking_alert_settings는 기본값을 덮어씌워
    돌려주므로 "group_names 키가 아직 남아 있나"를 그 값으로는 판정할 수 없다."""
    if not BREAKING_ALERT_FILE.exists():
        return {}
    try:
        data = store.read_json(BREAKING_ALERT_FILE)
    except (json.JSONDecodeError, TypeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _drop_group_names() -> None:
    data = _raw_config()
    if "group_names" not in data:
        return
    del data["group_names"]
    store.write_json(BREAKING_ALERT_FILE, data, indent=2)
