# Design Ref: DESIGN.md §1 화면 구성 — 최신 회차를 밝은 카드형 정적 HTML 화면으로 렌더링
import html
import json
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from app.classifier import classify_articles
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
    DEFAULT_KEYWORDS,
    FONT_STACK,
    HIGHLIGHT_COLORS,
    OUTPUT_HTML_PATH,
    SETTINGS_SERVER_HOST,
    SETTINGS_SERVER_PORT,
)
from app.atomic_write import atomic_write_text
from app.curation import display_group_name, filter_hidden, load_group_labels, load_group_overrides
from app.summary_overrides import apply_summary_overrides
from app.manual_keyword_note import load_manual_keyword_note
from app.custom_groups import load_custom_groups
from app.group_order import apply_group_order
from app.highlight import highlight_keywords
from app.manual_articles import load_manual_articles
from app.naver_api import outlet_display_label
from app.settings import all_search_keywords, load_settings
from app.storage import is_today, load_latest_run
from app.summarizer import extract_keywords, summarize_groups

# [추가: 2026-07-26] "뉴스가 잠잠" 빈 상태 문구용 손글씨체 — app.config.CUTE_FONT_BASE64
# 참고(오프라인에서도 깨지지 않도록 base64로 파일에 직접 박아 넣은 서브셋 폰트).
_CUTE_FONT_FACE_CSS = (
    f"@font-face {{ font-family: '{CUTE_FONT_NAME}'; "
    f"src: url(data:font/woff2;base64,{CUTE_FONT_BASE64}) format('woff2'); font-display: swap; }}"
)

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>언론 모니터링</title>
<style>
  {cute_font_face}
  body {{ margin: 0; background: {bg}; color: {text}; font-family: {font_stack}; }}
  .container {{
    max-width: 800px; margin: 24px auto; padding: 24px; background: {card};
    border: 1px solid {border}; border-radius: 8px;
  }}
  /* [수정: 2026-07-27] 상단 고정 바 — HOME + 다른 두 화면(초안/실시간)으로 바로 이동.
     🗑️(숨긴 기사 관리)는 하단 고정 바로 옮겼다 — 상단이 3개 내비게이션으로 꽉 차서
     자리를 따로 뺐다. 이 화면은 file://로 직접 열릴 수도 있어 절대경로(설정 서버
     주소)를 쓰는 링크가 섞여 있다 — home.html은 같은 폴더의 정적 파일이라 상대경로로도
     항상 동작한다. */
  .container {{ padding-top: 60px; padding-bottom: 56px; }}
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
  .bottombar {{
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 20;
    background: {card}; border-top: 1px solid {border}; box-shadow: 0 -2px 8px rgba(0, 0, 0, 0.08);
  }}
  .bottombar-inner {{
    max-width: 800px; margin: 0 auto; padding: 10px 24px;
    display: flex; justify-content: space-between; align-items: center; gap: 10px;
  }}
  .bottombar a {{ color: {accent}; text-decoration: none; font-size: 1.1rem; padding: 6px 10px; border-radius: 6px; }}
  .bottombar a:hover {{ background: {hover}; }}
  /* [추가: 2026-08-03] 하단바 🖍️ — 형광펜 단어를 설정 화면까지 안 가고 이 화면에서
     바로 추가·삭제할 수 있는 팝오버. 쓰레기통(숨긴 기사 관리) 바로 왼쪽에 둔다. */
  .highlight-wrap {{ position: relative; }}
  .highlight-toggle {{
    background: transparent; border: none; font-size: 1.1rem; cursor: pointer;
    padding: 6px 10px; border-radius: 6px;
  }}
  .highlight-toggle:hover {{ background: {hover}; }}
  .highlight-popover {{
    display: none; position: absolute; bottom: 100%; right: 0; margin-bottom: 8px;
    background: {card}; border: 1px solid {border}; border-radius: 8px;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.12); padding: 12px; width: 220px; z-index: 30;
  }}
  .highlight-popover.is-open {{ display: block; }}
  .highlight-chips {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }}
  .highlight-chip {{
    display: inline-flex; align-items: center; gap: 4px; padding: 3px 8px; border-radius: 14px;
    font-size: 0.82rem; color: {text}; cursor: pointer;
  }}
  .highlight-chip button {{
    background: transparent; border: none; padding: 0; font-size: 0.8rem; cursor: pointer;
    color: inherit; line-height: 1;
  }}
  .highlight-empty {{ color: {muted}; font-size: 0.8rem; margin: 0 0 8px; }}
  .highlight-add-form {{ display: flex; gap: 6px; }}
  .highlight-add-form input {{ flex: 1; font-size: 0.85rem; padding: 5px 8px; min-width: 0; }}
  .highlight-add-form button {{ font-size: 0.82rem; padding: 5px 10px; white-space: nowrap; }}
  /* [추가: 2026-08-05] 소제목 미니 목차 — 소제목 하나 안에 기사가 많아지면 원하는
     소제목을 찾으려고 계속 스크롤해야 하는 문제가 있어, 평소엔 숨겨진 버튼(펼침형,
     사용자가 고정형 대신 선택)을 눌러야만 목차가 나타나게 했다. 화면을 항상 가리지
     않으면서, 필요할 때 소제목 이름을 눌러 바로 그 위치로 스크롤 이동한다. */
  .toc-toggle-btn {{
    position: fixed; right: 20px; bottom: 20px; width: 46px; height: 46px; border-radius: 50%;
    background: {accent}; color: #ffffff; border: none; font-size: 1.2rem; cursor: pointer;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.18); z-index: 200;
    display: flex; align-items: center; justify-content: center;
  }}
  .toc-toggle-btn:hover {{ background: {header}; }}
  .toc-popover {{
    display: none; position: fixed; right: 20px; bottom: 74px; width: 200px;
    max-height: 320px; overflow-y: auto; background: {card}; border: 1px solid {border};
    border-radius: 8px; padding: 8px; box-shadow: 0 4px 16px rgba(0, 0, 0, 0.15); z-index: 200;
  }}
  .toc-popover.is-open {{ display: block; }}
  /* [추가: 2026-08-05] 목차 안에서 소제목 순서까지 바로 바꿀 수 있게 항목마다 작은
     ▲▼를 붙였다 — 이름 클릭(이동)과 화살표 클릭(순서 변경)을 분리해서, 클릭 하나에
     동작 하나만 대응하도록 했다(선택 후 상단 고정 화살표 방식은 이름 클릭의 기존
     "이동" 의미와 충돌해서 채택하지 않음). */
  .toc-row {{ display: flex; align-items: center; justify-content: space-between; border-radius: 4px; }}
  .toc-row a {{
    flex: 1; min-width: 0; padding: 6px 8px; font-size: 0.85rem; color: {text};
    text-decoration: none; border-radius: 4px; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis;
  }}
  .toc-row a:hover {{ background: {hover}; }}
  .toc-row-btns {{ display: flex; gap: 1px; flex-shrink: 0; padding-right: 4px; }}
  .toc-order-btn {{
    background: transparent; border: none; color: {muted}; font-size: 0.72rem; cursor: pointer;
    padding: 2px 4px; border-radius: 4px;
  }}
  .toc-order-btn:hover {{ background: {hover}; }}
  .toc-order-btn:disabled {{ opacity: 0.3; cursor: default; }}
  /* [추가: 2026-08-05] 방금 순서를 옮긴 항목을 기사의 "방금 승격" 표시와 같은 노란색으로
     한 번 표시한다 — 팝오버가 새로고침 후 자동으로 다시 열리는 동안, 여러 개를 연달아
     옮길 때 방금 뭘 옮겼는지 헷갈리지 않게. */
  .toc-row.toc-row-moved {{ background: #E7EFE8; }}
  .toc-empty {{ color: {muted}; font-size: 0.82rem; padding: 6px 8px; }}
  /* [추가: 2026-07-30] app.preview_renderer와 동일한 체크박스 일괄이동 바 —
     평소엔 숨어있다가 체크박스를 선택하면 왼쪽에 나타나고, 🗑️는 그대로 오른쪽에 남는다. */
  .bulk-move-bar {{ display: none; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 0.85rem; }}
  .bulk-move-bar.is-active {{ display: flex; }}
  .bulk-move-bar .bulk-move-count {{ font-weight: 600; color: {header}; white-space: nowrap; }}
  #bulk-move-select {{
    border: 1px solid {accent}; border-radius: 4px; padding: 5px 8px; font-size: 0.85rem;
    color: {text}; background: {card};
  }}
  .bulk-move-bar button {{ padding: 5px 12px; font-size: 0.85rem; }}
  /* [추가: 2026-08-04] 일괄 위/아래 이동 버튼 — 선택한 기사가 서로 다른 소제목에
     걸쳐 있거나 소제목의 맨 위/아래에 닿으면 비활성화된다(updateBulkMoveBar). */
  #bulk-move-up, #bulk-move-down {{ padding: 5px 10px; }}
  #bulk-move-up:disabled, #bulk-move-down:disabled {{ opacity: 0.35; cursor: not-allowed; }}
  .bulk-move-bar .clear-btn {{ background: transparent; color: {muted}; }}
  .bulk-move-bar .clear-btn:hover {{ background: {hover}; }}
  .article-select {{ margin-top: 3px; flex-shrink: 0; }}
  .group-move-select {{
    flex-shrink: 0; width: 100px; border: 1px solid {border}; border-radius: 4px;
    padding: 2px 4px; font-size: 0.78rem; color: {muted}; background: {card};
  }}
  /* [추가: 2026-08-04] 소제목별/시간순/언론사순 보기 전환 — 실제 소제목 구성은 그대로
     두고 화면에 나열하는 순서만 바꾸는 용도라, 다른 액션 버튼과 톤을 맞추되 select임을
     알 수 있게 테두리를 살짝 강조한다. */
  .view-mode-select {{
    border: 1px solid {accent}; border-radius: 6px; padding: 6px 10px; font-size: 0.85rem;
    color: {text}; background: {card};
  }}
  /* [수정: 2026-08-04] 기사가 없을 때만 걸리던 점선 테두리를, 사용자가 직접 만든
     소제목이면 기사가 있어도 계속 유지되도록 바꿨다(.subheading-empty -> .subheading-custom)
     — 자동 분류 소제목과 구분되도록, 아이콘 대신 색을 진한 하늘색으로 키워 눈에 더
     잘 띄게 했다(사용자 피드백: 아이콘은 소제목이 길면 놓치기 쉽지만 테두리는 카드
     전체를 감싸 계속 보인다). */
  /* [수정: 2026-08-05] 빈 여백이 너무 넓어 보인다는 피드백 — 패딩을 줄였다. 사실 가장
     큰 원인은 아래 .subheading h2 쪽 브라우저 기본 margin이었다(같이 수정). */
  .subheading-custom {{ border: 2px dashed #7DD3FC; border-radius: 8px; padding: 8px 16px; }}
  .empty-group-hint {{ color: {muted}; font-size: 0.85rem; margin: 6px 0 0; }}
  header h1 {{ font-size: 1.4rem; margin-bottom: 12px; color: {header}; }}
  .actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  /* [수정: 2026-08-03] button 태그는 브라우저 기본 스타일상 body의 font-family를 물려받지
     않아(a 태그와 달리) 지금까지 Arial 등 시스템 기본체로 렌더링되고 있었고, appearance:
     auto(네이티브 OS 버튼 껍데기)까지 남아있어 폰트를 맞춰도 렌더링이 미묘하게 달랐다 —
     font-family: inherit·appearance: none으로 고쳤는데도 "txt로 저장"(a 태그)만 여전히
     글자 위치가 달라 보인다는 세 번째 재지적으로, 크롬이 button 태그 텍스트는 내부적으로
     수직 중앙 정렬해주지만 a 태그는 그런 처리가 없다는 걸 마지막으로 발견 — align-items:
     center를 직접 지정해 브라우저 기본 동작에 기대지 않고 두 태그를 완전히 동일하게 그린다. */
  button, a.btn {{
    background: {accent}; color: #ffffff; border: none; border-radius: 4px;
    padding: 6px 14px; font-size: 0.9rem; font-family: inherit; cursor: pointer; text-decoration: none;
    appearance: none; -webkit-appearance: none;
    display: inline-flex; align-items: center; justify-content: center;
    user-select: none; -webkit-user-select: none;
  }}
  button:hover, a.btn:hover {{ background: {header}; }}
  /* [수정: 2026-08-03] 화면 상단 액션 툴바(복사·txt로 저장·Telegram 전송·지난 기사)만
     "정직한 파란색" 원색 채움 대신 톤온톤 소프트 필로 — 디자인 시안 B안(사용자 선택).
     .actions button 쪽이 위 전역 button 규칙보다 더 구체적이라(class+element > element)
     이 툴바 안에서만 덮어쓰고, 🗑️/↑/↓ 등 다른 버튼은 그대로 원색 유지. */
  .actions button, .actions a.btn {{
    background: {hover}; color: {accent}; font-weight: 600; border-radius: 8px;
  }}
  .actions button:hover, .actions a.btn:hover {{ background: {tonal_hover}; }}
  /* [수정: 2026-08-03] "+ 새 소제목 만들기"만 나머지 툴바 버튼과 기능이 달라(내용을
     내보내는 게 아니라 새 그릇을 만드는 것) 시안 A(고스트 아웃라인)로 유일하게 다르게
     둔다 — .actions .create-group-btn이 위 .actions button보다 구체적이라(class 2개)
     덮어쓴다. */
  .actions .create-group-btn {{
    background: transparent; color: {accent}; border: 1px solid {ghost_border};
    border-radius: 6px; font-weight: 400;
  }}
  .actions .create-group-btn:hover {{ background: {hover}; border-color: {ghost_border_hover}; }}
  .subheading {{ margin-top: 28px; }}
  /* [수정: 2026-08-05] h2 브라우저 기본 margin(위아래 약 15px)이 빈 소제목 카드를 실제
     내용보다 훨씬 커 보이게 만드는 주범이었다 — margin을 직접 지정해 제거. 겸사겸사
     ✏️/🗑️/↑↓를 제목 바로 옆이 아니라 줄 오른쪽 끝에 붙이고 싶다는 요청도 이 flex로
     함께 해결(왼쪽 = 제목, 오른쪽 = 아이콘 묶음). 기사 한 줄(.article summary)은 반대로
     "짧은 제목 뒤 버튼이 화면 끝까지 멀어지는" 문제 때문에 flex-start를 의도적으로 쓰고
     있어 그대로 둔다 — 소제목 헤더와 기사 한 줄은 서로 다른 문제라 같은 답을 쓰지 않는다. */
  .subheading h2 {{
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    font-size: 1.1rem; color: {header}; border-bottom: 1px solid {border};
    margin: 0 0 6px; padding-bottom: 6px;
  }}
  .subheading-title {{ display: flex; align-items: center; gap: 4px; min-width: 0; }}
  .subheading-icons {{ display: flex; align-items: center; gap: 2px; flex-shrink: 0; }}
  .rename-btn {{
    background: transparent; border: none; color: {muted}; font-size: 0.9rem; cursor: pointer;
    vertical-align: middle; user-select: none; -webkit-user-select: none;
  }}
  /* [추가: 2026-08-03] 소제목 순서 조정 버튼 — 기사용 .move-btn과 같은 톤이되 소제목
     헤더 안에 있어 별도 클래스로 둔다. */
  /* [수정: 2026-08-05] 기사 ↑/↓(.move-btn)과 가로폭이 안 맞아서 나란히 보면 좁아 보였다
     — 패딩·글자 크기를 .move-btn과 통일했다. */
  .order-btn {{
    background: transparent; border: none; color: {muted}; font-size: 1rem; cursor: pointer;
    vertical-align: middle; padding: 2px 6px; user-select: none; -webkit-user-select: none;
  }}
  .order-btn:disabled {{ opacity: 0.35; cursor: default; }}
  .article {{ margin: 10px 0; line-height: 1.5; padding: 6px 8px; border-radius: 8px; }}
  /* [추가: 2026-08-05] 마우스를 올린 기사 줄을 연한 회색으로 표시해 긴 목록에서도 지금
     어느 줄을 보고 있는지 놓치지 않게 한다. 체크 표시({hover}, 파란색)·방금 승격
     표시(노란색)와 겹치지 않는 중립적인 회색을 골랐다 — 이 규칙을 :has(체크됨) 규칙보다
     먼저 둬서, 체크된 채로 마우스를 올려도 "체크됨" 파란색이 우선 보이게 한다(체크는
     능동적으로 선택한 상태라 스쳐 지나가는 마우스 오버보다 우선순위가 높아야 한다). */
  .article:hover {{ background: #F3F4F6; }}
  /* [추가: 2026-08-03] 일괄 이동용 체크박스를 켠 기사는 연한 배경으로 표시해 "지금
     뭘 골랐는지" 스크롤하면서도 바로 보이게 한다 (디자인 시안 A, 사용자 선택) —
     자바스크립트 변경 없이 :has()만으로 동작. */
  .article:has(.article-select:checked) {{ background: {hover}; }}
  /* [추가: 2026-08-05] "📌 직접 추가한 기사"에서 승격시킨 기사가 자동분류 목록 어디에
     들어갔는지 한눈에 안 보여서, 새로고침 직후 딱 한 번 옅은 노란색으로 표시한다(체크박스
     선택 배경색{hover}과는 다른 색이어야 두 상태가 헷갈리지 않는다 — 승격은 "방금 도착",
     체크는 "지금 선택 중"으로 의미가 다르다). */
  .article.just-promoted {{ background: #FFF9C4; }}
  /* [수정: 2026-08-05] 노란색은 이미 "직접 추가한 기사"를 승격했을 때, 그리고 초안
     화면의 "새로 도착한 기사"(.is-new-arrival) 표시에도 쓰이고 있어 — 순서를 옮긴
     기사까지 같은 노란색을 쓰면 세 가지 서로 다른 의미가 한 색으로 겹쳐 헷갈린다는
     피드백. 순서 이동 전용으로 연두색을 새로 뺐다(기존 팔레트의 형광펜용 라임색
     #C6FF00과도 톤이 달라 헷갈리지 않는다). */
  .article.just-moved {{ background: #E7EFE8; }}
  /* [수정: 2026-07-30] 🗑️를 제목 바로 옆에 붙인다 — space-between으로 두면 창이 넓을 때
     제목이 짧으면 버튼이 화면 오른쪽 끝까지 멀리 떨어져 보여서(제목-버튼 간 시각적 연결이
     끊김) 잘못된 행을 누를 위험이 있었다. flex-start로 바꿔 제목 길이와 무관하게 버튼이
     항상 제목 바로 뒤에 붙게 한다. */
  .article summary {{
    cursor: pointer; display: flex; align-items: baseline; justify-content: flex-start; gap: 8px;
    -webkit-tap-highlight-color: transparent;
  }}
  .article summary::marker {{ color: {muted}; }}
  /* [추가: 2026-08-07] 모바일 사파리에서 제목(summary)을 눌러 펼치면 그 뒤로 제목 글자가
     보라색으로 바뀌어 보인다는 제보 — 이 앱 CSS가 지정한 색이 아니라, 사파리가 탭할 때
     보여주는 하이라이트 효과(-webkit-tap-highlight-color)가 제대로 안 지워지고 남는
     것으로 보인다(iOS 사파리에서 종종 나타나는 현상). 색을 투명 처리해 아예 안 나타나게
     막는다. */
  .title-line {{ min-width: 0; overflow-wrap: anywhere; color: {text}; -webkit-tap-highlight-color: transparent; }}
  /* [수정: 2026-08-05] 제목 길이에 따라 "다른 소제목"/🔄/✏️/🗑️/↑↓가 매번 다른 자리에
     떠서 줄마다 위치가 들쭉날쭉하다는 피드백 — 이 묶음만 margin-left: auto로 항상 줄
     오른쪽 끝에 붙게 했다. 체크박스·제목·게시 시각은 그대로 flex-start로 왼쪽에 붙어
     있어(위 주석의 우려는 여전히 유효), 둘 다 원하는 형태를 함께 만족한다 — 왼쪽 묶음은
     제목 옆에, 오른쪽 액션 묶음은 항상 같은 자리(표 형태처럼 일정한 클릭 위치)에. */
  .article-actions {{ margin-left: auto; display: flex; align-items: center; gap: 4px; flex-shrink: 0; }}
  .article-summary {{ margin: 6px 0 4px 20px; color: {text}; font-size: 0.95rem; }}
  .article-footer {{ display: flex; align-items: center; gap: 8px; }}
  .article .url {{ flex: 1; color: {muted}; font-size: 0.9rem; word-break: break-all; text-decoration: underline; }}
  /* [수정: 2026-07-30] "이미 확인함" 표시를 브라우저에 영구 기억(localStorage)하는 대신,
     "지금 펼쳐서 보고 있는 기사"에만 실시간으로 적용한다 — 예전 방식은 클릭했던 기사가
     전부 보라색으로 남아 화면이 정신없어진다는 피드백에 따른 변경. details가 열려있는
     동안에만 :has()로 색을 입히므로(자바스크립트 불필요), 닫거나 다른 기사를 열면 이
     기사는 자동으로 원래 색으로 돌아간다. */
  .article:has(details[open]) .title-line, .article:has(details[open]) .url {{ color: {seen_color}; }}
  /* [수정: 2026-08-03] 게시 시각은 복사/내보내기 텍스트(_build_plain_text)에는 원래도
     포함되지 않지만, 화면에서 마우스로 직접 드래그해 복사할 땐 화면에 보이는 대로
     같이 딸려왔다 — user-select: none으로 이 부분만 드래그 선택 자체가 안 되게 한다
     (화면 표시는 그대로 유지, 클립보드에만 안 실림). */
  .pub-time {{
    flex-shrink: 0; color: {muted}; font-size: 0.8rem; white-space: nowrap;
    user-select: none; -webkit-user-select: none;
  }}
  .move-btn, .hide-btn {{
    flex-shrink: 0; background: transparent; border: none; color: {muted}; font-size: 1rem;
    cursor: pointer; padding: 2px 6px; user-select: none; -webkit-user-select: none;
  }}
  .move-btn:disabled {{ color: {border}; cursor: not-allowed; }}
  .hide-btn:hover {{ color: {error}; }}
  /* [추가: 2026-08-05] 원문 다시 가져오기 버튼 — 평소엔 숨겨두고 그 기사 카드에
     마우스를 올렸을 때만 나타난다(제목 옆이 붐비지 않게). */
  .refetch-btn {{
    flex-shrink: 0; background: transparent; border: none; color: {muted}; font-size: 1rem;
    cursor: pointer; padding: 2px 6px; user-select: none; -webkit-user-select: none;
    opacity: 0; transition: opacity 0.15s;
  }}
  .article:hover .refetch-btn {{ opacity: 1; }}
  .refetch-btn:disabled {{ opacity: 0.35 !important; cursor: not-allowed; }}
  /* [추가: 2026-08-05] ✏️ 직접 수정 — 🔄가 원문에서도 못 찾는 경우의 최후 수단으로
     펼쳐지는 인라인 편집 칸. */
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
  .empty {{ text-align: center; margin-top: 80px; font-size: 1.3rem; color: {muted}; }}
  .cute-caption {{ font-family: '{cute_font_name}', sans-serif; font-size: 1.4rem; margin-top: 6px; }}
  .bottom {{ margin-top: 40px; border-top: 1px solid {border}; padding-top: 16px; }}
  .bottom h3 {{ font-size: 1rem; color: {header}; }}
  .manual-divider {{
    display: flex; align-items: center; gap: 10px; margin: 32px 0 4px;
    color: {muted}; font-size: 0.75rem;
  }}
  .manual-divider::before, .manual-divider::after {{ content: ""; flex: 1; border-top: 1px dashed {border}; }}
  .manual-zone {{ border: 1px dashed {border}; border-radius: 8px; padding: 14px 18px; margin-top: 10px; }}
  .manual-zone-head {{ display: flex; align-items: baseline; justify-content: space-between; gap: 10px; flex-wrap: wrap; }}
  .manual-zone-title {{ font-weight: 600; color: {text}; font-size: 0.95rem; }}
  .manual-zone-hint {{ font-size: 0.75rem; color: {muted}; }}
  .manual-zone .article:first-child {{ margin-top: 10px; }}
  /* [추가: 2026-08-05] "+ 직접 키워드 작성하기" — AI 키워드 블록과 별개로, 이용자가
     자유 서식으로 적어두는 메모 한 줄. 저장된 메모가 있으면 처음부터 열려서 보이고,
     없으면 버튼을 눌러야 나타난다(is-open 토글). */
  /* [수정: 2026-08-07] 스크롤해서 기사를 읽다가 메모를 적으려면 위로 되돌아가야 했던
     문제 — sticky로 고정해 상단 바(topbar, 높이만큼 top: 60px) 바로 아래 붙어서 화면에
     계속 보이게 한다. background를 명시해야 아래로 스크롤된 기사 카드들이 비쳐 보이지
     않는다. */
  .keyword-note-zone {{
    display: none; align-items: center; gap: 8px; border: 1px dashed {border}; border-radius: 8px;
    padding: 10px 14px; margin: 10px 0 4px; flex-wrap: wrap;
    position: sticky; top: 60px; z-index: 15; background: {card};
  }}
  .keyword-note-zone.is-open {{ display: flex; }}
  .keyword-note-zone .keyword-note-label {{ font-weight: 600; color: {text}; font-size: 0.85rem; white-space: nowrap; }}
  .keyword-note-zone input {{
    flex: 1; min-width: 220px; padding: 6px 10px; border: 1px solid {border}; border-radius: 6px;
    font-size: 0.88rem; color: {text}; background: {card};
  }}
  .keyword-note-zone button {{ font-size: 0.82rem; padding: 5px 10px; white-space: nowrap; }}
</style>
</head>
<body>
<div class="topbar"><div class="topbar-inner">
  <a href="home.html">홈</a>
  <a href="{preview_href}">📝 스크랩 초안</a>
  <a href="{live_href}">🔴 실시간 현황</a>
</div></div>
<div class="container">
  <header>
    <h1>언론 모니터링 {run_slot} 기준</h1>
    <div class="actions">
      <button onclick="copyPlainText()">복사</button>
      <a class="btn" href="data:text/plain;charset=utf-8,{export_href}" download="{export_filename}">다운로드</a>
      <button onclick="sendToTelegram(this)">Telegram</button>
      <button onclick="sendToEmail(this)">Email</button>
      <button onclick="exportReport(this)" title="media_report 앱으로 이 회차를 내보냅니다">📤 보고서로 내보내기</button>
      <a class="btn" href="history.html">지난 기사</a>
      <select class="view-mode-select" onchange="applyViewMode(this.value)" title="소제목 구성은 그대로 두고 화면에 나열하는 순서만 바꿉니다">
        <option value="subheading" selected>소제목별</option>
        <option value="group-time">소제목 내 시간순</option>
        <option value="time">시간순</option>
        <option value="outlet">언론사순</option>
      </select>
      <button class="create-group-btn" type="button" onclick="createCustomGroup()">+ 새 소제목 만들기</button>
      <button class="create-group-btn" type="button" onclick="toggleKeywordNote()">+ 직접 키워드 작성하기</button>
    </div>
  </header>
  <div class="keyword-note-zone{note_open_class}" id="keyword-note-zone" data-run-slot="{run_slot_raw}">
    <span class="keyword-note-label">키워드 작성 :</span>
    <input type="text" id="keyword-note-input" value="{note_value_attr}" placeholder="키워드 a, 키워드 b, 키워드 c...">
    <button type="button" onclick="saveKeywordNote()">저장</button>
    <button class="clear-btn" type="button" onclick="clearKeywordNote()">삭제</button>
  </div>
  {body}
</div>
<button type="button" class="toc-toggle-btn" onclick="toggleTocPopover()" title="소제목 목차">☰</button>
<div class="toc-popover" id="toc-popover"></div>
<div class="bottombar"><div class="bottombar-inner">
  <div class="bulk-move-bar" id="bulk-move-bar">
    <span class="bulk-move-count" id="bulk-move-count"></span>
    <select id="bulk-move-select"></select>
    <button type="button" onclick="bulkMoveSelected()">옮기기</button>
    <button type="button" id="bulk-move-up" onclick="bulkMoveOrder('up')" title="선택한 기사들을 통째로 위로 이동(같은 소제목 안에서만)">↑</button>
    <button type="button" id="bulk-move-down" onclick="bulkMoveOrder('down')" title="선택한 기사들을 통째로 아래로 이동(같은 소제목 안에서만)">↓</button>
    <button type="button" onclick="bulkHideSelected()" title="선택한 기사 전부 숨기기 (되돌리기 가능)">🗑️</button>
    <button class="clear-btn" type="button" onclick="clearSelection()">선택 해제</button>
  </div>
  <div class="highlight-wrap">
    <button type="button" class="highlight-toggle" onclick="toggleHighlightPopover()" title="형광펜 단어 편집">🖍️</button>
    <div class="highlight-popover" id="highlight-popover">
      <div class="highlight-chips" id="highlight-chips"></div>
      <form class="highlight-add-form" onsubmit="return addHighlightWord(event);">
        <input type="text" id="highlight-new-word" placeholder="단어 추가" maxlength="20">
        <button type="submit">추가</button>
      </form>
    </div>
  </div>
  <a href="{hidden_href}" title="숨긴 기사 관리">🗑️</a>
</div></div>
<script>
const PLAIN_TEXT = {plain_text_json};
function copyPlainText() {{
  navigator.clipboard.writeText(PLAIN_TEXT)
    .then(() => alert("클립보드에 복사했습니다."))
    .catch(() => alert("복사에 실패했습니다."));
}}
function sendToTelegram(btn) {{
  btn.disabled = true;
  fetch("http://{settings_host}:{settings_port}/telegram-send-scrap", {{
    method: "POST",
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{text: PLAIN_TEXT}})
  }}).then(function(res) {{
    if (res.ok) {{ alert("텔레그램으로 보냈습니다."); }}
    else {{ alert("전송에 실패했습니다 — 설정 화면에서 텔레그램 연결 상태를 확인해주세요."); }}
  }}).catch(function() {{
    alert("전송에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }}).finally(function() {{
    btn.disabled = false;
  }});
}}
// [추가: 2026-08-07] media_report(별도 프로젝트) export 계약 — 서버가 최신 회차를
// 다시 계산해 data/export/에 JSON으로 저장한다. PLAIN_TEXT를 보내는 Telegram/Email과
// 달리 본문을 클라이언트가 만들지 않는다(app.export.export_latest_run 참고).
function exportReport(btn) {{
  btn.disabled = true;
  fetch("http://{settings_host}:{settings_port}/export-report", {{ method: "POST" }})
    .then(function(res) {{
      if (res.ok) {{ alert("보고서 앱으로 내보냈습니다."); }}
      else {{ alert("내보내기에 실패했습니다."); }}
    }}).catch(function() {{
      alert("내보내기에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
    }}).finally(function() {{
      btn.disabled = false;
    }});
}}
// [추가: 2026-08-06] app.renderer의 sendToTelegram과 같은 이유·동작 — 이메일판.
function sendToEmail(btn) {{
  btn.disabled = true;
  fetch("http://{settings_host}:{settings_port}/email-send-scrap", {{
    method: "POST",
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{text: PLAIN_TEXT}})
  }}).then(function(res) {{
    if (res.ok) {{ alert("이메일로 보냈습니다."); }}
    else {{ alert("전송에 실패했습니다 — 설정 화면에서 이메일 연결 상태·받는 사람을 확인해주세요."); }}
  }}).catch(function() {{
    alert("전송에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }}).finally(function() {{
    btn.disabled = false;
  }});
}}
// [추가: 2026-08-03] 하단바 🖍️ 팝오버 — 형광펜 단어를 이 화면에서 바로 추가·삭제한다.
// 추가/삭제 둘 다 app.settings.toggle_highlight_keyword(있으면 빼고 없으면 추가)
// 하나를 그대로 재사용한다 — 검색 키워드 설정 화면의 🖍️ 버튼과 완전히 같은 동작.
const HIGHLIGHT_WORDS = {highlight_words_json};
function renderHighlightChips() {{
  var wrap = document.getElementById("highlight-chips");
  if (!HIGHLIGHT_WORDS.length) {{
    wrap.innerHTML = '<p class="highlight-empty">등록된 형광펜 단어가 없어요.</p>';
    return;
  }}
  wrap.innerHTML = HIGHLIGHT_WORDS.map(function(item) {{
    var word = item.word.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    var attr = item.word.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
    // [수정: 2026-08-05] 칩 자체를 클릭하면 색이 팔레트 다음 순번으로 순환된다(본문
    // 안 단어 클릭으로 순환하던 방식은 삭제 요청에 따라 없앴다 — 이제 색 순환은 이
    // 팝오버 칩에서만 가능하다). ×(삭제) 버튼은 클릭이 칩까지 안 번지게 stopPropagation.
    return '<span class="highlight-chip" data-word="' + attr + '" style="background:' + item.color_hex +
      '" onclick="cycleChipColor(this)" title="클릭하면 색이 바뀝니다">' + word +
      '<button type="button" data-word="' + attr + '" onclick="event.stopPropagation(); removeHighlightWord(this)" title="제거">×</button></span>';
  }}).join("");
}}
function toggleHighlightPopover() {{
  var pop = document.getElementById("highlight-popover");
  var opening = !pop.classList.contains("is-open");
  pop.classList.toggle("is-open");
  if (opening) {{ renderHighlightChips(); }}
}}
// [추가: 2026-08-05] 팝오버 바깥을 클릭하면 닫힌다(표준적인 팝오버 동작) — 🖍️를 다시
// 눌러 닫는 방법과 별개로 하나 더 생기는 것뿐이라 서로 안 부딪힌다. 🖍️ 버튼 자체도
// .highlight-wrap 안에 있어서, 여는 클릭이 그대로 "바깥 클릭"으로 오인돼 열리자마자
// 닫히는 일은 없다.
document.addEventListener("click", function(e) {{
  var pop = document.getElementById("highlight-popover");
  if (pop.classList.contains("is-open") && !e.target.closest(".highlight-wrap")) {{
    pop.classList.remove("is-open");
  }}
}});
// [추가: 2026-08-05] 소제목 미니 목차(펼침형) — 소제목 안에 기사가 많아지면 원하는
// 소제목을 찾으려고 계속 스크롤해야 하는 문제를 풀기 위해, 평소엔 숨겨진 버튼을
// 눌러야만 소제목 이름 목록이 나타나게 했다(고정형 대신 펼침형을 사용자가 선택 —
// 화면을 항상 가리지 않으면서, 필요할 때만 누르면 된다). 서버에 새 데이터를 요청하지
// 않고 화면에 이미 그려진 .subheading[data-toc-name]을 그대로 읽어서 만든다.
function buildTocPopover() {{
  var pop = document.getElementById("toc-popover");
  var sections = document.querySelectorAll(".subheading[data-toc-name]");
  pop.innerHTML = "";
  if (!sections.length) {{
    pop.innerHTML = '<p class="toc-empty">소제목이 없어요.</p>';
    return;
  }}
  // [추가: 2026-08-05] 방금 이 화살표로 옮긴 항목이 있으면 한 번 노란색으로 표시한다
  // (moveTocOrder가 새로고침 직전 남겨둔 값 — 한 번 쓰고 지운다).
  var justMoved = sessionStorage.getItem("tocJustMovedName");
  if (justMoved) {{ sessionStorage.removeItem("tocJustMovedName"); }}
  sections.forEach(function(sec, idx) {{
    var renameBtn = sec.querySelector(".rename-btn[data-current]");
    var rawName = renameBtn ? renameBtn.dataset.name : "";
    var row = document.createElement("div");
    row.className = "toc-row" + (rawName && rawName === justMoved ? " toc-row-moved" : "");
    var a = document.createElement("a");
    a.href = "#";
    a.textContent = sec.dataset.tocName;
    // [추가: 2026-08-05] 사용자가 직접 만든 소제목(.subheading-custom, 화면에서 하늘색
    // 점선 테두리로 표시되는 것과 같은 기준)은 목차에서도 볼드로 표시해 자동 분류
    // 소제목과 구분되게 한다.
    if (sec.classList.contains("subheading-custom")) {{ a.style.fontWeight = "700"; }}
    a.onclick = function(e) {{ e.preventDefault(); scrollToSubheading(sec.id); }};
    var btns = document.createElement("span");
    btns.className = "toc-row-btns";
    var up = document.createElement("button");
    up.type = "button"; up.className = "toc-order-btn"; up.textContent = "▲"; up.title = "위로";
    up.disabled = idx === 0;
    up.onclick = function() {{ moveTocOrder(rawName, "up"); }};
    var down = document.createElement("button");
    down.type = "button"; down.className = "toc-order-btn"; down.textContent = "▼"; down.title = "아래로";
    down.disabled = idx === sections.length - 1;
    down.onclick = function() {{ moveTocOrder(rawName, "down"); }};
    btns.appendChild(up);
    btns.appendChild(down);
    row.appendChild(a);
    row.appendChild(btns);
    pop.appendChild(row);
  }});
}}
function toggleTocPopover() {{
  var pop = document.getElementById("toc-popover");
  var opening = !pop.classList.contains("is-open");
  pop.classList.toggle("is-open");
  if (opening) {{ buildTocPopover(); }}
}}
function scrollToSubheading(id) {{
  var el = document.getElementById(id);
  if (el) {{ el.scrollIntoView({{behavior: "smooth", block: "start"}}); }}
  document.getElementById("toc-popover").classList.remove("is-open");
}}
// [추가: 2026-08-05] 목차 팝오버 안 항목별 ▲▼ — app.renderer moveGroupOrder와 같은
// /save-group-order를 그대로 쓰지만, 헤더 버튼과 달리 새로고침 후에도 팝오버가 계속
// 열린 채로 이어서 조정할 수 있어야 해서(여러 개를 연달아 옮기는 경우가 많다)
// sessionStorage에 "다시 열기" 신호와 "방금 옮긴 항목" 이름을 남겨둔다.
function moveTocOrder(rawName, direction) {{
  var names = Array.prototype.map.call(document.querySelectorAll(".subheading h2 .rename-btn[data-current]"), function(b) {{ return b.dataset.name; }});
  var i = names.indexOf(rawName);
  var j = direction === "up" ? i - 1 : i + 1;
  if (i < 0 || j < 0 || j >= names.length) {{ return; }}
  var tmp = names[i]; names[i] = names[j]; names[j] = tmp;
  fetch("http://{settings_host}:{settings_port}/save-group-order", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{order: JSON.stringify(names)}})
  }}).then(function(res) {{
    if (res.ok) {{
      sessionStorage.setItem("tocJustMovedName", rawName);
      sessionStorage.setItem("tocPopoverReopen", "1");
      location.reload();
    }} else {{ alert("순서 변경에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("순서 변경에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
document.addEventListener("click", function(e) {{
  var pop = document.getElementById("toc-popover");
  if (pop.classList.contains("is-open") && !e.target.closest(".toc-popover") && !e.target.closest(".toc-toggle-btn")) {{
    pop.classList.remove("is-open");
  }}
}});
function _postToggleHighlight(word) {{
  return fetch("http://{settings_host}:{settings_port}/keywords/toggle-highlight", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{word: word}})
  }});
}}
function removeHighlightWord(btn) {{
  _postToggleHighlight(btn.dataset.word).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else {{ alert("삭제에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("삭제에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function addHighlightWord(evt) {{
  evt.preventDefault();
  var input = document.getElementById("highlight-new-word");
  var word = input.value.trim();
  if (!word) {{ return false; }}
  _postToggleHighlight(word).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else {{ alert("추가에 실패했습니다 — 형광펜 단어는 최대 개수까지 등록돼 있을 수 있어요."); }}
  }}).catch(function() {{
    alert("추가에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
  return false;
}}
// [추가: 2026-08-04] 형광펜 단어를 본문에서 직접 클릭하면 팔레트 다음 색으로 바뀐다 —
// 같은 단어는 색이 전역으로 묶여 있으므로, 새로고침 없이 data-word가 같은 모든
// <span>(제목·요약, 다른 소제목의 다른 기사까지)을 한 번에 갱신한다.
// [수정: 2026-08-05] 형광펜 팝오버의 칩을 클릭하면 그 단어 색이 팔레트 다음 순번으로
// 바뀐다 — 본문 안 하이라이트된 단어를 직접 클릭해 바꾸던 방식은 삭제 요청에 따라
// 없앴고(app.highlight.highlight_keywords에서 onclick 제거), 색 순환은 이제 이
// 팝오버 칩에서만 가능하다. 같은 단어가 본문 여러 곳(제목·요약, 다른 기사)에 나와도
// data-word로 한 번에 찾아 배경색을 맞춘다 — 클릭 자체는 칩에서만 가능해도, 색은
// 여전히 단어 하나에 전역으로 묶여 있으므로 본문 표시도 즉시 따라와야 한다.
function cycleChipColor(chip) {{
  var word = chip.dataset.word;
  fetch("http://{settings_host}:{settings_port}/keywords/cycle-highlight-color", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{word: word}})
  }}).then(function(res) {{
    if (!res.ok) {{ alert("색 변경에 실패했습니다. 다시 시도해주세요."); return null; }}
    return res.json();
  }}).then(function(data) {{
    if (!data) return;
    chip.style.background = data.color_hex;
    document.querySelectorAll('.hl-word[data-word="' + CSS.escape(word) + '"]').forEach(function(span) {{
      span.style.backgroundColor = data.color_hex;
    }});
  }}).catch(function() {{
    alert("색 변경에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function hideArticle(btn) {{
  var url = btn.dataset.url;
  var article = btn.closest(".article");
  fetch("http://{settings_host}:{settings_port}/hide-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{
      url: url, outlet: btn.dataset.outlet, title: btn.dataset.title, pubDate: btn.dataset.pubDate
    }})
  }}).then(function(res) {{
    if (res.ok) {{ article.remove(); }}
    else {{ alert("숨기기에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("숨기기에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
// [추가: 2026-08-05] "🔄 원문에서 다시 가져오기" — 이 기사 하나만 원문 페이지의
// og:title/og:description으로 제목·요약을 다시 가져와 저장한다(app.summary_overrides).
// 자동이 아니라 눌렀을 때만 호출되므로 전체 스크랩 속도에는 영향이 없다.
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
// [추가: 2026-08-05] ✏️ 직접 수정 — 🔄가 원문에서도 못 찾는 경우의 최후 수단. 클릭한
// 기사 카드 안에 제목·요약 입력 칸을 바로 펼친다(펼쳐진 요약 위치, details를 강제로 열어
// 보여준다). 저장하면 🔄와 같은 저장소(app.summary_overrides)에 그대로 담긴다.
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
// [추가: 2026-08-04] 소제목별/시간순/언론사순 보기 전환 — 실제 소제목 구성(분류·순서)은
// 서버에 저장된 그대로 두고, 화면에서 기사를 훑어보는 순서만 바꾼다. 시의성 있는 기사를
// 솎아낼 때 소제목을 넘나들며 찾는 대신 한 줄로 쭉 보고 🗑️로 바로 걸러낼 수 있게 하는
// 용도라, 카드(.article) DOM 노드를 실제로 옮겼다가 되돌린다 — 복제하면 🗑️ 등 기존
// 버튼이 참조하는 노드와 화면에 보이는 노드가 달라져 상태가 어긋나기 때문. 처음 전환할
// 때 각 기사의 원래 위치(부모 소제목·다음 형제)를 한 번만 기억해뒀다가, "소제목별"로
// 돌아갈 때 그대로 복원한다.
var _FLAT_VIEW_HOMES = null;
function _restoreSubheadingView() {{
  if (_FLAT_VIEW_HOMES) {{
    // 뒤에서부터 복원해야 한다 — 앞 기사의 home.next가 아직 flat-view 안에 있는 뒤 기사를
    // 가리킬 수 있는데, insertBefore는 참조 노드가 "지금" 같은 부모의 자식이어야 하기
    // 때문이다(순서대로 복원하면 아직 안 옮겨진 next를 참조하다 에러 남). 뒤에서부터
    // 복원하면 각 기사의 next는 이미 제자리로 돌아와 있어 항상 유효하다.
    _FLAT_VIEW_HOMES.slice().reverse().forEach(function(home) {{
      if (!document.body.contains(home.el)) {{ return; }}
      if (home.next && home.next.parentNode === home.parent) {{
        home.parent.insertBefore(home.el, home.next);
      }} else {{
        home.parent.appendChild(home.el);
      }}
    }});
  }}
  document.querySelectorAll(".subheading").forEach(function(s) {{ s.style.display = ""; }});
  var flat = document.getElementById("flat-view");
  if (flat) {{ flat.remove(); }}
}}
function applyViewMode(mode) {{
  _restoreSubheadingView();
  if (mode === "subheading") {{ return; }}
  var firstSub = document.querySelector(".subheading");
  if (!firstSub) {{ return; }}
  if (!_FLAT_VIEW_HOMES) {{
    _FLAT_VIEW_HOMES = Array.prototype.map.call(document.querySelectorAll(".subheading .article"), function(el) {{
      return {{ el: el, parent: el.parentElement, next: el.nextElementSibling }};
    }});
  }}
  // [추가: 2026-08-04] "소제목 내 시간순" — 소제목 경계는 그대로 두고, 각 소제목 안
  // 카드만 시간순으로 다시 배열한다(시간순/언론사순처럼 통째로 한 줄로 펼치지 않음).
  if (mode === "group-time") {{
    document.querySelectorAll(".subheading").forEach(function(section) {{
      var inGroup = Array.prototype.filter.call(section.children, function(el) {{
        return el.classList.contains("article");
      }});
      inGroup.sort(function(a, b) {{
        var ad = (a.querySelector(".hide-btn").dataset.pubDate || "");
        var bd = (b.querySelector(".hide-btn").dataset.pubDate || "");
        return ad < bd ? 1 : ad > bd ? -1 : 0;
      }});
      inGroup.forEach(function(el) {{ section.appendChild(el); }});
    }});
    return;
  }}
  var articles = Array.prototype.filter.call(document.querySelectorAll(".subheading .article"), function(el) {{
    return document.body.contains(el);
  }});
  if (mode === "time") {{
    articles.sort(function(a, b) {{
      var ad = (a.querySelector(".hide-btn").dataset.pubDate || "");
      var bd = (b.querySelector(".hide-btn").dataset.pubDate || "");
      return ad < bd ? 1 : ad > bd ? -1 : 0;
    }});
  }} else if (mode === "outlet") {{
    articles.sort(function(a, b) {{
      return (Number(a.dataset.outletRank) || 0) - (Number(b.dataset.outletRank) || 0);
    }});
  }}
  document.querySelectorAll(".subheading").forEach(function(s) {{ s.style.display = "none"; }});
  var flat = document.createElement("section");
  flat.id = "flat-view";
  flat.className = "subheading";
  firstSub.parentNode.insertBefore(flat, firstSub);
  articles.forEach(function(el) {{ flat.appendChild(el); }});
}}
// [추가: 2026-07-30] 스크랩 초안에 먼저 만든 체크박스 다중 선택 + 하단 "장바구니" 바를
// 완성본에도 그대로 붙였다(app.preview_renderer와 동일 패턴, 이 화면 자체 컨벤션대로
// reload 방식 유지). 소제목 이름은 화면에 이미 그려진 ✏️ 버튼의 data-name/data-current를
// 읽어서 쓴다 — 이름표(rename)가 붙어 있어도 서버에는 항상 원본 이름으로 보내야 한다.
function getAllGroups() {{
  return Array.prototype.map.call(document.querySelectorAll(".subheading h2 .rename-btn[data-current]"), function(btn) {{
    // [추가: 2026-08-07] 일괄이동 바 드롭다운도 사용자가 만든 소제목을 볼드로
    // 표시하기 위해, 그 소제목 카드에 이미 붙어있는 subheading-custom 클래스를 같이 읽는다.
    var custom = btn.closest(".subheading").classList.contains("subheading-custom");
    return {{value: btn.dataset.name, label: btn.dataset.current, custom: custom}};
  }});
}}
// [추가: 2026-08-03] 소제목 자체의 화면 순서를 바꾼다 — 서버가 "지금 순서"를 다시 계산할
// 필요 없이, 화면에 이미 그려진 소제목 순서(getAllGroups와 같은 셀렉터)를 그대로 읽어
// 인접한 두 개만 맞바꾼 뒤 전체 순서를 통째로 저장한다.
function moveGroupOrder(btn, direction) {{
  var names = Array.prototype.map.call(document.querySelectorAll(".subheading h2 .rename-btn[data-current]"), function(b) {{ return b.dataset.name; }});
  var i = names.indexOf(btn.dataset.name);
  var j = direction === "up" ? i - 1 : i + 1;
  if (i < 0 || j < 0 || j >= names.length) {{ return; }}
  var tmp = names[i]; names[i] = names[j]; names[j] = tmp;
  fetch("http://{settings_host}:{settings_port}/save-group-order", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{order: JSON.stringify(names)}})
  }}).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else {{ alert("순서 변경에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("순서 변경에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
// [추가: 2026-08-04] 소제목 헤더 체크박스 — 그 소제목(.subheading) 안의 기사 체크박스를
// 전부 같은 상태로 맞춘다. 반대 방향(기사 하나씩 해제했을 때 헤더가 자동으로 풀리는 것)은
// 굳이 동기화하지 않는다 — "전체 선택" 버튼 역할이면 충분하고, 되돌릴 땐 다시 눌러 끄면 된다.
function toggleGroupSelectAll(checkbox) {{
  var section = checkbox.closest(".subheading");
  if (!section) {{ return; }}
  section.querySelectorAll(".article-select").forEach(function(cb) {{ cb.checked = checkbox.checked; }});
  updateBulkMoveBar();
}}
function updateBulkMoveBar() {{
  var checked = document.querySelectorAll(".article-select:checked");
  var bar = document.getElementById("bulk-move-bar");
  if (checked.length === 0) {{ bar.classList.remove("is-active"); return; }}
  bar.classList.add("is-active");
  document.getElementById("bulk-move-count").textContent = "☑️ " + checked.length + "개 선택됨";
  var select = document.getElementById("bulk-move-select");
  var options = '<option value="" selected disabled>이동할 소제목</option>';
  getAllGroups().forEach(function(g) {{
    var opt = document.createElement("option");
    opt.value = g.value;
    opt.textContent = g.label;
    if (g.custom) {{ opt.style.fontWeight = "700"; }}
    options += opt.outerHTML;
  }});
  select.innerHTML = options;
  // [추가: 2026-08-04] 일괄 위/아래 이동 버튼 활성화 여부 — 선택한 기사가 전부 같은
  // 소제목 안에 있고, 그 소제목의 맨 위/아래에 닿지 않았을 때만 누를 수 있다.
  var upBtn = document.getElementById("bulk-move-up");
  var downBtn = document.getElementById("bulk-move-down");
  var checkedArticles = Array.prototype.map.call(checked, function(cb) {{ return cb.closest(".article"); }});
  var sections = checkedArticles.map(function(el) {{ return el.closest(".subheading"); }});
  var sameSection = sections.every(function(s) {{ return s === sections[0]; }});
  if (sameSection) {{
    var siblings = Array.prototype.filter.call(sections[0].children, function(el) {{ return el.classList.contains("article"); }});
    var positions = checkedArticles.map(function(el) {{ return siblings.indexOf(el); }}).sort(function(a, b) {{ return a - b; }});
    upBtn.disabled = positions[0] === 0;
    downBtn.disabled = positions[positions.length - 1] === siblings.length - 1;
  }} else {{
    upBtn.disabled = true;
    downBtn.disabled = true;
  }}
}}
function bulkMoveOrder(direction) {{
  var urls = Array.prototype.map.call(document.querySelectorAll(".article-select:checked"), function(cb) {{ return cb.dataset.url; }});
  if (!urls.length) {{ return; }}
  var body = new URLSearchParams();
  urls.forEach(function(u) {{ body.append("urls", u); }});
  body.append("direction", direction);
  fetch("http://{settings_host}:{settings_port}/bulk-move-order", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: body
  }}).then(function(res) {{
    // [추가: 2026-08-05] 위/아래로 여러 번 눌러야 원하는 위치까지 옮겨지는 경우가
    // 많은데, 새로고침될 때마다 체크가 풀리면 매번 다시 체크해야 해서 헷갈린다는
    // 피드백 — 이동 직전 체크된 URL을 sessionStorage에 남겨두고, 새로고침 후
    // 아래 로직이 그 URL들을 다시 체크해준다.
    if (res.ok) {{ sessionStorage.setItem("bulkMoveCheckedUrls", JSON.stringify(urls)); location.reload(); }}
    else {{ alert("순서 변경에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("순서 변경에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
// [추가: 2026-08-05] 체크박스로 선택한 기사 여러 개를 한꺼번에 숨긴다 — hideGroup(소제목
// 통째로 숨기기)과 같은 방식(URL별로 /hide-article을 여러 번 호출)이라 서버 쪽 추가
// 구현 없이 그대로 재사용한다. hideGroup과 달리 선택한 기사가 여러 소제목에 걸쳐 있어도
// 상관없다 — 숨기기는 소제목 경계와 무관한 동작이라서.
function bulkHideSelected() {{
  var urls = Array.prototype.map.call(document.querySelectorAll(".article-select:checked"), function(cb) {{ return cb.dataset.url; }});
  if (!urls.length) {{ return; }}
  if (!confirm(urls.length + "개 기사를 숨길까요? (숨긴 기사 관리에서 하나씩 되돌릴 수 있어요)")) return;
  Promise.all(urls.map(function(url) {{
    return fetch("http://{settings_host}:{settings_port}/hide-article", {{
      method: "POST", keepalive: true,
      headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
      body: new URLSearchParams({{url: url}})
    }});
  }})).then(function(responses) {{
    if (responses.every(function(res) {{ return res.ok; }})) {{ location.reload(); }}
    else {{ alert("일부 기사를 숨기지 못했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("숨기기에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function clearSelection() {{
  document.querySelectorAll(".article-select:checked").forEach(function(cb) {{ cb.checked = false; }});
  updateBulkMoveBar();
}}
function _postGroupMove(urls, target) {{
  var body = new URLSearchParams();
  urls.forEach(function(u) {{ body.append("urls", u); }});
  body.append("target", target);
  return fetch("http://{settings_host}:{settings_port}/bulk-move-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: body
  }});
}}
function bulkMoveSelected() {{
  var urls = Array.prototype.map.call(document.querySelectorAll(".article-select:checked"), function(cb) {{ return cb.dataset.url; }});
  var target = document.getElementById("bulk-move-select").value;
  if (!urls.length || !target) {{ return; }}
  _postGroupMove(urls, target).then(function(res) {{
    if (res.ok) {{ sessionStorage.setItem("justMovedUrls", JSON.stringify(urls)); location.reload(); }}
    else {{ alert("이동에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("이동에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function moveArticleToGroup(select) {{
  var url = select.closest(".article").dataset.url;
  var target = select.value;
  if (!target) {{ return; }}
  _postGroupMove([url], target).then(function(res) {{
    if (res.ok) {{ sessionStorage.setItem("justMovedUrls", JSON.stringify([url])); location.reload(); }}
    else {{ alert("소제목 이동에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("소제목 이동에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function createCustomGroup() {{
  var name = prompt("새 소제목 이름을 입력하세요");
  if (name === null) return;
  name = name.trim();
  if (!name) {{ alert("소제목 이름은 비워둘 수 없습니다."); return; }}
  // [수정: 2026-08-04] 지금 이 화면에 떠 있는 소제목들과만 중복 검사하도록, 현재
  // 그려진 소제목 원래 이름 목록을 같이 보낸다(회차끼리는 같은 이름이어도 무방).
  var body = new URLSearchParams({{name: name}});
  getAllGroups().forEach(function(g) {{ body.append("active_names", g.value); }});
  fetch("http://{settings_host}:{settings_port}/add-custom-group", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: body
  }}).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else if (res.status === 409) {{ alert("중복된 소제목이 있습니다 — 다른 이름을 써주세요."); }}
    else {{ alert("소제목 만들기에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("소제목 만들기에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
// [추가: 2026-08-05] "+ 직접 키워드 작성하기" — AI 키워드 블록과 별개로 자유 서식 메모를
// 적어두는 칸. 열기/닫기는 화면에서만 토글하고(저장 안 함), 저장·삭제는 새로고침해서
// 복사/txt/텔레그램 텍스트에도 바로 반영되게 한다(그 텍스트들은 페이지 로드 시점에
// 이미 다 만들어져 있어서 새로고침 없이는 갱신할 방법이 없다).
function toggleKeywordNote() {{
  document.getElementById("keyword-note-zone").classList.toggle("is-open");
}}
function saveKeywordNote() {{
  var text = document.getElementById("keyword-note-input").value;
  var runSlot = document.getElementById("keyword-note-zone").dataset.runSlot;
  fetch("http://{settings_host}:{settings_port}/save-manual-keyword-note", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{text: text, run_slot: runSlot}})
  }}).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else {{ alert("저장하지 못했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("저장하지 못했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function clearKeywordNote() {{
  document.getElementById("keyword-note-input").value = "";
  saveKeywordNote();
}}
function removeCustomGroup(btn) {{
  var name = btn.dataset.name;
  if (!confirm('"' + name + '" 소제목을 삭제할까요?')) return;
  fetch("http://{settings_host}:{settings_port}/remove-custom-group", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{name: name}})
  }}).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else {{ alert("삭제에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("삭제에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function moveArticle(btn) {{
  var url = btn.dataset.url;
  var direction = btn.dataset.direction;
  fetch("http://{settings_host}:{settings_port}/move-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{url: url, direction: direction}})
  }}).then(function(res) {{
    if (res.ok) {{ sessionStorage.setItem("justMovedUrls", JSON.stringify([url])); location.reload(); }}
    else {{ alert("순서 변경에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("순서 변경에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function promoteManualArticle(btn) {{
  var url = btn.dataset.url;
  fetch("http://{settings_host}:{settings_port}/promote-manual-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{url: url}})
  }}).then(function(res) {{
    // [추가: 2026-08-05] 새로고침된 페이지에서 방금 승격된 기사를 찾아 표시할 수 있게,
    // sessionStorage에 남겨둔다(아래 justPromotedUrl 확인 로직 참고).
    if (res.ok) {{ sessionStorage.setItem("justPromotedUrl", url); location.reload(); }}
    else {{ alert("정식 스크랩으로 승격하지 못했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("정식 스크랩으로 승격하지 못했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function promoteManualArticleToDraft(btn) {{
  var url = btn.dataset.url;
  fetch("http://{settings_host}:{settings_port}/promote-manual-article-to-draft", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{url: url}})
  }}).then(function(res) {{
    if (res.ok) {{ sessionStorage.setItem("justPromotedUrl", url); location.reload(); }}
    else {{ alert("다음 회차 초안에 포함하지 못했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("다음 회차 초안에 포함하지 못했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function renameGroup(btn) {{
  var name = btn.dataset.name;
  var current = btn.dataset.current;
  var newLabel = prompt("소제목 이름을 입력하세요", current);
  if (newLabel === null) return;
  newLabel = newLabel.trim();
  if (!newLabel) {{ alert("소제목 이름은 비워둘 수 없습니다."); return; }}
  // [수정: 2026-08-04] createCustomGroup과 동일한 이유 — 지금 화면에 떠 있는 소제목만
  // 중복 검사 대상으로 보낸다.
  var body = new URLSearchParams({{name: name, label: newLabel}});
  getAllGroups().forEach(function(g) {{ body.append("active_names", g.value); }});
  fetch("http://{settings_host}:{settings_port}/rename-group", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: body
  }}).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else if (res.status === 409) {{ alert("중복된 소제목이 있습니다 — 다른 이름을 써주세요."); }}
    else {{ alert("이름 변경에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("이름 변경에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function hideGroup(btn) {{
  var section = btn.closest(".subheading");
  if (!confirm("이 소제목의 기사를 전부 숨길까요? (숨긴 기사 관리에서 하나씩 되돌릴 수 있어요)")) return;
  var urls = Array.prototype.map.call(section.querySelectorAll(".hide-btn[data-url]"), function (el) {{
    return el.dataset.url;
  }});
  Promise.all(urls.map(function (url) {{
    return fetch("http://{settings_host}:{settings_port}/hide-article", {{
      method: "POST", keepalive: true,
      headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
      body: new URLSearchParams({{url: url}})
    }});
  }})).then(function (responses) {{
    if (responses.every(function (res) {{ return res.ok; }})) {{ location.reload(); }}
    else {{ alert("일부 기사를 숨기지 못했습니다. 다시 시도해주세요."); }}
  }}).catch(function () {{
    alert("숨기기에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function renderRelativeTimes() {{
  var now = Date.now();
  document.querySelectorAll(".pub-time-relative[data-pub-date]").forEach(function (el) {{
    var pub = new Date(el.dataset.pubDate).getTime();
    if (isNaN(pub)) return;
    var minutes = Math.floor((now - pub) / 60000);
    var text;
    if (minutes < 1) text = "방금 전";
    else if (minutes < 60) text = minutes + "분 전";
    else if (minutes < 1440) text = Math.floor(minutes / 60) + "시간 전";
    else text = Math.floor(minutes / 1440) + "일 전";
    el.textContent = text;
  }});
}}
renderRelativeTimes();
// [추가: 2026-08-05] promoteManualArticle(To Draft)이 남겨둔 "방금 승격한 URL"이 있으면
// 그 기사 카드를 한 번 표시하고 지운다(한 번 보고 나면 사라져야, 나중에 다른 이유로
// 새로고침했을 때 엉뚱하게 계속 표시되지 않는다).
(function() {{
  var justPromotedUrl = sessionStorage.getItem("justPromotedUrl");
  if (!justPromotedUrl) {{ return; }}
  sessionStorage.removeItem("justPromotedUrl");
  var el = document.querySelector('.article[data-url="' + CSS.escape(justPromotedUrl) + '"]');
  if (el) {{ el.classList.add("just-promoted"); }}
}})();
// [추가: 2026-08-05] bulkMoveOrder가 남겨둔 "이동 직전 체크했던 URL들"을 다시 체크해
// 준다 — 일괄 위/아래 이동은 목표 위치까지 여러 번 눌러야 하는 경우가 많은데,
// 새로고침 때마다 체크가 풀리면 그때마다 다시 선택해야 해서 불편했다.
(function() {{
  var raw = sessionStorage.getItem("bulkMoveCheckedUrls");
  if (!raw) {{ return; }}
  sessionStorage.removeItem("bulkMoveCheckedUrls");
  var urls = JSON.parse(raw);
  urls.forEach(function(u) {{
    var cb = document.querySelector('.article-select[data-url="' + CSS.escape(u) + '"]');
    if (cb) {{ cb.checked = true; }}
    var el = document.querySelector('.article[data-url="' + CSS.escape(u) + '"]');
    if (el) {{ el.classList.add("just-moved"); }}
  }});
  updateBulkMoveBar();
}})();
// [추가: 2026-08-05] moveArticle/moveArticleToGroup/bulkMoveSelected가 남겨둔 "방금
// 옮긴 기사들"을 한 번 표시한다 — 소제목 미니 목차에서 방금 옮긴 소제목을 표시하는 것과
// 같은 이유로, 기사도 옮기고 나면 어떤 게 방금 움직였는지 한눈에 보여야 헷갈리지 않는다.
(function() {{
  var raw = sessionStorage.getItem("justMovedUrls");
  if (!raw) {{ return; }}
  sessionStorage.removeItem("justMovedUrls");
  JSON.parse(raw).forEach(function(u) {{
    var el = document.querySelector('.article[data-url="' + CSS.escape(u) + '"]');
    if (el) {{ el.classList.add("just-moved"); }}
  }});
}})();
// [추가: 2026-08-05] moveTocOrder가 남겨둔 신호가 있으면 목차 팝오버를 다시 열어둔다 —
// 소제목 순서를 여러 개 연달아 조정할 때마다 ☰ 버튼을 매번 다시 누르지 않아도 되게.
(function() {{
  if (!sessionStorage.getItem("tocPopoverReopen")) {{ return; }}
  sessionStorage.removeItem("tocPopoverReopen");
  document.getElementById("toc-popover").classList.add("is-open");
  buildTocPopover();
}})();
</script>
</body>
</html>
"""


def apply_line_template(template: str, outlet: str, title: str) -> str:
    """{outlet}·{title} 자리표시자를 치환한다 (PRD.md 기능1 규칙 5).

    str.format이 아니라 단순 문자열 치환(.replace)을 쓴다 — outlet/title 값이나
    template 자체에 우연히 중괄호가 들어 있어도 format 필드로 잘못 해석되거나
    깨지지 않는다(DESIGN.md §3).
    """
    return template.replace("{outlet}", outlet).replace("{title}", title)


def format_slot_time_kr(hhmm: str) -> str:
    """"HH:MM"을 "9시"/"9시 30분"/"12시"처럼 자연스러운 한국어 시각 표현으로 바꾼다.

    [추가: 2026-07-29] 헤더 "언론 모니터링 [HH:MM] 기준"의 워딩 변경 요청 — 시는 앞자리
    0을 없애고(09 -> 9), 분이 0이면 "0분"을 아예 생략한다("09:00" -> "9시", "09:30" ->
    "9시 30분", "12:00" -> "12시"). 파일명(export_filename)처럼 정렬·고유성이 필요한
    곳에는 이 표현을 쓰지 않고 원본 "HH:MM"을 그대로 쓴다.
    """
    hour_str, _, minute_str = hhmm.partition(":")
    hour = int(hour_str)
    minute = int(minute_str) if minute_str else 0
    return f"{hour}시" if minute == 0 else f"{hour}시 {minute}분"


def _format_pub_time(article: dict) -> str:
    """게시 시각을 "HH:MM · N분 전" 형태로 화면에만 보여준다(순서 조정할 때 어느
    기사가 더 최근인지 참고용, 복사/내보내기 텍스트에는 안 들어간다 — _build_plain_text가
    이 값을 아예 안 쓰므로 자동으로 제외된다).

    [수정: 2026-07-27] "게시" 라벨은 없앴다 — 이 화면엔 다른 종류의 시각이 섞여 있지
    않아 라벨 없이도 무슨 시각인지 헷갈리지 않고, 기사마다 반복되니 자리만 차지한다는
    피드백(숨긴 기사 관리 화면에서 이미 같은 이유로 라벨 없이 시각만 보여주기로 한
    선례와도 일치한다).
    - "HH:MM"은 고정값이라 자동 새로고침으로 정적 파일을 다시 읽기만 해도 안 틀어진다.
    - "N분 전"은 페이지가 열릴 때마다(60초 자동 새로고침 포함) 브라우저에서 자바스크립트로
      다시 계산한다(renderRelativeTimes, live.html과 같은 방식) — 서버가 값을 굳혀두면
      시간이 지날수록 틀린 값이 되므로, data-pub-date만 실어두고 텍스트는 JS가 채운다.
    - pub_date가 없는 기사(이 필드 도입 전에 저장된 지난 회차 등)는 조용히 표시를 생략한다.
    """
    pub_date = article.get("pub_date")
    if not pub_date:
        return ""
    try:
        parsed = datetime.fromisoformat(pub_date)
    except ValueError:
        return ""
    escaped_pub_date = html.escape(pub_date)
    return (
        f'<span class="pub-time">{parsed.strftime("%H:%M")} · '
        f'<span class="pub-time-relative" data-pub-date="{escaped_pub_date}"></span></span>'
    )


def render_article(
    article: dict,
    highlight_words: list,
    line_template: str = DEFAULT_ARTICLE_LINE_TEMPLATE,
    move: Optional[dict] = None,
    move_handler: str = "moveArticle",
    up_handler: Optional[str] = None,
    up_title: str = "위로 이동",
    extra_buttons_html: str = "",
    checkbox: bool = False,
    group_select_html: str = "",
    outlet_rank: int = 0,
) -> str:
    """기사 한 건을 렌더링한다.

    제목 줄은 <details>/<summary>로 감싸 클릭하면 저장된 요약이 펼쳐지고(PRD 규칙13,
    자바스크립트 없이 HTML 기본 기능만 사용), URL은 새 탭으로 여는 링크다(PRD 규칙14).
    [수정: 2026-07-25] 제목·펼쳐지는 요약 둘 다에 형광펜 단어 하이라이트를 적용한다
    (PRD 규칙6) — 목록을 훑을 때 제목만 보고도 바로 눈에 띄는 게 낫다는 요청. 같은
    단어는 제목/요약 어디서든 항상 같은 색이다(app.highlight.highlight_keywords).
    highlight_words는 검색 키워드와 무관한, 설정 화면에서 별도로 지정하는 값이다.
    line_template은 첫 줄("ㅇ (언론사) 제목")의 형식을 정하며, 설정 화면에서 바꿀 수 있다(규칙5).

    move: {"disable_up": bool, "disable_down": bool} — 소제목 안에서 위/아래로 옮기는
    ↑/↓ 버튼을 보여준다(규칙21). None이면 버튼 자체를 안 보여준다 (지난 기사 화면·
    "직접 추가한 기사" 구획처럼 "그룹 내 순서"라는 개념이 없는 곳에서는 의미가 없다).
    move_handler/up_handler: ↑/↓ 버튼이 호출할 JS 함수 이름(up_handler를 생략하면
    move_handler와 같다).
    extra_buttons_html: [추가: 2026-07-28] move 버튼 뒤에 끼워 넣을 추가 버튼 HTML —
    "직접 추가한 기사" 구획(_render_manual_section)이 기사마다 "📝 초안에 포함"·
    "📗 완성본에 포함" 버튼을 붙이는 데 쓴다.

    checkbox: [추가: 2026-07-30] 기사 앞에 다중 선택용 체크박스를 보여준다 — 스크랩
    초안(preview.html)의 "여러 개 선택해서 한 번에 다른 소제목으로 옮기기" 하단 바가
    쓴다. 기본은 꺼짐(다른 화면엔 필요 없음).
    group_select_html: [추가: 2026-07-30] 🗑️ 앞에 끼워 넣을 "다른 소제목으로" 드롭다운
    HTML — 소제목 하나만 있거나 이동할 곳이 없으면 호출하는 쪽이 빈 문자열을 넘겨
    아예 안 보이게 한다.
    outlet_rank: [추가: 2026-08-04] "언론사순" 보기 전환(applyViewMode)이 정렬 기준으로
    쓰는 값 — 호출하는 쪽이 이미 언론사 우선순위로 정렬된 회차 전체 기사 목록에서의
    인덱스를 넘긴다. 기본값 0은 이 기능이 필요 없는 화면(지난 기사·직접 추가한 기사 등)의
    호출부에 영향 없다.

    [수정: 2026-07-28] 🗑️ 숨기기 버튼을 하단 footer에서 제목 줄(summary) 옆으로
    옮겼다 — 요약을 펼치면 footer가 아래로 밀려나면서 숨기기 버튼이 제목에서 멀어져,
    기사가 많을 때 어떤 버튼이 어떤 기사 것인지 헷갈린다는 피드백에 따른 것. summary
    안에 버튼을 넣으면 클릭이 위(펼치기)로도 번질 수 있어 JS에서 stopPropagation으로
    막는다(hideArticle 자체는 그대로, 호출 전에 이벤트 전파만 끊는다).
    """
    outlet = html.escape(article["outlet"])
    # [추가: 2026-07-29] 화면 표시용 언론사 이름만 outlet_display_label을 거친다(매일경제만
    # 빨간 글자로, 매경이코노미와 안 구분되는 걸 화면에서 알려주는 표식) — data-outlet
    # 속성(위 outlet)은 원본 그대로 둬서 숨긴 기사 메타데이터 등이 canonical 값을 유지한다.
    # [수정: 2026-07-30] outlet_display_label이 이제 이미 이스케이프된 HTML 조각(색
    # span 포함)을 돌려주므로 여기서 다시 html.escape()하면 안 된다(<span> 태그가
    # 그대로 글자로 보이게 됨).
    outlet_display = outlet_display_label(article["outlet"])
    title = highlight_keywords(article["title"], highlight_words)
    title_attr = html.escape(article["title"])
    pub_date_attr = html.escape(article.get("pub_date") or "")
    url = html.escape(article["url"])
    summary_html = highlight_keywords(article.get("summary", ""), highlight_words)
    # 템플릿 자체도 이스케이프해, 사용자가 입력한 특수문자가 HTML로 해석되지 않게 한다.
    line_html = apply_line_template(html.escape(line_template), outlet_display, title)
    move_html = ""
    if move is not None:
        up_disabled = " disabled" if move.get("disable_up") else ""
        down_disabled = " disabled" if move.get("disable_down") else ""
        effective_up_handler = up_handler or move_handler
        # [수정: 2026-07-30] ↑/↓가 summary 안으로 옮겨오면서, 🗑️와 같은 이유로
        # stopPropagation이 필요해졌다 — 없으면 클릭이 위(펼치기 토글)로도 번진다.
        move_html = (
            f'<button class="move-btn" type="button" data-url="{url}" data-direction="up" '
            f'onclick="event.stopPropagation(); {effective_up_handler}(this);"'
            f'{up_disabled} title="{up_title}">↑</button>'
            f'<button class="move-btn" type="button" data-url="{url}" data-direction="down" '
            f'onclick="event.stopPropagation(); {move_handler}(this);"'
            f'{down_disabled} title="아래로 이동">↓</button>'
        )
    hide_btn_html = (
        f'<button class="hide-btn" type="button" data-url="{url}" data-outlet="{outlet}" '
        f'data-title="{title_attr}" data-pub-date="{pub_date_attr}" '
        f'onclick="event.stopPropagation(); hideArticle(this);" '
        'title="이 기사 숨기기 (되돌리기 가능)">🗑️</button>'
    )
    # [추가: 2026-08-05] 네이버 API의 제목/요약이 사진 설명이나 문장 중간 등 이상한
    # 지점에서 잘려 있을 때, 그 기사 하나만 원문 페이지 값으로 다시 가져오는 버튼 —
    # 평소엔 숨어 있다가(.refetch-btn, CSS) 그 기사 카드에 마우스를 올리면 나타난다.
    refetch_btn_html = (
        f'<button class="refetch-btn" type="button" data-url="{url}" '
        'onclick="event.stopPropagation(); refetchSummary(this);" '
        'title="원문에서 다시 가져오기">🔄</button>'
    )
    # [추가: 2026-08-05] 🔄가 원문에서도 못 찾는 경우(언론사 페이지 자체가 이상한 경우)를
    # 위한 최후 수단 — 제목·요약을 사용자가 직접 타이핑해서 고칠 수 있다. 🔄와 같은
    # 저장소(app.summary_overrides)를 쓴다. data-title/data-summary는 하이라이트 적용
    # 전 원본 텍스트라 편집 칸에 그대로 채워 넣기 좋다.
    edit_btn_html = (
        f'<button class="refetch-btn" type="button" data-url="{url}" '
        f'data-title="{title_attr}" data-summary="{html.escape(article.get("summary", ""))}" '
        'onclick="event.stopPropagation(); editSummary(this);" '
        'title="제목·요약 직접 수정">✏️</button>'
    )
    # [수정: 2026-07-30] 게시 시각과 🗑️/↑/↓를 전부 제목 줄(summary)로 옮겼다 — 예전엔
    # footer(URL 옆)에 있었는데, 요약을 펼치면 footer가 아래로 밀려나면서 "게시 시각이
    # URL 쪽으로 이동한 것처럼" 보여 헷갈린다는 피드백. 이제 제목만 보고 언제·뭘 할지
    # 바로 판단할 수 있다. 순서는 [🗑️][↑][↓] — "먼저 남길지 거를지 정하고(🗑️), 나중에
    # 순서를 정리한다(↑↓)"는 실제 작업 흐름 그대로다.
    # [추가: 2026-07-30] 체크박스는 summary 맨 앞에 둔다 — .article summary가 이미
    # flex 한 줄이라(app.preview_renderer 등 각 화면 CSS), 별도 레이아웃 손볼 것 없이
    # 그 줄 맨 앞자리에 자연스럽게 낀다. 클릭이 위(펼치기)로 안 번지게 stopPropagation.
    checkbox_html = (
        f'<input type="checkbox" class="article-select" data-url="{url}" '
        'onclick="event.stopPropagation();" onchange="updateBulkMoveBar();">'
        if checkbox
        else ""
    )
    return (
        f'<div class="article" data-url="{url}" data-outlet-rank="{outlet_rank}">'
        "<details>"
        f'<summary>{checkbox_html}<span class="title-line">{line_html}</span>{_format_pub_time(article)}'
        f'<span class="article-actions">{group_select_html}{refetch_btn_html}{edit_btn_html}{hide_btn_html}{move_html}</span></summary>'
        f'<p class="article-summary">{summary_html}</p>'
        "</details>"
        '<div class="article-footer">'
        f'<a class="url" href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>'
        f"{extra_buttons_html}"
        "</div>"
        "</div>"
    )


def _group_select_html(current_name: str, all_names: list, labels: dict, custom_names: Optional[set] = None) -> str:
    """"다른 소제목으로" 드롭다운 — app.preview_renderer._group_select_html과 동일한
    이유·동작(스크랩 초안과 완성본 양쪽 다 자동분류가 완벽하지 않아 여러 개를 바로
    잡아야 하는 경우가 있어 붙였다). 지금 속한 소제목은 옵션에서 빼고, 옮길 곳이
    아예 없으면(소제목이 이거 하나뿐) 빈 문자열을 돌려줘 드롭다운을 숨긴다.

    [추가: 2026-08-07] 자동 분류 소제목과 사용자가 만든 소제목이 텍스트만 봐서는
    구분이 안 돼 헷갈린다는 피드백 — 화면 카드에 이미 쓰는 하늘색 점선 테두리 표식과
    같은 기준(custom_names)으로, 사용자가 만든 소제목만 볼드로 보여준다. 네이티브
    <select>는 배경색·아이콘 같은 꾸밈은 못 넣어도 font-weight 같은 글자 스타일은
    입력해도 되어(대부분의 데스크톱 브라우저에서 실제로 렌더링됨), 이 정도 구분에는
    충분하다.
    """
    custom_names = custom_names or set()
    others = [n for n in all_names if n != current_name]
    if not others:
        return ""
    options = "".join(
        f'<option value="{html.escape(n)}"'
        + (' style="font-weight:700"' if n in custom_names else "")
        + f'>{html.escape(_group_option_label(n, labels))}</option>'
        for n in others
    )
    return (
        '<select class="group-move-select" onclick="event.stopPropagation();" '
        'onchange="event.stopPropagation(); moveArticleToGroup(this);">'
        '<option value="" selected disabled>다른 소제목</option>'
        f"{options}"
        "</select>"
    )


def _group_option_label(name: str, labels: dict) -> str:
    """드롭다운 옵션 글자 — [수정: 2026-08-05] 원래 이름을 괄호로 같이 보여주던 걸
    되돌렸다. 이용자가 이름표를 한 번 바꾸면 그 이후로는 원래 이름을 신경 쓸 일이
    없다는 피드백에 따라 표시 이름 하나만 보여준다(app.renderer._group_select_html/
    app.preview_renderer._group_select_html 공용)."""
    return display_group_name(name, labels)


def _render_groups(
    groups: list, highlight_words: list, line_template: str, labels: dict, rank_by_url: dict, custom_names: set
) -> str:
    """소제목별 화면을 렌더링한다.

    ↑/↓ 버튼은 각 소제목 안에서만이 아니라 소제목 경계도 넘나들 수 있어(규칙21),
    맨 처음 소제목의 첫 기사(↑)·맨 마지막 소제목의 마지막 기사(↓)일 때만 버튼을
    비활성화한다. 그 외 소제목 경계에서는 버튼이 계속 활성 상태이고, 클릭하면
    서버(move_article)가 "같은 소제목 내 순서 변경"과 "옆 소제목으로 이동"을 알아서
    구분해 처리한다 — 화면(버튼) 쪽은 이 둘을 구분할 필요가 없다.

    소제목 옆 ✏️ 버튼은 표시 이름만 바꾼다(기능2 규칙 8) — 분류 자체는 항상 원래
    소제목 단어(group["name"])를 기준으로 하므로, 버튼의 data-name에는 원래 단어를
    그대로 담아 서버가 어떤 단어의 이름표를 바꿀지 정확히 알 수 있게 한다.

    [수정: 2026-07-30] 스크랩 초안에 먼저 만든 체크박스 다중 선택 + "다른 소제목으로"
    드롭다운을 완성본에도 그대로 붙였다(app.preview_renderer._render_preview_groups와
    동일 패턴) — 자동분류가 완벽하지 않아 여러 개를 한꺼번에 바로잡아야 하는 경우가
    초안뿐 아니라 완성본에서도 필요해서다. 기사가 0개인 소제목(사용자가 미리 만든
    빈 소제목)은 안내 문구를 보여주고, 헤더의 🗑️도 "삭제"로 동작한다.
    """
    all_names = [g["name"] for g in groups]
    sections = []
    last_group_index = len(groups) - 1
    for group_index, group in enumerate(groups):
        original_name = html.escape(group["name"])
        display_name = html.escape(display_group_name(group["name"], labels))
        if not group["articles"]:
            articles_html = (
                '<p class="empty-group-hint">아직 기사가 없어요 — 체크박스로 선택하거나 '
                "드롭다운으로 기사를 여기로 옮겨보세요.</p>"
            )
            delete_btn = (
                f'<button class="rename-btn" type="button" data-name="{original_name}" '
                'onclick="removeCustomGroup(this)" title="이 빈 소제목 삭제">🗑️</button>'
            )
        else:
            last_index = len(group["articles"]) - 1
            group_select = _group_select_html(group["name"], all_names, labels, custom_names)
            articles_html = "\n".join(
                render_article(
                    a,
                    highlight_words,
                    line_template,
                    move={
                        "disable_up": i == 0 and group_index == 0,
                        "disable_down": i == last_index and group_index == last_group_index,
                    },
                    checkbox=True,
                    group_select_html=group_select,
                    outlet_rank=rank_by_url.get(a["url"], 0),
                )
                for i, a in enumerate(group["articles"])
            )
            delete_btn = (
                '<button class="rename-btn" type="button" onclick="hideGroup(this)" '
                'title="이 소제목 통째로 숨기기(기사 각각 숨긴 것과 동일 — 되돌리기 가능)">🗑️</button>'
            )
        # [추가: 2026-08-03] 소제목 자체의 화면 순서를 ↑/↓로 바꾼다(기사 순서와는 별개) —
        # app.group_order.save_group_order가 저장을 맡고, 여기서는 맨 처음/마지막
        # 소제목일 때만 버튼을 비활성화한다(기사 ↑/↓와 동일한 관례).
        order_up_disabled = " disabled" if group_index == 0 else ""
        order_down_disabled = " disabled" if group_index == last_group_index else ""
        order_html = (
            f'<button class="order-btn" type="button" data-name="{original_name}" '
            f'onclick="moveGroupOrder(this, \'up\');"{order_up_disabled} title="소제목 위로 이동">↑</button>'
            f'<button class="order-btn" type="button" data-name="{original_name}" '
            f'onclick="moveGroupOrder(this, \'down\');"{order_down_disabled} title="소제목 아래로 이동">↓</button>'
        )
        # [추가: 2026-08-04] 소제목 안 기사를 한 번에 전부 체크(다른 소제목으로 일괄
        # 이동용) — 기사가 없는 소제목엔 체크할 대상이 없으니 렌더링하지 않는다.
        select_all_html = (
            '<input type="checkbox" class="group-select-all" '
            'onclick="event.stopPropagation();" onchange="toggleGroupSelectAll(this);" '
            'title="이 소제목 기사 전체 선택"> '
            if group["articles"]
            else ""
        )
        heading = (
            f'<h2><span class="subheading-title">{select_all_html}&lt;{display_name}&gt;</span>'
            f'<span class="subheading-icons">'
            f'<button class="rename-btn" type="button" data-name="{original_name}" '
            f'data-current="{display_name}" onclick="renameGroup(this)" title="소제목 이름 바꾸기">✏️</button> '
            f"{delete_btn}{order_html}</span></h2>"
        )
        section_class = "subheading subheading-custom" if group["name"] in custom_names else "subheading"
        sections.append(
            f'<section class="{section_class}" id="subheading-{group_index}" data-toc-name="{display_name}">'
            f"{heading}{articles_html}</section>"
        )
    return "\n".join(sections)


def _render_bottom(articles: list, groups: list, keywords: list, highlight_words: list, labels: dict) -> str:
    # keywords(검색어)는 소제목/키워드 추출에서 "뻔한 단어"를 걸러내는 용도로만 쓰고,
    # 실제로 형광펜을 칠하는 기준은 highlight_words(형광펜 단어, 별도 설정)다.
    keyword_tags = ", ".join(html.escape(k) for k in extract_keywords(articles, keywords))
    summary_lines = "\n".join(
        f'<p><strong>&lt;{html.escape(display_group_name(s["name"], labels))}&gt;</strong> '
        f'{highlight_keywords(s["summary"], highlight_words)}</p>'
        for s in summarize_groups(groups)
    )
    return (
        '<div class="bottom">'
        "<h3>🤖 AI가 추출한 주요 키워드</h3>"
        f"<p>{keyword_tags}</p>"
        "<h3>💬 AI가 읽은 소제목별 주요 요약</h3>"
        f"{summary_lines}"
        "</div>"
    )


def _render_manual_section(manual_articles: list, highlight_words: list, line_template: str) -> str:
    """"직접 추가한 기사" 구획을 렌더링한다 (실시간 기사 현황의 "📌" 결과물).

    자동 소제목 그룹과 분리된 별도 목록이다 — <소제목N> 라벨이 없고, 최대 5개 제한과도
    무관하며, 복사/내보내기 텍스트(_build_plain_text)에도 포함하지 않는다(실시간 현황에서
    참고용으로 옮겨둔 것일 뿐 "공식 스크랩 결과"가 아니라는 성격 때문 — 당일 자정이 지나면
    app.manual_articles가 자동으로 비운다).

    [수정: 2026-07-28] 기사마다 "📝 초안에 포함"·"📗 완성본에 포함" 버튼 두 개를 항상
    보여준다 — 예전엔 맨 위 기사의 ↑ 버튼만 승격으로 동작해서, 원하는 기사를 승격하려면
    먼저 순서를 맨 위까지 옮겨야 하는 번거로움이 있었다(피드백: "너무 빡세다"). 순서와
    무관하게 아무 기사나 바로 승격할 수 있게 되면서, 목록 안 순서바꾸기(↑/↓,
    moveManualArticle) 자체가 더 이상 필요 없어져 같이 뺐다. 두 버튼 다 어느 화면
    (완성본·초안)에서 눌러도 동일하게 동작한다(app.settings_server의
    /promote-manual-article, /promote-manual-article-to-draft). 비어 있으면 아예
    렌더링하지 않는다.
    """
    if not manual_articles:
        return ""
    promote_buttons_template = (
        '<button class="move-btn" type="button" data-url="{url}" '
        'onclick="promoteManualArticleToDraft(this)" title="다음 회차 초안에 포함">📝</button>'
        '<button class="move-btn" type="button" data-url="{url}" '
        'onclick="promoteManualArticle(this)" title="정식 스크랩으로 승격">📗</button>'
    )
    articles_html = "\n".join(
        render_article(
            a,
            highlight_words,
            line_template,
            extra_buttons_html=promote_buttons_template.format(url=html.escape(a["url"])),
        )
        for a in manual_articles
    )
    return (
        '<div class="manual-divider">이 아래는 복사·내보내기에 포함되지 않음</div>'
        '<div class="manual-zone">'
        '<div class="manual-zone-head">'
        '<span class="manual-zone-title">📌 직접 추가한 기사</span>'
        '<span class="manual-zone-hint">📝 다음 회차 초안에 포함 · 📗 정식 스크랩으로 승격 · 자정에 자동으로 비워져요</span>'
        "</div>"
        f"{articles_html}"
        "</div>"
    )


def _build_plain_text(run_slot: str, groups: list, line_template: str, labels: dict) -> str:
    """복사/내보내기용 메모장 형식 텍스트를 만든다 (PRD.md 기능1 규칙 5·10·11).

    화면에 표시된 언론사·기사제목·URL 목록만 담는다 — 하단의 🤖 키워드·💬 요약
    블록은 규칙10이 "소제목별 스크랩 목록(언론사·기사제목·URL)"만 명시하므로 제외한다.
    첫 줄 형식은 화면과 동일하게 line_template을 따른다. 소제목 이름은 사용자가 붙인
    이름표(labels)가 있으면 그걸로 표시한다 — 화면과 항상 같은 이름을 보여줘야 한다.
    """
    lines = [f"언론 모니터링 {format_slot_time_kr(run_slot)} 기준"]
    # [추가: 2026-08-05] "+ 직접 키워드 작성하기"로 적어둔 메모가 있으면 헤더 바로 아래
    # "- {메모}" 한 줄로 끼워 넣는다 — AI 키워드 블록(하단)과 달리 이건 화면 맨 위에 있고,
    # 내보내기 텍스트에서도 항상 헤더 다음 줄에 온다.
    note = load_manual_keyword_note((datetime.now().strftime("%Y-%m-%d"), run_slot))
    if note:
        lines.append(f"- {note}")
    lines.append("")
    if not groups:
        lines.append("💤")
    else:
        for group in groups:
            lines.append(f"<{display_group_name(group['name'], labels)}>")
            for article in group["articles"]:
                lines.append(apply_line_template(line_template, article["outlet"], article["title"]))
                lines.append(article["url"])
                # [추가: 2026-07-27] 기사마다 빈 줄을 넣어 URL과 다음 기사 제목이 붙어
                # 보이지 않게 한다 — 보고용으로 복사해 쓸 때 훑어보기 쉽다는 요청.
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_latest_plain_text(
    keywords: Optional[list] = None,
    highlight_words: Optional[list] = None,
    line_template: Optional[str] = None,
) -> Optional[str]:
    """텔레그램 자동 전송처럼 화면(HTML) 없이도 최신 회차의 복사/내보내기 텍스트가 필요할
    때 쓴다. generate_screen과 같은 방식으로 최신 회차를 찾되, 오늘 회차가 아직 없으면
    (아직 스크랩 전이거나 자정이 지나 어제 회차만 남음) 예외 대신 None을 반환한다 —
    호출하는 쪽(app.scheduler)이 "보낼 게 없다"로 조용히 넘어갈 수 있어야 하기 때문이다.
    """
    run = load_latest_run()
    if run is None or not is_today(run):
        return None
    if keywords is None or highlight_words is None or line_template is None:
        settings = load_settings()
        keywords = keywords if keywords is not None else all_search_keywords(settings)
        highlight_words = highlight_words if highlight_words is not None else settings["highlight_keywords"]
        line_template = line_template if line_template is not None else settings["article_line_template"]
    articles = apply_summary_overrides(filter_hidden(run["articles"]))
    labels = load_group_labels()
    groups = (
        classify_articles(
            articles, keywords, forced_groups=load_group_overrides(), custom_group_names=load_custom_groups()
        )
        if articles
        else []
    )
    groups = apply_group_order(groups)
    return _build_plain_text(run["run_slot"], groups, line_template, labels)


def render_page(
    run_slot: str,
    articles: list,
    keywords: Optional[list] = None,
    highlight_words: Optional[list] = None,
    line_template: Optional[str] = None,
) -> str:
    """한 회차 데이터를 완성된 HTML 문서 문자열로 렌더링한다.

    수집된 기사가 없으면 (PRD.md 기능1 규칙 7) 본문을 "💤"로 대체한다.
    헤더의 회차 시각(run_slot)은 이 경우에도 그대로 표시한다.

    keywords: 검색 키워드 — 소제목 분류·주요 키워드 추출에서 "뻔한 단어"를 제외하는 데 쓴다.
    highlight_words: 형광펜 단어 — 요약에 하이라이트를 칠하는 기준이며 keywords와 무관하다(규칙6).
    line_template: 기사 첫 줄("ㅇ (언론사) 제목") 형식. keywords/highlight_words와도 무관하다(규칙5).
    """
    keywords = keywords if keywords is not None else DEFAULT_KEYWORDS
    highlight_words = highlight_words if highlight_words is not None else DEFAULT_HIGHLIGHT_KEYWORDS
    line_template = line_template if line_template is not None else DEFAULT_ARTICLE_LINE_TEMPLATE
    labels = load_group_labels()
    custom_names = load_custom_groups()
    groups = (
        classify_articles(articles, keywords, forced_groups=load_group_overrides(), custom_group_names=custom_names)
        if articles
        else []
    )
    # [추가: 2026-07-30] 스크랩 초안과 마찬가지로, "+ 새 소제목 만들기"로 미리 만들어둔
    # 빈 소제목도 완성본에 같이 보여준다(app.preview_renderer._compute_preview_content와
    # 같은 이유·같은 방식) — 요약/키워드 블록(_render_bottom)·복사 텍스트에는 원래의
    # groups만 넘겨 빈 placeholder가 섞이지 않게 한다.
    existing_names = {g["name"] for g in groups}
    empty_custom_groups = [{"name": n, "articles": []} for n in custom_names if n not in existing_names]
    # [추가: 2026-08-03] 사용자가 ↑/↓로 직접 정한 소제목 순서를 얹는다 — 빈 소제목까지
    # 포함한 전체 목록에 적용한 뒤, 요약·복사 텍스트에 넘길 groups도 이 순서를 그대로
    # 반영하도록 다시 추려낸다(화면과 복사 텍스트의 소제목 순서가 항상 같아야 하므로).
    render_groups = apply_group_order(groups + empty_custom_groups)
    groups = [g for g in render_groups if g["articles"]]
    # [추가: 2026-08-04] "언론사순" 보기가 참고할 순위 — articles는 이미 언론사 우선순위로
    # 정렬돼 저장된 회차 전체 목록이므로, 그 안 인덱스를 그대로 순위로 쓴다.
    rank_by_url = {a["url"]: i for i, a in enumerate(articles)}
    if not render_groups:
        body = '<div class="empty">💤<div class="cute-caption">뉴스가 잠잠</div></div>'
    else:
        body = _render_groups(render_groups, highlight_words, line_template, labels, rank_by_url, set(custom_names))
        if groups:
            body += _render_bottom(articles, groups, keywords, highlight_words, labels)
    # "직접 추가한 기사"(실시간 현황 → 📌)는 이 회차에 실제로 수집된 기사가 하나도
    # 없어도(💤) 독립적으로 존재할 수 있으므로, 위 분기와 무관하게 항상 이어붙인다.
    # [수정: 2026-07-27] 전체를 자동으로 정식 결과에 합치는 대신, 맨 위 기사를 ↑로
    # "승격"시켜야만 실제 소제목(groups)에 들어간다(_handle_promote_manual_article,
    # app/settings_server.py) — 그전까지는 예전처럼 복사/내보내기에서 계속 제외된다.
    manual_articles = filter_hidden(load_manual_articles())
    body += _render_manual_section(manual_articles, highlight_words, line_template)

    plain_text = _build_plain_text(run_slot, groups, line_template, labels)
    export_filename = f"언론모니터링_{run_slot.replace(':', '-')}.txt"

    manual_keyword_note = load_manual_keyword_note((datetime.now().strftime("%Y-%m-%d"), run_slot))
    return _PAGE_TEMPLATE.format(
        run_slot=html.escape(format_slot_time_kr(run_slot)),
        run_slot_raw=html.escape(run_slot),
        body=body,
        note_open_class=" is-open" if manual_keyword_note else "",
        note_value_attr=html.escape(manual_keyword_note),
        # json.dumps로 JS 문자열 리터럴로 안전하게 이스케이프하고, "</script"가 섞여
        # 있어도 스크립트 태그가 조기 종료되지 않도록 "</"를 "<\/"로 한 번 더 바꾼다.
        plain_text_json=json.dumps(plain_text, ensure_ascii=False).replace("</", "<\\/"),
        highlight_words_json=json.dumps(
            [
                {"word": item["word"], "color_hex": HIGHLIGHT_COLORS[item.get("color", 0) % len(HIGHLIGHT_COLORS)]}
                for item in highlight_words
            ],
            ensure_ascii=False,
        ).replace("</", "<\\/"),
        export_href=quote(plain_text),
        export_filename=html.escape(export_filename),
        settings_host=SETTINGS_SERVER_HOST,
        settings_port=SETTINGS_SERVER_PORT,
        hidden_href=f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/hidden",
        live_href=f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/live.html",
        preview_href=f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/preview.html",
        font_stack=FONT_STACK,
        cute_font_face=_CUTE_FONT_FACE_CSS,
        cute_font_name=CUTE_FONT_NAME,
        bg=COLOR_BG,
        card=COLOR_CARD,
        header=COLOR_HEADER,
        accent=COLOR_ACCENT,
        text=COLOR_TEXT,
        muted=COLOR_TEXT_MUTED,
        border=COLOR_BORDER,
        hover=COLOR_HOVER,
        error=COLOR_ERROR,
        # [추가: 2026-07-28, 톤다운: 2026-08-07] "이미 확인한 기사"(제목 펼침/링크 클릭)
        # 표시 전용 색 — 원래 보라색(#A855F7)이 너무 튄다는 피드백으로 밝은 남색으로
        # 교체했다. 이 화면에서만 쓰는 상태 표시라 공용 COLOR_* 팔레트에는 넣지 않고
        # 여기서 상수로 둔다.
        seen_color="#3B5FA0",
        # [추가: 2026-08-03] 액션 툴바 버튼 리디자인(시안 B/A, 사용자 선택) 전용 색 —
        # accent(#2563EB)를 옅게 탄 톤온톤 hover와, 고스트 아웃라인용 연한 테두리.
        # 둘 다 이 툴바에서만 쓰는 값이라 공용 COLOR_* 팔레트에는 넣지 않는다.
        tonal_hover="#DCEAFE",
        ghost_border="#C7D9F7",
        ghost_border_hover="#A9C6F5",
    )


# [추가: 2026-07-26] 오늘 아직 완료된 회차가 하나도 없을 때(앱을 막 켰거나, 자정이 지나
# 어제 회차만 남아 있을 때) index.html 자리에 대신 보여줄 안내 화면. main.py(최초 실행 시)와
# app.scheduler.run_scheduler(매 tick, 자정 이후 오늘 첫 회차 전까지) 둘 다에서 쓴다 —
# 자정을 넘겨도 어제 스크랩 화면이 계속 떠 있는 걸 막기 위해서다.
_WAITING_PAGE = f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8" /><title>언론 모니터링</title>
<style>body{{margin:0;background:{COLOR_BG};color:{COLOR_TEXT};font-family:{FONT_STACK};
display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;
font-size:1.2rem;text-align:center;padding:60px 24px 0;line-height:1.8;box-sizing:border-box;}}
.icon{{font-size:2rem;}}
.topbar{{position:fixed;top:0;left:0;right:0;z-index:20;background:{COLOR_CARD};
border-bottom:1px solid {COLOR_BORDER};box-shadow:0 2px 8px rgba(0,0,0,0.06);}}
.topbar-inner{{max-width:800px;margin:0 auto;padding:12px 24px;display:flex;
justify-content:space-between;align-items:center;}}
.topbar a{{color:{COLOR_ACCENT};text-decoration:none;font-size:0.92rem;font-weight:600;
padding:6px 10px;border-radius:6px;}}
.topbar a:hover{{background:{COLOR_HOVER};}}</style>
</head>
<body>
<div class="topbar"><div class="topbar-inner">
<a href="home.html">홈</a>
<a href="history.html">지난 기사</a>
<a href="index.html">새로고침</a>
</div></div>
<div><div class="icon">🐰⏱️</div>아직 스크랩 시간 전입니다.<br>예정 시각이 되면 자동으로 시작됩니다.</div>
</body>
</html>
"""


def generate_waiting_page() -> Path:
    """오늘 첫 회차가 아직 끝나지 않았을 때 index.html 대신 보여줄 안내 화면을 만든다."""
    atomic_write_text(OUTPUT_HTML_PATH, _WAITING_PAGE)
    return OUTPUT_HTML_PATH


def generate_screen(
    keywords: Optional[list] = None,
    highlight_words: Optional[list] = None,
    line_template: Optional[str] = None,
) -> Path:
    """저장된 최신 회차를 읽어 index.html로 렌더링한다 (PRD.md 기능2 규칙 6: 최신 회차만 표시).

    keywords/highlight_words/line_template을 생략하면 설정 화면에 저장된 값을 각각 쓴다
    (규칙2 검색 키워드, 규칙6 형광펜 단어, 규칙5 출력 줄 형식 — 서로 별개 설정).
    """
    run = load_latest_run()
    if run is None or not is_today(run):
        # [수정: 2026-07-26] 회차가 아예 없을 때뿐 아니라, 자정이 지나 어제 회차만
        # 남아있을 때도 같은 예외로 취급한다 — 호출하는 쪽(main.py, app.scheduler)이
        # 어제 화면 대신 안내 화면(generate_waiting_page)을 보여주게 하기 위해서다.
        raise RuntimeError("오늘 완료된 회차가 없습니다 — 아직 스크랩 전이거나 자정이 지나 어제 회차만 남아있습니다.")
    if keywords is None or highlight_words is None or line_template is None:
        settings = load_settings()
        keywords = keywords if keywords is not None else all_search_keywords(settings)
        highlight_words = highlight_words if highlight_words is not None else settings["highlight_keywords"]
        line_template = line_template if line_template is not None else settings["article_line_template"]
    articles = apply_summary_overrides(filter_hidden(run["articles"]))
    html_text = render_page(run["run_slot"], articles, keywords, highlight_words, line_template)
    atomic_write_text(OUTPUT_HTML_PATH, html_text)
    return OUTPUT_HTML_PATH
