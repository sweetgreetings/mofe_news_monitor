"""기사 원문으로 사진 기사를 가리는 판정 (`looks_like_photo_caption`의 원문 층).

제목·네이버 요약만으로는 사진 기사를 추정할 수밖에 없다 — 요약은 네이버가 아무 데서나
잘라 날짜 도장·기자 이메일이 사라지고, 제목 모양은 언론사마다 다르다. 원문을 보면
사진 기사는 **사진 한 장 + 캡션 한 문장**이라 언론사·제목 모양과 상관없이 모양이 같다.

- **네이버 미러(`n.news.naver.com`)만** 본다. 한 구조(`#dic_area`)라 본문을 확실히
  꺼낼 수 있다. 언론사 자체 도메인은 사이트마다 구조가 달라 아직 안 한다.
- 원문은 판정에만 쓰고 저장하지 않는다. 저장하는 건 URL별 참/거짓 한 칸뿐이다.
- 판정은 뒤에서 한다(`start()` 이후 `verdict()`가 모르는 URL을 줄 세운다). 화면은 기다리지 않고,
  판정이 끝나기 전엔 기존 추정을 쓴다 — 다음 새로고침부터 원문 판정이 보인다.
- **`start()`는 `main.py`가 중복 실행 자물쇠 뒤에서만 부른다.** 테스트·진단 스크립트가
  `looks_like_photo_caption`을 불러도 네트워크·파일 쓰기가 일어나지 않게 하는 스위치다.
"""

from __future__ import annotations

import html
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from app.atomic_write import atomic_write_text
from app.config import DATA_DIR

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))

PHOTO_BODY_FILE = DATA_DIR / "photo_body_verdicts.json"

# 판정 기록 보관 기간 — 이보다 오래된 기사는 기존 추정으로 돌아간다(화면에 뜨는 건 대개 최근 기사).
PHOTO_BODY_RETENTION_DAYS = 90

# 사진 기사 본문 상한(캡션 글자를 뺀 본문, 기자 줄·날짜 도장·이메일 포함). 실측: 사진 기사
# 79~260자, 같은 구간의 일반 기사는 단신·[속보]뿐이고 그건 아래 첫 문장 조건이 가른다.
MAX_PHOTO_BODY_CHARS = 300

_FETCH_CONCURRENCY = 4
_FETCH_TIMEOUT_SEC = 5
_MAX_FETCH_ATTEMPTS = 3

_MIRROR_HOST = "n.news.naver.com"
_BODY_RE = re.compile(r'<article[^>]*id="dic_area"[^>]*>(.*?)</article>', re.S)
_PHOTO_RE = re.compile(r'class="end_photo_org"')
_IMG_DESC_RE = re.compile(r'<em class="img_desc"[^>]*>.*?</em>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
# 첫 문장 앞의 기자 줄 — "(서울=연합뉴스) 신현우 기자 = ", "[이데일리 노진환 기자] ",
# "[데일리안 = 홍금표기자] ". 앞 60자 안에서 한 번만 뗀다.
_BYLINE_RE = re.compile(r"^.{0,60}?기자\s*(?:=|\]|\))\s*")
_FIRST_SENTENCE_RE = re.compile(r".+?다\.")
# 캡션 첫 문장: 촬영 날짜("22일")가 있고 "~하고 있다." / "~해 있다."로 끝난다.
_CAPTION_DAY_RE = re.compile(r"\d{1,2}일")
_CAPTION_END_RE = re.compile(r"있다\.$")

_UNDECIDED = 2  # 저장값: 1 사진 · 0 일반 기사 · 2 원문으론 못 가름

_lock = threading.Lock()
_verdicts: Optional[dict] = None  # {날짜: {url: 0|1|2}}
_index: dict = {}                 # {url: True|False|None} — _verdicts를 평평하게 편 조회용
_pending: set = set()
_attempts: dict = {}
_unsaved = 0
_started = False
_executor: Optional[ThreadPoolExecutor] = None


def body_text_and_photos(page_html: str) -> Optional[tuple[str, int]]:
    """네이버 미러 페이지에서 (캡션을 뺀 본문 글자, 사진 수)를 꺼낸다. 본문이 없으면 None."""
    match = _BODY_RE.search(page_html)
    if not match:
        return None
    body = match.group(1)
    photos = len(_PHOTO_RE.findall(body))
    body = _IMG_DESC_RE.sub("", body)
    text = _SPACE_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", body))).strip()
    return text, photos


def looks_like_photo_body(text: str, photos: int) -> Optional[bool]:
    """원문 본문이 사진 기사 모양인가 — 세 갈래로 답한다.

    - True: 사진 1장 이상 + 짧은 본문 + 첫 문장이 캡션(`N일 … 있다.`).
    - False: 사진이 없거나 본문이 길다 — 원문으로 보면 분명히 일반 기사다.
    - None: 짧은데 첫 문장이 캡션 모양이 아니다(`…출근하고 있다. (사진=재정경제부)`처럼
      날짜가 없는 캡션, `[그래픽]` 설명) — 원문만으론 못 가르니 기존 추정에 맡긴다.
    """
    if photos < 1 or len(text) > MAX_PHOTO_BODY_CHARS:
        return False
    rest = _BYLINE_RE.sub("", text, count=1)
    first = _FIRST_SENTENCE_RE.match(rest)
    if not first:
        return None
    sentence = first.group(0).strip()
    if _CAPTION_DAY_RE.search(sentence) and _CAPTION_END_RE.search(sentence):
        return True
    return None


def judge_page(page_html: str) -> Optional[bool]:
    """페이지 HTML 하나를 판정한다. 본문을 못 찾았거나 원문만으론 못 가르면 None."""
    parsed = body_text_and_photos(page_html)
    if parsed is None:
        return None
    return looks_like_photo_body(*parsed)


def is_mirror_url(url: str) -> bool:
    return f"//{_MIRROR_HOST}/" in (url or "")


def _today() -> str:
    return datetime.now(_KST).strftime("%Y-%m-%d")


def _load_locked() -> None:
    global _verdicts, _index
    if _verdicts is not None:
        return
    try:
        data = json.loads(PHOTO_BODY_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    cutoff = (datetime.now(_KST) - timedelta(days=PHOTO_BODY_RETENTION_DAYS)).strftime("%Y-%m-%d")
    _verdicts = {d: v for d, v in data.items() if isinstance(v, dict) and d >= cutoff}
    _index = {}
    for day in sorted(_verdicts):
        for url, flag in _verdicts[day].items():
            _index[url] = None if flag == _UNDECIDED else bool(flag)


def verdict(url: str) -> Optional[bool]:
    """원문 판정 결과. 아직 없으면 None이고, 켜져 있으면 뒤에서 판정하도록 줄 세운다."""
    if not is_mirror_url(url):
        return None
    with _lock:
        _load_locked()
        found = _index.get(url)
        if url not in _index and _started:
            _enqueue_locked(url)
    return found


def _enqueue_locked(url: str) -> None:
    if url in _pending or _attempts.get(url, 0) >= _MAX_FETCH_ATTEMPTS:
        return
    _pending.add(url)
    _executor.submit(_check_one, url)


def _fetch_html(url: str) -> Optional[str]:
    try:
        response = requests.get(url, timeout=_FETCH_TIMEOUT_SEC, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
    except requests.exceptions.RequestException:
        return None
    return response.text


def _check_one(url: str) -> None:
    global _unsaved
    try:
        page = _fetch_html(url)
        parsed = body_text_and_photos(page) if page is not None else None
        result = looks_like_photo_body(*parsed) if parsed is not None else None
    except Exception:  # 판정 실패가 앱을 멈추면 안 된다 — 이 기사는 기존 추정을 쓴다.
        logger.warning("원문 사진 판정 실패: %s", url, exc_info=True)
        parsed = None
    with _lock:
        _pending.discard(url)
        if parsed is None:  # 못 받았거나 본문 구조가 다르다 — 몇 번만 다시 받아 본다.
            _attempts[url] = _attempts.get(url, 0) + 1
        else:
            # 「모름」도 적어 둔다(2) — 다시 받아도 같은 답이라 매번 받으러 가지 않게.
            _verdicts.setdefault(_today(), {})[url] = _UNDECIDED if result is None else int(result)
            _index[url] = result
            _unsaved += 1
        # 줄이 비었을 때 저장하되(마지막 기사가 실패했어도), 계속 밀려들어도 50건마다는 남긴다.
        if _unsaved and (not _pending or _unsaved >= 50):
            _persist_locked()
            _unsaved = 0


def _persist_locked() -> None:
    try:
        atomic_write_text(PHOTO_BODY_FILE, json.dumps(_verdicts, ensure_ascii=False))
    except OSError:
        logger.warning("원문 사진 판정 기록을 저장하지 못했습니다", exc_info=True)


def start() -> None:
    """뒤에서 원문을 받아 판정하기 시작한다(main.py가 중복 실행 자물쇠 뒤에서 한 번)."""
    global _started, _executor
    with _lock:
        if _started:
            return
        _executor = ThreadPoolExecutor(max_workers=_FETCH_CONCURRENCY, thread_name_prefix="photo-body")
        _started = True
