"""담당자가 직접 끈 사진 추정 (`📷 사진 추정`이 틀렸을 때 그 기사 하나만).

사진 판정(`app.filters.looks_like_photo_caption`)은 표식·원문·제목 모양 세 층으로
알아맞히는 일이라 틀릴 때가 있다. 틀린 기사를 담당자가 ⋯ 더보기에서 한 건씩 끄는 자리가
여기다 — **기능 전체를 끄는 설정이 아니다.**

- **`app.summary_overrides`와 같은 성격이다.** 「이 기사가 사진기사인가」는 그 URL이
  가리키는 **기사에 관한 사실**이지, 어느 보고서에 넣고 뺄지(`hidden_articles`)나 어느
  소제목에 담을지(`group_overrides`) 같은 **편집 결정**이 아니다. 그래서 URL 키 전역이고,
  어느 화면에서 껐든 모든 화면이 함께 본다. 나이로 지우지 않는다.
- **`app.photo_body`의 판정 기록에 섞지 않는다.** 거긴 90일 만료(관측 기록이라 그게
  맞다)라, 담당자가 내린 결정을 거기 적으면 90일 뒤 조용히 되살아난다.
- **입히는 자리는 `looks_like_photo_caption()` 맨 앞 한 곳**이다. 그래서 화면 여섯·
  「📷 사진 추정 (N)」 건수·수시 「사진 제외」·홈 부정 추정 범위가 저절로 따라온다.
- **방향은 해제뿐이다.** 「사진으로 표시」는 만들지 않았다 — 요청에 없다. 저장 형식은
  `photo` 값을 참으로도 담을 수 있게 열어 두되, 화면이 쓰는 건 거짓 하나다.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import PHOTO_OVERRIDES_FILE

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))

# 한 화면이 기사 수백 건을 훑으며 기사마다 이 값을 묻는다(기관 기사 찾기는 수천 건).
# 파일을 그때마다 열지 않도록 (mtime_ns, size)가 같으면 읽어둔 것을 쓴다 — app.run_index가
# 회차 파일에 쓰는 방식과 같다. 쓰기는 같은 잠금 안에서 캐시까지 갱신한다.
_lock = threading.Lock()
_cache: Optional[dict] = None
_cache_sig: Optional[tuple] = None


def _signature() -> Optional[tuple]:
    try:
        st = PHOTO_OVERRIDES_FILE.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _read_locked() -> dict:
    global _cache, _cache_sig
    sig = _signature()
    if _cache is not None and sig == _cache_sig:
        return _cache
    data: dict = {}
    if sig is not None:
        try:
            parsed = json.loads(PHOTO_OVERRIDES_FILE.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                data = parsed
        except (json.JSONDecodeError, TypeError, OSError) as exc:
            # 깨진 파일 때문에 화면이 멈추면 안 된다 — 비어 있는 것으로 보고 경고만 남긴다.
            logger.warning("사진 추정 해제 기록을 읽지 못했습니다: %s", exc)
    _cache, _cache_sig = data, sig
    return data


def load_photo_overrides() -> dict:
    """{url: {"photo": bool, "at": ISO}} 전체를 돌려준다(읽기 전용으로 쓴다)."""
    with _lock:
        return _read_locked()


def photo_override(url: str) -> Optional[bool]:
    """그 URL에 담당자가 적어 둔 판정. 적은 적이 없으면 None(= 앱의 추정을 그대로 쓴다)."""
    if not url:
        return None
    entry = load_photo_overrides().get(url)
    if not isinstance(entry, dict):
        return None
    value = entry.get("photo")
    return value if isinstance(value, bool) else None


def is_photo_cleared(url: str) -> bool:
    """담당자가 「사진 추정 해제」를 눌러 둔 기사인가 — ⋯ 메뉴의 글자를 고르는 데 쓴다."""
    return photo_override(url) is False


def set_photo_override(url: str, photo: Optional[bool]) -> None:
    """그 기사의 사진 판정을 담당자 값으로 적는다. `photo=None`이면 항목째 지워
    앱의 추정으로 되돌린다(「사진 추정 되돌리기」)."""
    if not url:
        return
    global _cache, _cache_sig
    with _lock:
        data = dict(_read_locked())
        if photo is None:
            if url not in data:
                return
            data.pop(url)
        else:
            data[url] = {"photo": photo, "at": datetime.now(_KST).isoformat(timespec="seconds")}
        atomic_write_text(PHOTO_OVERRIDES_FILE, json.dumps(data, ensure_ascii=False, indent=2))
        _cache, _cache_sig = data, _signature()
    logger.info("사진 추정 %s: %s", "되돌림" if photo is None else "해제", url)


def _reset_cache_for_tests() -> None:
    """테스트가 파일을 갈아끼운 뒤 부른다(실행 중인 앱은 서명이 달라져 저절로 다시 읽는다)."""
    global _cache, _cache_sig
    with _lock:
        _cache, _cache_sig = None, None
