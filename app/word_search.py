"""낱말로 기사를 찾는 규칙 한 곳 — 「무엇이 걸렸나(matches)」·「어디가 걸렸나(mark)」·
「왜 걸렸나(snippet)」 셋.

낱말 칸 자체의 문법은 app.search_terms(띄어쓰기는 덩어리, `+`가 AND) 하나이고, 이
모듈은 그 조각들을 **쌓인 글자와 대조하고 화면에 표시하는** 쪽만 맡는다. 쓰는 화면은
기관 기사 찾기(app.agency_archive_renderer)와 정기 보관함(app.history_renderer)이다 —
두 화면이 같은 함수를 봐야 「한 화면에선 걸리는데 다른 화면에선 안 걸리는」 일이 없다.
"""

import html

from app.search_terms import split_terms

# 스니펫 — 낱말 앞뒤로 이만큼만 잘라 한 줄로 보여준다. 넘치면 CSS가 한 줄에서 자른다.
SNIPPET_BEFORE = 28
SNIPPET_AFTER = 52


def matches(article: dict, word: str) -> bool:
    """`+`로 묶은 조각이 **전부** 든 기사만. 조각 하나하나는 글자 그대로 부분일치다.

    대조하는 글자는 제목 + 요약이다. 넘기는 기사 dict는 **담당자가 고친 값을 입힌 뒤**
    (app.summary_overrides.apply_summary_overrides)여야 한다 — 저장된 원본으로만 찾으면
    하필 그가 신경 써서 고친 기사가 자기가 고친 말로는 안 찾힌다.
    """
    terms = split_terms(word)
    if not terms:
        return True
    haystack = f"{article.get('title') or ''} {article.get('summary') or ''}".lower()
    return all(t.lower() in haystack for t in terms)


def mark(text: str, word: str) -> str:
    """찾은 낱말만 노랑. 노랑은 **찾은 낱말**을 뜻한다(형광펜은 안 칠한다).

    `+`로 여러 조각을 넣었으면 조각마다 칠한다. 한 자리에서 여러 조각이 걸리면 긴 쪽을
    칠한다 — 짧은 조각이 긴 조각을 가운데서 자르면 안 된다(`가상자산+가상자산 과세`).
    """
    escaped = html.escape(text)
    needles = [html.escape(t).lower() for t in split_terms(word)]
    if not needles:
        return escaped
    low = escaped.lower()
    out, i, n = [], 0, len(escaped)
    while i < n:
        hit = max((x for x in needles if low.startswith(x, i)), key=len, default="")
        if hit:
            out.append(f"<mark>{escaped[i:i + len(hit)]}</mark>")
            i += len(hit)
            continue
        # 걸리는 조각이 없는 자리에서 `&amp;` 같은 실체 참조를 가운데서 쪼개지 않는다.
        if escaped[i] == "&":
            end = escaped.find(";", i, i + 10)
            if end > 0:
                out.append(escaped[i:end + 1])
                i = end + 1
                continue
        out.append(escaped[i])
        i += 1
    return "".join(out)


def snippet(summary: str, title: str, word: str) -> str:
    """요약에서 **찾은 낱말 주변만** 한 줄로. 없으면 빈 문자열(그 줄을 안 그린다).

    접힌 행에 이 한 줄이 필요한 이유는 실측이다 — 낱말이 **제목엔 없고 요약에만** 있는
    비율이 낱말마다 15~100%였다(「예산」은 전부). 그냥 접으면 담당자가 「왜 이게
    나왔지」를 알 수 없고, 잘못 걸린 기사(「기획예산처」에 걸린 「예산」)도 펼쳐 보기
    전엔 못 거른다.

    **제목에 이미 있는 낱말은 스니펫을 안 그린다** — 제목이 이미 답이라 한 줄이 중복이다.
    `+`로 여러 조각을 넣었으면 **제목에 없는 조각**을 기준으로 자른다.
    """
    terms = split_terms(word)
    if not (summary and terms):
        return ""
    low_title, low_sum = (title or "").lower(), summary.lower()
    missing = [t for t in terms if t.lower() not in low_title]
    if not missing:
        return ""  # 조각이 전부 제목에 있다 — 제목이 곧 답이다
    at = min((low_sum.find(t.lower()) for t in missing if t.lower() in low_sum), default=-1)
    if at < 0:
        return ""
    lo = max(0, at - SNIPPET_BEFORE)
    hi = min(len(summary), at + SNIPPET_AFTER)
    cut = ("…" if lo > 0 else "") + summary[lo:hi] + ("…" if hi < len(summary) else "")
    return f'<div class="snip">{mark(cut, word)}</div>'
