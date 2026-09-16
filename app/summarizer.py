# Design Ref: PRD.md 기능2 규칙 4·5 — 전체 주요 키워드 + 소제목별 요약 자동 추출 (규칙 기반, LLM 없음)
import re
from collections import Counter
from typing import Optional

from app.config import (
    DEFAULT_KEYWORDS,
    LANDING_KEYWORD_COUNT,
    SUMMARY_MAX_CHARS,
    SUMMARY_MAX_SENTENCES,
)
from app.tokenizer import STOPWORDS, tokenize

# 문장 끝(마침표·물음표·느낌표, 한국어 "다." 포함)을 경계로 문장을 나눈다.
_SENTENCE_PATTERN = re.compile(r"[^.!?]*[.!?]|[^.!?]+$")


def _rank_keywords_by_frequency(
    articles: list,
    keywords: Optional[list],
    use_summary: bool = False,
    exclude_keywords: bool = True,
    extra_exclude: Optional[list] = None,
) -> list:
    """전체 기사의 단어를 문서빈도 내림차순으로 정렬해 (단어, 빈도) 쌍 목록을 돌려준다.

    검색 키워드는 거의 모든 제목/요약에 있어(특히 AND 모드) 소제목·워드클라우드 단어로는
    쓸모없어 기본적으로 제외한다. 빈도가 같으면 먼저 등장한 단어를 우선하고, 그것도
    같으면 단어 자체로 정렬해 결과를 결정적으로 만든다. 소제목 분류(app.classifier)와
    extract_keyword_frequencies(워드클라우드)가 공유하는 내부 로직이다.

    use_summary: [추가: 2026-07-24] True면 제목 대신 네이버 요약(description)에서 단어를
    뽑는다 — 진입 화면 워드클라우드용. 소제목 분류(classifier.py)와 같은 이유로, 요약문이
    보도자료 내용을 더 통일되게 옮겨써서 겹치는 단어가 잘 드러난다.

    exclude_keywords: [수정: 2026-07-25] False면 검색 키워드도 그대로 집계에 포함한다 —
    워드클라우드는 OR 검색에서 특정 키워드(예: 인물명)가 실제로 얼마나 자주 언급되는지도
    보여주고 싶다는 요청으로, 워드클라우드(extract_keyword_frequencies)만 이 값을 꺼둔다.
    소제목 분류는 그대로 제외 유지 — 부서명 같은 키워드가 소제목이 되면 기사 전체가
    한 그룹으로 뭉쳐버리기 때문이다.

    extra_exclude: [추가: 2026-07-25] 설정 화면 "☁️ 워드클라우드 제외어"에서 사용자가
    직접 등록한 단어 목록. exclude_keywords와 무관하게 항상 추가로 제외한다.
    """
    if not articles:
        return []

    keywords = keywords if keywords is not None else DEFAULT_KEYWORDS
    stopwords = (
        STOPWORDS
        | ({k.lower() for k in keywords} if exclude_keywords else set())
        | {w.lower() for w in (extra_exclude or [])}
    )

    if use_summary:
        token_sets = [tokenize(a.get("summary") or a["title"], stopwords) for a in articles]
    else:
        token_sets = [tokenize(a["title"], stopwords) for a in articles]
    doc_freq = Counter(word for tokens in token_sets for word in tokens)
    first_seen = {}
    for i, tokens in enumerate(token_sets):
        for word in tokens:
            first_seen.setdefault(word, i)

    ranked = sorted(doc_freq, key=lambda w: (-doc_freq[w], first_seen[w], w))
    return [(word, doc_freq[word]) for word in ranked]


def extract_keyword_frequencies(
    articles: list,
    keywords: Optional[list] = None,
    top_n: int = LANDING_KEYWORD_COUNT,
    exclude_words: Optional[list] = None,
) -> list:
    """진입 화면 워드클라우드용으로, (단어, 빈도) 쌍을 top_n개까지 돌려준다 (PRD.md 기능3 규칙 3).

    [수정: 2026-07-24] 제목이 아니라 요약(description)에서 단어를 뽑도록 변경 — 소제목
    분류 기준을 요약으로 바꾼 것과 같은 이유(규칙 참고). [수정: 2026-07-25] 검색
    키워드도 제외하지 않고 그대로 집계한다(exclude_keywords=False) — 화면에서 색을
    다르게 표시해 구분한다(app.landing_renderer.render_word_cloud). exclude_words는
    사용자가 설정 화면에서 직접 등록한 워드클라우드 제외어 목록.
    """
    ranked = _rank_keywords_by_frequency(
        articles, keywords, use_summary=True, exclude_keywords=False, extra_exclude=exclude_words
    )
    return ranked[:top_n]


def _trim_summary(text: str) -> str:
    """요약문을 3문장 이내·250자 이내로 자른다 (AI_RULES.md — 요약 규칙 참고).

    네이버 요약에 흔한 말줄임표("...")는 마침표 3개가 연달아 있어, 단순히 "."로
    문장을 나누면 내용 없는 "." 조각이 진짜 문장 자리를 빼앗는다. 그래서 문장부호만
    남는 조각은 버리고(strip(" .!?")로 내용 유무 판단), 원래 문장 텍스트(s.strip())는
    그대로 보존한다.

    [수정: 2026-08-12] 예전엔 문장을 먼저 이어붙인 뒤 글자 수로 그냥 잘라서
    "...서울 중구."처럼 문장 중간에서 끊기는 일이 잦았다(실사용 중 발견). 이제
    문장 경계 안에서만 자른다 — 다음 문장을 넣으면 한도를 넘는 시점에서 멈추고,
    그때까지 담은 문장은 전부 온전하다. 문장 하나조차 한도를 넘는 극단적인 경우만
    마지막 수단으로 단어 경계에서 자른다(그래도 글자 중간보다는 낫다).
    """
    text = text.strip()
    sentences = [s.strip() for s in _SENTENCE_PATTERN.findall(text) if s.strip(" .!?")]
    kept: list = []
    length = 0
    for s in sentences[:SUMMARY_MAX_SENTENCES]:
        extra = len(s) + (1 if kept else 0)  # 문장 사이 띄어쓰기 1칸
        if length + extra > SUMMARY_MAX_CHARS:
            break  # 첫 문장이라도 한도를 넘으면 여기서 멈춘다 — kept가 비어야 아래
            # 단어-경계 폴백이 실행된다("kept and " 가드가 있으면 첫 문장은 무조건
            # 통과해버려 폴백이 죽은 코드가 되는 버그가 있었다).
        kept.append(s)
        length += extra
    if kept:
        return " ".join(kept)
    if not sentences:
        return ""
    first = sentences[0]
    if len(first) <= SUMMARY_MAX_CHARS:
        return first
    cut = first[:SUMMARY_MAX_CHARS].rsplit(" ", 1)[0]
    return (cut or first[:SUMMARY_MAX_CHARS]).rstrip() + "…"


def _summarize_group_from_first_article(group: dict) -> str:
    """소제목의 첫 기사 하나만 보고 대표 요약문을 만든다 (규칙 기반, LLM 없음).

    우선순위 가장 높은(맨 앞) 기사부터 훑어 요약(description)이 있는 첫 기사를 대표로 삼고,
    3문장·250자 이내로 다듬는다. 모든 기사에 요약이 없으면 맨 앞 기사 제목을 대신 쓴다.
    """
    articles = group["articles"]
    for article in articles:
        summary = (article.get("summary") or "").strip()
        if summary:
            return _trim_summary(summary)
    return _trim_summary(articles[0]["title"]) if articles else ""


def summarize_group(group: dict) -> str:
    """소제목 그룹 하나의 대표 요약문을 만든다.

    [수정: 2026-08-12] AI_RULES.md 참고 — group["summary"]가 있으면(app.llm_classifier가
    분류와 같은 호출에서 그 소제목 전체를 읽고 쓴 진짜 요약) 그걸 그대로 쓴다. 첫 기사
    하나만 보고 대표하는 옛 방식보다 소제목 전체 내용을 반영하고, 화면 문구("AI가 읽은
    소제목별 주요 요약")도 그제야 사실이 된다. _trim_summary는 AI가 250자 규칙을
    안 지켰을 때의 안전망일 뿐, 정상적으로는 이미 규칙 안에 있어 그대로 통과한다.

    group["summary"]가 없으면(규칙 기반 분류로 폴백했거나, LLM이 이 필드를 안 채운 옛
    캐시) _summarize_group_from_first_article로 대신한다.

    [제거: 2026-08-20] use_llm_summary 파라미터가 있었다 — 규칙 기반 요약을 강제하던
    유일한 호출부가 없어지면서 함께 지웠다(경위는 HISTORY.md 참고).
    """
    llm_summary = (group.get("summary") or "").strip()
    if llm_summary:
        return _trim_summary(llm_summary)
    return _summarize_group_from_first_article(group)


def summarize_groups(groups: list) -> list:
    """소제목 그룹 목록을 [{"name": 소제목, "summary": 요약문}, ...]로 바꾼다 (PRD 규칙 5)."""
    return [{"name": g["name"], "summary": summarize_group(g)} for g in groups]
