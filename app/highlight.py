# Design Ref: PRD.md 기능1 규칙 6 — 기사 제목·요약에서 형광펜 단어를 단어별로 다른 색으로 하이라이트
import html
import re

from app.config import HIGHLIGHT_COLORS, HIGHLIGHT_TEXT_COLOR
from app.search_terms import split_terms


def highlight_keywords(text: str, keywords: list[dict]) -> str:
    """
    텍스트(제목 또는 요약)에서 키워드와 일치하는 부분을 색이 입혀진 <span>으로 감싼
    HTML 조각을 돌려준다. 제목·요약 어디에 쓰이든 같은 단어는 항상 같은 색이다.

    - 원문은 화면에 그대로 노출되므로 HTML 특수문자(<, >, & 등)를 이스케이프한다.
      키워드로 감싸는 <span> 태그만 실제 태그로 남고, 나머지 텍스트는 안전하게 처리된다.
    - keywords는 [{"word": 단어, "color": 팔레트 인덱스}, ...] 형태다. [수정: 2026-07-25]
      전엔 등록 순서(자리)로 색을 자동 배정했는데, 단어 하나를 지우면 뒤에 있던 단어들
      색이 밀려 바뀌는 문제가 있었다 — 그래서 색을 단어에 직접 못박아 저장하고, 여기서는
      그 값을 그대로 쓰기만 한다(색 배정 로직 없음).
    - 키워드가 여러 개면 모두 하이라이트한다. 긴 키워드를 우선 매칭해
      짧은 키워드가 긴 키워드 일부만 잘라 강조하는 일을 막는다 (예: "재정" vs "재정경제부").
    - 영문 키워드를 위해 대소문자를 구분하지 않고 일치시키되, 강조 시에는 원문 표기를 그대로 유지한다.
    - 검색어 규칙은 앱 어디서나 같다(app.search_terms) — 띄어 쓴 말은 붙어 있는 그대로
      칠하고, `+`로 묶은 단어는 **조각마다** 칠한다(`가상자산+과세` → 두 낱말이 각각
      칠해진다). 예전엔 칸 값을 통째로 찾아 `+`가 든 단어는 어디에도 안 칠해졌다.
      조각끼리는 등록 단어의 색을 함께 쓴다 — 한 단어가 두 색이면 같은 단어로 안 읽힌다.
    """
    # 빈 단어는 제외한다 (빈 패턴은 모든 위치에 매칭되어 오작동한다).
    valid_items = [item for item in keywords if item.get("word", "").strip()]

    if not text:
        return ""
    if not valid_items:
        return html.escape(text)

    # 조각 → (색, 등록 단어). data-word는 **등록 단어 그대로** 둔다 — 형광펜 팝오버가
    # `.hl-word[data-word="…"]`로 같은 단어가 나온 모든 자리를 찾아 색을 바꾸므로(renderer의
    # cycleChipColor), 여기에 조각을 적으면 `+` 든 단어의 색 바꾸기가 조용히 안 먹는다.
    color_by_term: dict[str, str] = {}
    word_by_term: dict[str, str] = {}
    for item in valid_items:
        color = HIGHLIGHT_COLORS[item.get("color", 0) % len(HIGHLIGHT_COLORS)]
        registered = item["word"].lower()
        for term in split_terms(item["word"]):
            key = term.lower()
            if key in color_by_term:
                continue  # 먼저 등록된 단어가 그 조각의 색을 지킨다
            color_by_term[key] = color
            word_by_term[key] = registered

    if not color_by_term:
        return html.escape(text)

    # 긴 조각부터 매칭하도록 정렬한 뒤 정규식 대안(alternation)으로 합친다.
    sorted_terms = sorted(color_by_term, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(t) for t in sorted_terms), re.IGNORECASE)

    # 매칭 부분/비매칭 부분을 나눠 각각 이스케이프하고, 매칭 부분만 span으로 감싼다.
    result = []
    last_end = 0
    for match in pattern.finditer(text):
        result.append(html.escape(text[last_end:match.start()]))
        matched_text = html.escape(match.group())
        term_key = match.group().lower()
        color = color_by_term[term_key]
        word_key = word_by_term[term_key]
        # [수정: 2026-08-05] 본문 안 단어를 직접 클릭해 색을 바꾸던 기능은 삭제 요청에
        # 따라 없앴다 — 색 순환은 이제 "🖍️ 형광펜 단어 편집" 팝오버의 칩에서만 가능하다
        # (cycleChipColor). data-word는 그대로 남겨둔다 — 팝오버에서 색을 바꿀 때 같은
        # 단어가 나온 모든 자리(제목·요약, 다른 기사)를 한 번에 찾아 배경색을 갱신하는 데
        # 여전히 필요하다.
        result.append(
            f'<span class="hl-word" data-word="{html.escape(word_key)}" '
            f'style="background-color:{color};color:{HIGHLIGHT_TEXT_COLOR}">{matched_text}</span>'
        )
        last_end = match.end()
    result.append(html.escape(text[last_end:]))
    return "".join(result)
