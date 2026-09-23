# Design Ref: 사용자 요청(2026-08-11) — 규칙 기반 소제목이 "영 시원치 않다"는 피드백.
#
# 기존 app/classifier.py는 요약문의 단어 빈도만 보고 가장 흔한 단어를 그대로 소제목으로
# 삼는다. 그래서 "방안"·"등을"처럼 뜻이 옅은 단어가 소제목이 되거나, 같은 사건을 다룬
# 기사들이 표현이 달라 서로 다른 소제목으로 흩어지는 일이 잦았다.
#
# 이 모듈은 그 분류만 Claude에게 맡긴다. 설계 원칙 두 가지:
#   (1) 절대 앱을 멈추지 않는다 — API 키가 없거나, SDK가 없거나, 호출이 실패하거나,
#       응답이 이상하면 무조건 None을 돌려주고 호출부(classifier.classify_articles)가
#       기존 규칙 기반으로 조용히 되돌아간다. PRD의 "LLM 없이 동작" 전제는 그대로 유효하다.
#   (2) 같은 기사 묶음은 한 번만 부른다 — 초안(preview.html)은 요청마다 화면을 새로
#       그리므로 캐시가 없으면 새로고침 한 번에 API를 한 번씩 부르게 된다. 기사 URL
#       집합을 키로 메모리에 캐시해, 기사 구성이 바뀔 때만 새로 호출한다.
import json
import logging
import threading
from typing import Optional

from app.config import AI_RULES_FILE, MAX_SUBHEADINGS
from app.config import LLM_MODEL as LLM_MODEL_DEFAULT
from app.credentials import llm_api_key, llm_is_configured, llm_model

logger = logging.getLogger(__name__)


def _log_usage(label: str, response, article_count: int) -> None:
    """API 호출 한 번이 실제로 쓴 토큰을 INFO로 남긴다.

    이 파일의 호출 지점은 넷이고 토큰량이 제각각이라(회차 전체 분류 vs 몇 건 배정),
    추정으로는 하루 비용을 못 가른다. 실패해도 분류를 막으면 안 되므로 전부 감싼다.
    LLM_COST_USAGE.md "실측 갱신 방법" 참고.
    """
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        logger.info(
            "LLM 호출[%s] 기사 %d건 — 입력 %s / 출력 %s 토큰 (모델 %s)",
            label, article_count, usage.input_tokens, usage.output_tokens, llm_model(),
        )
    except Exception:
        pass

# 한 번에 모델에 넘길 기사 수 상한. 이보다 많으면 LLM 분류를 포기하고 규칙 기반으로 넘긴다.
#
# [수정: 2026-08-11] 60 → 150. 60은 근거 없이 보수적으로 잡은 값이었는데, 실제로 담당자가
# 2026-08-11 17시 회차(61건)에서 소제목이 <대통령>/<부총리>/<세제>/<개편안>처럼 망가진 걸
# 발견해 제보했다 — 딱 1건 넘겨서 규칙 기반(단어 빈도)으로 조용히 폴백한 결과였다. 저장된
# 42회차 중 3회차(61·61·62건)가 이미 이 문턱에 걸려 있었고, 하필 **기사가 가장 많은 바쁜
# 회차**에만 터져서 소제목이 가장 필요한 순간에 품질이 무너졌다.
#
# 실측 근거: 23건 = 입력 4,565토큰이므로 기사당 약 200토큰, 61건이면 약 12,000토큰이다.
# 모델 컨텍스트(200K)에 비하면 무시할 수준이고 비용도 회차당 20원 안쪽이다. 진짜 제약은
# 컨텍스트가 아니라 출력(max_tokens 안에 모든 기사 번호+소제목별 요약을 담아야 함)이다.
# [수정: 2026-08-20] 소제목별 요약이 추가된 뒤로는 61건=250토큰이 아니라 훨씬 크다 —
# 76건·상한15 실측 출력이 2,003토큰이었다(그래서 max_tokens을 2000->4096으로 올림,
# 위 classify_with_llm 참고). 150건이면 산술적으로 3,500~4,000토큰대까지 커질 수 있어
# 4096이 여유롭다고 단정할 수 없다 — 150건 근접 회차가 생기면 다시 실측할 것.
MAX_ARTICLES_FOR_CLASSIFY = 150

# 요약문을 이만큼만 잘라서 보낸다. 네이버 description은 원래 짧고(대개 100자 안팎)
# 주제 판단에는 앞부분이면 충분해서, 긴 것만 잘라 입력 토큰을 줄인다.
_SUMMARY_CHARS = 120

# [추가: 2026-08-11] 초안에서 자동 재분류를 끈 뒤(allow_call=False) 새로 들어온 기사를
# 모아두는 임시 묶음의 이름. 담당자가 "소제목 다시 만들기"를 누르면 사라지고 제자리를
# 찾아간다. 정기 회차(확정본)에서는 항상 새로 분류하므로 여기엔 등장하지 않는다.
# [수정: 2026-08-11] "📂 아직 소제목 안에 분류되지 않은 기사" → "📂 소제목 미분류".
# 담당자 화면에는 대기 성격의 칸이 둘(📌 담아둔 기사 / 이 칸) 있어서 긴 설명형 이름이
# 오히려 둘을 헷갈리게 만들었다 — 짧게 줄이되 "무엇을 해야 하는지"(=분류)는 이름에
# 남겼다. 저장 데이터(group_labels/group_order/group_overrides/회차 파일)에 옛 이름이
# 키로 남아있는 게 없음을 확인하고 바꿨으므로 마이그레이션은 필요 없다.
UNCLASSIFIED_GROUP_NAME = "📂 소제목 미분류"

# [추가: 2026-08-26] 위 이름을 **화면에 그릴 때만** 쓰는 조각. 앞의 📂를 떼어낸 글자와,
# 그 자리에 대신 붙일 SVG 아이콘 이름이다. 저장값(UNCLASSIFIED_GROUP_NAME)은 그대로
# 둬야 한다 — group_order.json·LLM 캐시·"기타" 병합이 전부 이 문자열로 대조한다.
UNCLASSIFIED_ICON = "folder_open"
UNCLASSIFIED_DISPLAY_TEXT = UNCLASSIFIED_GROUP_NAME.split(" ", 1)[1]

# [추가: 2026-09-10] 어디에도 안 맞는 기사를 담는 받이 소제목 이름. AI·규칙 기반·마감 흡수
# (classify_for_finalize)가 전부 이 문자열을 쓴다. 지금은 소제목 순서 고정
# (app.group_order.apply_group_order — 항상 맨 뒤)에서만 이 상수를 읽고, 다른 곳의
# "기타" 문자열은 아직 그대로다.
ETC_GROUP_NAME = "기타"

# 초안 첫 진입에서 자동 분류를 시도할 최소 기사 수. 회차 시작 직후엔 기사가 2~3건뿐인데
# 그걸로 소제목을 나눠봐야 의미가 없고, 어차피 나중에 다시 만들어야 해서 호출만 버린다.
MIN_ARTICLES_FOR_AUTO = 5

# [수정: 2026-08-13] 시스템 프롬프트 두 개(_SYSTEM_PROMPT/_ASSIGN_SYSTEM_PROMPT)를 코드
# 상수에서 AI_RULES.md 파일로 옮겼다 — 그 파일이 실제 프롬프트 원본이고, 여기서는 매
# 호출 시 그 파일을 읽어 그대로 API에 보낸다(_load_prompt 참고). 프롬프트를 고치려면
# 코드가 아니라 AI_RULES.md를 고치면 된다.
def _load_prompt(section: str) -> Optional[str]:
    """AI_RULES.md에서 마커(<!-- {section}:START/END -->) 사이 텍스트를 그대로 읽어온다.

    파일이 없거나 섹션이 빠져 있으면 None — 이 모듈의 다른 모든 실패와 같은 원칙으로
    조용히 규칙 기반 폴백으로 넘어간다(호출부가 None을 확인한다). 요청마다 다시 읽는
    이유는 API 호출(네트워크 왕복)에 비하면 작은 로컬 파일 읽기 비용은 무시할 수준이고,
    담당자가 앱 재시작 없이 AI_RULES.md를 고치면 바로 다음 호출부터 반영되게 하기 위해서다.
    """
    try:
        text = AI_RULES_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("AI_RULES.md를 찾을 수 없어 LLM 소제목 분류를 건너뜁니다: %s", AI_RULES_FILE)
        return None
    start_marker = f"<!-- {section}:START -->"
    end_marker = f"<!-- {section}:END -->"
    start = text.find(start_marker)
    end = text.find(end_marker)
    if start == -1 or end == -1 or end < start:
        logger.warning("AI_RULES.md에서 %s 섹션을 찾을 수 없어 LLM 소제목 분류를 건너뜁니다", section)
        return None
    return text[start + len(start_marker):end].strip()

_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "소제목 이름 (20자 이내 한국어 명사구)"},
                    "article_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "이 소제목에 속하는 기사의 번호",
                    },
                    "summary": {
                        "type": "string",
                        "description": "이 묶음 전체를 아우르는 요약 (3문장·250자 이내 한국어, 완결된 문장으로 끝맺음. '기타'는 빈 문자열)",
                    },
                },
                "required": ["name", "article_ids", "summary"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["groups"],
    "additionalProperties": False,
}


# URL 집합 -> 분류 결과 캐시. ThreadingHTTPServer가 요청을 여러 스레드로 처리하므로
# 락으로 보호한다(app/curation.py의 hidden_articles 락과 같은 이유).
_cache: dict = {}
_cache_lock = threading.Lock()
# 캐시가 무한정 자라지 않게 하는 상한 — 하루치 회차 + 초안 갱신을 다 담고도 남는다.
_CACHE_MAX = 40
# 직전에 성공한 분류의 소제목 이름 — 다음 호출에 "이 이름을 되도록 그대로 써라"로 넘긴다
# (_build_prompt 참고).
_last_names: list = []
# [추가: 2026-08-14] _last_names가 "어느 회차의" 이름인지. round_id_for_slot/round_id_for_run
# 참고 — 지금 호출의 round_id와 이게 다르면 _last_names를 프롬프트에 안 넘긴다(사용자 요청:
# "소제목은 그날그날 다르게 쓰여도 된다, 이전 회차 소제목을 기억할 필요는 없다" — 이 힌트가
# 회차를 넘어 살아있는 게 정확히 그 "필요 없는 기억"이었다).
_last_round_id: Optional[tuple] = None
# [추가: 2026-08-12] 프로세스당 한 번만 디스크에서 캐시를 불러오면 되므로 중복 로드를 막는다.
_cache_hydrated = False
# [추가: 2026-08-12] 직전 classify_with_llm 호출이 "API는 설정돼 있었는데 실패했고, 대체할
# 이전 분류도 없어 결국 규칙 기반으로 떨어졌는지" — 확정본이 자동발송을 보류할지 판단하는
# 근거다(app.scraper.collect_run이 이 값을 읽어 회차 파일에 classification_degraded로
# 저장한다). API 미설정·기사 수 범위 밖처럼 "정상적으로 건너뛴" 경우는 여기 해당하지
# 않는다 — 그건 애초에 실패가 아니라 이 기능을 안 쓰기로 한 선택이다.
_last_real_attempt_failed = False
# [추가: 2026-08-20] 직전 classify_with_llm 호출이 결국 **규칙 기반(단어 빈도) 폴백으로
# 떨어졌는지**. _last_real_attempt_failed보다 넓다 — 저쪽은 "API를 실제로 불렀는데
# 실패한" 경우만 True라, 오늘 사고처럼 "부를 권한이 없는데(allow_call=False) 쓸 캐시도
# 없어서" 조용히 None을 돌려준 경우는 잡지 못했다. 그 상태가 화면에 아무 표시 없이
# <부총리> 같은 한 단어 소제목으로 나타났고, 담당자가 눈으로 발견할 때까지 아무도
# 몰랐다(2026-08-20 09:30 회차). 초안 화면이 이 값을 읽어 경고 띠를 띄운다.
# API 키가 아예 없는 경우(이 기능을 안 쓰기로 한 선택)는 폴백이 아니므로 False다.
_last_result_was_rule_based = False
# [추가: 2026-08-20] 위 폴백들 중에서도 "기사 수가 MAX_ARTICLES_FOR_CLASSIFY를 넘어서"
# 건너뛴 경우만 따로 표시한다 — 이 경우는 재분류 버튼을 눌러도 기사 수를 줄이지 않는 한
# 매번 똑같이 실패하므로, 초안 화면이 일반 폴백과 다른 안내(몇 건을 줄여야 하는지)를
# 보여줘야 한다.
_last_skipped_too_many_articles = False
# [추가: 2026-08-20] 실패 원인이 정확히 "크레딧 부족"(anthropic.BadRequestError)이었는지.
# 이 경우 재분류 버튼을 다시 눌러도 매번 똑같이 실패한다는 점이 기사 수 초과와 같아서
# (_last_skipped_too_many_articles), 초안 화면이 "다시 눌러보라"는 대신 크레딧 확인을
# 안내하는 별도 문구를 보여준다.
_last_credit_error = False

_client = None
# [추가: 2026-08-20] _client를 만들 때 실제로 쓴 키 — _get_client가 이 값과 지금
# app.credentials.llm_api_key()를 비교해 키가 바뀌었는지 판단한다.
_client_key: Optional[str] = None


def _accept(groups: list) -> list:
    """분류 결과를 실제로 얻어낸 반환 지점 — 폴백 플래그를 내린다.

    기본값을 "폴백했다(True)"로 두고 여기서만 내리는 이유는, 반환 지점을 하나 빠뜨렸을 때
    틀리는 방향이 "괜한 경고"여야지 "조용한 침묵"이면 안 되기 때문이다(경고는 눈에 띄어
    바로 고칠 수 있지만, 침묵은 이번처럼 담당자가 사고를 당한 뒤에야 드러난다).
    """
    global _last_result_was_rule_based
    _last_result_was_rule_based = False
    return groups


def last_classification_was_rule_based() -> bool:
    """직전 classify_with_llm이 규칙 기반으로 떨어졌는지 — 호출 직후에만 유효하다
    (다음 호출이 덮어쓴다). app.preview_renderer가 경고 띠를 띄울지 판단하는 근거."""
    return _last_result_was_rule_based


def last_classification_skipped_too_many_articles() -> bool:
    """[추가: 2026-08-20] 직전 classify_with_llm이 기사 수 상한(MAX_ARTICLES_FOR_CLASSIFY)
    때문에 건너뛰었는지 — 호출 직후에만 유효하다. True면 재분류 버튼을 눌러도 소용없다
    (기사를 줄여야 한다)는 뜻이라, app.preview_renderer가 다른 문구를 보여준다."""
    return _last_skipped_too_many_articles


def last_classification_failed_credit_error() -> bool:
    """[추가: 2026-08-20] 직전 classify_with_llm의 실패 원인이 크레딧 부족
    (anthropic.BadRequestError)이었는지 — 호출 직후에만 유효하다. True면 기사 수 초과와
    같은 이유로 재분류 버튼을 다시 눌러도 소용없다(충전 전까진 매번 같은 결과)."""
    return _last_credit_error


def round_id_for_slot(slot: dict) -> tuple:
    """초안 회차의 식별자 — (오늘 날짜, 이 회차의 끝 시각). [추가: 2026-08-14]

    자정을 넘기면 다음 슬롯이 새 날짜로 시작하므로 date.today()만으로 "오늘의 이
    회차"가 충분히 특정된다. app.preview_renderer가 slot dict를 이미 들고 있는
    모든 지점에서 이걸로 round_id를 만들어 classify_articles 계열에 넘긴다.
    """
    from datetime import date

    return (date.today().isoformat(), slot["end"])


def round_id_for_run(run: dict) -> tuple:
    """저장된 회차(run)의 식별자 — (run_at의 날짜, run_slot). [추가: 2026-08-14]

    초안이 확정본으로 넘어갈 때도 같은 슬롯이면 같은 round_id가 나오게 만든 것이
    핵심이다(run_at 날짜 형식이 date.today().isoformat()과 같은 YYYY-MM-DD라 그대로
    맞아떨어진다) — 그래야 확정 저장 시점의 캐시 조회가 "방금 그 초안"만 정확히
    찾고, 다른 날 다른 회차의 캐시를 착각해서 끌어오지 않는다.
    """
    return (run["run_at"][:10], run["run_slot"])


def round_key(round_id: Optional[tuple]) -> str:
    """round_id 튜플을 JSON dict의 문자열 키로 바꾼다. [추가: 2026-08-14]

    이름표(group_labels)·소제목 순서(group_order)를 회차 단위로 저장할 때 공용으로
    쓴다 — "소제목은 그날그날 다르게 쓰여도 된다, 이전 회차를 기억할 필요 없다"는
    사용자 요청에 따라 이 두 파일도 LLM 캐시와 같은 방식(회차 단위로 완전히 격리)으로
    바꾼다. round_id=None(회차 개념이 없는 호출부)는 전용 버킷으로 격리해, 실수로
    다른 회차 데이터와 섞이지 않게 한다.
    """
    return "|".join(round_id) if round_id else "_no_round"


def _entry_key_of(entry: dict) -> tuple:
    """파일에 저장된 캐시 항목 하나를 메모리 캐시의 키로 되돌린다."""
    round_id = tuple(entry["round_id"]) if entry.get("round_id") else None
    return (round_id, frozenset(entry["urls"]), entry["max"])


def _persist_cache_locked() -> None:
    """지금 캐시를 파일에 남긴다 — **호출자가 이미 _cache_lock을 쥔 상태**에서만
    불러야 한다(threading.Lock은 재진입이 안 되므로, 여기서 다시 락을 잡으면 교착된다).

    [수정: 2026-09-02] 메모리를 파일에 **통째로 덮어쓰던** 것을, 파일을 먼저 읽어
    **병합한 뒤** 쓰도록 바꿨다(2차 방어선). 캐시는 프로세스 시작 때 딱 한 번만
    하이드레이트되므로(_ensure_cache_hydrated), 어떤 이유로든 앱이 두 개 돌면 나중에
    저장한 쪽이 **상대가 그 사이 만든 분류를 파일에서 지워버린다** — 2026-09-02에 실제로
    그렇게 초안의 좋은 소제목이 디스크에서까지 사라졌다(그래서 확정본이 몇 시간 전
    스냅샷을 재사용해 소제목을 새로 지어냈다). 앱 중복 실행 자체는 main.py의 포트
    자물쇠가 막지만, 여기서 지우지만 않으면 그 방어가 뚫려도 분류가 유실되지 않는다.

    같은 키가 양쪽에 있으면 **메모리 쪽을 남긴다**(방금 계산한 것이 더 최신이다).
    _CACHE_MAX(40)는 메모리 캐시의 상한이지 파일 상한이 아니었고 지금도 아니다 — 다만
    병합으로 파일이 무한정 자라지 않도록, 파일에서 온 항목은 최근 것부터 상한의 2배까지만
    남긴다(오늘 하루 회차·초안 조작을 다 담고도 남는 크기).
    """
    from app.atomic_write import atomic_write_text
    from app.config import LLM_CLASSIFICATION_CACHE_FILE

    mine = {
        (round_id, urls, max_sub): [[name, list(u), summary] for name, u, summary in groups]
        for (round_id, urls, max_sub), groups in _cache.items()
    }
    merged = []
    try:
        on_disk = json.loads(LLM_CLASSIFICATION_CACHE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        on_disk = {}
    for entry in on_disk.get("entries", []):
        try:
            key = _entry_key_of(entry)
        except (KeyError, TypeError):
            continue  # 형식이 깨진 항목은 조용히 버린다(재분류로 자연히 다시 채워진다).
        if key in mine:
            continue  # 메모리 쪽이 이긴다.
        merged.append(entry)
    merged = merged[-(_CACHE_MAX * 2) :]
    for (round_id, urls, max_sub), groups in mine.items():
        merged.append(
            {
                "round_id": list(round_id) if round_id else None,
                "urls": sorted(urls),
                "max": max_sub,
                "groups": groups,
            }
        )
    data = {
        "entries": merged,
        "last_names": list(_last_names),
        "last_round_id": list(_last_round_id) if _last_round_id else None,
    }
    atomic_write_text(LLM_CLASSIFICATION_CACHE_FILE, json.dumps(data, ensure_ascii=False))


def _ensure_cache_hydrated() -> None:
    """앱이 막 시작돼 메모리 캐시가 비어 있으면, 지난번에 저장해둔 캐시를 한 번만 불러온다.

    [추가: 2026-08-12] 이게 없으면 앱을 재시작할 때마다 초안의 좋은 소제목 이름이
    규칙 기반(단어 빈도)으로 되돌아가는 사고가 반복됐다 — 담당자가 실제로 겪고 제보한
    문제. 프로세스 시작 후 첫 classify_with_llm 호출에서 한 번만 디스크를 읽는다.

    [수정: 2026-08-14] round_id가 없던 옛 캐시 파일(이 필드가 생기기 전에 저장된 것)도
    그대로 읽을 수 있어야 한다 — 없으면 None으로 채운다(round_id=None인 호출부, 예:
    수시 모니터링과 똑같은 취급 — 회차 스코프 없이 예전처럼 기사 URL 겹침만으로 판단).
    """
    global _cache_hydrated, _last_names, _last_round_id
    if _cache_hydrated:
        return
    with _cache_lock:
        if _cache_hydrated:
            return
        _cache_hydrated = True
        try:
            from app.config import LLM_CLASSIFICATION_CACHE_FILE

            data = json.loads(LLM_CLASSIFICATION_CACHE_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return
        for entry in data.get("entries", []):
            # 키를 만드는 식은 _entry_key_of 하나뿐이다 — 저장(_persist_cache_locked)과
            # 읽기가 각자 만들면 한쪽만 고쳤을 때 조용히 어긋난다.
            key = _entry_key_of(entry)
            # [수정: 2026-08-12] 요약 필드 추가 전(2요소 [name, urls])에 저장된 캐시 파일도
            # 그대로 읽을 수 있어야 한다 — 없으면 빈 요약으로 채운다(첫 기사 발췌로 폴백).
            _cache[key] = [
                (item[0], list(item[1]), item[2] if len(item) > 2 else "")
                for item in entry["groups"]
            ]
        _last_names = list(data.get("last_names", []))
        _last_round_id = tuple(data["last_round_id"]) if data.get("last_round_id") else None


def llm_attempt_failed_without_fallback() -> bool:
    """직전 classify_with_llm 호출이 정말 실패했고(API 설정은 돼 있었음) 대체할 이전
    분류도 없었는지 — app.scraper.collect_run이 이 값을 회차 파일에 기록해, 확정본
    화면(app.renderer)이 "AI 분류 실패" 경고를 보여주고 자동발송을 보류할지 정한다."""
    return _last_real_attempt_failed


def is_configured() -> bool:
    """LLM 소제목을 쓸 수 있는 상태인지 — 설정 화면(/llm)에 저장된 키나 .env의
    ANTHROPIC_API_KEY 둘 중 하나는 있어야 한다(app.credentials.llm_is_configured)."""
    return llm_is_configured()


def _get_client():
    """Anthropic 클라이언트를 처음 쓸 때 한 번만 만든다(모듈 임포트 시점엔 안 만든다).

    [수정: 2026-08-20] 키를 모듈 임포트 시점 상수(ANTHROPIC_API_KEY)가 아니라
    app.credentials.llm_api_key()로 매번 조회한다 — 설정 화면에서 키를 바꿔도 이
    캐시가 옛 키를 계속 물고 있으면 앱을 재시작하기 전까진 저장이 반영되지 않는다.
    캐시 자체는 유지하되(요청마다 클라이언트를 새로 만드는 비용을 피하려는 목적이었으므로),
    직전에 클라이언트를 만들 때 쓴 키와 지금 키가 다르면 무효화하고 다시 만든다."""
    global _client, _client_key
    key = llm_api_key()
    if _client is None or _client_key != key:
        import anthropic  # 설치 안 돼 있으면 여기서 ImportError -> 호출부가 폴백

        _client = anthropic.Anthropic(api_key=key)
        _client_key = key
    return _client


def test_llm_credentials(api_key: str, model: str) -> tuple[bool, str]:
    """설정 화면의 [연결 테스트] — 저장 전에 입력한 키로 max_tokens=1짜리 최소 호출을
    직접 보내본다. 저장된 값이 아니라 화면이 지금 넘겨준 값을 쓴다(app.naver_api.
    test_naver_credentials와 같은 이유) — 전역 캐시(_get_client)는 절대 건드리지 않는다."""
    api_key = (api_key or "").strip()
    model = (model or "").strip() or LLM_MODEL_DEFAULT
    if not api_key:
        return False, "키를 입력해주세요."
    try:
        import anthropic
    except ImportError:
        return False, "anthropic 패키지가 설치돼 있지 않습니다 (pip install anthropic)."
    client = anthropic.Anthropic(api_key=api_key)
    try:
        client.messages.create(model=model, max_tokens=1, messages=[{"role": "user", "content": "hi"}])
    except anthropic.AuthenticationError:
        return False, "인증 실패 (401) — 키가 만료됐거나 잘못됐습니다."
    except anthropic.NotFoundError:
        return False, f"모델을 찾을 수 없습니다 ({model}) — 모델명을 확인해주세요."
    except anthropic.PermissionDeniedError:
        return False, "이 키로는 해당 모델을 쓸 수 없습니다 (권한 없음)."
    except anthropic.RateLimitError:
        return False, "요청 한도를 초과했습니다 (429) — 잠시 후 다시 시도해주세요."
    except anthropic.BadRequestError as error:
        return False, f"요청이 거부됐습니다 — 결제 수단·크레딧을 확인해주세요. ({error})"
    except anthropic.APIConnectionError:
        return False, "네트워크 연결에 실패했습니다 — 인터넷 연결을 확인해주세요."
    except anthropic.AnthropicError as error:
        return False, f"연결에 실패했습니다 — {error}"
    return True, f"연결됐습니다 ({model} 응답 확인)"


def _cache_key(articles: list, max_subheadings: int, round_id: Optional[tuple] = None) -> tuple:
    """기사 구성이 같으면 같은 키 — 순서가 바뀌어도(언론사 정렬 등) 재호출하지 않는다.

    round_id: [추가: 2026-08-14] 이 분류가 속한 회차(round_id_for_slot/round_id_for_run).
    캐시 키의 일부로 넣어, 회차가 다르면 URL이 아무리 겹쳐도 아예 "다른 캐시"로 취급되게
    한다(_find_reusable/_find_partial이 round_id까지 맞아야 재사용한다 — 아래 참고).
    round_id=None인 호출부(수시 모니터링 등, 회차 개념이 없는 곳)는 예전처럼 회차 구분
    없이 URL 겹침만으로 판단한다(하위 호환 — None은 None과만 같다).
    """
    return (round_id, frozenset(a["url"] for a in articles), max_subheadings)


def _store_locked(key: tuple, groups: list) -> None:
    """분류 결과를 메모리 캐시에 넣는다 (호출자가 락을 쥔 상태).

    상한(_CACHE_MAX)에 닿으면 **오래된 항목부터 하나씩** 뺀다. 넣으려는 항목과 같은
    회차의 항목은 가장 나중까지 남긴다. 예전처럼 캐시를 통째로 비우면, 시작할 때 파일에서
    40개 넘게 불러온 프로세스는 새 분류 하나를 넣는 순간 다른 회차 분류를 전부 잃는다 —
    그러면 직전 확정본을 다시 저장하는 큐레이션(snapshot_group_names)이 캐시를 못 찾고
    규칙 기반 이름으로 회차 파일을 덮어쓴다(HISTORY.md "캐시 통째 비우기로 확정본
    소제목이 규칙 기반으로 덮이던 문제"). 파일 쪽은 _persist_cache_locked가 병합하므로
    여기서 뺀 항목도 파일에는 남는다.
    """
    _cache.pop(key, None)
    while len(_cache) >= _CACHE_MAX:
        victim = next((k for k in _cache if k[0] != key[0]), None) or next(iter(_cache))
        del _cache[victim]
    _cache[key] = [(g["name"], [a["url"] for a in g["articles"]], g.get("summary", "")) for g in groups]


def copy_round_cache(src_round: tuple, dst_round: tuple) -> int:
    """src 회차 이름으로 저장된 분류를 dst 회차 이름으로 복사하고, 복사한 항목 수를 돌려준다.

    「✂ 오늘만 여기서 끊기」(app.today_cuts) — 끊는 순간 회차 이름이 바뀌는데, 캐시는
    회차가 같아야만 재사용되므로(_find_reusable/_find_partial) 복사하지 않으면 끊은 회차의
    확정본이 초안 분류를 못 찾고 AI를 새로 불러 소제목 이름을 갈아엎는다. 옮기지 않고
    복사한다 — 나뉜 두 회차가 모두 이 분류를 이어 쓴다.
    """
    _ensure_cache_hydrated()
    with _cache_lock:
        copies = {
            (dst_round, urls, max_sub): [(name, list(u), summary) for name, u, summary in groups]
            for (round_id, urls, max_sub), groups in _cache.items()
            if round_id == tuple(src_round)
        }
        for key, groups in copies.items():
            _cache.pop(key, None)
            while len(_cache) >= _CACHE_MAX:
                victim = next((k for k in _cache if k[0] not in (tuple(src_round), dst_round)), None) or next(iter(_cache))
                del _cache[victim]
            _cache[key] = groups
        if copies:
            _persist_cache_locked()
        return len(copies)


def _find_reusable(key: tuple) -> Optional[list]:
    """기사가 "줄어들기만" 한 경우 재분류 없이 기존 분류를 재사용한다 (호출자가 락을 쥔 상태).

    [추가: 2026-08-11] 정확히 같은 기사 묶음일 때만 캐시를 쓰면, 실제로는 재분류가 전혀
    필요 없는 상황에서도 API를 부르게 된다는 걸 실측으로 확인했다:
      - 정기 회차 1건이 호출 2회를 썼다. 수집 시점(app.scraper)엔 기사 전체로 분류하는데,
        화면(app.renderer.generate_screen)은 숨긴 기사를 뺀 목록으로 다시 분류해서
        두 캐시 키가 어긋났기 때문이다.
      - 담당자가 🗑️로 기사를 하나 숨길 때마다 매번 새로 호출됐다.
    둘 다 "기존 묶음에서 기사를 빼기만 하면 되는" 경우다 — 남은 기사의 주제가 달라진 게
    아니므로 다시 물어볼 이유가 없다. 그래서 지금 기사 집합을 통째로 포함하는 캐시가
    있으면 그걸 재사용하고, 그중에서도 가장 작은(=가장 가까운) 것을 고른다.

    반대로 기사가 새로 "늘어난" 경우는 재사용하지 않는다 — 새 기사는 어느 묶음에도
    속하지 않아 실제로 분류가 필요하다.

    [수정: 2026-08-14] round_id가 일치하는 캐시만 후보로 본다 — 같은 날 다른 회차끼리도
    기사가 많이 겹치는데(연속 보도), round_id 없이는 그걸 "이 회차가 줄어든 것"으로
    잘못 판단해 전 회차의 분류(이름 포함)를 그대로 끌어왔다. "회차마다 새로 분류하고,
    전 회차 기억은 안 쓴다"는 원칙(사용자 요청, 2026-08-14)을 여기서 코드로 보장한다.
    """
    round_id, urls, max_subheadings = key
    best = None
    for (cached_round_id, cached_urls, cached_max), groups in _cache.items():
        if cached_round_id != round_id or cached_max != max_subheadings or not urls < cached_urls:
            continue
        if best is None or len(cached_urls) < len(best[0]):
            best = (cached_urls, groups)
    return best[1] if best else None


def _find_partial(key: tuple) -> Optional[list]:
    """지금 기사 묶음과 충분히 겹치는 캐시를 찾아 재사용한다 (호출자가 락을 쥔 상태).

    _find_reusable의 반대 방향이다. 초안에서 자동 재분류를 끈 상태(allow_call=False)에서
    새 기사가 들어왔을 때 쓴다 — 기존 소제목 구성을 그대로 두고, 캐시에 없는 새 기사는
    _rebuild가 UNCLASSIFIED_GROUP_NAME 묶음으로 따로 모은다.

    [수정: 2026-08-12] 원래는 "캐시 ⊂ 현재"(기사가 늘기만 한 경우)만 재사용했는데,
    그 조건이 실사고를 냈다: 초안은 새로고침마다 네이버를 다시 검색하므로 기사가 들락날락
    하는 게 정상인데(검색 순위 변동·중복 제거 결과 차이), **기사 하나가 추가되면서 동시에
    다른 하나가 빠지면** 부분집합도 상위집합도 아니게 되어 재사용이 통째로 실패했다.
    그러면 classify_with_llm이 None을 돌려주고 규칙 기반(단어 빈도)으로 폴백해서, 잘 나와
    있던 소제목이 갑자기 "일자리"·"의원"·"납입"처럼 한 단어로 뭉개졌다(담당자 제보).
    빠졌던 기사가 다음 검색에 다시 잡히면 저절로 정상으로 돌아와서 더 헷갈렸다.

    엄격한 포함 관계는 애초에 필요 없었다 — _rebuild가 이미 사라진 URL을 걸러내고
    (`if u in by_url`) 새 URL은 임시 칸으로 모으기 때문이다. 그래서 **겹치는 기사가 가장
    많은** 캐시를 고르되, 캐시된 기사의 과반이 지금도 남아 있을 때만 쓴다(과반 조건이
    없으면 전혀 다른 회차의 캐시를 잘못 가져올 수 있다).

    [수정: 2026-08-14] "전혀 다른 회차"를 과반 조건만으로 걸러내던 것을, round_id 일치
    조건으로 바꿔 원천적으로 막는다 — 과반 조건은 여전히 필요하다(같은 회차 안에서도
    검색 결과가 크게 물갈이되면 겹침이 적을 수 있어, 그 경우는 재사용하지 않는 게 맞다).
    round_id가 다르면 겹침이 100%여도 후보에서 제외한다.

    [수정: 2026-08-20] **과반의 분모에서 "담당자가 숨긴 기사"를 뺀다.** 예전엔 분모가
    캐시에 담긴 기사 수 전체였는데, 그 안에는 담당자가 그 사이 숨긴 기사까지 다 들어
    있다 — 숨길수록 분모는 그대로인데 분자(지금 화면에 남은 기사와의 겹침)만 줄어서,
    **회차 기사의 절반 넘게 숨기면 이 조건이 반드시 깨졌다**. 거기에 새 기사가 1건만
    들어와도 _find_reusable(부분집합 조건)까지 동시에 막혀 재사용 경로가 전멸하고,
    잘 나와 있던 LLM 소제목이 규칙 기반 한 단어("<부총리>")로 뭉개졌다
    (2026-08-20 09:30 회차 사고 — 캐시 57건 중 29건을 숨긴 시점에 발생, 실측 재현:
    겹침 26 × 2 = 52 < 57로 탈락. HISTORY.md 참고).

    숨긴 기사는 어차피 모든 화면에서 filter_hidden으로 빠지므로 "이 분류를 아직 써도
    되는가"를 판단하는 분모에 낄 이유가 없다 — 분모는 "캐시에 담긴 기사 중 지금도
    화면에 나올 수 있는 것"이어야 한다. app.curation을 지연 임포트하는 이유는
    _build_prompt와 같다(app.curation -> app.classifier -> 이 모듈 순환 방지).
    """
    from app.curation import load_hidden_urls

    round_id, urls, max_subheadings = key
    hidden_urls = load_hidden_urls()
    best = None
    for (cached_round_id, cached_urls, cached_max), groups in _cache.items():
        if cached_round_id != round_id or cached_max != max_subheadings:
            continue
        overlap = len(cached_urls & urls)
        # 분모: 캐시된 기사 중 숨겨지지 않은 것만(위 [수정: 2026-08-20] 참고).
        live_cached = len(cached_urls - hidden_urls)
        if overlap == 0 or overlap * 2 < live_cached:
            continue
        if best is None or overlap > best[0]:
            best = (overlap, groups)
    return best[1] if best else None


def _round_cache_emptied(key: tuple) -> bool:
    """이 회차의 분류 캐시가 있긴 한데, 담긴 기사를 담당자가 전부 숨겼는지 (호출자가 락을 쥔 상태).

    초안 자동 분류는 회차당 한 번이라 기사가 5~6건일 때 쓰이는데, 그 몇 건을 다 숨기면
    _find_partial이 빌려 올 게 없어 규칙 기반(단어 빈도)으로 떨어졌다. 그 분류가 틀린 게
    아니라 **보여줄 기사가 사라졌을 뿐**이라, 이때는 남은 기사를 전부 📂 소제목 미분류로
    보여주는 게 맞다(가짜 한 단어 소제목보다 정직하다). → H: 초안 첫 분류 기사를 다 숨기면
    규칙 기반으로 떨어지던 문제
    """
    from app.curation import load_hidden_urls

    round_id, _, max_subheadings = key
    hidden_urls = load_hidden_urls()
    return any(
        cached_round_id == round_id and cached_max == max_subheadings and not (cached_urls - hidden_urls)
        for (cached_round_id, cached_urls, cached_max) in _cache
    )


def _rebuild(cached: list, articles: list, keep_leftover: bool = False) -> list:
    """캐시(소제목 이름 + URL 목록)를 지금 기사 목록에 다시 입힌다.

    캐시엔 URL만 담아뒀다 — 기사 dict 자체는 매번 최신 것을 쓴다(🔄 다시 가져오기·
    ✏️ 직접 수정으로 제목/요약이 바뀔 수 있기 때문).

    [수정: 2026-08-14] 캐시는 "어느 기사가 어느 소제목인가"(소속)만 정하고, **소제목
    안의 순서는 지금 넘어온 articles의 순서를 그대로 따른다**. 예전엔 캐시에 적힌 URL
    순서대로 늘어놨는데, 그건 곧 "캐시를 만든 시점의 언론사 우선순위 정렬"이라 그 뒤에
    담당자가 ↑/↓로 바꾼 순서를 매 렌더링마다 조용히 되돌려버렸다(2026-08-14 제보 —
    확정본에서 기사를 올려도 새로고침하면 제자리, 초안도 마찬가지). 담당자의 손질이
    AI/언론사 순서보다 우선한다는 원칙(CLAUDE.md "정렬 우선순위")을 코드로 지키는
    지점이다 — 다른 분류 경로(_parse_groups·_apply_forced_groups)는 이미 입력 순서를
    지키고 있었고, 여기만 예외였다.

    keep_leftover: 캐시에 없는 기사(=그 사이 새로 들어온 기사)를 버리지 않고
    UNCLASSIFIED_GROUP_NAME 묶음으로 맨 뒤에 모은다.
    """
    by_url = {a["url"]: a for a in articles}
    order_index = {a["url"]: i for i, a in enumerate(articles)}
    groups = [
        {
            "name": name,
            "articles": sorted((by_url[u] for u in urls if u in by_url), key=lambda a: order_index[a["url"]]),
            "summary": summary,
        }
        for name, urls, summary in cached
        if any(u in by_url for u in urls)
    ]
    if keep_leftover:
        known = {u for _, urls, _ in cached for u in urls}
        leftover = [a for a in articles if a["url"] not in known]
        if leftover:
            groups.append({"name": UNCLASSIFIED_GROUP_NAME, "articles": leftover, "summary": ""})
    return groups


def cached_group_layout(articles: list, max_subheadings: int = MAX_SUBHEADINGS, round_id: Optional[tuple] = None) -> list:
    """classify_with_llm이 방금 입힌 캐시 분류의 소제목 [(이름, 요약), …]을 캐시 순서대로 돌려준다.

    _rebuild는 지금 보이는 기사가 하나도 없는 소제목을 빼고 돌려주는데, 그 소제목으로
    배정된(「AI 기사 배정」·↑↓·드롭다운) 기사가 아직 보이면 소제목이 남아야 한다 —
    app.classifier._apply_forced_groups가 이 목록으로 되살릴 자리와 요약을 찾는다.
    classify_with_llm과 같은 조회 순서를 쓰므로 API를 부르지 않는다. 캐시가 없으면 [].
    """
    if not is_configured():
        return []
    _ensure_cache_hydrated()
    key = _cache_key(articles, max_subheadings, round_id)
    with _cache_lock:
        cached = _cache.get(key) or _find_reusable(key) or _find_partial(key)
        if not cached and round_id is not None:
            cached = _emptied_round_cache(key)
    return [(name, summary) for name, _, summary in cached] if cached else []


def _emptied_round_cache(key: tuple) -> Optional[list]:
    """_round_cache_emptied가 참인 그 캐시 항목의 분류를 돌려준다 (호출자가 락을 쥔 상태).

    첫 분류 기사를 담당자가 전부 숨기면 _find_partial은 빌려 올 게 없어 None이다. 그래도
    「AI 기사 배정」으로 그 소제목에 넣은 기사가 보이면 소제목은 남아야 하므로, 되살릴
    이름·요약은 이 항목에서 가져온다. → H: 첫 분류 기사를 다 숨기면 소제목이 사라지던 문제
    """
    from app.curation import load_hidden_urls

    round_id, _, max_subheadings = key
    hidden_urls = load_hidden_urls()
    for (cached_round_id, cached_urls, cached_max), groups in _cache.items():
        if cached_round_id == round_id and cached_max == max_subheadings and not (cached_urls - hidden_urls):
            return groups
    return None


def cached_group_names(articles: list, max_subheadings: int = MAX_SUBHEADINGS, round_id: Optional[tuple] = None) -> set:
    """지금 이 기사 묶음의 분류가 LLM에서 나온 것이면 그 소제목 이름들을 돌려준다.

    [추가: 2026-08-11] 화면의 "🤖" 배지를 **진짜 AI가 만든 소제목에만** 붙이기 위한
    근거다 — API 키가 없거나 호출이 실패해 규칙 기반으로 폴백한 회차에도 배지가 붙으면
    거짓말이 된다. 캐시를 읽기만 하므로 API를 부르지 않는다(빈 집합 = 규칙 기반).
    """
    if not is_configured():
        return set()
    _ensure_cache_hydrated()
    key = _cache_key(articles, max_subheadings, round_id)
    with _cache_lock:
        cached = _cache.get(key) or _find_reusable(key) or _find_partial(key)
    return {name for name, _, _ in cached} if cached else set()


def _build_prompt(articles: list, max_subheadings: int, previous_names: Optional[list] = None) -> str:
    lines = []
    for i, a in enumerate(articles):
        summary = (a.get("summary") or "").strip().replace("\n", " ")[:_SUMMARY_CHARS]
        lines.append(f"[{i}] {a['title']}\n    {summary}")
    # [추가: 2026-08-11] 직전 분류에서 쓴 이름을 알려준다 — 기사 1건을 숨기기만 해도
    # 기사 구성이 달라져 다시 호출되는데, 그때마다 이름이 통째로 바뀌면 담당자가 보던
    # 화면이 매번 뒤집힌다("세제 개편-부동산" -> "부동산 세제개편 정책"). 내용이 그대로면
    # 이름도 그대로 두게 해서 화면이 안정적으로 유지되게 한다.
    carryover = ""
    if previous_names:
        # [수정: 2026-08-11] 담당자가 ✏️로 바꿔둔 이름이 있으면 원래 이름 대신 그걸 넘긴다.
        # 예전엔 자동 생성된 원래 이름만 넘겨서, "세제개편"을 "부동산 세제개편"으로 바꿔둔
        # 담당자가 재분류를 요청하면 AI가 그 의도를 전혀 모른 채 이름을 지었다 — 바뀐 이름이
        # 살아남는 게 순전히 "AI가 우연히 같은 원래 이름을 다시 냈는지"에 달려 있었다.
        # (이름표는 {원래 이름: 표시 이름}으로 저장되므로, AI가 표시 이름을 그대로 내주면
        #  그 자체가 화면에 뜨는 이름이 되어 어느 쪽이든 담당자 의도대로 보인다.)
        # app.curation은 app.classifier -> 이 모듈을 거쳐 순환 임포트가 되므로 지연 임포트한다.
        from app.curation import display_group_name, load_group_labels

        labels = load_group_labels()
        shown = [display_group_name(name, labels) for name in previous_names]
        carryover = (
            "\n\n직전에 쓰던 소제목: " + ", ".join(shown) + "\n"
            "내용이 그대로인 묶음은 위 이름을 글자 그대로 재사용하라. "
            "묶음이 실제로 달라졌을 때만 새 이름을 지어라."
        )
    return (
        f"아래 기사 {len(articles)}건을 소제목 최대 {max_subheadings}개로 묶어라.\n"
        "각 기사 앞의 [번호]를 article_ids에 그대로 쓴다."
        + carryover
        + "\n\n"
        + "\n".join(lines)
    )


def _parse_groups(payload: dict, articles: list, max_subheadings: int) -> Optional[list]:
    """모델 응답을 [{"name":…, "articles":[…]}] 형태로 바꾼다. 쓸 수 없으면 None.

    구조화 출력(json_schema)을 쓰므로 JSON 형식 자체는 보장되지만, 내용까지 보장되진
    않는다 — 없는 번호, 중복 배정, 빠뜨린 기사는 여기서 직접 바로잡는다.
    """
    seen = set()
    result = []
    for group in payload.get("groups", [])[:max_subheadings]:
        name = (group.get("name") or "").strip()
        if not name:
            continue
        members = []
        for idx in group.get("article_ids", []):
            if not isinstance(idx, int) or not 0 <= idx < len(articles) or idx in seen:
                continue  # 없는 번호이거나 이미 다른 소제목에 들어간 기사
            seen.add(idx)
            members.append(idx)
        if members:
            summary = (group.get("summary") or "").strip()
            result.append({"name": name, "indexes": members, "summary": summary})

    if not result:
        return None

    # 모델이 빠뜨린 기사는 "기타"로 모은다(한 건도 빠지면 안 된다는 규칙을 코드로 보장).
    # 이렇게 새로 생기거나 채워진 "기타"는 요약을 안 주므로 빈 문자열로 둔다(app.summarizer가
    # 알아서 첫 기사 발췌로 폴백한다) — 방금 코드가 강제로 섞어 넣은 기사까지 모델이
    # 요약했다고 우길 수 없기 때문이다.
    missing = [i for i in range(len(articles)) if i not in seen]
    if missing:
        etc = next((g for g in result if g["name"] == "기타"), None)
        if etc is not None:
            etc["indexes"].extend(missing)
        elif len(result) < max_subheadings:
            result.append({"name": "기타", "indexes": missing, "summary": ""})
        else:
            result[-1]["indexes"].extend(missing)

    # 소제목 안의 기사 순서는 입력 순서(=언론사 우선순위)를 그대로 지킨다(PRD 규칙 7).
    return [
        {
            "name": g["name"],
            "articles": [articles[i] for i in sorted(g["indexes"])],
            "summary": g["summary"],
        }
        for g in result
    ]


# "기타"가 이 정도 이상이면(비율·최소 건수 둘 다 넘어야) 재정리를 한 번 더 시도한다.
_ETC_REFINE_MIN_RATIO = 0.25
_ETC_REFINE_MIN_COUNT = 6


def _refine_etc_bucket(groups: list, articles_total: int, max_subheadings: int, system_prompt: str) -> list:
    """1차 분류 결과의 "기타"가 유독 크면, 그 안에서만 한 번 더 쟁점을 찾아본다.

    [추가: 2026-08-24] 실측(89건 회차, 상한 15) — 1차 호출이 "기타"에 39~45건(전체의
    44~51%)을 몰아넣는 회차가 흔했다. 안을 열어보면 대부분 같은 사건(국회 예결위 결산
    심사)을 다루는 사진 캡션류 기사였고, 요약문에 "예산결산특별위원회"가 명시돼 있는데도
    1차 호출은 묶지 못했다. AI_RULES.md에 "캡션형 기사도 같은 사건이면 한 묶음" 지침을
    추가해봤지만 재현해보니 편차가 컸다(같은 프롬프트·같은 기사로 3회 재시도해 기타
    1건~39건까지 들쭉날쭉 — temperature=0으로 고정해도 마찬가지였다, 긴 응답에서 후반부
    판단이 흔들리는 것으로 보인다). 반면 "기타"만 따로 떼어 다시 분류를 시키면 훨씬
    안정적으로 정리됐다(재현: 40건 → 4건, 여러 번 반복해도 비슷). 이미 만들어진 다른
    소제목·기사 89건 전체를 한꺼번에 고려할 때보다, 남은 기사만 놓고 다시 볼 때 모델이
    훨씬 잘한다 — 그래서 1차 호출 하나로 끝내지 않고 기타가 클 때만 그 부분만 다시 부른다.

    비용은 "기타"가 실제로 크게 남은 회차에만 발생한다(대부분은 이 문턱에 안 걸린다).
    실패해도 예외를 삼키고 1차 결과의 "기타"를 그대로 둔다 — 추가 개선 시도일 뿐이므로
    실패했다고 이미 성공한 1차 분류 전체를 버릴 이유가 없다(classify_with_llm의 나머지
    부분과 같은 "절대 앱을 멈추지 않는다" 원칙).
    """
    etc_idx = next((i for i, g in enumerate(groups) if g["name"] == "기타"), None)
    if etc_idx is None:
        return groups
    etc_articles = groups[etc_idx]["articles"]
    if len(etc_articles) < _ETC_REFINE_MIN_COUNT or len(etc_articles) / articles_total < _ETC_REFINE_MIN_RATIO:
        return groups

    # 남은 소제목 슬롯 — 이미 이름 붙은 다른 묶음 수를 상한에서 뺀 만큼만 새로 만들 수
    # 있다(전체 상한 max_subheadings를 이 재정리 때문에 넘기지 않기 위해). _parse_groups가
    # 항상 len(groups) <= max_subheadings를 보장하므로 room은 최소 1이다 — 딱 1개뿐이면
    # 여러 쟁점으로 쪼갤 순 없지만(_parse_groups가 나머지를 전부 그 한 묶음에 합친다),
    # [수정: 2026-08-24] 실측해보니 1차 호출이 상한(15)을 거의 항상 다 채워 room=1이
    # 되는 경우가 흔했다(=재정리가 가장 필요한 바로 그 상황). 처음엔 "room<2면 실익이
    # 없다"고 판단해 건너뛰었는데, 그러면 정작 도움이 필요한 회차에서 이 함수가 항상
    # 조용히 아무 일도 안 했다(89건 회차에서 재현 확인: 기타 39건이 그대로 남음). room=1
    # 이어도 "기타"를 통째로 재검토해 하나의 제대로 된 이름으로 묶는 것 자체가 의미
    # 있다("기타"라는 이름 자체가 정보 손실이다) — 그래서 1까지는 시도한다.
    room = max_subheadings - (len(groups) - 1)

    existing_names = [g["name"] for i, g in enumerate(groups) if i != etc_idx]
    avoid = (
        "\n\n이미 다른 소제목으로 쓰인 이름이니 겹치지 않는 새 이름을 지어라: " + ", ".join(existing_names)
        if existing_names
        else ""
    )

    try:
        response = _get_client().messages.create(
            model=llm_model(),
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": _build_prompt(etc_articles, room) + avoid}],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        )
        _log_usage("기타 재정리", response, len(etc_articles))
        text = next((b.text for b in response.content if b.type == "text"), None)
        refined = _parse_groups(json.loads(text), etc_articles, room) if text else None
    except Exception:
        logger.warning("'기타' 재정리 호출 실패 — 1차 분류 결과를 그대로 둡니다", exc_info=True)
        return groups

    if not refined:
        return groups
    return groups[:etc_idx] + groups[etc_idx + 1:] + refined


def classify_with_llm(
    articles: list,
    max_subheadings: int = MAX_SUBHEADINGS,
    allow_call: bool = True,
    force: bool = False,
    round_id: Optional[tuple] = None,
) -> Optional[list]:
    """기사를 Claude로 소제목별 분류한다. 쓸 수 없는 상황이면 조용히 None을 돌려준다.

    반환 형태는 app.classifier.classify_articles와 같다:
    [{"name": 소제목, "articles": [기사, …]}, …]

    allow_call: [추가: 2026-08-11] False면 API를 새로 부르지 않고 캐시로만 답한다.
    스크랩 초안에서 쓴다 — 자동 재분류가 담당자의 수동 정리(기사 이동·소제목 이름
    변경·순서)를 소리 없이 날려버리기 때문이다. 이 수동 작업들은 전부 소제목 "이름"을
    키로 저장되는데, 재분류로 이름이 한 글자만 달라져도("물가안정 대책" → "물가안정
    정책") 그 기록이 통째로 무효가 되는 걸 실측으로 확인했다. 그래서 초안에서는 회차당
    한 번만 자동으로 분류하고, 그 뒤로는 담당자가 버튼을 눌러야 다시 분류한다.
    캐시에 없는 새 기사는 UNCLASSIFIED_GROUP_NAME 묶음에 따로 모아 화면 맨 아래
    보여준다 — 기존 묶음을 건드리지 않으면서 "아직 반영 안 됐다"를 눈에 보이게 한다.

    force: 담당자가 "소제목 다시 만들기"를 직접 눌렀을 때 — 캐시를 무시하고 새로 부른다.

    round_id: [추가: 2026-08-14] round_id_for_slot(초안)/round_id_for_run(확정본)으로
    만든 회차 식별자. 캐시 조회·저장 모두 이 값까지 맞아야 같은 항목으로 본다 — "다음
    회차는 전 회차 메모리를 안 쓴다"는 원칙(사용자 요청)을 지키는 지점이다. 프롬프트에
    넣는 "직전에 쓰던 소제목" 힌트도 _last_round_id가 지금 round_id와 같을 때만(=같은
    회차 안에서 다시 부르는 경우) 실제로 넘긴다 — 다른 회차로 넘어가면 이 힌트도 자동으로
    끊긴다. round_id=None(회차 개념이 없는 호출부, 예: 수시 모니터링)은 예전처럼 동작한다.

    None을 돌려주는 경우:
      - .env에 ANTHROPIC_API_KEY가 없음 (정상 — 이 기능을 안 쓰기로 한 선택)
      - anthropic 패키지 미설치 (정상)
      - 기사가 너무 적거나(2건 미만) 너무 많음(MAX_ARTICLES_FOR_CLASSIFY 초과) (정상)
      - allow_call=False인데 쓸 만한 캐시가 하나도 없음 (정상 — 초안의 의도된 동작)
      - [수정: 2026-08-12] API 호출 실패(네트워크·인증·rate limit 등) — **진짜 실패**인
        경우만 여기 남는다. 예전엔 실패하면 곧바로 규칙 기반(단어 빈도)으로 떨어졌는데,
        "이미 잘 분류돼 있는데 이번 호출만 실패한" 경우까지 그렇게 처리하면 멀쩡하던
        소제목이 한 단어로 뭉개졌다(담당자 제보, 2026-08-12). 이제 실패해도 먼저 이전
        분류를 재사용할 수 있는지 본다(_find_reusable/_find_partial) — 그래도 정말 없을
        때만 None을 돌려주고, 그 사실을 llm_attempt_failed_without_fallback()으로
        확정본이 알 수 있게 남긴다.
    """
    global _last_real_attempt_failed, _last_result_was_rule_based, _last_skipped_too_many_articles
    global _last_credit_error
    _last_real_attempt_failed = False
    _last_result_was_rule_based = False
    _last_skipped_too_many_articles = False
    _last_credit_error = False
    _ensure_cache_hydrated()
    if not is_configured():
        return None
    # 여기서부터는 "분류를 얻어내지 못하면 규칙 기반 폴백"이 확정이다 — 기본을 True로
    # 두고, 실제로 결과를 돌려주는 지점(_accept)에서만 내린다.
    _last_result_was_rule_based = True
    # [추가: 2026-08-11] 기사 수 때문에 건너뛰는 경우는 로그를 남긴다. 예전엔 이 분기가
    # 조용히 None을 돌려줘서, 실제로 3개 회차가 이 문턱에 걸려 규칙 기반으로 폴백했는데도
    # 담당자가 "소제목이 갑자기 이상해졌다"는 증상만 보고 원인을 알 수 없었다.
    if not 2 <= len(articles) <= MAX_ARTICLES_FOR_CLASSIFY:
        logger.warning(
            "기사 %d건은 LLM 소제목 분류 범위(2~%d건) 밖이라 규칙 기반으로 대체합니다",
            len(articles),
            MAX_ARTICLES_FOR_CLASSIFY,
        )
        # [추가: 2026-08-20] 기사가 너무 적어서(2건 미만) 건너뛴 경우는 이 함수 호출부
        # 어디에서도 화면에 도달하지 않는다 — 초안 배너 조건(MIN_ARTICLES_FOR_AUTO=5,
        # app.preview_renderer._compute_preview_content)이 이미 그보다 크다. 그래서
        # 이 플래그는 사실상 "너무 많아서"만 의미하고, 문구도 그렇게 고정해도 안전하다.
        _last_skipped_too_many_articles = True
        return None

    key = _cache_key(articles, max_subheadings, round_id)
    if not force:
        with _cache_lock:
            cached = _cache.get(key) or _find_reusable(key)
            if cached is not None:
                return _accept(_rebuild(cached, articles))
            if not allow_call:
                # 새로 부르지 않기로 한 상태 — 기사가 늘어난 경우엔 기존 분류를 그대로
                # 두고 새 기사만 따로 모아 보여준다(재분류 없이도 화면이 성립하도록).
                partial = _find_partial(key)
                if partial is not None:
                    return _accept(_rebuild(partial, articles, keep_leftover=True))
                # 이 회차 분류의 기사를 전부 숨긴 경우 — 전부 미분류로(_round_cache_emptied).
                # 마감(app.classifier.classify_for_finalize)은 이 결과를 받으면 처음부터 분류한다.
                if round_id is not None and _round_cache_emptied(key):
                    return _accept([{"name": UNCLASSIFIED_GROUP_NAME, "articles": list(articles), "summary": ""}])
                return None

    # [수정: 2026-08-14] force=True(담당자가 "🤖 전체 재분류" 버튼을 직접 누른 경우)는
    # allow_call 값과 무관하게 항상 실제 호출로 진행해야 한다 — force는 "캐시 무시"뿐
    # 아니라 "이번엔 진짜로 불러라"라는 담당자의 명시적 지시이기 때문이다. allow_call의
    # 기본값이 False로 바뀌면서(app.classifier.classify_articles 참고 — 큐레이션 조작은
    # 기본적으로 AI를 부르지 않는다), force_llm=True를 넘기는 호출부(snapshot_group_names
    # 경유)가 allow_llm_call은 따로 안 넘기고 새 기본값(False)을 물려받는데, 그 상태로
    # 여기서 그냥 "allow_call이 False니 return None"해버리면 재분류 버튼이 조용히
    # 아무 일도 안 하게 된다. force가 이 게이트를 무시하게 해 그 사고를 막는다.
    if not force and not allow_call:
        return None

    global _last_names, _last_round_id
    groups = None
    system_prompt = _load_prompt("SYSTEM_PROMPT")
    # [수정: 2026-08-14] _last_names는 round_id가 지금 호출과 같을 때만 프롬프트에 넣는다
    # — 다르면(=새 회차) "직전에 쓰던 소제목" 힌트가 사실 이전 회차의 이름이라, 그대로
    # 넘기면 AI가 다른 사건에 전 회차 이름을 재사용하려 든다(사용자 요청: 회차마다 새로
    # 분류하고 전 회차 기억은 안 쓴다).
    carryover_names = _last_names if _last_round_id == round_id else []
    # [추가: 2026-08-12] 네트워크 순단·rate limit처럼 한 번 더 시도하면 넘어갈 수 있는
    # 실패가 실제로 있어 즉시 1회 재시도한다(사용자 요청 — "폴백을 막을 방법"). 대기 없이
    # 바로 재시도한다: 이 호출은 HTTP 요청 처리 중에 동기로 실행되므로 스크랩 재시도
    # (5분 간격)처럼 오래 기다릴 수 없고, 순단성 오류는 보통 즉시 재시도로 충분하다.
    # system_prompt가 None(AI_RULES.md 없음/섹션 누락)이면 API를 부를 수 없으므로 루프를
    # 건너뛰고 곧장 아래 "groups is None" 폴백 경로로 넘어간다.
    for attempt in range(2 if system_prompt else 0):
        try:
            response = _get_client().messages.create(
                model=llm_model(),
                # [수정: 2026-08-20] 2000 -> 4096. 상한을 8->15로 올린 뒤 76건 회차가
                # 정확히 2000에서 끊긴 채(stop_reason=max_tokens) 우연히 파싱된 걸
                # 실측으로 발견했다 — 다음 회차엔 진짜로 잘려 조용히 규칙 기반 폴백으로
                # 떨어질 뻔했다. 4096으로 재실행하니 2003토큰에서 자연 종료(end_turn)—
                # 실사용량 대비 2배 여유. max_tokens는 상한일 뿐 실제 쓴 만큼만 청구되므로
                # 이 변경 자체는 비용에 영향이 없다(같은 76건 회차, 같은 32.6원).
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": _build_prompt(articles, max_subheadings, carryover_names)}],
                output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            )
            _log_usage("소제목 분류", response, len(articles))
            text = next((b.text for b in response.content if b.type == "text"), None)
            groups = _parse_groups(json.loads(text), articles, max_subheadings) if text else None
            if groups is None:
                # [추가: 2026-08-27] 호출은 성공(HTTP 200)했는데 쓸 만한 분류를 못 얻은 경우.
                # 응답에 text 블록이 아예 없거나(text is None), 있어도 _parse_groups가 빈
                # 결과를 돌려준(모델이 groups: []를 주거나 이름이 전부 빈 문자열) 상황이다.
                #
                # 예전엔 여기서 그냥 break로 빠져나가 **남은 재시도 1회를 안 썼고**, 로그도
                # 한 줄 안 남겨서 사후에 원인을 가릴 방법이 전혀 없었다(2026-08-27 06:00 회차:
                # POST 200이 딱 1번 찍히고 경고 없이 규칙 기반 폴백 — 위 둘 중 어느 쪽이었는지
                # 끝내 판별 못 했다). 예외 경로는 이미 재시도하는데 이쪽만 안 하는 건 일관성도
                # 없다 — 둘 다 "이번 호출에서 결과를 못 얻었다"는 같은 상태다.
                logger.warning(
                    "LLM 응답에서 소제목을 못 뽑았습니다 (%d번째 시도) — stop_reason=%s, "
                    "content 블록=%s, 기사 %d건, text 앞부분=%r",
                    attempt + 1,
                    getattr(response, "stop_reason", None),
                    [getattr(b, "type", "?") for b in response.content],
                    len(articles),
                    (text or "")[:200],
                )
                if attempt == 0:
                    continue
            break
        except Exception as exc:
            # [추가: 2026-08-20] anthropic.BadRequestError(크레딧 부족 등)인지 별도로
            # 표시해둔다 — 이 원인은 재시도해도 항상 같은 결과라, 초안 화면이 "다시
            # 눌러보라"는 일반 안내 대신 크레딧 확인을 안내해야 한다. 패키지가 아예 없는
            # 경우(ImportError)까지 anthropic 모듈을 다시 import하려 들면 안 되므로 감싼다.
            try:
                import anthropic as _anthropic_mod

                if isinstance(exc, _anthropic_mod.BadRequestError):
                    _last_credit_error = True
            except ImportError:
                pass
            if attempt == 0:
                logger.warning("LLM 소제목 분류 실패 — 한 번 더 시도합니다", exc_info=True)
                continue
            # 어떤 예외든 여기서 삼킨다 — 소제목 품질 개선 기능 하나 때문에 스크랩 전체가
            # 멈추면 안 된다. 원인 파악은 로그로 하고, 아래에서 이전 분류 재사용을 시도한다.
            logger.exception("LLM 소제목 분류 재시도도 실패")

    if groups is None:
        # [추가: 2026-08-12] "이미 분류가 있으면 LLM이 실패해도 그걸 따른다" — 규칙 기반
        # (단어 빈도)으로 완전히 새로 짓는 것보다, 조금 낡았더라도 AI가 지은 이름을
        # 유지하는 편이 훨씬 낫다. force=True(담당자가 명시적으로 재분류를 요청한 경우)
        # 에도 적용한다 — 재분류 자체가 실패했다고 화면이 한 단어 소제목으로 망가지는
        # 것보다는 "달라진 게 없다"가 낫다. 캐시에 없는 새 기사는 임시 칸으로 모은다.
        with _cache_lock:
            fallback = _cache.get(key) or _find_reusable(key) or _find_partial(key)
        if fallback is not None:
            logger.warning("LLM 응답을 받지 못해 직전 분류를 그대로 재사용합니다")
            return _accept(_rebuild(fallback, articles, keep_leftover=True))
        _last_real_attempt_failed = True
        return None

    # [추가: 2026-08-24] 1차 분류의 "기타"가 유독 크면 그 부분만 다시 부른다 —
    # _refine_etc_bucket 참고. 실패해도 예외를 삼키고 원래 groups를 그대로 돌려주므로
    # 이 호출이 classify_with_llm 자체의 실패 경로를 새로 만들지 않는다.
    groups = _refine_etc_bucket(groups, len(articles), max_subheadings, system_prompt)

    with _cache_lock:
        _store_locked(key, groups)
        _last_names = [g["name"] for g in groups]
        _last_round_id = round_id
        _persist_cache_locked()
    return _accept(groups)


def seed_cache(articles: list, groups: list, max_subheadings: int = MAX_SUBHEADINGS, round_id: Optional[tuple] = None) -> None:
    """API를 부르지 않고 조립한 분류 결과를, 방금 API로 받은 것처럼 캐시에 직접 심는다.

    [추가: 2026-08-13] app.classifier.classify_for_finalize가 회차 확정 저장 직후 호출한다.
    그 함수는 API를 새로 부르지 않고(allow_call=False) 초안 캐시를 재사용 + assign_to_existing로
    새 기사를 끼워 넣는 방식으로 결과를 만드는데, 이 방식은 _cache에 **지금 이 정확한 기사
    구성(확정본 전체)의 항목을 남기지 않는다** — 재사용/부분재사용 경로는 새 키로 쓰지
    않기 때문이다. 그대로 두면 확정본에서 처음 기사를 숨기거나 옮길 때
    (app.settings_server._handle_move_article 등이 부르는 snapshot_group_names는
    allow_llm_call 기본값 True) 정확한 캐시 적중이 없어 다시 API를 부르고, 그때 또
    이름이 바뀔 수 있었다(2026-08-13 17시 회차 사고의 재발 경로). 여기서 캐시를 직접
    채워두면 이후 같은 기사 구성(또는 기사가 줄기만 한 부분집합, 예: 하나 숨김)으로
    다시 조회될 때 _cache.get/_find_reusable이 바로 맞아떨어져 API를 다시 부르지 않는다.

    round_id: [추가: 2026-08-14] 확정 저장 시점엔 round_id_for_run(run)을 넘긴다 — 이
    직전 초안의 round_id_for_slot(slot)과 같은 값이 나오도록 맞춰뒀으므로(같은 슬롯이면
    날짜·끝시각이 같다), 초안에서 만든 분류가 그대로 확정본 캐시로 이어진다.
    """
    key = _cache_key(articles, max_subheadings, round_id)
    global _last_names, _last_round_id
    with _cache_lock:
        _store_locked(key, groups)
        _last_names = [g["name"] for g in groups]
        _last_round_id = round_id
        _persist_cache_locked()


# [추가: 2026-08-12] 초안의 "🤖 미분류 기사 분류" 전용 — 증분 배정.
#
# 기존 재분류(classify_with_llm)와 근본적으로 다른 점: 이건 **묶는 게 아니라 고르는**
# 작업이다. 이미 있는 소제목 목록을 주고 그 중 하나를 고르게 하므로, 모델이 기존 소제목의
# 이름을 바꿀 방법 자체가 없다. 담당자가 다듬어둔 분류가 절대 안 흔들리는 이유다.
#
# 규칙(사용자와 합의, 2026-08-12):
#   - 📂 소제목 미분류에 있는 기사만 대상으로 한다.
#   - 기존 소제목 중 맞는 게 있으면 거기 넣는다.
#   - 정말 없으면 새 소제목을 제안한다(기존 소제목은 건드리지 않으므로 안전하다).
#   - **담당자가 직접 만든 소제목은 후보에서 뺀다** — 담당자가 특정 기사만 넣으려고 만든
#     칸일 수 있는데 모델은 그 의도를 알 방법이 없다. 잘못 넣으면 되돌리기 번거롭고
#     신뢰가 깨지는 반면, 안 넣어서 생기는 손해는 드롭다운 한 번이라 비대칭이다.
#   - "기타"는 후보로 주지 않는다 — 선택지에 있으면 모델이 애매한 걸 전부 거기로 흘려버려
#     증분 배정의 의미가 없어진다. 자리가 없을 때만 호출부가 "기타"로 떨어뜨린다.
_ASSIGN_SCHEMA = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "article_id": {"type": "integer", "description": "기사 번호"},
                    "group": {
                        "type": ["string", "null"],
                        "description": "배정할 기존 소제목 이름. 맞는 게 없으면 null",
                    },
                    "new_group_name": {
                        "type": ["string", "null"],
                        "description": "group이 null일 때만, 새로 만들 소제목 이름 (20자 이내)",
                    },
                },
                "required": ["article_id", "group", "new_group_name"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["assignments"],
    "additionalProperties": False,
}


_ASSIGN_SAMPLE_TITLES = 3
_ASSIGN_TITLE_CHARS = 45

# [추가: 2026-08-26] assign_to_existing 실패 원인 구분용 전역 — 담당자가 "🤖 미분류 배정"을
# 눌렀는데 아무것도 안 바뀌면 왜인지 알려줘야 한다(예전엔 무조건 None이라 호출부가 이유를
# 몰라 조용히 204만 돌려줬다). 기본값을 "성공"이 아니라 "이유 불명"으로 둬서, 반환 지점을
# 하나라도 빠뜨렸을 때 틀리는 방향이 "괜한 경고"이지 "조용한 침묵"이 아니게 한다.
_last_assign_failure = "unknown"


def last_assign_failure_reason() -> Optional[str]:
    """직전 assign_to_existing 호출이 실패/무결과였다면 그 이유, 성공했으면 None.

    값: "no_candidates"(배정할 기존 소제목이 없음) | "not_configured"(API 키 없음) |
    "api_error"(호출·응답 해석 실패) | "no_match"(모델이 응답했지만 쓸만한 배정이 하나도
    없음 — 지금 있는 소제목 중 맞는 게 정말 없다는 뜻으로, 오류가 아니라 정상적인 결과다).
    """
    return _last_assign_failure


def build_assign_candidates(
    groups: list, exclude_names, labels: Optional[dict] = None, priority_urls: Optional[set] = None
) -> list:
    """assign_to_existing에 넘길 후보 소제목 목록을 만든다.

    [추가: 2026-08-26] 예전엔 소제목 "이름"만 모델에게 보여줬다 — 그러다 보니 두 가지를
    놓쳤다: (1) 담당자가 ✏️로 고친 이름("세제개편" → "세제개편 관련")은 화면 표시용일
    뿐 내부적으로는 여전히 원래 이름이라, 모델은 원래(더 좁아 보이는) 이름만 보고 판단해
    담당자가 일부러 넓혀둔 분류 의도를 몰랐다. (2) 그 소제목에 이미 어떤 기사가 들어있는지
    전혀 안 보여줘서, 이름 하나로는 판단이 안 서는 경계 사례(예: "세제개편 관련" 칸에
    가업상속공제 기사가 이미 있다는 사실)를 놓쳤다.

    - display_name: app.curation.display_group_name으로 표시 이름을 구해 보여준다.
      모델이 표시 이름으로 답하면 assign_to_existing이 원본 이름으로 역매핑해 저장하므로
      group_overrides 등 내부 저장은 항상 원본 이름 그대로다.
    - samples: 그 소제목의 대표 기사 제목 최대 _ASSIGN_SAMPLE_TITLES건. priority_urls
      (담당자가 직접 그 소제목으로 옮긴 기사의 URL)가 있으면 그것부터 채운다 — AI가 자기
      자신의 자동 분류 결과를 근거로 다음 배정을 정당화하는 자기강화 루프를 막기 위해서다
      (담당자가 의도를 갖고 옮긴 기사가 훨씬 신뢰할 수 있는 신호).
    """
    from app.curation import display_group_name  # 순환 임포트 회피 — _build_prompt와 동일

    exclude = set(exclude_names or ())
    priority_urls = priority_urls or set()
    labels = labels or {}
    candidates = []
    for g in groups:
        if g["name"] in exclude or g["name"] in (UNCLASSIFIED_GROUP_NAME, "기타"):
            continue
        priority = [a for a in g["articles"] if a["url"] in priority_urls]
        rest = [a for a in g["articles"] if a["url"] not in priority_urls]
        samples = [a["title"][:_ASSIGN_TITLE_CHARS] for a in (priority + rest)[:_ASSIGN_SAMPLE_TITLES]]
        candidates.append(
            {
                "name": g["name"],
                "display_name": display_group_name(g["name"], labels),
                "samples": samples,
            }
        )
    return candidates


def assign_to_existing(articles: list, groups) -> Optional[dict]:
    """미분류 기사들을 기존 소제목에 배정한다. {기사 url: 소제목 이름} 또는 실패 시 None.

    groups: build_assign_candidates가 만든 [{"name", "display_name", "samples"}, ...],
    또는(하위 호환) 이름 문자열만 담긴 리스트 — app.adhoc.classifier처럼 표시 이름·대표
    기사 개념이 없는 호출부는 문자열 리스트를 그대로 넘겨도 된다.

    돌려주는 이름은 groups 안의 원본 이름(display_name이 아니라 name)이거나, 모델이
    새로 제안한 이름이다. 호출부가 소제목 개수 상한을 확인해 새 이름을 받아들일지
    ("기타"로 보낼지) 정한다.

    실패(설정 없음·호출 오류·응답 해석 불가)하거나 모델이 정말 맞는 곳이 없다고 답하면
    None — 호출부는 아무것도 바꾸지 않고 미분류 상태를 그대로 둔다(기사를 잃거나 엉뚱한
    데 넣는 것보다 안전하다). 실패 이유는 last_assign_failure_reason()으로 구분한다.
    """
    global _last_assign_failure
    normalized = [
        g if isinstance(g, dict) else {"name": g, "display_name": g, "samples": []} for g in (groups or [])
    ]
    if not articles or not normalized:
        _last_assign_failure = "no_candidates"
        return None
    if not is_configured():
        _last_assign_failure = "not_configured"
        return None
    system_prompt = _load_prompt("ASSIGN_SYSTEM_PROMPT")
    if system_prompt is None:
        _last_assign_failure = "not_configured"
        return None
    display_to_raw = {}
    for g in normalized:
        display_to_raw.setdefault(g.get("display_name") or g["name"], g["name"])
    lines = [
        f"[{i}] ({a.get('outlet', '')}) {a.get('title', '')}\n{(a.get('summary') or '')[:_SUMMARY_CHARS]}"
        for i, a in enumerate(articles)
    ]
    group_lines = []
    for g in normalized:
        block = f"- {g.get('display_name') or g['name']}"
        for title in g.get("samples") or []:
            block += f"\n    · {title}"
        group_lines.append(block)
    prompt = (
        "기존 소제목(각 이름 아래 · 로 시작하는 줄은 그 소제목에 이미 들어있는 기사 제목 몇 건 — "
        "판단 참고용일 뿐 강제 규칙이 아니다):\n"
        + "\n".join(group_lines)
        + f"\n\n아래 기사 {len(articles)}건을 위 소제목 중 하나에 배정하라.\n"
        + "각 기사 앞의 [번호]를 article_id에 그대로 쓴다.\n\n"
        + "\n".join(lines)
    )
    try:
        response = _get_client().messages.create(
            model=llm_model(),
            # [수정: 2026-08-25] 2000 -> 8000. 이 함수는 원래 초안의 "🤖 미분류 배정"
            # 버튼용(몇 건 배정)이라 2000으로 잡았는데, classify_for_finalize가 회차 마감
            # 때 "초안 이후 새로 들어온 기사 전부"를 여기로 넘기면서 규모가 완전히
            # 달라졌다. 실측(2026-08-25 17:00 회차): 112건을 배정하는 응답 JSON이
            # 8,016자(최소 2,900토큰)라 2000에서 잘렸고 -> json.loads 예외 -> None ->
            # 호출부의 "빠진 기사는 전부 기타" 규칙이 112건을 통째로 "기타"에 넣었다
            # (그 회차 119건 중 112건이 기타). 스키마가 세 필드를 모두 required로
            # 요구해(article_id/group/new_group_name) 항목당 30토큰 가까이 쓰는 것도
            # 이유다. 150건이면 약 4,000토큰이므로 8000은 2배 여유다.
            # max_tokens는 상한일 뿐 실제 쓴 만큼만 청구되므로 비용 영향은 없다.
            # (같은 종류의 잘림을 classify_with_llm에서 2026-08-20에 이미 겪고
            # 2000->4096으로 고쳤는데, 이 함수만 2000으로 남아 있었다.)
            max_tokens=8000,
            system=system_prompt,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": _ASSIGN_SCHEMA}},
        )
        _log_usage("미분류 배정", response, len(articles))
        text = next((b.text for b in response.content if b.type == "text"), None)
        payload = json.loads(text) if text else None
    except Exception:
        logger.exception("LLM 증분 배정 실패 — 미분류 상태를 그대로 둡니다")
        _last_assign_failure = "api_error"
        return None
    if not payload:
        _last_assign_failure = "api_error"
        return None

    result = {}
    for item in payload.get("assignments", []):
        idx = item.get("article_id")
        if not isinstance(idx, int) or not 0 <= idx < len(articles):
            continue
        raw_group = (item.get("group") or "").strip()
        if raw_group in display_to_raw:
            # 모델이 표시 이름(담당자가 ✏️로 고친 이름)으로 답했으면 내부 저장용
            # 원본 이름으로 되돌린다 — group_overrides 등은 항상 원본 이름 기준이다.
            name = display_to_raw[raw_group]
        else:
            # 표시 이름 목록에 없는 값이면 기존 소제목이 아니라고 보고, new_group_name을
            # 새 소제목 제안으로 취급한다(모델이 실수로 group에 새 이름을 넣었을 때를
            # 대비해 그 값도 마지막 대안으로 남겨둔다).
            name = (item.get("new_group_name") or raw_group or "").strip()
        if not name or name == "기타":
            continue
        result[articles[idx]["url"]] = name
    if not result:
        _last_assign_failure = "no_match"
        return None
    _last_assign_failure = None
    return result


# [추가: 2026-09-15] 「AI 기사 나누기」(초안 하단바)의 실패 이유 — last_assign_failure_reason과
# 같은 방식이다. 기본값을 "이유 불명"으로 둬서 반환 지점을 빠뜨렸을 때 조용히 성공으로
# 읽히지 않게 한다.
_last_split_failure = "unknown"


def last_split_failure_reason() -> Optional[str]:
    """직전 split_group_articles가 실패했다면 그 이유, 성공했으면 None.

    값: "not_configured"(API 키·프롬프트 없음) | "api_error"(호출·응답 해석 실패).
    모델이 "더 나눌 게 없다"며 한 묶음으로 돌려준 것은 실패가 아니다 — 그 판단은
    호출부(app.group_split.plan_split)가 결과를 보고 한다.
    """
    return _last_split_failure


def split_group_articles(articles: list, max_subheadings: int, current_name: str, avoid_names) -> Optional[list]:
    """소제목 하나에 담긴 기사들(담당자가 체크한 것)만 떼어 쟁점별로 다시 묶는다.

    [추가: 2026-09-15] 초안 하단바의 「AI 기사 나누기」 전용. 하는 일은 _refine_etc_bucket과
    같다 — 한 묶음만 떼어 **같은 분류 프롬프트(SYSTEM_PROMPT)**로 다시 묻는다. 그 함수를 만들
    때 "전체를 한꺼번에 볼 때보다 남은 기사만 놓고 다시 볼 때 모델이 훨씬 잘 묶는다"는 게
    실측으로 확인됐고(40건 → 4건), 이 기능이 먹힐 근거도 그것이다. 다른 점은 둘:
    (1) 원래 소제목 이름을 알려주고 "담당자가 너무 커서 나눠 달라고 했다"는 맥락을 준다 —
    이름을 원래보다 구체적으로 짓게 하려는 것이다. (2) 여기서는 캐시를 안 건드린다 —
    결과는 호출부가 배정 기록(group_overrides)으로 남긴다(「AI 기사 배정」과 같은 경로라
    초안·마감 후 확정본 어디서든 같은 방식으로 따라간다).

    반환: [{"name", "articles", "summary"}, …] (_parse_groups와 같은 모양) 또는 실패 시 None.
    """
    global _last_split_failure
    if not articles:
        _last_split_failure = "api_error"
        return None
    if not is_configured():
        _last_split_failure = "not_configured"
        return None
    system_prompt = _load_prompt("SYSTEM_PROMPT")
    if system_prompt is None:
        _last_split_failure = "not_configured"
        return None
    avoid = [n for n in (avoid_names or []) if n and n != current_name]
    request = (
        f"\n\n[요청] 위 기사 {len(articles)}건은 지금 「{current_name}」라는 소제목 하나에 들어 있다. "
        "담당자가 한 소제목에 너무 많은 쟁점이 섞였다며 쟁점별로 나눠 달라고 요청했다. "
        "같은 사안 안에서도 쟁점이 다르면(예: 후보자 의혹 / 정책 구상 / 다른 인물 청문회) 나눠라. "
        f"새 이름은 「{current_name}」보다 구체적으로 짓는다. 여러 쟁점에 두루 걸치는 기사만 "
        f"남는다면 그 묶음은 「{current_name}」라는 이름을 글자 그대로 써도 된다. "
        "정말 한 쟁점뿐이라면 억지로 나누지 말고 하나로 둔다."
    )
    if avoid:
        request += "\n이미 다른 소제목으로 쓰이는 이름이니 겹치지 않게 지어라: " + ", ".join(avoid)
    groups = None
    for attempt in range(2):
        try:
            response = _get_client().messages.create(
                model=llm_model(),
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": _build_prompt(articles, max_subheadings) + request}],
                output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            )
            _log_usage("소제목 나누기", response, len(articles))
            text = next((b.text for b in response.content if b.type == "text"), None)
            groups = _parse_groups(json.loads(text), articles, max_subheadings) if text else None
            if groups:
                break
            logger.warning(
                "소제목 나누기 응답에서 묶음을 못 뽑았습니다 (%d번째 시도) — stop_reason=%s",
                attempt + 1,
                getattr(response, "stop_reason", None),
            )
        except Exception:
            logger.warning("소제목 나누기 호출 실패 (%d번째 시도)", attempt + 1, exc_info=True)
    if not groups:
        _last_split_failure = "api_error"
        return None
    _last_split_failure = None
    return groups
