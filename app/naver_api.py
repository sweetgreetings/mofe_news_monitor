# Design Ref: PRD.md 기능1 규칙 2 — 키워드 그룹 중 하나라도 조건을 만족하면 수집(그룹간 OR), 건수 제한 없음, 당일 기사만
import html
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import urlparse, urlunparse

import requests

from app.api_usage import record_api_call
from app.config import COLOR_ERROR, SEARCH_LOOKBACK_MIN
from app.credentials import naver_client_id, naver_client_secret, naver_is_configured

logger = logging.getLogger(__name__)

NAVER_NEWS_API_URL = "https://openapi.naver.com/v1/search/news.json"


class NaverNotConfiguredError(requests.exceptions.RequestException):
    """네이버 API 키가 없어 요청 자체를 시도하지 않았을 때 올린다.

    [추가: 2026-08-20] app.credentials가 생기면서 키가 없는 상태로도 앱이 켜질 수
    있게 됐다 — 그 상태에서 수집이 걸리면 이 예외로 즉시 실패시킨다. response에
    status_code=401을 심어두는 이유는, 이미 있는 재시도 판단 로직(app.naver_api.
    _retryable, app.scraper._is_retryable)이 둘 다 "response가 없으면(연결 실패 등)
    재시도, 4xx면 포기"로 짜여 있어서다 — 키가 없는 건 5분 뒤에 다시 시도한다고
    해결되는 일시적 오류가 아니라 사람이 등록해야 하는 상태이므로, 새 예외 종류를
    추가하는 대신 기존 로직이 이미 "포기"로 분류하는 모양(401)을 그대로 빌린다."""

    def __init__(self, message: str = "네이버 API 키가 등록돼 있지 않습니다."):
        response = requests.Response()
        response.status_code = 401
        super().__init__(message, response=response)


def test_naver_credentials(client_id: str, client_secret: str) -> tuple[bool, str]:
    """설정 화면의 [연결 테스트] — 저장 전에 입력한 값이 실제로 되는지 확인한다.

    저장된 값이 아니라 화면이 지금 넘겨준 값으로 직접 호출한다(아직 저장하지 않은
    상태에서도 확인할 수 있어야 하므로 app.credentials를 거치지 않는다). "테스트"
    키워드로 1건만 조회해 호출 자체를 최소화한다.
    """
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    if not client_id or not client_secret:
        return False, "Client ID와 Client Secret을 모두 입력해주세요."
    headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}
    try:
        response = requests.get(
            NAVER_NEWS_API_URL, headers=headers, params={"query": "테스트", "display": 1}, timeout=10
        )
    except requests.exceptions.RequestException as error:
        return False, f"네트워크 연결에 실패했습니다 — {error}"
    if response.status_code == 200:
        return True, '연결됐습니다 ("테스트" 1건 조회 성공)'
    if response.status_code in (401, 403):
        return False, "인증 실패 — Client ID/Secret을 다시 확인해주세요."
    if response.status_code == 429:
        return False, "요청 한도를 초과했습니다 (429) — 잠시 후 다시 시도해주세요."
    return False, f"연결에 실패했습니다 (HTTP {response.status_code})"
_MAX_DISPLAY = 100  # 네이버 API가 허용하는 1회 요청 최대 건수
_MAX_START = 1000  # 네이버 API가 허용하는 최대 조회 시작 위치 (이 이상은 API 자체가 지원 안 함)
# [추가: 2026-08-06] 키워드별 검색을 동시에 몇 개까지 허용할지 — 실시간 현황(live.html)이
# 등록된 키워드를 하나씩 순서대로 물어봐 13개 기준 약 24초가 걸리던 것을 동시 처리로
# 줄이기 위해 도입했다. 처음엔 5로 잡았다("가장 느린 키워드 하나의 시간까지만 기다리면
# 된다"는 계산이었는데, 이건 키워드가 동시성 이하로 적을 때만 맞는 얘기였다).
# [수정: 2026-08-13] 그룹/키워드 상한을 올리는 논의 중 실제로 재봤다(100키워드, 재시도
# 없이 1회 시도 기준 — search_keywords 참고): 동시성 5는 429가 0%였지만 10은 5%,
# 15는 20%, 20은 86%까지 치솟았다 — "느려지는 만큼 늘려도 괜찮다"는 예전 직관이
# 틀렸다는 뜻이다(429는 선형이 아니라 어느 지점부터 급격히 나빠진다). 재시도 로직이
# 흡수할 수 있는 여유만큼만 5→8로 올렸다. 8보다 더 올리고 싶으면 반드시 다시 실측할 것
# (스크래치패드에 실측 스크립트 있음) — 감으로 올리지 말 것(CODING_CONVENTIONS §1).
_MAX_CONCURRENT_KEYWORD_SEARCHES = 8

# [추가: 2026-08-13] search_keywords가 호출마다 새로 만드는 ThreadPoolExecutor는 "자기
# 안에서"만 동시성을 _MAX_CONCURRENT_KEYWORD_SEARCHES로 제한한다 — 서로 다른 호출자가
# 동시에 검색을 돌리면(예: 정기 스케줄러 tick과 수시 카드 재수집이 같은 순간에 걸리는
# 경우) 실제 네이버 호출은 합쳐서 8+8=16까지 오를 수 있고, 이건 위 실측(동시성 15에서
# 429 20%)의 위험 구간이다. 겹칠 확률 자체는 낮다(정기 자동 호출은 스케줄러 tick 하나뿐,
# 하루 4회×7초 — HISTORY.md "수시 모니터링" "API 동시 호출" 항목 참고) — 그래서 이건
# 성능 최적화가 아니라 "만에 하나의 429 폭주 방지"용 안전장치다. 프로세스 전체에서
# 하나뿐인 전역 세마포어로 모든 호출자를 묶는다. 키워드 단위(_search_one_keyword 함수
# 전체)가 아니라 **HTTP 요청 하나하나**(페이지네이션의 각 페이지)에 걸어야 한다 —
# 키워드 단위로 걸면 최대 10페이지짜리 검색 하나가 그 사이 다른 키워드의 슬롯까지
# 계속 붙들고 있게 된다.
_GLOBAL_REQUEST_SEM = threading.Semaphore(_MAX_CONCURRENT_KEYWORD_SEARCHES)

# [추가: 2026-08-24] 제목이 "..."로 잘린 기사의 전체 제목을 다시 가져올 때(_fetch_full_title)
# 동시에 몇 건까지 허용할지 — _GLOBAL_REQUEST_SEM과는 무관한 별도 상한이다. 네이버
# API 하나가 아니라 서로 다른 언론사 사이트로 요청이 흩어지므로 한 서버에 몰릴 위험이
# 낮고(429 대상이 아니다), 실측(2026-08-24, '정부' 검색 1페이지 100건)으로는 24건이
# 잘려 있었다 — 순차로 처리하면 응답이 느린/안 뜨는 사이트가 하나만 섞여도 그때마다
# 최대 3초(_fetch_full_title의 timeout)씩 그대로 블로킹됐다(실측 사례: 검색어 하나
# 추가에 66초). 10은 24건 정도를 한두 배치로 끝낼 수 있는 여유이면서, 열어보는 언론사
# 사이트 수(=서버 자원)가 한 번에 지나치게 몰리지 않는 값이다.
_TITLE_REFETCH_CONCURRENCY = 10

# [추가: 2026-08-13] 키워드 하나가 429(rate limit)·타임아웃 등으로 실패해도, 예전엔
# 그 자리에서 바로 포기하고 빈 목록으로 조용히 대체했다 — 결과 0건("오늘 기사 없음")과
# 구분이 안 돼 화면·회차 파일 어디에도 실패 흔적이 안 남았다(정기 스크랩·실시간 현황
# 둘 다). 재시도 없이 포기하기 전에 짧게 다시 시도하고, 그래도 안 되면 이번엔 "실패"라고
# 위 계층(app.scraper/app.live_renderer)에 알린다 — 처리 방식은 호출부가 정한다
# (정기 스크랩은 이미 있는 5분 간격 전체 재시도로 넘기고, 실시간 현황은 캐시를 건드리지
# 않고 화면에 경고를 띄운다).
_KEYWORD_RETRY_ATTEMPTS = 3  # 최초 1회 + 재시도 2회


def _retryable(error: requests.exceptions.RequestException) -> bool:
    """일시적 오류(재시도할 가치가 있는 오류)인지 판단한다 — app.scraper._is_retryable와
    같은 기준(연결 실패·타임아웃·5xx·429는 재시도, 그 외 4xx는 즉시 포기)이지만, 이
    함수는 순환 임포트를 피하려고 이 모듈 안에 따로 둔다(app.scraper가 이 모듈을
    임포트하므로 반대 방향 임포트는 불가능)."""
    response = getattr(error, "response", None)
    if response is None:
        return True
    if response.status_code == 429:
        return True
    return not (400 <= response.status_code < 500)


def _retry_wait_seconds(error: requests.exceptions.RequestException, attempt: int) -> float:
    """재시도 전 대기 시간. 429는 응답의 Retry-After 헤더를 최우선으로 따르고
    (네이버가 언제 다시 받아줄지 스스로 알려주는 값이라 추측보다 정확하다), 없으면
    시도 횟수에 비례해 늘어나는 backoff를 쓴다(연결 오류·5xx는 더 짧게)."""
    response = getattr(error, "response", None)
    if response is not None and response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return 3.0 * (attempt + 1)
    return 1.0 * (attempt + 1)

_TAG_PATTERN = re.compile(r"</?b>")

# PRD.md 기능1 규칙 2 — "당일"은 한국 시간(KST) 기준. 네이버 API의 pubDate도 항상 KST(+0900)로
# 내려오므로, 실행 서버의 시스템 시간대와 무관하게 KST로 고정해 비교한다.
_KST = timezone(timedelta(hours=9))

# PRD.md 기능1 규칙 3의 언론사 우선순위 (방송사 -> 주요 언론사 순). 이 순서 자체가
# app.sorter.sort_by_outlet_priority의 우선순위로도 재사용되므로 순서를 바꾸지 말 것 —
# 새 언론사를 추가할 땐 우선순위상 맞는 위치에 끼워 넣는다.
PRIORITY_OUTLETS = (
    "KBS",
    "MBC",
    "조선일보",
    "중앙일보",
    "동아일보",
    "한국경제",
    "매일경제",
    "서울경제",
    "한국일보",
    "머니투데이",
    "이데일리",
    "연합뉴스",
    "뉴시스",
)

# 위 우선순위 언론사를 URL 도메인으로 식별하기 위한 매핑 (한 언론사가 도메인을 여러
# 개 쓸 수 있어 값에 중복이 있을 수 있다). 네이버 뉴스 검색 API는 언론사명을 직접
# 내려주지 않아 도메인으로 추정한다.
OUTLET_DOMAINS = {
    "kbs.co.kr": "KBS",
    "imnews.imbc.com": "MBC",
    "mbc.co.kr": "MBC",
    "mbn.co.kr": "MBN",
    # [추가: 2026-08-03] MBN·매일경제가 같은 매경미디어그룹 소속이라 mk.co.kr을 함께
    # 쓰는데, MBN 기사의 실제 원문 링크는 mbn.co.kr이 아니라 mbn.mk.co.kr 서브도메인이라
    # (실사례로 확인) 이 항목이 없으면 아래 "mk.co.kr" 규칙에 서브도메인으로 잡혀
    # "매일경제"로 오분류된다 — koreajoongangdaily.joins.com과 같은 케이스.
    "mbn.mk.co.kr": "MBN",
    "news.sbs.co.kr": "SBS",
    "ytn.co.kr": "YTN",
    "khan.co.kr": "경향신문",
    "kmib.co.kr": "국민일보",
    "naeil.com": "내일신문",
    "donga.com": "동아일보",
    "munhwa.com": "문화일보",
    "seoul.co.kr": "서울신문",
    "segye.com": "세계일보",
    "asiatoday.co.kr": "아시아투데이",
    "chosun.com": "조선일보",
    "joongang.co.kr": "중앙일보",
    "joins.com": "중앙일보",
    "hani.co.kr": "한겨레",
    "hankookilbo.com": "한국일보",
    "mk.co.kr": "매일경제",
    "mt.co.kr": "머니투데이",
    "sedaily.com": "서울경제",
    "asiae.co.kr": "아시아경제",
    "edaily.co.kr": "이데일리",
    "fnnews.com": "파이낸셜뉴스",
    "hankyung.com": "한국경제",
    "heraldcorp.com": "헤럴드경제",
    "koreajoongangdaily.joins.com": "코리아중앙데일리",
    # [추가: 2026-08-14] 코리아중앙데일리가 독립 도메인으로 옮겨간 것을 실측 표본에서
    # 확인했다(7건 전부 koreajoongangdaily.com). 옛 도메인 항목도 과거 기사를 위해 남긴다.
    "koreajoongangdaily.com": "코리아중앙데일리",
    "koreatimes.co.kr": "코리아타임스",
    "koreaherald.com": "코리아헤럴드",
    "etnews.com": "전자신문",
    "dt.co.kr": "디지털타임스",
    "yna.co.kr": "연합뉴스",
    "newsis.com": "뉴시스",
    "news1.kr": "뉴스1",
    "pressian.com": "프레시안",
    "dailian.co.kr": "데일리안",
    "imaeil.com": "매일신문",
    # [추가: 2026-07-28] 신규 언론사 매핑. "한경비즈니스"는 한국경제(hankyung.com)의
    # 자매 매체지만 도메인이 다른 하위 도메인(magazine.hankyung.com)이라 별도 항목으로
    # 등록한다 — 더 구체적인(긴) 도메인이 먼저 매칭되므로 "한국경제" 판정과 안 겹친다.
    # [수정: 2026-07-29] 이후 OUTLET_CATEGORIES에서 각자 맞는 카테고리로 재분류했다
    # (방송사/통신사 및 종합 일간지/기타 언론사/주간지).
    "nocutnews.co.kr": "노컷뉴스",
    "nongmin.com": "농민신문",
    "magazine.hankyung.com": "한경비즈니스",
    "news.jtbc.co.kr": "JTBC",
    "tf.co.kr": "더팩트",
    "newspim.com": "뉴스핌",
    # [추가: 2026-07-29] 조선비즈 도메인 매핑 버그 수정 — 이전엔 조선비즈 도메인
    # (biz.chosun.com)이 상위 도메인 "chosun.com"(조선일보) 규칙에 걸려 조선일보로
    # 잘못 판정됐다. 더 구체적인(긴) 도메인이 먼저 매칭되는 기존 정렬 규칙 덕분에
    # 이 항목을 추가하는 것만으로 바로 잡힌다.
    "biz.chosun.com": "조선비즈",
    "chosunbiz.com": "조선비즈",
    # [추가: 2026-07-29] <주간지> 카테고리 신설에 맞춰 시사저널 도메인 추가.
    # "시사저널e"(sisajournal-e.com)는 이름은 비슷하지만 별개 매체라 매핑하지 않는다.
    "sisajournal.com": "시사저널",
    # [추가: 2026-07-30] 주간조선 — 조선일보(chosun.com)의 하위 도메인이지만 조선비즈
    # (biz.chosun.com)와 같은 이유로 더 구체적인 도메인이 먼저 매칭돼 안전하게 분리된다.
    "weekly.chosun.com": "주간조선",
    # [추가: 2026-07-30] TV조선 — 마찬가지로 조선일보(chosun.com)의 하위 도메인이라
    # 위와 같은 이유로 별도 항목이 필요하다.
    "news.tvchosun.com": "TV조선",
    # [추가: 2026-07-30] 땅집고(부동산 전문 매체) — 역시 조선일보(chosun.com)의 하위
    # 도메인이라 별도 항목 없이는 조선일보로 잘못 잡혔다. 저장된 기록에서 2건 확인됨
    # (2026-07-28 14:00, 2026-07-29 17:00 회차).
    "realty.chosun.com": "땅집고",
    # [추가: 2026-08-05] IT조선 — 마찬가지로 조선일보(chosun.com)의 하위 도메인이라
    # 별도 항목 없이는 조선일보로 잘못 잡혔다(it.chosun.com 기사가 실제로 "(조선일보)"로
    # 표시된 것을 사용자가 확인해 요청).
    "it.chosun.com": "IT조선",
    # [추가: 2026-08-21] 월간조선 — 마찬가지로 조선일보(chosun.com)의 하위 도메인이라
    # 별도 항목 없이는 조선일보로 잘못 잡혔다(monthly.chosun.com 기사가 실제로 "조선일보"로
    # 표시된 것을 사용자가 확인해 요청, 원문 URL idxno=71164).
    "monthly.chosun.com": "월간조선",
    # [추가: 2026-08-03] 조세일보 — 사용자 요청으로 경제일간 카테고리 맨 끝에 추가.
    "joseilbo.com": "조세일보",
    # [추가: 2026-08-07] 사용자가 화면에서 직접 매핑 안 된 도메인(ichannela.com)을 발견해
    # 요청한 4개 언론사.
    "ichannela.com": "채널A",
    "sisain.co.kr": "시사IN",
    "economist.co.kr": "이코노미스트",
    "kyeonggi.com": "경기일보",
}

# [추가: 2026-08-14] 네이버 언론사 코드(oid) → 언론사명.
#
# 네이버 뉴스 검색 API의 `link`가 네이버 미러 링크일 때 그 경로는
# `n.news.naver.com/mnews/article/{oid}/{기사ID}` 형태이고, 이 {oid}가 네이버가 매체마다
# 부여한 고유 번호다. 도메인 추정(OUTLET_DOMAINS)과 달리 한 회사가 도메인을 공유해도
# 매체별로 번호가 갈리므로, 계열 매체를 정확히 구분할 수 있는 유일한 단서다.
# (예: mk.co.kr을 매일경제 009 / 매경이코노미 024가 함께 쓴다.)
#
# 아래 표는 추측이 아니라 실측이다 — 실제 검색어로 모은 기사 9,174건에서 oid 88개를
# 추려, 각 oid마다 네이버 기사 페이지를 열어 og:article:author(네이버가 표기하는 언론사명)로
# 88개 전부 확인했다. 주석의 건수는 그 표본에서의 출현 횟수(빈도 감각용).
# 새 언론사가 필요하면 같은 방법으로 확인해서 추가한다 — 번호를 짐작해서 넣지 않는다.
NAVER_OID_OUTLETS = {
    # 설정 화면 "언론사 선택" 목록에 등록된 매체 — 이름은 그 목록의 표기를 그대로 쓴다
    # (화이트리스트 비교·우선순위 정렬이 이름 문자열로 이뤄지므로 달라지면 안 된다).
    "003": "뉴시스",          # 313건
    "421": "뉴스1",           # 281건
    "001": "연합뉴스",        # 251건
    "018": "이데일리",        # 167건
    "008": "머니투데이",      # 128건
    "277": "아시아경제",      # 116건
    "009": "매일경제",        # 114건
    "016": "헤럴드경제",      # 111건
    "015": "한국경제",        # 105건
    "014": "파이낸셜뉴스",    # 105건
    "056": "KBS",             # 99건
    "079": "노컷뉴스",        # 90건
    "119": "데일리안",        # 89건
    "011": "서울경제",        # 84건
    "052": "YTN",             # 81건
    "366": "조선비즈",        # 70건
    "028": "한겨레",          # 63건
    "629": "더팩트",          # 59건
    "021": "문화일보",        # 58건
    "025": "중앙일보",        # 58건
    "023": "조선일보",        # 57건
    "214": "MBC",             # 56건
    "022": "세계일보",        # 51건
    "032": "경향신문",        # 49건
    "020": "동아일보",        # 47건
    "469": "한국일보",        # 47건
    "055": "SBS",             # 45건
    "005": "국민일보",        # 45건
    "448": "TV조선",          # 45건
    "081": "서울신문",        # 44건
    "123": "조세일보",        # 43건
    "586": "시사저널",        # 43건
    "088": "매일신문",        # 41건
    "029": "디지털타임스",    # 40건
    "437": "JTBC",            # 35건
    "002": "프레시안",        # 34건
    "057": "MBN",             # 33건
    "243": "이코노미스트",    # 27건
    "030": "전자신문",        # 26건
    "666": "경기일보",        # 23건
    "449": "채널A",           # 22건
    "024": "매경이코노미",    # 18건 — 매일경제(009)와 mk.co.kr을 공유하던 바로 그 매체
    "050": "한경비즈니스",    # 15건
    "662": "농민신문",        # 11건
    "053": "주간조선",        # 7건
    "640": "코리아중앙데일리",  # 7건
    "044": "코리아헤럴드",    # 2건
    "308": "시사IN",          # 1건
    # 설정 목록 밖의 매체 — 지금은 도메인 문자열("ohmynews.com")이 그대로 화면에 찍히는데,
    # 사람이 읽을 수 있는 이름으로 바꿔주는 용도다. 이 표에 있다고 해서 "언론사 선택"
    # 체크박스 목록(OUTLET_CATEGORIES)에 올라가는 건 아니다 — 그건 담당자가 고르는 목록이라
    # 임의로 늘리지 않는다.
    "374": "SBS Biz",         # 73건
    "422": "연합뉴스TV",      # 56건
    "031": "아이뉴스24",      # 53건
    "047": "오마이뉴스",      # 42건
    "417": "동행미디어 시대",  # 36건
    "215": "한국경제TV",      # 35건
    "082": "부산일보",        # 29건
    "656": "대전일보",        # 29건
    "138": "디지털데일리",    # 29건
    "092": "지디넷코리아",    # 23건
    "660": "kbc광주방송",     # 23건
    "658": "국제신문",        # 20건
    "654": "강원도민일보",    # 17건
    "293": "블로터",          # 15건
    "661": "JIBS",            # 12건
    "117": "마이데일리",      # 11건
    "648": "비즈워치",        # 11건
    "655": "CJB청주방송",     # 9건
    "087": "강원일보",        # 9건
    "006": "미디어오늘",      # 8건
    "382": "스포츠동아",      # 7건 — donga.com을 써서 동아일보로 잡히던 매체
    "310": "여성신문",        # 6건
    "468": "스포츠서울",      # 6건
    "665": "더스쿠프",        # 5건
    "657": "대구MBC",         # 4건
    "262": "신동아",          # 3건 — 동아일보로 잡히던 매체
    "077": "AP연합뉴스",      # 3건
    "144": "스포츠경향",      # 2건 — 경향신문으로 잡히던 매체
    "033": "주간경향",        # 2건 — 경향신문으로 잡히던 매체
    "346": "헬스조선",        # 2건 — 조선일보로 잡히던 매체
    "108": "스타뉴스",        # 2건
    "076": "스포츠조선",      # 1건
    "607": "뉴스타파",        # 1건
    "584": "동아사이언스",    # 1건
    "659": "전주MBC",         # 1건
    "037": "주간동아",        # 1건 — 동아일보로 잡히던 매체
    "127": "기자협회보",      # 1건
    "356": "게임메카",        # 1건
    "296": "코메디닷컴",      # 1건
    "036": "한겨레21",        # 1건 — 한겨레로 잡히던 매체
}

# 설정 화면의 "언론사 선택" 카테고리별 목록 (PRD.md 기능1 규칙 16). 사용자가 준 순서를 그대로 따른다.
# OBS·이투데이·내일신문은 사용자 요청으로 목록에서 제외됨. [수정: 2026-07-23] 순서 갱신.
# [수정: 2026-07-29] "[7.28. 추가된 언론사]" 임시 카테고리를 없애고 아래처럼 재분류했다.
OUTLET_CATEGORIES = {
    "방송사": ("KBS", "MBC", "SBS", "YTN", "MBN", "JTBC", "TV조선", "채널A"),
    "전국종합일간": (
        "조선일보", "동아일보", "중앙일보", "문화일보", "한국일보", "서울신문",
        "세계일보", "경향신문", "한겨레", "국민일보", "아시아투데이",
    ),
    "경제일간": (
        "매일경제", "한국경제", "머니투데이", "이데일리",
        "서울경제", "파이낸셜뉴스", "헤럴드경제", "아시아경제", "조선비즈", "조세일보",
    ),
    "영자일간": ("코리아중앙데일리", "코리아타임스", "코리아헤럴드"),
    # [수정: 2026-07-29] "통신사"에서 "통신사 및 종합 일간지"로 이름 변경 — 통신사가
    # 아닌 노컷뉴스·뉴스핌·더팩트까지 이 카테고리에 함께 담기게 되어 이름 범위를 넓혔다.
    "통신사 및 종합 일간지": ("연합뉴스", "뉴시스", "뉴스1", "노컷뉴스", "뉴스핌", "더팩트", "이코노미스트"),
    "전문일간": ("전자신문", "디지털타임스", "IT조선"),
    "기타 언론사": ("프레시안", "데일리안", "매일신문", "농민신문", "땅집고", "경기일보"),
    # [추가: 2026-07-29] 신규 카테고리 — 주간지.
    # [수정: 2026-08-14] 매경이코노미는 "도메인을 확인하지 못해" 이름만 올려둔 상태였는데,
    # 애초에 전용 도메인이 없는 매체였다(매일경제와 mk.co.kr 공유). 네이버 oid 024로
    # 판별되면서 이 항목이 실제로 동작하기 시작했다 — NAVER_OID_OUTLETS 참고.
    # [추가: 2026-08-14] 신동아·주간동아(동아일보 계열), 주간경향(경향신문 계열),
    # 한겨레21(한겨레 계열) — oid 판별이 도입되기 전엔 전부 모지(母紙) 이름으로 몰래
    # 섞여 들어오고 있었다(사용자 요청으로 시사 주간지 카테고리 일관성에 맞춰 추가).
    "주간지": (
        "한경비즈니스", "주간조선", "월간조선", "매경이코노미", "시사저널", "시사IN",
        "신동아", "주간동아", "주간경향", "한겨레21",
    ),
}

ALL_OUTLET_NAMES = frozenset(name for names in OUTLET_CATEGORIES.values() for name in names)

# [추가: 2026-07-29] mk.co.kr 도메인은 매일경제(일간지)·매경이코노미(주간지)가 같이 쓴다.
# [수정: 2026-08-14] 네이버 oid로 둘이 정확히 갈리면서(009/024) 이 표식의 조건이 좁아졌다 —
# 예전엔 "매일경제"면 무조건 빨간 글자였지만, 이제 oid로 확정된 기사는 확실하므로 표식을
# 붙이지 않는다. 남는 불확실 케이스는 네이버 미러 링크가 없어 도메인 폴백으로 "매일경제"가
# 된 mk.co.kr 기사뿐이다(실측 표본 133건 중 1건). 그것만 빨갛게 남긴다 — 전부 빨갛게
# 칠하면 경고가 배경 소음이 되어 정작 확인이 필요한 1건이 묻힌다.
# 저장/비교에 쓰이는 outlet 원본 값(화이트리스트 비교, 우선순위 정렬 등)은 그대로 두고,
# 복사/내보내기 텍스트에도 넣지 않는다 — 순전히 화면 확인용.
_MK_AMBIGUOUS_OUTLET = "매일경제"


def outlet_display_label(outlet: str, url: Optional[str] = None) -> str:
    """화면에 보여줄 언론사 이름 HTML 조각 — 매경이코노미와 섞였을 *가능성이 남은*
    매일경제 기사만 빨간 글자로 강조한다.

    url에는 그 기사의 링크를 넘긴다. 그 링크가 네이버 미러 링크라 oid로 언론사가 확정된
    경우엔 표식을 붙이지 않는다. url을 안 넘기면(옛 호출부) 확정 여부를 알 수 없으므로
    안전하게 표식을 붙인다 — 모르는 걸 확실한 척하지 않는다.

    [수정: 2026-07-30] 처음엔 "매일경제**"처럼 문자만 덧붙였는데, 눈에 잘 안 띈다는
    피드백으로 빨간 글자 색으로 바꿨다. 이미 완성된(이스케이프된) HTML 조각을
    돌려주므로, 호출하는 쪽(app.renderer.render_article 등)에서 이 결과를 다시
    html.escape()하면 안 된다 — 그러면 <span> 태그가 그대로 글자로 보이게 된다.
    """
    escaped = html.escape(outlet)
    if outlet != _MK_AMBIGUOUS_OUTLET:
        return escaped
    if url and outlet_from_naver_link(url):
        return escaped  # oid로 확정된 기사 — 헷갈릴 여지가 없다
    return f'<span style="color:{COLOR_ERROR}">{escaped}</span>'


def _clean_text(raw: str) -> str:
    """네이버 API가 검색어 강조용으로 넣는 <b> 태그와 HTML 엔티티(&quot; 등)를 제거한다."""
    return html.unescape(_TAG_PATTERN.sub("", raw))


_TITLE_TAG_PATTERN = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_OG_TITLE_PATTERN = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)["\']'
    r'|<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']og:title["\']',
    re.IGNORECASE,
)
# [추가: 2026-08-05] "🔄 원문에서 다시 가져오기" 버튼이 요약을 보완할 때 쓴다 —
# og:description은 언론사가 직접 써둔 완결된 문장인 경우가 많아, 네이버 API의
# description(기사 중간 아무 데서나 잘린 스니펫)보다 자연스럽다.
_OG_DESC_PATTERN = re.compile(
    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']*)["\']'
    r'|<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']og:description["\']',
    re.IGNORECASE,
)


def _fetch_page_html(url: str, timeout: int) -> Optional[str]:
    """기사 원문 페이지의 HTML을 받아온다 — 원문에서 뭔가를 읽는 함수들의 공용 입구.

    본문 전체를 쓰려는 게 아니라 <head>의 메타태그를 읽으려는 것이다(PRD.md "본문
    크롤링 없음" 원칙과는 다른 성격). 접속 오류·타임아웃은 조용히 None — 부르는 쪽이
    전부 "못 가져오면 원래 값을 그대로 쓴다"로 돼 있어 여기서 예외를 올리면 안 된다.

    언론사 사이트가 응답 헤더에 charset을 안 밝히면 requests가 기본값(ISO-8859-1)으로
    잘못 짐작해 한글이 깨진다 — apparent_encoding(내용 기반 추정)으로 보정한다.
    """
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
    except requests.exceptions.RequestException:
        return None
    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding
    return response.text


def _best_title_from_html(text: str) -> Optional[str]:
    """og:title과 <title> 태그를 둘 다 확인해 더 완전해 보이는 쪽을 고른다.

    [수정: 2026-08-05] og:title이 있으면 무조건 그걸 썼는데, 실제로 언론사 CMS 쪽
    버그로 og:title 자체가 문장 중간에서 잘려 있는 경우가 있었다(한국일보,
    n.news.naver.com/mnews/article/469/0000946328 — og:title은 "...6070에 "에서
    끊기는데 같은 페이지의 <title> 태그엔 전체 문장이 다 있었다). 그래서 og:title이
    있어도 <title> 태그(언론사명이 붙어 있을 수 있음)와 길이를 비교해 더 긴 쪽을
    쓴다 — 정상적인 경우 <title>은 " - 언론사명"이 붙어 og:title보다 짧을 이유가
    없으므로, 이 비교만으로 잘린 케이스를 걸러낼 수 있다.
    """
    og_match = _OG_TITLE_PATTERN.search(text)
    og_title = None
    if og_match:
        og_title = (og_match.group(1) or og_match.group(2) or "").strip() or None
    title_match = _TITLE_TAG_PATTERN.search(text)
    title_tag = None
    if title_match:
        title_tag = title_match.group(1).strip() or None
    if og_title and title_tag:
        best = og_title if len(og_title) >= len(title_tag) else title_tag
    else:
        best = og_title or title_tag
    return html.unescape(best) if best else None


def _fetch_full_title(url: str) -> Optional[str]:
    """네이버 API가 "..."로 잘라 보낸 제목을, 그 기사의 실제 페이지에서 다시 가져온다.

    본문 전체를 읽는 게 아니라 <head>의 og:title/title 태그만 뽑아오는 용도다
    (PRD.md "본문 크롤링 없음" 원칙과는 다른 성격 — 제목만 보완). 둘 중 더 완전해
    보이는 쪽을 고른다(_best_title_from_html 참고 — og:title이 있어도 무조건 우선하지
    않는다).

    실패(접속 오류·타임아웃·태그 없음)하면 조용히 None을 돌려준다 — 호출하는 쪽은
    실패 시 원래(잘린) 제목을 그대로 쓰므로, 이 함수가 실패해도 전체 수집은 멈추지
    않는다. 짧은 타임아웃(3초)으로 느려짐을 제한한다.
    """
    text = _fetch_page_html(url, timeout=3)
    if text is None:
        return None
    return _best_title_from_html(text)


def fetch_full_title_and_summary(url: str) -> Optional[dict]:
    """"🔄 원문에서 다시 가져오기" 버튼(app.settings_server._handle_refetch_summary)이
    호출한다 — 그 기사 하나만 원문 페이지의 og:title/og:description으로 제목·요약을
    보완한다. _fetch_full_title과 같은 방식(og 메타태그, 접속 오류는 조용히 None)이지만,
    이건 자동이 아니라 사용자가 직접 버튼을 눌렀을 때만 호출되므로 전체 수집 시간에는
    영향이 없다 — 그래서 title뿐 아니라 summary도 항상 같이 시도한다("..."로 끝날 때만
    보완하는 _fetch_full_title과 달리 조건이 없다).

    title·summary 중 하나만 찾아도(다른 하나가 없어도) 찾은 것만 담아 돌려준다. 언론사
    페이지 태그 표기가 제각각이라 og 메타 자체가 없을 수 있고, 그러면 그 필드는 그냥
    없는 채로(None) 반환한다 — 호출하는 쪽(app.summary_overrides.set_summary_override)이
    있는 값만 저장한다. 원문 접속 자체가 실패하면(타임아웃 등) None을 돌려준다.
    """
    text = _fetch_page_html(url, timeout=5)
    if text is None:
        return None

    # [수정: 2026-08-05] og:title을 무조건 우선하지 않는다 — _best_title_from_html
    # 참고(언론사 CMS 버그로 og:title 자체가 잘려 있던 실제 사례).
    title = _best_title_from_html(text)

    summary = None
    og_desc = _OG_DESC_PATTERN.search(text)
    if og_desc:
        summary = (og_desc.group(1) or og_desc.group(2) or "").strip() or None

    if not title and not summary:
        return None
    return {
        "title": title,
        "summary": html.unescape(summary) if summary else None,
    }


# [추가: 2026-09-21] "+ URL로 추가"(담당자가 네이버에서 직접 찾아온 기사 한 건을
# 붙여넣는 입구)가 쓴다. 네이버 검색 API를 거치지 않은 기사라 pubDate가 없어서
# 원문 페이지에서 직접 읽는다.
#
# 실측(2026-09-21, 저장된 회차의 pub_date를 정답으로 대조 — CODING_CONVENTIONS §1):
#   · n.news.naver.com 30건 — data-date-time 30/30 정확. 페이지당 정확히 1개라
#     첫 매치가 곧 게시시각이다(수정시각이 함께 찍히는 경우가 없었다). 같은 표본에서
#     article:published_time·datePublished·meta[name=date]는 0/30, 즉 부재.
#   · 언론사 자체 도메인 14건 — data-date-time은 0건이고 article:published_time이
#     13건 존재. 그중 7건 정확 일치, 6건은 1~14분 차이(최초게시 vs 최종수정으로 보인다),
#     1건(동아일보 자체 도메인)은 후보가 아예 없었다.
# 저장된 기사의 93.5%가 네이버 미러라 주 경로는 오차가 없다. 자체 도메인에 남는
# ±15분 오차는 그대로 둔다 — 같은 언론사 안에서 정렬 자리가 조금 달라지는 정도다.
_NAVER_DATE_TIME_PATTERN = re.compile(r'data-date-time=["\']([^"\']+)["\']')
_ARTICLE_PUBLISHED_PATTERN = re.compile(
    r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)["\']'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']article:published_time["\']',
    re.IGNORECASE,
)
_JSONLD_PUBLISHED_PATTERN = re.compile(r'"datePublished"\s*:\s*"([^"]+)"')
_LOOSE_DATETIME_PATTERN = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})")


def _normalize_pub_date(raw: Optional[str]) -> Optional[str]:
    """페이지에서 읽은 시각 문자열을 KST ISO 형식으로 통일한다(네이버 API pub_date와 같은 모양).

    오프셋이 붙어 있으면(`+09:00`) 그대로 해석하고, 없으면(`2026-09-21 02:10:07`)
    한국 기사이므로 KST로 읽는다. 어느 쪽도 파싱이 안 되면 None — 부르는 쪽이
    "발행시각 없음"으로 처리한다(지어내지 않는다).
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        match = _LOOSE_DATETIME_PATTERN.search(raw)
        if not match:
            return None
        year, month, day, hour, minute = (int(g) for g in match.groups())
        parsed = datetime(year, month, day, hour, minute)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_KST)
    return parsed.astimezone(_KST).isoformat()


def _pub_date_from_html(text: str) -> Optional[str]:
    """원문 페이지 HTML에서 발행시각을 뽑는다 — 네이버 미러 우선, 자체 도메인 폴백.

    후보 순서는 위 실측 그대로다(네이버 data-date-time → article:published_time →
    JSON-LD datePublished). 하나도 없으면 None.
    """
    naver_match = _NAVER_DATE_TIME_PATTERN.search(text)
    if naver_match:
        normalized = _normalize_pub_date(naver_match.group(1))
        if normalized:
            return normalized
    meta_match = _ARTICLE_PUBLISHED_PATTERN.search(text)
    if meta_match:
        normalized = _normalize_pub_date(meta_match.group(1) or meta_match.group(2))
        if normalized:
            return normalized
    jsonld_match = _JSONLD_PUBLISHED_PATTERN.search(text)
    if jsonld_match:
        return _normalize_pub_date(jsonld_match.group(1))
    return None


# 네이버 미러 페이지의 og:article:author는 "{언론사} | 네이버" 형태다 — 실측(2026-09-21)
# 에서 서로 다른 22개 매체 전부 존재했고, 이 접미사만 떼면 22/22가 NAVER_OID_OUTLETS의
# 이름과 글자까지 같았다. oid가 표에 없는 매체의 폴백으로 쓴다.
_OG_AUTHOR_PATTERN = re.compile(
    r'<meta[^>]+property=["\']og:article:author["\'][^>]+content=["\']([^"\']*)["\']'
    r'|<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']og:article:author["\']',
    re.IGNORECASE,
)
_NAVER_AUTHOR_SUFFIX = "| 네이버"


def _outlet_from_page(text: str) -> Optional[str]:
    """페이지의 og:article:author로 언론사명을 읽는다. 없으면 None."""
    match = _OG_AUTHOR_PATTERN.search(text)
    if not match:
        return None
    author = html.unescape((match.group(1) or match.group(2) or "")).strip()
    if author.endswith(_NAVER_AUTHOR_SUFFIX):
        author = author[: -len(_NAVER_AUTHOR_SUFFIX)].strip()
    return author or None


# 「+ 수기로 기사 추가」가 받지 않는 주소 — 블로그·카페·커뮤니티·SNS 글은 언론사 기사가
# 아니다. 실측(2026-09-21): 네이버 검색 API로 뽑은 블로그 40건·카페 15건이 **전부** 그대로
# 담겼다(언론사 칸에 blog.naver.com 같은 도메인). 「언론사 이름을 못 찾으면 기사가 아니다」로
# 거르면 블로그·카페 55/55는 걸리지만 언론사 자체 도메인 기사 74건 중 22건(이투데이·
# 아주경제·부산일보·연합뉴스TV 등)도 같이 막혀 기각했다 — 그래서 페이지가 아니라 **주소의
# 호스트**로 판정한다. 목록에 없는 곳은 통과한다(담아둔 기사는 보고서 밖이라 담당자가 본다).
_NON_NEWS_HOSTS = (
    "blog.naver.com", "cafe.naver.com", "post.naver.com", "in.naver.com", "kin.naver.com",
    "tistory.com", "brunch.co.kr", "velog.io", "medium.com", "blog.daum.net", "cafe.daum.net",
    "dcinside.com", "theqoo.net", "fmkorea.com", "clien.net", "ruliweb.com", "instiz.net",
    "ppomppu.co.kr", "mlbpark.donga.com", "bobaedream.co.kr", "82cook.com", "etoland.co.kr",
    "youtube.com", "youtu.be", "x.com", "twitter.com", "facebook.com", "instagram.com",
    "threads.net", "threads.com",
)


def is_non_news_url(url: str) -> bool:
    """블로그·카페·커뮤니티·SNS 주소면 True — 호스트가 목록의 도메인이거나 그 하위 도메인
    (m.blog.naver.com, xxx.tistory.com, gall.dcinside.com)일 때."""
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("m."):
        host = host[2:]
    return any(host == h or host.endswith("." + h) for h in _NON_NEWS_HOSTS)


def fetch_article_by_url(url: str) -> Optional[dict]:
    """URL 하나로 기사 dict(언론사·제목·요약·발행시각)를 만든다 — "+ URL로 추가"의 본체.

    네이버 검색 API를 한 번도 안 부른다(담당자가 이미 찾아온 기사다). 페이지는 한 번만
    받아 제목·요약·발행시각·언론사를 거기서 다 읽는다.

    언론사는 resolve_outlet(네이버 oid → 도메인 추정)을 그대로 쓰되, 네이버 미러
    링크인데 oid가 표에 없으면 도메인 추정이 "n.news.naver.com"이라는 쓸모없는 값을
    내놓는다 — 그 경우에만 페이지의 og:article:author로 폴백한다.

    제목을 못 읽으면 None — 제목 없이는 보고서 한 줄을 세울 수 없다. 접속 실패도 None.
    """
    text = _fetch_page_html(url, timeout=5)
    if text is None:
        return None

    title = _best_title_from_html(text)
    if not title:
        return None

    summary = None
    og_desc = _OG_DESC_PATTERN.search(text)
    if og_desc:
        summary = (og_desc.group(1) or og_desc.group(2) or "").strip() or None

    outlet = resolve_outlet(url, url)
    if outlet == urlparse(url).netloc.removeprefix("www."):
        # 도메인 추정이 이름을 못 찾고 도메인 문자열을 그대로 돌려준 경우다.
        outlet = _outlet_from_page(text) or outlet

    article = {
        "outlet": outlet,
        "title": title,
        "url": url,
        "summary": html.unescape(summary) if summary else "",
    }
    pub_date = _pub_date_from_html(text)
    if pub_date:
        article["pub_date"] = pub_date
    # 네이버 미러면 「기사원문」 링크를 원문 주소로 적는다 — 실측(2026-09-22, 17건)에서
    # 네이버 API의 originallink와 전부 같은 값이었다.
    if urlparse(url).netloc.lower() in _NAVER_NEWS_HOSTS:
        m = _ORIGIN_LINK_PATTERN.search(text)
        if m:
            article["original_url"] = html.unescape(m.group(1)).strip()
    return article


_ORIGIN_LINK_PATTERN = re.compile(r'href="([^"]+)"[^>]*class="media_end_head_origin_link"')


def _url_host_key(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


def original_url_keys(url: str) -> set:
    """언론사 원문 주소를 「같은 기사인가」 대조용 키로 — 「+ 수기로 기사 추가」의 중복 판정.

    담당자가 붙여 넣는 원문 주소와 네이버가 준 originallink는 꼬리가 다를 수 있다
    (네이버 쪽엔 `utm_*`·`ref=A`·`input=1195m`이 붙는다). 키는 둘:
      1. 주소 전체 — `www.`·`m.`·끝 `/`·`#…`·`utm_*`만 뗀다. 나머지 쿼리는 기사 번호일
         수 있어(`idxno=`·`ncd=`·`newsId=`) 남긴다.
      2. 경로만 — 경로가 기사를 가리키는 모양일 때만(두 칸 이상, 마지막 칸이 6자 이상이고
         `articleView.html`·`view.do` 같은 파일 이름이 아닐 때). 실측(2026-09-22, 원문 주소
         1,144건): 이 모양 541건에서 서로 다른 기사가 같은 키가 된 경우 0건.
    """
    parsed = urlparse(url.strip())
    if not parsed.netloc:
        return set()
    host = _url_host_key(url)
    path = parsed.path.rstrip("/")
    query = "&".join(sorted(kv for kv in parsed.query.split("&") if kv and not kv.lower().startswith("utm_")))
    keys = {f"{host}{path}?{query}" if query else f"{host}{path}"}
    segs = [s for s in path.split("/") if s]
    if len(segs) >= 2 and len(segs[-1]) >= 6 and "." not in segs[-1]:
        keys.add(f"{host}{path}#path")
    return keys


def _parse_pub_date(pub_date_str: str) -> Optional[datetime]:
    """네이버 API의 pubDate(RFC 822 형식)를 파싱한다. 형식이 이상하면 None."""
    try:
        parsed = parsedate_to_datetime(pub_date_str)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_KST)
    return parsed


def _is_today_kst(pub_date: datetime) -> bool:
    return pub_date.astimezone(_KST).date() == datetime.now(_KST).date()


def kst_today_at(hhmm: str) -> datetime:
    """오늘(KST) 날짜에 "HH:MM" 시각을 합친 시간대 인식 datetime을 만든다.

    회차별 시간창 수집(app.scraper.collect_run)에서 창의 시작/끝 경계를 pub_date와
    비교할 때 쓴다. 실행 서버의 시스템 시간대와 무관하게 항상 KST 기준 "오늘"이다.
    """
    hour, minute = map(int, hhmm.split(":"))
    return datetime.now(_KST).replace(hour=hour, minute=minute, second=0, microsecond=0)


def incremental_search_after(last_seen: datetime, earliest: datetime, now: Optional[datetime] = None) -> datetime:
    """증분 검색(초안 app.preview_renderer / 실시간 현황 app.live_renderer)의 하한을 정한다.

    두 화면은 매번 처음부터 다시 검색하지 않고 "지금까지 본 가장 최신 pub_date"(last_seen)
    이후만 추가로 검색한다. 그런데 네이버는 기사를 **발행시각 순서대로 색인하지 않는다** —
    16:54 기사를 이미 받아온 뒤에 16:35 기사가 색인되는 일이 흔하다. last_seen을 그대로
    하한으로 쓰면 그런 기사는 `after < pub_date` 조건에 영영 걸리지 않아, 그 화면에서
    통째로 사라진다(초안에서는 "📂 소제목 미분류"로도 안 보이고, 회차 마감 수집에서야
    처음 나타나 "마감 후 자동 배정" 배지가 붙는다 — 실측·배경은 HISTORY.md 참고).

    그래서 last_seen과 "지금 − SEARCH_LOOKBACK_MIN(60분)" 중 **이른 쪽**을 하한으로 쓴다
    — 최근 한 시간 구간은 매 새로고침마다 다시 훑는다는 뜻이다. 다시 받아온 기사는 두
    호출부 모두 URL로 중복 제거하므로 화면·건수·matched_keywords는 달라지지 않는다.

    earliest: 그보다 더 내려가면 안 되는 바닥(초안은 그 회차의 시작 시각, 실시간 현황은
    당일 0시) — 창 밖 기사를 캐시에 섞어 넣지 않기 위한 하한이다.
    """
    now = now or datetime.now(_KST)
    return max(min(last_seen, now - timedelta(minutes=SEARCH_LOOKBACK_MIN)), earliest)


# 도메인 문자열이 긴(구체적인) 것부터 검사해야 한다 — 예: "koreajoongangdaily.joins.com"
# (코리아중앙데일리)이 그 자신의 상위 도메인 "joins.com"(중앙일보)으로 먼저 매칭되는
# 오판정을 막는다. 모듈 로드 시 한 번만 정렬해 매 호출마다 다시 정렬하지 않는다.
_SORTED_OUTLET_DOMAINS = sorted(OUTLET_DOMAINS.items(), key=lambda kv: len(kv[0]), reverse=True)


def _extract_outlet(url: str) -> str:
    """URL 도메인으로 언론사명을 *추정*한다. 목록에 없으면 도메인 자체를 그대로 쓴다.

    [수정: 2026-08-14] 이제 이건 폴백 경로다 — 우선 경로는 네이버 oid를 읽는
    outlet_from_naver_link()이고, 판별 진입점은 resolve_outlet()이다. 도메인만으로는
    한 회사가 도메인을 공유하는 계열 매체를 원리적으로 구분할 수 없다(매일경제/매경이코노미,
    동아일보/스포츠동아·신동아·주간동아, 경향신문/스포츠경향·주간경향 등).
    """
    domain = urlparse(url).netloc.removeprefix("www.")
    for known_domain, outlet_name in _SORTED_OUTLET_DOMAINS:
        # 서브도메인(biz.chosun.com)은 매칭하되, "notchosun.com"처럼 접미사만
        # 우연히 같은 무관한 도메인은 잘못 매칭되지 않도록 경계를 확인한다.
        if domain == known_domain or domain.endswith("." + known_domain):
            return outlet_name
    return domain


# 네이버 미러 링크의 경로에서 언론사 코드(oid)를 뽑는다. 실측상 경로는 전부
# "/mnews/article/{oid}/{기사ID}" 형태였지만, 과거 링크 형식("/article/...")도 함께 받는다.
_NAVER_ARTICLE_PATTERN = re.compile(r"^/(?:mnews/)?article/(\d+)/")


def outlet_from_naver_link(url: str) -> Optional[str]:
    """네이버 뉴스 미러 링크면 그 안의 언론사 코드(oid)로 언론사명을 확정한다.

    네이버 링크가 아니거나(언론사 자체 도메인) 표에 없는 oid면 None — 호출하는 쪽이
    도메인 추정으로 폴백한다. 도메인 추정과 달리 이건 "추정"이 아니라 네이버가 부여한
    매체 고유 번호를 그대로 읽는 것이라 계열 매체도 정확히 갈린다(NAVER_OID_OUTLETS 참고).
    """
    parsed = urlparse(url)
    if parsed.netloc.removeprefix("www.") != "n.news.naver.com":
        return None
    match = _NAVER_ARTICLE_PATTERN.match(parsed.path)
    if not match:
        return None
    return NAVER_OID_OUTLETS.get(match.group(1))


def resolve_outlet(naver_link: str, original_link: str) -> str:
    """기사 하나의 언론사명을 정한다 — 네이버 oid 우선, 없으면 도메인 추정 폴백.

    [추가: 2026-08-14] 기존엔 original_link의 도메인만 봤다. 실측 표본 3,947건 대조에서
    도메인 방식이 36건(0.9%)을 다른 언론사 이름으로 잘못 붙이고 있었고(매경이코노미→매일경제
    18건 등), 587건(14.9%)은 이름 대신 도메인 문자열이 그대로 찍히고 있었다. 두 문제 모두
    oid 경로가 해결한다. 표본 기사의 43%가 네이버 미러 링크였고 나머지는 oid가 아예 없어
    (네이버 뉴스 채널 미제휴 매체) 도메인 폴백이 계속 필요하다 — 그쪽은 도메인이 매체마다
    갈려서 지금 방식으로 이미 맞는다(뉴스핌·아시아투데이·땅집고·IT조선·코리아타임스 확인).
    """
    return outlet_from_naver_link(naver_link) or _extract_outlet(original_link)


def _strip_naver_query(url: str) -> str:
    """네이버 뉴스(n.news.naver.com) 링크의 ?sid= 같은 부가 쿼리스트링을 지운다.

    기사 자체는 경로(/mnews/article/{언론사코드}/{기사ID})만으로 이미 특정되고,
    sid는 "어느 섹션에서 봤는지"를 나타내는 부가 정보라 지워도 같은 기사로 연결된다.
    그 외 언론사 자체 도메인(예: ?idxno=, ?no=)은 쿼리스트링 자체가 그 기사를 찾아가는
    핵심 라우팅 정보인 경우가 많아 손대지 않는다 — n.news.naver.com일 때만 적용한다.
    """
    parsed = urlparse(url)
    if parsed.netloc != "n.news.naver.com":
        return url
    return urlunparse(parsed._replace(query="", fragment=""))


_NAVER_NEWS_HOSTS = {"n.news.naver.com", "m.news.naver.com", "news.naver.com"}
_NAVER_ARTICLE_PATH_RE = re.compile(r"^/(?:mnews/)?(?:hotissue/)?article/(\d{3})/(\d{10})/?$")
_NAVER_OID_AID_RE = re.compile(r"(?:^|&)(oid|aid)=(\d+)")


def normalize_article_url(url: str) -> str:
    """담당자가 붙여 넣은 기사 주소를 수집이 저장하는 모양으로 맞춘다 — 「+ 수기로 기사 추가」용.

    수집은 네이버 뉴스 기사를 `https://n.news.naver.com/mnews/article/{oid}/{aid}` 한
    모양으로만 저장한다(_strip_naver_query). 그런데 브라우저에서 복사한 주소는
    `?sid=101` 꼬리가 붙거나, `/article/…`(mnews 없음)·`m.news.naver.com`·옛
    `read.naver?oid=&aid=` 모양이다 — 그대로 두면 같은 기사가 URL 대조(중복 거부·숨김·
    이미 실림)를 전부 빠져나간다. 네이버 뉴스 기사로 확실히 읽힐 때만 바꾸고, 그 밖의
    주소(언론사 자체 도메인 — 쿼리가 곧 기사 번호일 수 있다)는 손대지 않는다.
    """
    parsed = urlparse(url.strip())
    if parsed.netloc.lower() not in _NAVER_NEWS_HOSTS:
        return url.strip()
    m = _NAVER_ARTICLE_PATH_RE.match(parsed.path)
    if m:
        oid, aid = m.groups()
    else:
        params = dict(_NAVER_OID_AID_RE.findall(parsed.query))
        oid, aid = params.get("oid"), params.get("aid")
        if not (oid and aid and len(oid) == 3 and len(aid) == 10):
            return url.strip()
    return f"https://n.news.naver.com/mnews/article/{oid}/{aid}"


def _search_one_keyword(
    keyword: str,
    after: Optional[datetime] = None,
    before: Optional[datetime] = None,
    include_unparsed_dates: bool = False,
) -> tuple[list[dict], bool]:
    """
    한 키워드로 검색해 당일(KST) 게시된 기사만 모은다 (PRD.md 기능1 규칙 2).

    after/before를 주면(회차별 시간창 수집, PRD 규칙 2 갱신) "after < pub_date <= before"
    구간의 기사만 남긴다 — 경계 기사가 인접한 두 창에 겹쳐 들어가지 않도록 하한은
    제외, 상한은 포함한다.

    sort="date"로 최신순 정렬해서 받으므로, "당일보다 오래됐거나(after 이하)"인 기사가
    한 건이라도 나오면 그 뒤로는 전부 더 오래된 기사다 — 그 지점에서 이 키워드의
    페이지네이션을 그만둔다(불필요한 API 호출을 줄인다). before보다 새 기사(보충 실행이라
    실제 시각이 창 끝을 지난 경우)는 이 창의 몫이 아니므로 건너뛰되, 그 뒤에 이 창에 속한
    기사가 더 있을 수 있어 페이지네이션은 계속한다.

    [추가: 2026-08-14] pubDate를 못 읽는 낱개 기사는 기본적으로 그 기사만 건너뛴다
    (페이지네이션 중단 신호로도 쓰지 않는다) — 시각을 모르면 당일 창인지도, after/before
    구간에 속하는지도 판단할 수 없어 회차별 시간창 수집(app.scraper.collect_run)에는
    절대 넣으면 안 된다. include_unparsed_dates=True를 넘긴 호출부(app.live_renderer,
    실시간 현황 전용)만 이 기사를 `pub_date: None`으로 담아 결과에 포함한다 — "실시간은
    필터 없이 날것 그대로"라는 이 화면의 원칙상, 시각을 못 읽었다는 기계적 사정으로
    기사 자체를 조용히 버리면 안 된다는 사용자 결정에 따른 것이다(담당자가 원문을
    직접 열어 발행일을 확인하도록 화면에 "발행시각 불명"으로 표시한다). 정기 스크랩·
    수시 모니터링은 이 플래그를 안 쓰므로 기존 동작(건너뜀) 그대로다.

    반환값은 (기사 목록, 조회 상한에 걸려 잘렸는지) — [수정: 2026-08-24] 예전엔 기사
    목록만 돌려주고 "잘렸는지"는 로그로만 남겨, 이걸 다시 알아야 하는 호출부
    (app.adhoc.collector의 §6.4b AND 상한 거부)가 `len(results) >= _MAX_START`로
    직접 재추정했다. 그 추정은 이 함수가 시간창 밖 기사(continue)나 pubDate를 못 읽은
    기사(건너뜀)를 스킵하면서도 `start`는 그대로 올리는 경우를 놓친다 — 그런 기사가
    하나만 섞여도 `results`가 1,000건 밑으로 떨어져 실제로는 상한에 걸렸는데도
    "안 걸렸다"고 오판했다(§6.4b가 막으려던 바로 그 상황이 새는 통로였다). 이 함수가
    이미 정확히 알고 있는 `start > _MAX_START` 판정을 그대로 반환값에 실어, 다시
    추정하지 않고 그대로 받아 쓰게 한다.
    """
    # [추가: 2026-08-20] 요청 직전에 매번 다시 읽는다(app.credentials가 설정 화면
    # 저장값을 즉시 반영하므로, 모듈 최상단에서 한 번만 읽어 캐싱하면 안 된다 — 이
    # 프로젝트에서 이미 app.storage.ARTICLES_DIR이 같은 이유로 호출 시점 조회를
    # 쓴다). 키가 아예 없으면 요청을 시도조차 하지 않고 바로 실패시킨다.
    if not naver_is_configured():
        raise NaverNotConfiguredError()
    headers = {
        "X-Naver-Client-Id": naver_client_id(),
        "X-Naver-Client-Secret": naver_client_secret(),
    }
    results = []
    start = 1
    # [추가: 2026-08-18] 조회 상한에 걸렸을 때 "어디까지 내려갔는지"를 로그에 남기기 위한 값.
    # 최신순 정렬이라 뒤로 갈수록 오래된 기사이므로, 마지막으로 본 날짜가 곧 도달 지점이다.
    deepest_pub_date: Optional[datetime] = None
    while start <= _MAX_START:
        # _GLOBAL_REQUEST_SEM — 네이버 뉴스 검색 API 호출 전체(정기·수시 통틀어)를
        # 하나의 상한으로 묶는다. _fetch_full_title/fetch_full_title_and_summary는
        # 네이버가 아니라 개별 언론사 사이트를 호출하므로 여기 걸지 않는다 — 무관한
        # 병목이 생긴다.
        with _GLOBAL_REQUEST_SEM:
            response = requests.get(
                NAVER_NEWS_API_URL,
                headers=headers,
                params={"query": keyword, "display": _MAX_DISPLAY, "start": start, "sort": "date"},
                timeout=10,
            )
        # [추가: 2026-08-20] 정기 스크랩·실시간 현황·수시 모니터링·[단독]·[속보] 폴링
        # 전부가 결국 여기 하나로 모이는 유일한 실제 요청 지점이다 — app.api_usage가
        # 일일 호출 한도 소진을 실측으로 감시할 수 있는 것도 이 한 곳에서만 세기
        # 때문이다. 429 등 실패 응답도 네이버에 도달한 호출이므로 raise_for_status
        # 이전에 센다.
        record_api_call()
        response.raise_for_status()
        items = response.json().get("items", [])
        if not items:
            break

        reached_older_article = False
        # [수정: 2026-08-24] 제목이 "..."로 잘린 기사마다 그 자리에서 _fetch_full_title을
        # 동기로(하나씩 순서대로) 불렀었다 — 언론사 사이트 응답이 느리거나 안 뜨면 호출당
        # 최대 3초까지 그대로 블로킹됐다. 실측(2026-08-24, 키워드 '정부' 1페이지 100건):
        # 24건이 제목이 잘려 있었다 — 이 페이지 하나만으로도 최악의 경우(전부 타임아웃)
        # 72초가 걸릴 수 있는 구조였고, 실제로 수시 모니터링의 "꼭 포함할 검색어" 추가
        # 하나가 66초 걸린 사례가 있었다(대부분의 시간이 이 순차 재요청이었다). 이 페이지
        # 안에서 "포함할 기사"로 확정된 것들의 제목 재요청은 서로 아무 의존관계가 없으므로
        # (다른 언론사 사이트로 흩어져 있어 네이버 API 호출 상한 `_GLOBAL_REQUEST_SEM`과도
        # 무관하다), 이 창에 속하는지부터 먼저 순차로 가려낸 뒤(pending) 제목 재요청만
        # 한꺼번에 동시로 보낸다.
        pending: list[tuple[dict, Optional[datetime]]] = []
        for item in items:
            pub_date = _parse_pub_date(item.get("pubDate", ""))
            if pub_date is None:
                if include_unparsed_dates:
                    pending.append((item, None))
                continue  # 날짜를 못 읽으면 이 기사만 건너뛴다 (페이지네이션 중단 신호로는 안 씀)
            deepest_pub_date = pub_date
            if not _is_today_kst(pub_date):
                reached_older_article = True
                break  # 최신순 정렬이므로 여기서부터는 전부 더 오래된 기사
            if after is not None and pub_date <= after:
                reached_older_article = True
                break  # 이 창의 하한보다 오래된 기사 -> 여기서부터는 전부 이전 창(또는 그 이전)
            if before is not None and pub_date > before:
                continue  # 이 창보다 나중(보충 실행 등) -> 건너뛰되 페이지네이션은 계속
            pending.append((item, pub_date))

        truncated_urls = list(
            {
                _strip_naver_query(item["link"])
                for item, _ in pending
                if _clean_text(item["title"]).endswith("...")
            }
        )
        # [추가: 2026-07-27] 네이버 API가 제목을 "..."로 잘라 보내는 경우, 그 기사의
        # 실제 페이지에서 온전한 제목을 다시 가져온다. 잘리지 않은 제목이 대다수라 이
        # 추가 요청은 잘린 것에만 발생하고, 실패해도(다음 줄 or title) 원래 제목을 쓴다.
        refetched_titles: dict[str, str] = {}
        if truncated_urls:
            with ThreadPoolExecutor(max_workers=min(len(truncated_urls), _TITLE_REFETCH_CONCURRENCY)) as executor:
                for url, full_title in zip(truncated_urls, executor.map(_fetch_full_title, truncated_urls)):
                    if full_title:
                        refetched_titles[url] = full_title

        for item, pub_date in pending:
            title = _clean_text(item["title"])
            url = _strip_naver_query(item["link"])
            if title.endswith("..."):
                title = refetched_titles.get(url) or title
            results.append(
                {
                    # [수정: 2026-08-14] 예전엔 "네이버 미러 링크는 도메인이 전부
                    # n.news.naver.com이라 언론사를 알 수 없다"고 보고 원본 링크의
                    # 도메인만 썼는데, 미러 링크의 *경로*에는 언론사 코드(oid)가 박혀
                    # 있다. 그쪽이 계열 매체까지 정확하므로 oid를 먼저 보고, 없을 때만
                    # 원본 링크 도메인으로 폴백한다(resolve_outlet).
                    "outlet": resolve_outlet(item["link"], item["originallink"]),
                    "title": title,
                    "url": url,
                    "summary": _clean_text(item["description"]),
                    # [추가: 2026-07-25] 화면에 게시일자를 직접 표시하진 않지만(PRD 규칙5),
                    # 실시간 기사 현황의 최신순 정렬과 "몇 분 전" 표시(화면 전용, 복사/내보내기
                    # 텍스트에는 안 들어감)에 쓰려고 파싱된 값을 그대로 실어 보낸다.
                    "pub_date": pub_date.isoformat() if pub_date else None,
                }
            )
            # 네이버 미러 기사면 언론사 원문 주소도 적어 둔다 — 담당자가 원문 주소로
            # 「+ 수기로 기사 추가」를 했을 때 같은 기사인지 알아볼 유일한 단서다(original_url_keys).
            original = (item.get("originallink") or "").strip()
            if original and original != url and urlparse(url).netloc.lower() in _NAVER_NEWS_HOSTS:
                results[-1]["original_url"] = original

        if reached_older_article or len(items) < _MAX_DISPLAY:
            break
        start += _MAX_DISPLAY

    # [추가: 2026-08-18] start는 루프 맨 아래에서만 증가하므로, break로 빠져나왔다면
    # 여전히 _MAX_START 이하다. _MAX_START를 넘겼다는 건 "창의 하한에 닿기 전에 네이버
    # 조회 상한(최신 1000건)에 먼저 걸렸다"는 뜻 — 더 오래된 기사가 남아 있어도 API가
    # 더 주지 않아 결과가 조용히 잘린다(실패로도 안 잡힌다).
    #
    # 화면 경고는(정기·실시간·breaking alert 쪽은) 아직 만들지 않았다 — 이 로그가
    # 유일한 안내다. 실측(2026-08-18, 등록 키워드 9개)에서 상한에 닿는 건 '정부'
    # 하나뿐이었고(약 284건/시간 · 하루 6,800건 환산), 나머지 8개는 1000번째 기사가
    # 수 주~3개월 전이라 근처에도 못 갔다. 정기 회차는 창이 짧아 걸릴지 애매해(새벽 창은
    # 낮 속도로 추정한 값이라 과대추정), 실제로 얼마나 자주 어느 키워드에서 일어나는지를
    # 이 로그로 먼저 모은 뒤 화면 표시 여부를 정하기로 했다. 수시 모니터링만 예외 —
    # 아래 반환값을 app.adhoc.collector가 §6.4b(AND 상한 거부) 판정에 그대로 쓴다.
    # 배경·수치는 HISTORY.md "네이버 조회 상한(1000건) 잘림" 참고.
    capped = start > _MAX_START
    if capped:
        logger.warning(
            "네이버 조회 상한(최신 %d건)에 걸려 결과가 잘렸습니다 — 키워드=%r, 요청창=%s~%s, "
            "%s까지만 수집됨(%d건). 더 오래된 기사는 API가 주지 않습니다.",
            _MAX_START,
            keyword,
            after.strftime("%m-%d %H:%M") if after else "당일 0시",
            before.strftime("%m-%d %H:%M") if before else "현재",
            deepest_pub_date.strftime("%m-%d %H:%M") if deepest_pub_date else "?",
            len(results),
        )
    return results, capped


def _match_group(group_results: list[list[dict]], mode: str) -> list[dict]:
    """한 그룹 안에서 키워드별 검색 결과를 그 그룹의 OR/AND 방식으로 합친다.

    mode="OR": 키워드 중 하나라도 포함된 기사를 모두 모은다 (합집합).
    mode="AND": 키워드를 모두 포함한 기사만 남긴다 (URL 기준 교집합).
    키워드가 1개뿐이면 OR/AND 결과는 같다.

    주의: 각 키워드는 최신 1000건까지만 조회하므로(_MAX_START), 어느 한 키워드의
    당일 기사량이 1000건을 넘으면 두 키워드 모두에 실린 기사라도 교집합에서
    누락될 수 있다. "재정경제부"/"재경부" 수준의 키워드에서는 발생 가능성이 낮다.
    """
    if mode == "OR" or len(group_results) <= 1:
        return [article for results in group_results for article in results]

    url_sets = [{a["url"] for a in results} for results in group_results]
    common_urls = set.intersection(*url_sets)
    return [article for results in group_results for article in results if article["url"] in common_urls]


def search_keywords(
    keywords: list[str],
    after: Optional[datetime] = None,
    before: Optional[datetime] = None,
    include_unparsed_dates: bool = False,
) -> tuple[dict[str, list[dict]], list[str], list[str]]:
    """
    키워드 목록 각각을 검색해 {키워드: 검색결과} 딕셔너리로 돌려준다. 그룹 OR/AND
    매칭(app.naver_api.match_articles_to_groups)과 분리된 순수 검색 단계다 —
    [추가: 2026-08-13] app.live_renderer가 키워드별로 다른 after(마지막으로 본 시각)를
    쓰는 증분 캐시를 하려면 그룹 단위가 아니라 키워드 단위로 검색을 걸 수 있어야
    해서 분리했다(search_articles_by_groups는 이 함수를 감싼 하위호환 래퍼).

    after: datetime 하나면 모든 키워드에 동일하게 적용한다. {키워드: datetime|None}
    딕셔너리면 키워드마다 다른 하한을 쓴다(없는 키워드나 값이 None이면 당일 0시부터,
    즉 하한 없음) — 새로 등록된 키워드는 기존 키워드의 "마지막으로 본 시각"을 몰라서
    처음부터(당일 0시) 검색해야 하는 경우에 쓴다.

    [수정: 2026-08-06] 키워드를 하나씩 순서대로 검색하면 키워드 수만큼 대기 시간이
    그대로 더해진다(13개 기준 약 24초 실측) — 동시에 최대 _MAX_CONCURRENT_KEYWORD_
    SEARCHES개까지 병렬로 검색한다. [수정: 2026-08-13] 실측(100키워드, 재시도 없이
    1회 시도 기준)으로 동시성별 429 발생률을 쟀다 — 5: 0%, 10: 5%, 15: 20%, 20: 86%.
    15부터 사실상 못 쓰는 수준이라 "느린 만큼 늘려도 된다"는 예전 가정을 버리고 5→8로만
    올렸다(재시도로 흡수 가능한 수준의 여유만 더 확보). 키워드 하나가 실패(타임아웃·
    네트워크 오류·429)해도 나머지 결과는 그대로 살리고 빈 목록으로 대체한다 — 병렬화
    전엔 한 키워드 실패가 곧 전체 검색 실패였는데, 여러 개를 동시에 돌리면서 그 실패가
    상대적으로 더 자주 눈에 띌 수 있어 이 기회에 격리했다.
    [수정: 2026-08-13] 격리는 유지하되, 포기하기 전에 스레드 안에서 짧게 재시도한다
    (429는 Retry-After만큼, 그 외는 짧은 backoff) — 그래도 실패하면 빈 목록으로
    대체하는 건 같지만, 이번엔 "실패했다"는 사실 자체를 failed_keywords로 같이 돌려줘
    위 계층이 조용히 넘어가지 않게 한다(정상 검색됐지만 결과가 0건인 것과는 다르다).

    include_unparsed_dates: _search_one_keyword로 그대로 전달한다 — 기본 False(정기
    스크랩·수시 모니터링과 동일하게 pubDate 파싱 실패 기사를 건너뜀), app.live_renderer만
    True로 호출해 그 기사를 `pub_date: None`으로 담아 온다.

    반환값 세 번째 요소 capped_keywords: [추가: 2026-08-24] `_search_one_keyword`가
    네이버 조회 상한(1,000건)에 걸려 결과가 잘린 것으로 판정한 키워드 이름 목록 —
    실패(failed_keywords)와는 다른 축이다(검색 자체는 성공했지만 결과가 불완전하다는
    뜻). app.adhoc.collector의 §6.4b AND 상한 거부가 이 값을 그대로 쓴다. 기존
    호출부(app.breaking_alert_sender, app.live_renderer 등)는 이 값을 안 쓰고 버려도
    되지만, 2-튜플 언패킹은 더 이상 안 맞으므로 호출부 전부를 3-튜플로 맞췄다.
    """
    def _after_for(keyword: str) -> Optional[datetime]:
        if isinstance(after, dict):
            return after.get(keyword)
        return after

    def _search_safely(keyword: str) -> tuple[list[dict], bool, bool]:
        keyword_after = _after_for(keyword)
        last_error: Optional[requests.exceptions.RequestException] = None
        for attempt in range(_KEYWORD_RETRY_ATTEMPTS):
            try:
                results, capped = _search_one_keyword(
                    keyword, after=keyword_after, before=before, include_unparsed_dates=include_unparsed_dates
                )
                return results, False, capped
            except requests.exceptions.RequestException as error:
                last_error = error
                if attempt < _KEYWORD_RETRY_ATTEMPTS - 1 and _retryable(error):
                    time.sleep(_retry_wait_seconds(error, attempt))
                    continue
                break
        logger.warning("키워드 검색 실패(재시도 소진), 빈 결과로 대체합니다: %r", keyword, exc_info=last_error)
        return [], True, False

    with ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_KEYWORD_SEARCHES) as executor:
        raw_by_keyword = dict(zip(keywords, executor.map(_search_safely, keywords)))
    results_by_keyword = {keyword: results for keyword, (results, _failed, _capped) in raw_by_keyword.items()}
    failed_keywords = [keyword for keyword, (_results, failed, _capped) in raw_by_keyword.items() if failed]
    capped_keywords = [keyword for keyword, (_results, _failed, capped) in raw_by_keyword.items() if capped]
    return results_by_keyword, failed_keywords, capped_keywords


def match_articles_to_groups(
    groups: list[dict],
    results_by_keyword: dict[str, list[dict]],
    track_keyword_matches: bool = False,
) -> list[dict]:
    """
    키워드별 원시 검색 결과(app.naver_api.search_keywords)에 그룹 OR/AND 규칙을 적용해
    최종 기사 목록을 만든다(URL 기준 중복 제거 포함) — 검색과 매칭을 분리해뒀다.

    groups: [{"keywords": [...], "mode": "OR"|"AND"}, ...] 형태. 그룹 "안"에서는 그 그룹의
    mode(OR=하나라도 포함/AND=모두 포함)를 따르고, 그룹 "사이"는 항상 OR이다.

    같은 기사가 여러 그룹에 걸려 중복 수집돼도 URL 기준으로 한 번만 남긴다(먼저 나온
    그룹 순서 유지).

    [추가: 2026-08-13] results_by_keyword를 검색 시점이 아니라 여기서 다시 넘겨받는
    구조라, app.live_renderer처럼 키워드별 원시 결과를 캐시해뒀다가 그룹 구성(이름·
    모드·소속)만 바뀌었을 땐 재검색 없이 이 함수만 다시 돌리면 새 구성이 정확히
    반영된다(재분류가 재검색을 요구하지 않는다) — results_by_keyword.get(keyword, [])로
    조회해, 그룹에 있지만 캐시에 없는 키워드(예: 이번에 막 추가돼 아직 결과가 없는
    키워드)는 조용히 빈 결과로 취급한다.

    track_keyword_matches: True면 각 기사 dict에 "matched_keywords"(그 기사가 실제로
    걸린 모든 키워드 목록)를 덧붙인다 — app.live_renderer의 그룹 필터 칩용. 위 URL
    중복 제거는 "어느 그룹에 배정할지"만 정할 뿐 실제로 여러 키워드에 걸린 사실
    자체는 사라지지 않으므로, results_by_keyword를 다시 훑어 URL별로 어떤 키워드의
    검색 결과에 있었는지 모은다(그룹 배정과 무관한, 사실 그대로의 매칭 정보).
    """
    seen_urls: set = set()
    articles: list[dict] = []
    for group in groups:
        group_results = [results_by_keyword.get(keyword, []) for keyword in group["keywords"]]
        for article in _match_group(group_results, group.get("mode", "OR")):
            if article["url"] not in seen_urls:
                seen_urls.add(article["url"])
                articles.append(article)

    if track_keyword_matches:
        url_keywords: dict[str, list[str]] = {}
        for keyword, results in results_by_keyword.items():
            for a in results:
                url_keywords.setdefault(a["url"], []).append(keyword)
        for article in articles:
            article["matched_keywords"] = url_keywords.get(article["url"], [])

    return articles


def search_articles_by_groups(
    groups: list[dict],
    after: Optional[datetime] = None,
    before: Optional[datetime] = None,
    track_keyword_matches: bool = False,
) -> tuple[list[dict], list[str]]:
    """
    키워드 그룹 목록으로 기사를 검색해 모아 반환한다 (건수 제한 없음, PRD.md 기능1 규칙 2 갱신).
    app.naver_api.search_keywords + match_articles_to_groups를 그대로 이어붙인
    하위호환 래퍼 — app.scraper.collect_run처럼 매번 통째로 새로 검색해도 되는
    (캐시가 필요 없는) 호출부는 이 함수 하나로 충분하다.

    after/before: 회차별 시간창 수집의 하한(제외)·상한(포함) — app.scraper.collect_run이
    설정된 스케줄 창에서 계산해 넘겨준다. 둘 다 생략하면 당일 전체를 그대로 모은다.

    같은 키워드가 여러 그룹에 중복 등록돼 있어도 네이버 API 호출은 키워드당 한 번만
    한다 (그룹이 최대 6개(기관정보+5)·그룹당 최대 5개까지 늘어날 수 있어, 호출 낭비를
    줄이는 게 응답 지연에 직접 영향을 준다).

    반환값은 (articles, failed_keywords) — failed_keywords는 재시도(_KEYWORD_RETRY_
    ATTEMPTS회)를 다 써도 끝내 실패한 키워드 이름 목록이다(정상적으로 검색됐지만
    결과가 0건인 것과는 다르다). 비어 있으면 전부 성공했다는 뜻. 실패를 어떻게 다룰지
    (재시도할지, 경고만 띄울지)는 호출부의 몫이다 — 이 함수는 "무엇이 실패했는지"만
    사실대로 보고한다.
    """
    unique_keywords: list[str] = []
    seen_keywords = set()
    for group in groups:
        for keyword in group["keywords"]:
            if keyword not in seen_keywords:
                seen_keywords.add(keyword)
                unique_keywords.append(keyword)

    # capped_keywords는 이 래퍼의 반환 계약(articles, failed_keywords)에 없던 값이라
    # 여기선 버린다 — 정기 스크랩·랜딩 등 이 래퍼를 쓰는 호출부는 상한 판정을 아직
    # 안 쓴다(위 search_keywords 반환값 3번째 요소 docstring 참고). 수시 모니터링은
    # 이 래퍼가 아니라 search_keywords를 직접 불러(app.adhoc.collector) 값을 받는다.
    results_by_keyword, failed_keywords, _capped_keywords = search_keywords(unique_keywords, after=after, before=before)
    articles = match_articles_to_groups(groups, results_by_keyword, track_keyword_matches=track_keyword_matches)
    return articles, failed_keywords
