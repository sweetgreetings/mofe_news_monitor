# Design Ref: PRD.md 기능1 규칙 2 — 키워드 그룹 중 하나라도 조건을 만족하면 수집(그룹간 OR), 건수 제한 없음, 당일 기사만
import html
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import urlparse, urlunparse

import requests

from app.config import COLOR_ERROR, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET

NAVER_NEWS_API_URL = "https://openapi.naver.com/v1/search/news.json"
_MAX_DISPLAY = 100  # 네이버 API가 허용하는 1회 요청 최대 건수
_MAX_START = 1000  # 네이버 API가 허용하는 최대 조회 시작 위치 (이 이상은 API 자체가 지원 안 함)

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
    # [추가: 2026-08-03] 조세일보 — 사용자 요청으로 경제일간 카테고리 맨 끝에 추가.
    "joseilbo.com": "조세일보",
}

# 설정 화면의 "언론사 선택" 카테고리별 목록 (PRD.md 기능1 규칙 16). 사용자가 준 순서를 그대로 따른다.
# OBS·이투데이·내일신문은 사용자 요청으로 목록에서 제외됨. [수정: 2026-07-23] 순서 갱신.
# [수정: 2026-07-29] "[7.28. 추가된 언론사]" 임시 카테고리를 없애고 아래처럼 재분류했다.
OUTLET_CATEGORIES = {
    "방송사": ("KBS", "MBC", "SBS", "YTN", "MBN", "JTBC", "TV조선"),
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
    "통신사 및 종합 일간지": ("연합뉴스", "뉴시스", "뉴스1", "노컷뉴스", "뉴스핌", "더팩트"),
    "전문일간": ("전자신문", "디지털타임스"),
    "기타 언론사": ("프레시안", "데일리안", "매일신문", "농민신문", "땅집고"),
    # [추가: 2026-07-29] 신규 카테고리 — 주간지. 매경이코노미는 아직 도메인을 확인하지
    # 못해 OUTLET_DOMAINS에는 등록하지 못했다(사용자 확인 필요) — 이름만 선택 목록에
    # 올려두고, 도메인이 확정되면 OUTLET_DOMAINS에 추가하면 된다.
    "주간지": ("한경비즈니스", "주간조선", "매경이코노미", "시사저널"),
}

ALL_OUTLET_NAMES = frozenset(name for names in OUTLET_CATEGORIES.values() for name in names)

# [추가: 2026-07-29] mk.co.kr 도메인은 매일경제(일간지)·매경이코노미(주간지)가 같이 쓰는데,
# 언론사 판별은 도메인만 보고 하다 보니(_extract_outlet) 둘을 구분할 방법이 없다 — 매경이코노미
# 기사도 그냥 "매일경제"로 잡힌다. 화면에 "매일경제"로 보이는 기사가 실은 매경이코노미일 수
# 있다는 걸 이용자가 한 번 더 확인하도록 화면 표시용 텍스트에만 표식을 붙인다. 아래에서
# 저장/비교에 쓰이는 outlet 원본 값(화이트리스트 비교, 우선순위 정렬 등)은 그대로 두고,
# 복사/내보내기 텍스트에도 넣지 않는다 — 순전히 화면 확인용.
_MK_AMBIGUOUS_OUTLET = "매일경제"


def outlet_display_label(outlet: str) -> str:
    """화면에 보여줄 언론사 이름 HTML 조각 — 매일경제만 매경이코노미와 섞였을 가능성을
    알리기 위해 빨간 글자로 강조한다(mk.co.kr 도메인을 두 매체가 같이 써서 도메인만으론
    구분이 안 됨, app.naver_api._extract_outlet 참고).

    [수정: 2026-07-30] 처음엔 "매일경제**"처럼 문자만 덧붙였는데, 눈에 잘 안 띈다는
    피드백으로 빨간 글자 색으로 바꿨다. 이미 완성된(이스케이프된) HTML 조각을
    돌려주므로, 호출하는 쪽(app.renderer.render_article 등)에서 이 결과를 다시
    html.escape()하면 안 된다 — 그러면 <span> 태그가 그대로 글자로 보이게 된다.
    """
    escaped = html.escape(outlet)
    if outlet == _MK_AMBIGUOUS_OUTLET:
        return f'<span style="color:{COLOR_ERROR}">{escaped}</span>'
    return escaped


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
    try:
        response = requests.get(url, timeout=3, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
    except requests.exceptions.RequestException:
        return None
    # 언론사 사이트가 응답 헤더에 charset을 안 밝히면 requests가 기본값(ISO-8859-1)으로
    # 잘못 짐작해 한글이 깨진다 — apparent_encoding(내용 기반 추정)으로 보정한다.
    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding
    return _best_title_from_html(response.text)


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
    try:
        response = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
    except requests.exceptions.RequestException:
        return None
    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding
    text = response.text

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


# 도메인 문자열이 긴(구체적인) 것부터 검사해야 한다 — 예: "koreajoongangdaily.joins.com"
# (코리아중앙데일리)이 그 자신의 상위 도메인 "joins.com"(중앙일보)으로 먼저 매칭되는
# 오판정을 막는다. 모듈 로드 시 한 번만 정렬해 매 호출마다 다시 정렬하지 않는다.
_SORTED_OUTLET_DOMAINS = sorted(OUTLET_DOMAINS.items(), key=lambda kv: len(kv[0]), reverse=True)


def _extract_outlet(url: str) -> str:
    """URL 도메인으로 언론사명을 추정한다. 목록에 없으면 도메인 자체를 그대로 쓴다."""
    domain = urlparse(url).netloc.removeprefix("www.")
    for known_domain, outlet_name in _SORTED_OUTLET_DOMAINS:
        # 서브도메인(biz.chosun.com)은 매칭하되, "notchosun.com"처럼 접미사만
        # 우연히 같은 무관한 도메인은 잘못 매칭되지 않도록 경계를 확인한다.
        if domain == known_domain or domain.endswith("." + known_domain):
            return outlet_name
    return domain


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


def _search_one_keyword(
    keyword: str, after: Optional[datetime] = None, before: Optional[datetime] = None
) -> list[dict]:
    """
    한 키워드로 검색해 당일(KST) 게시된 기사만 모은다 (PRD.md 기능1 규칙 2).

    after/before를 주면(회차별 시간창 수집, PRD 규칙 2 갱신) "after < pub_date <= before"
    구간의 기사만 남긴다 — 경계 기사가 인접한 두 창에 겹쳐 들어가지 않도록 하한은
    제외, 상한은 포함한다.

    sort="date"로 최신순 정렬해서 받으므로, "당일보다 오래됐거나(after 이하)"인 기사가
    한 건이라도 나오면 그 뒤로는 전부 더 오래된 기사다 — 그 지점에서 이 키워드의
    페이지네이션을 그만둔다(불필요한 API 호출을 줄인다). before보다 새 기사(보충 실행이라
    실제 시각이 창 끝을 지난 경우)는 이 창의 몫이 아니므로 건너뛰되, 그 뒤에 이 창에 속한
    기사가 더 있을 수 있어 페이지네이션은 계속한다. pubDate를 못 읽는 낱개 기사는 그
    기사만 건너뛴다(페이지네이션 중단 신호로 쓰지 않는다).
    """
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    results = []
    start = 1
    while start <= _MAX_START:
        response = requests.get(
            NAVER_NEWS_API_URL,
            headers=headers,
            params={"query": keyword, "display": _MAX_DISPLAY, "start": start, "sort": "date"},
            timeout=10,
        )
        response.raise_for_status()
        items = response.json().get("items", [])
        if not items:
            break

        reached_older_article = False
        for item in items:
            pub_date = _parse_pub_date(item.get("pubDate", ""))
            if pub_date is None:
                continue  # 날짜를 못 읽으면 이 기사만 건너뛴다
            if not _is_today_kst(pub_date):
                reached_older_article = True
                break  # 최신순 정렬이므로 여기서부터는 전부 더 오래된 기사
            if after is not None and pub_date <= after:
                reached_older_article = True
                break  # 이 창의 하한보다 오래된 기사 -> 여기서부터는 전부 이전 창(또는 그 이전)
            if before is not None and pub_date > before:
                continue  # 이 창보다 나중(보충 실행 등) -> 건너뛰되 페이지네이션은 계속
            title = _clean_text(item["title"])
            url = _strip_naver_query(item["link"])
            # [추가: 2026-07-27] 네이버 API가 제목을 "..."로 잘라 보내는 경우, 그 기사의
            # 실제 페이지에서 온전한 제목을 다시 가져온다(_fetch_full_title). 잘리지 않은
            # 제목이 대다수라 이 추가 요청은 잘린 것에만 발생하고, 실패해도 원래 제목을 쓴다.
            if title.endswith("..."):
                title = _fetch_full_title(url) or title
            results.append(
                {
                    # 네이버가 기사를 자체 미러링하면 link가 n.news.naver.com이 되어
                    # 도메인만으로는 언론사를 알 수 없다. 언론사 판별은 원본 링크로 한다.
                    "outlet": _extract_outlet(item["originallink"]),
                    "title": title,
                    "url": url,
                    "summary": _clean_text(item["description"]),
                    # [추가: 2026-07-25] 화면에 게시일자를 직접 표시하진 않지만(PRD 규칙5),
                    # 실시간 기사 현황의 최신순 정렬과 "몇 분 전" 표시(화면 전용, 복사/내보내기
                    # 텍스트에는 안 들어감)에 쓰려고 파싱된 값을 그대로 실어 보낸다.
                    "pub_date": pub_date.isoformat(),
                }
            )

        if reached_older_article or len(items) < _MAX_DISPLAY:
            break
        start += _MAX_DISPLAY
    return results


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


def search_articles_by_groups(
    groups: list[dict],
    after: Optional[datetime] = None,
    before: Optional[datetime] = None,
) -> list[dict]:
    """
    키워드 그룹 목록으로 기사를 검색해 모아 반환한다 (건수 제한 없음, PRD.md 기능1 규칙 2 갱신).

    groups: [{"keywords": [...], "mode": "OR"|"AND"}, ...] 형태. 그룹 "안"에서는 그 그룹의
    mode(OR=하나라도 포함/AND=모두 포함)를 따르고, 그룹 "사이"는 항상 OR이다 — 등록된
    그룹 중 아무 그룹의 조건이나 만족하면 채택한다 ("기관 정보" 고정 그룹도 이 목록의
    그룹 하나로 넘어온다).

    after/before: 회차별 시간창 수집의 하한(제외)·상한(포함) — app.scraper.collect_run이
    설정된 스케줄 창에서 계산해 넘겨준다. 둘 다 생략하면 당일 전체를 그대로 모은다.

    같은 키워드가 여러 그룹에 중복 등록돼 있어도 네이버 API 호출은 키워드당 한 번만
    한다 (그룹이 최대 6개(기관정보+5)·그룹당 최대 5개까지 늘어날 수 있어, 호출 낭비를
    줄이는 게 응답 지연에 직접 영향을 준다).

    같은 기사가 여러 그룹에 걸려 중복 수집돼도 URL 기준으로 한 번만 남긴다(먼저 나온
    그룹 순서 유지). 완전 동일 제목 중복 제거·[포토] 제외·언론사 우선순위 정렬은
    이 함수 바깥(app.scraper.collect_run)에서 처리한다.
    """
    unique_keywords: list[str] = []
    seen_keywords = set()
    for group in groups:
        for keyword in group["keywords"]:
            if keyword not in seen_keywords:
                seen_keywords.add(keyword)
                unique_keywords.append(keyword)

    results_by_keyword = {
        keyword: _search_one_keyword(keyword, after=after, before=before) for keyword in unique_keywords
    }

    seen_urls: set = set()
    articles: list[dict] = []
    for group in groups:
        group_results = [results_by_keyword[keyword] for keyword in group["keywords"]]
        for article in _match_group(group_results, group.get("mode", "OR")):
            if article["url"] not in seen_urls:
                seen_urls.add(article["url"])
                articles.append(article)
    return articles
