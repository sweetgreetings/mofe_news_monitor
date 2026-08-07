# Design Ref: DESIGN.md §1 "지난 기사 더보기" — 최근 7일 회차를 날짜별 -> 시간대별 토글로 조회
import html
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import (
    COLOR_ACCENT,
    COLOR_BG,
    COLOR_BORDER,
    COLOR_CARD,
    COLOR_ERROR,
    COLOR_HEADER,
    COLOR_HOVER,
    COLOR_TEXT,
    COLOR_TEXT_MUTED,
    CUTE_FONT_BASE64,
    CUTE_FONT_NAME,
    DEFAULT_ARTICLE_LINE_TEMPLATE,
    DEFAULT_HIGHLIGHT_KEYWORDS,
    FONT_STACK,
    HISTORY_HTML_PATH,
    SETTINGS_SERVER_HOST,
    SETTINGS_SERVER_PORT,
)
from app.atomic_write import atomic_write_text
from app.curation import display_group_name, filter_hidden, load_group_labels
from app.summary_overrides import apply_summary_overrides
from app.renderer import render_article
from app.settings import load_settings
from app.storage import list_all_runs

# [추가: 2026-07-26] "뉴스가 잠잠" 빈 상태 문구용 손글씨체 — app.renderer와 동일 규칙(오프라인
# 대비 base64 내장). 파일별로 독립적인 이 앱의 렌더러 구조를 따라 여기서도 따로 정의한다.
_CUTE_FONT_FACE_CSS = (
    f"@font-face {{ font-family: '{CUTE_FONT_NAME}'; "
    f"src: url(data:font/woff2;base64,{CUTE_FONT_BASE64}) format('woff2'); font-display: swap; }}"
)

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>지난 기사 더보기</title>
<style>
  {cute_font_face}
  body {{ margin: 0; background: {bg}; color: {text}; font-family: {font_stack}; }}
  .container {{
    max-width: 800px; margin: 24px auto; padding: 24px; background: {card};
    border: 1px solid {border}; border-radius: 8px;
  }}
  h1 {{ font-size: 1.3rem; color: {header}; }}
  .empty {{ color: {muted}; margin-top: 12px; }}
  .slot-empty {{ text-align: center; color: {muted}; padding: 8px 0; }}
  .cute-caption {{ font-family: '{cute_font_name}', sans-serif; font-size: 1.1rem; margin-top: 4px; }}
  details.date {{ margin: 10px 0; border-bottom: 1px solid {border}; padding-bottom: 8px; }}
  details.date > summary {{ cursor: pointer; font-size: 1.1rem; color: {header}; }}
  details.slot {{ margin: 8px 0 8px 20px; }}
  details.slot > summary {{ cursor: pointer; }}
  /* [추가: 2026-07-30] 저장 시점의 소제목 구성(group 필드)이 있는 회차는 이 제목으로
     묶어서 보여준다 — 재분류가 아니라 저장해둔 결과를 그대로 복원하는 것뿐이다. */
  .history-group {{ margin-top: 14px; }}
  .history-group h3 {{
    font-size: 1rem; color: {header}; border-bottom: 1px solid {border};
    padding-bottom: 4px; margin: 0 0 6px 20px;
  }}
  .article {{ margin: 10px 0 10px 20px; line-height: 1.5; padding: 4px 6px; border-radius: 8px; }}
  /* [추가: 2026-08-05] app.renderer와 동일 — 마우스 오버 시 연한 회색 표시. */
  .article:hover {{ background: #F3F4F6; }}
  .article summary {{ cursor: pointer; -webkit-tap-highlight-color: transparent; }}
  .article summary::marker {{ color: {muted}; }}
  /* [추가: 2026-08-07] app.renderer와 동일 — 모바일 사파리 탭 하이라이트 잔상 방지. */
  .title-line {{ color: {text}; -webkit-tap-highlight-color: transparent; }}
  .article-summary {{ margin: 6px 0 4px 20px; color: {text}; font-size: 0.95rem; }}
  .article-footer {{ display: flex; align-items: center; gap: 8px; }}
  .article .url {{ flex: 1; color: {muted}; font-size: 0.9rem; word-break: break-all; text-decoration: underline; }}
  .hide-btn {{
    flex-shrink: 0; background: transparent; border: none; color: {muted}; font-size: 1rem;
    cursor: pointer; padding: 2px 6px; user-select: none; -webkit-user-select: none;
  }}
  .hide-btn:hover {{ color: {error}; }}
  /* [추가: 2026-08-05] app.renderer와 동일 — 원문 다시 가져오기 버튼, 평소 숨김. */
  .refetch-btn {{
    flex-shrink: 0; background: transparent; border: none; color: {muted}; font-size: 1rem;
    cursor: pointer; padding: 2px 6px; user-select: none; -webkit-user-select: none;
    opacity: 0; transition: opacity 0.15s;
  }}
  .article:hover .refetch-btn {{ opacity: 1; }}
  .refetch-btn:disabled {{ opacity: 0.35 !important; cursor: not-allowed; }}
  /* [추가: 2026-08-05] app.renderer와 동일 — ✏️ 직접 수정 인라인 편집 칸. */
  .edit-summary-form {{
    margin: 8px 0 4px 20px; padding: 10px 12px; border: 1px solid {accent}; border-radius: 8px;
    display: flex; flex-direction: column; gap: 8px;
  }}
  .edit-summary-form input, .edit-summary-form textarea {{
    width: 100%; box-sizing: border-box; padding: 6px 10px; border: 1px solid {border}; border-radius: 6px;
    font-size: 0.88rem; color: {text}; background: {card}; font-family: inherit;
  }}
  .edit-summary-form .edit-summary-actions {{ display: flex; justify-content: flex-end; gap: 8px; }}
  .edit-summary-form button {{ font-size: 0.82rem; padding: 5px 12px; }}
  /* [추가: 2026-07-26] 다른 화면들과 같은 상단 고정 바 — HOME만(이 화면엔 다른
     이동할 곳이 마땅치 않아 index.html/live.html처럼 한 항목만 둔다). */
  .container {{ padding-top: 60px; }}
  .topbar {{
    position: fixed; top: 0; left: 0; right: 0; z-index: 20;
    background: {card}; border-bottom: 1px solid {border}; box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
  }}
  .topbar-inner {{
    max-width: 800px; margin: 0 auto; padding: 12px 24px;
    display: flex; justify-content: space-between; align-items: center;
  }}
  .topbar a {{ color: {accent}; text-decoration: none; font-size: 0.92rem; font-weight: 600; padding: 6px 10px; border-radius: 6px; }}
  .topbar a:hover {{ background: {hover}; }}
</style>
</head>
<body>
<div class="topbar"><div class="topbar-inner">
  <a href="home.html">홈</a>
</div></div>
<div class="container">
  <h1>📅 지난 기사 더보기 (최근 7일)</h1>
  {body}
</div>
<script>
function hideArticle(btn) {{
  var url = btn.dataset.url;
  var article = btn.closest(".article");
  fetch("http://{settings_host}:{settings_port}/hide-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{url: url}})
  }}).then(function(res) {{
    if (res.ok) {{ article.remove(); }}
    else {{ alert("숨기기에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("숨기기에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
// [추가: 2026-08-05] app.renderer와 동일 — 원문에서 다시 가져오기.
function refetchSummary(btn) {{
  var url = btn.dataset.url;
  btn.disabled = true;
  fetch("http://{settings_host}:{settings_port}/refetch-summary", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{url: url}})
  }}).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else if (res.status === 404) {{ alert("원문에서 더 나은 제목·요약을 찾지 못했어요."); btn.disabled = false; }}
    else {{ alert("다시 가져오기에 실패했습니다. 다시 시도해주세요."); btn.disabled = false; }}
  }}).catch(function() {{
    alert("다시 가져오기에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
    btn.disabled = false;
  }});
}}
// [추가: 2026-08-05] app.renderer와 동일 — ✏️ 직접 수정.
function editSummary(btn) {{
  var article = btn.closest(".article");
  if (article.querySelector(".edit-summary-form")) {{ return; }}
  var url = btn.dataset.url;
  var form = document.createElement("div");
  form.className = "edit-summary-form";
  var titleInput = document.createElement("input");
  titleInput.type = "text";
  titleInput.value = btn.dataset.title;
  var summaryInput = document.createElement("textarea");
  summaryInput.rows = 2;
  summaryInput.value = btn.dataset.summary;
  var actions = document.createElement("div");
  actions.className = "edit-summary-actions";
  var cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "clear-btn";
  cancelBtn.textContent = "취소";
  cancelBtn.onclick = function(e) {{ e.stopPropagation(); form.remove(); }};
  var saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.textContent = "저장";
  saveBtn.onclick = function(e) {{
    e.stopPropagation();
    fetch("http://{settings_host}:{settings_port}/edit-summary", {{
      method: "POST", keepalive: true,
      headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
      body: new URLSearchParams({{url: url, title: titleInput.value, summary: summaryInput.value}})
    }}).then(function(res) {{
      if (res.ok) {{ location.reload(); }}
      else {{ alert("저장하지 못했습니다. 다시 시도해주세요."); }}
    }}).catch(function() {{
      alert("저장하지 못했습니다 — 앱이 실행 중인지 확인해주세요.");
    }});
  }};
  actions.appendChild(cancelBtn);
  actions.appendChild(saveBtn);
  form.appendChild(titleInput);
  form.appendChild(summaryInput);
  form.appendChild(actions);
  var details = article.querySelector("details");
  details.open = true;
  article.querySelector(".article-summary").insertAdjacentElement("afterend", form);
}}
</script>
</body>
</html>
"""


def _render_slot(run: dict, index: int, highlight_words: list, line_template: str, labels: dict) -> str:
    """회차 하나(시간대)를 토글로 렌더링한다.

    [수정: 2026-07-30] 예전엔 소제목 재분류를 안 하고 언론사 우선순위 순 평평한
    목록만 보여줬는데("당시 화면을 그대로 복원할 필요는 없어 단순화"), 저장 시점에
    함께 저장해둔 소제목 구성("group" 필드, app.classifier.snapshot_group_names)이
    있으면 그걸 그대로 써서 소제목별로 묶어 보여준다 — 재분류가 아니라 이미 저장된
    결과를 복원하는 것뿐이라 큐레이션 버튼(✏️/↑/↓)은 여전히 없다. 이 필드가 생기기
    전에 저장된 옛 회차(레거시 데이터)는 group 필드가 없으므로, 그때는 예전 방식대로
    평평한 목록으로 대체 표시한다(하위 호환).
    """
    articles = apply_summary_overrides(filter_hidden(run["articles"]))
    if not articles:
        body = '<div class="slot-empty">💤<div class="cute-caption">뉴스가 잠잠</div></div>'
    elif all("group" in a for a in articles):
        grouped: "OrderedDict" = OrderedDict()
        for a in articles:
            grouped.setdefault(a["group"], []).append(a)
        sections = []
        for name, group_articles in grouped.items():
            display_name = html.escape(display_group_name(name, labels))
            articles_html = "\n".join(render_article(a, highlight_words, line_template) for a in group_articles)
            sections.append(f'<div class="history-group"><h3>&lt;{display_name}&gt;</h3>{articles_html}</div>')
        body = "\n".join(sections)
    else:
        body = "\n".join(render_article(a, highlight_words, line_template) for a in articles)
    label = html.escape(f"{index}. {run['run_slot']}")
    return f'<details class="slot"><summary>{label}</summary>{body}</details>'


def _date_label(run_date, today) -> str:
    return f"당일 ({run_date.strftime('%m-%d')})" if run_date == today else run_date.strftime("%m-%d")


def _theme() -> dict:
    """이 페이지 템플릿이 공유하는 색상·폰트·포트 값. `.format(**_theme(), ...)`로 채운다."""
    return {
        "font_stack": FONT_STACK,
        "cute_font_face": _CUTE_FONT_FACE_CSS,
        "cute_font_name": CUTE_FONT_NAME,
        "settings_host": SETTINGS_SERVER_HOST,
        "settings_port": SETTINGS_SERVER_PORT,
        "bg": COLOR_BG,
        "card": COLOR_CARD,
        "header": COLOR_HEADER,
        "accent": COLOR_ACCENT,
        "text": COLOR_TEXT,
        "muted": COLOR_TEXT_MUTED,
        "border": COLOR_BORDER,
        "hover": COLOR_HOVER,
        "error": COLOR_ERROR,
    }


def render_history_page(
    runs: list,
    highlight_words: Optional[list] = None,
    line_template: Optional[str] = None,
) -> str:
    """저장된 회차를 날짜별(내림차순) -> 시간대별(오름차순) 토글로 렌더링한다 (PRD.md 기능2 규칙 6).

    highlight_words: 형광펜 단어(규칙6) — 검색 키워드와 무관한 별도 설정.
    line_template: 기사 첫 줄 형식(규칙5) — 메인 화면과 동일한 설정을 공유한다.
    """
    highlight_words = highlight_words if highlight_words is not None else DEFAULT_HIGHLIGHT_KEYWORDS
    line_template = line_template if line_template is not None else DEFAULT_ARTICLE_LINE_TEMPLATE

    if not runs:
        return _PAGE_TEMPLATE.format(
            body='<p class="empty">아직 저장된 지난 기사가 없습니다.</p>',
            **_theme(),
        )

    labels = load_group_labels()
    today = datetime.now().date()
    grouped: "OrderedDict" = OrderedDict()
    for run in runs:
        run_date = datetime.fromisoformat(run["run_at"]).date()
        grouped.setdefault(run_date, []).append(run)

    sections = []
    for run_date in sorted(grouped, reverse=True):
        day_runs = sorted(grouped[run_date], key=lambda r: r["run_slot"])
        slots_html = "\n".join(
            _render_slot(r, i, highlight_words, line_template, labels) for i, r in enumerate(day_runs, start=1)
        )
        date_label = html.escape(_date_label(run_date, today))
        sections.append(f'<details class="date"><summary>{date_label}</summary>{slots_html}</details>')

    return _PAGE_TEMPLATE.format(body="\n".join(sections), **_theme())


def generate_history_page(highlight_words: Optional[list] = None, line_template: Optional[str] = None) -> Path:
    """저장된 모든 회차(최근 7일, 보관 정책과 연동)를 history.html로 렌더링한다."""
    if highlight_words is None or line_template is None:
        settings = load_settings()
        highlight_words = highlight_words if highlight_words is not None else settings["highlight_keywords"]
        line_template = line_template if line_template is not None else settings["article_line_template"]
    html_text = render_history_page(list_all_runs(), highlight_words, line_template)
    atomic_write_text(HISTORY_HTML_PATH, html_text)
    return HISTORY_HTML_PATH
