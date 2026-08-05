# Design Ref: PRD.md 기능1 규칙 6 — 기사 제목·요약에서 형광펜 단어를 단어별로 다른 색으로 하이라이트
import html
import re

from app.config import HIGHLIGHT_COLORS, HIGHLIGHT_TEXT_COLOR


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
    """
    # 빈 단어는 제외한다 (빈 패턴은 모든 위치에 매칭되어 오작동한다).
    valid_items = [item for item in keywords if item.get("word", "").strip()]

    if not text:
        return ""
    if not valid_items:
        return html.escape(text)

    color_by_keyword = {
        item["word"].lower(): HIGHLIGHT_COLORS[item.get("color", 0) % len(HIGHLIGHT_COLORS)]
        for item in valid_items
    }

    # 긴 키워드부터 매칭하도록 정렬한 뒤 정규식 대안(alternation)으로 합친다.
    sorted_words = sorted({item["word"] for item in valid_items}, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(w) for w in sorted_words), re.IGNORECASE)

    # 매칭 부분/비매칭 부분을 나눠 각각 이스케이프하고, 매칭 부분만 span으로 감싼다.
    result = []
    last_end = 0
    for match in pattern.finditer(text):
        result.append(html.escape(text[last_end:match.start()]))
        matched_text = html.escape(match.group())
        word_key = match.group().lower()
        color = color_by_keyword[word_key]
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
