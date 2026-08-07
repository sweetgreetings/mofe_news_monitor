# Design Ref: 실시간 기사 현황 — 예정된 회차와 별개로 요청 시점마다 당일 0시~현재를 라이브로 재검색
import html
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from app.config import (
    COLOR_ACCENT,
    COLOR_BG,
    COLOR_BORDER,
    COLOR_CARD,
    COLOR_ERROR,
    COLOR_HEADER,
    COLOR_LIVE_BG,
    COLOR_TEXT,
    COLOR_TEXT_MUTED,
    FONT_STACK,
    HIGHLIGHT_COLORS,
    LIVE_HTML_PATH,
    SETTINGS_SERVER_HOST,
    SETTINGS_SERVER_PORT,
)
from app.atomic_write import atomic_write_text
from app.curation import load_hidden_urls
from app.summary_overrides import apply_summary_overrides
from app.filters import exclude_personnel_articles, exclude_photo_articles
from app.highlight import highlight_keywords
from app.live_cache import load_live_cache, save_live_cache
from app.manual_articles import load_manual_articles
from app.naver_api import kst_today_at, outlet_display_label, search_articles_by_groups
from app.scheduler import next_pending_slot
from app.settings import active_schedule_times, active_search_groups, load_settings
from app.storage import load_latest_run, load_today_runs

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>실시간 기사 현황</title>
<style>
  body {{ margin: 0; background: {bg}; color: {text}; font-family: {font_stack}; }}
  .container {{
    max-width: 800px; margin: 24px auto; padding: 24px; background: {card};
    border: 1px solid {border}; border-radius: 8px;
  }}
  /* [수정: 2026-07-27] 상단 고정 바 — HOME + 다른 두 화면(초안/완성본)으로 바로 이동.
     하단 고정 바는 🗑️(숨긴 기사 관리) 전용 — 상단이 3개 내비게이션으로 꽉 차서 자리를
     따로 뺐다. */
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
    display: flex; justify-content: flex-end;
  }}
  .bottombar a {{ color: {accent}; text-decoration: none; font-size: 1.1rem; padding: 6px 10px; border-radius: 6px; }}
  .bottombar a:hover {{ background: {hover}; }}
  /* [추가: 2026-08-03] app.renderer와 동일한 이유 — 하단바 🖍️ 형광펜 편집 팝오버. 이
     화면은 {{hover}}가 실시간 배지용 붉은 톤(COLOR_LIVE_BG)이라 여기서만 다른 화면과
     같은 파란 hover색(#EFF6FF)을 직접 쓴다 — {{hover}}를 그대로 쓰면 이 버튼만 빨갛게
     hover되어 다른 두 화면과 어긋나 보인다. */
  .highlight-wrap {{ position: relative; }}
  .highlight-toggle {{
    background: transparent; border: none; font-size: 1.1rem; cursor: pointer;
    padding: 6px 10px; border-radius: 6px;
  }}
  .highlight-toggle:hover {{ background: #EFF6FF; }}
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
  .live-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }}
  .live-badge {{ display: inline-flex; align-items: center; gap: 6px; color: {error}; font-size: 1.25rem; font-weight: 600; }}
  .pulse {{ width: 8px; height: 8px; border-radius: 50%; background: {error}; }}
  .refresh-btn {{
    background: {card}; color: {accent}; border: 1px solid {accent}; border-radius: 6px;
    padding: 7px 14px; font-size: 0.9rem; font-weight: 500; text-decoration: none; white-space: nowrap;
  }}
  .refresh-btn:hover {{ background: {hover}; }}
  /* [추가: 2026-07-26] '스크랩 언론사만 보기' — 서버 재검색 없이 이미 불러온 기사
     중에서 /outlets에 등록된 언론사만 화면에서 걸러 보여준다(노이즈 완화 목적).
     실시간 현황 자체의 "전체 언론사 라이브 미러" 성격은 그대로 유지된다. */
  .filter-row {{
    margin: 14px 0 4px; padding: 10px 14px; background: {bg}; border: 1px solid {border};
    border-radius: 8px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  }}
  .filter-row label {{ font-size: 0.92rem; display: flex; align-items: center; gap: 8px; cursor: pointer; }}
  .filter-row label:has(input:disabled) {{ color: {muted}; cursor: not-allowed; }}
  .live-row.is-hidden-by-filter {{ display: none; }}
  .stat-card {{
    background: {hover}; border-radius: 8px; padding: 14px 18px; margin: 16px 0 4px;
    display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
  }}
  .stat-num {{ font-size: 1.6rem; font-weight: 600; color: {header}; }}
  .stat-label {{ font-size: 0.85rem; color: {muted}; }}
  .live-row {{
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    padding: 12px 12px 12px 14px; border-top: 1px solid {border}; border-left: 3px solid transparent;
  }}
  /* [추가: 2026-08-05] app.renderer와 동일 — 마우스 오버 시 연한 회색 표시. 아래 상태별
     배경색(스크랩됨/숨김/담아둠 등) 규칙보다 먼저 둬서, 상태가 있는 줄은 마우스를
     올려도 그 상태 색이 그대로 우선한다(평범한 줄만 회색이 보인다). */
  .live-row:hover {{ background: #F3F4F6; }}
  /* [수정: 2026-07-27] 오늘 이미 스크랩된(자동+수동) 기사는 왼쪽 초록 색띠 + 옅은 초록
     배경으로 "이미 잘 담겼다"는 완료 느낌을 준다 — 회색 음영만으로는 존재감이 약했다.
     색띠는 형광펜(제목 안쪽 텍스트 배경색)과 자리가 겹치지 않아 서로 안 부딕친다. */
  .live-row.is-scrapped {{
    border-left-color: {scrap_green}; background: {scrap_green_bg}; opacity: 0.86;
  }}
  /* [추가: 2026-07-27] 스크랩 초안/완성본에 있다가 🗑️로 숨겨진 기사는 "이미 스크랩됨"과는
     다른 상태다(살아있는 초안이 아니라 휴지통에 있음) — 취소선으로 "제외됨"을 조용히
     알려준다. 스크랩됨처럼 색띠까지는 필요 없다는 판단으로 회색 음영만 유지한다. */
  .live-row.is-hidden {{ opacity: 0.5; text-decoration: line-through; }}
  /* [수정: 2026-07-27] "직접 추가한 기사"(임시보드)에만 담겨 있고 아직 정식 스크랩에
     안 들어간 기사는 회색 계열 색띠로 보여준다 — 처음엔 호박색을 썼는데, "아직 임시로
     담아둔 것"이 "이미 확정된 스크랩됨"(초록)보다 더 튀어 보이는 게 어색하다는 피드백에
     따라 차분한 회색(= "아직 결정 안 됨")으로 바꿨다. */
  .live-row.is-pinned {{
    border-left-color: {pin_gray}; background: {pin_gray_bg}; opacity: 0.86;
  }}
  /* [추가: 2026-07-29] 컴퓨터가 알아서 [초안]에 담았을 것으로 추정되는 기사 — 사용자가
     직접 담아둔 게 아니라 시스템이 검색 조건에 맞춰 자동으로 골랐다는 뜻이라 담아둠(회색)과
     다른 색으로 구분한다. */
  .live-row.is-auto-drafted {{
    border-left-color: {auto_teal}; background: {auto_teal_bg}; opacity: 0.86;
  }}
  .live-row-text {{ min-width: 0; }}
  /* [수정: 2026-07-29] 버그 수정 — 📌 버튼 옆에 🗑️ 아이콘을 추가하면서 .live-row의
     자식이 2개(텍스트+버튼)에서 3개(텍스트+버튼+아이콘)로 늘었는데, justify-content:
     space-between이 자식 "사이사이"에 남는 공간을 나눠 넣다 보니 버튼과 아이콘 사이가
     텅 비면서 핀이 화면 한가운데로 떠밀려 보였다. 두 버튼을 이 래퍼 하나로 묶어 다시
     "텍스트 vs 버튼 묶음" 2개짜리 레이아웃으로 되돌린다. */
  .live-row-actions {{ display: flex; align-items: center; gap: 6px; flex-shrink: 0; }}
  .outlet-tag {{ color: {muted}; font-size: 0.8rem; }}
  .pub-time {{ color: {muted}; font-size: 0.78rem; margin-left: 6px; }}
  .live-title {{ font-size: 0.95rem; margin: 2px 0 2px; overflow-wrap: anywhere; color: {text}; }}
  .live-title summary {{ cursor: pointer; -webkit-tap-highlight-color: transparent; color: {text}; }}
  .live-title summary::marker {{ color: {muted}; }}
  .live-summary {{ margin: 6px 0 4px 20px; color: {text}; font-size: 0.9rem; }}
  .live-url {{ font-size: 0.78rem; overflow-wrap: anywhere; }}
  .live-url a {{ color: {accent}; text-decoration: underline; }}
  /* [수정: 2026-07-29] 상태별 라벨 길이가 다 달라서(📌/📌 담아둠/✓ 스크랩됨/🤖 자동 선별/
     🗑️ 숨김) 버튼 너비가 들쭉날쭉했고, 그 옆에 붙는 휴지통 아이콘 위치까지 같이
     흔들려 지저분해 보였다 — min-width+가운데 정렬로 버튼 너비를 통일해 휴지통이
     항상 같은 자리에 오게 한다. */
  .add-btn {{
    flex-shrink: 0; background: {card}; color: {text}; border: 1px solid {border};
    border-radius: 6px; padding: 6px 12px; font-size: 0.8rem; cursor: pointer; white-space: nowrap;
    min-width: 92px; text-align: center;
  }}
  .add-btn:disabled {{ color: {muted}; cursor: not-allowed; }}
  .add-btn.added {{ color: {accent}; border-color: {accent}; }}
  .add-btn.is-scrapped-btn {{ color: {scrap_green}; border-color: {scrap_green}; background: {scrap_green_bg}; }}
  .add-btn.is-pinned-btn {{ color: {pin_gray}; border-color: {pin_gray}; background: {pin_gray_bg}; }}
  .add-btn.is-auto-drafted-btn {{ color: {auto_teal}; border-color: {auto_teal}; background: {auto_teal_bg}; }}
  /* [추가: 2026-07-29] "📌 담아둠" 옆의 작은 숨기기 아이콘 — 초안까지 안 가고 바로
     실시간 현황에서 필요없는 기사를 숨길 수 있게 한다(사용자 요청). 숨기면 스크랩
     화면(index/history)에서도 똑같이 안 보인다(app.curation.hide_article, 전역
     숨김) — live.html 전용 임시 숨김이 아니다(의도된 동작, 사용자 확인됨).*/
  .hide-from-live-btn {{
    flex-shrink: 0; background: transparent; border: none; color: {muted}; font-size: 1rem;
    cursor: pointer; padding: 4px 6px;
  }}
  .hide-from-live-btn:hover {{ color: {error}; }}
  /* [추가: 2026-08-05] 원문 다시 가져오기 버튼 — app.renderer.render_article과 동일한
     이유·동작(평소 숨김, 그 행에 마우스를 올리면 나타남). */
  .refetch-btn {{
    flex-shrink: 0; background: transparent; border: none; color: {muted}; font-size: 1rem;
    cursor: pointer; padding: 4px 6px; opacity: 0; transition: opacity 0.15s;
  }}
  .live-row:hover .refetch-btn {{ opacity: 1; }}
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
  .empty {{ text-align: center; margin: 60px 0; font-size: 1.2rem; color: {muted}; }}
  .error-box {{ text-align: center; margin: 60px 0; color: {muted}; font-size: 0.95rem; line-height: 1.7; }}
</style>
</head>
<body>
<div class="topbar"><div class="topbar-inner">
  <a href="{home_href}">홈</a>
  <a href="{preview_href}">📝 스크랩 초안</a>
  <a href="{scrap_href}">📗 스크랩 완성본</a>
</div></div>
<div class="container">
  <div class="live-head">
    <span class="live-badge"><span class="pulse"></span> 실시간 기사 현황</span>
    <a class="refresh-btn" href="live.html">🔄 새로고침</a>
  </div>
  {body}
</div>
<div class="bottombar"><div class="bottombar-inner">
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
// [추가: 2026-08-03] app.renderer와 동일한 이유·동작 — 하단바 🖍️ 형광펜 팝오버.
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
    // [수정: 2026-08-05] app.renderer와 동일 — 칩 클릭으로 색 순환.
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
// [추가: 2026-08-05] app.renderer와 동일 — 팝오버 바깥 클릭 시 닫힘.
document.addEventListener("click", function(e) {{
  var pop = document.getElementById("highlight-popover");
  if (pop.classList.contains("is-open") && !e.target.closest(".highlight-wrap")) {{
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
// [수정: 2026-08-05] app.renderer와 동일 — 팝오버 칩 클릭으로 색 순환(본문 클릭 방식은 삭제).
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
function addToScrap(btn) {{
  var params = new URLSearchParams({{
    outlet: btn.dataset.outlet, title: btn.dataset.title,
    url: btn.dataset.url, summary: btn.dataset.summary, pubDate: btn.dataset.pubDate
  }});
  fetch("http://{settings_host}:{settings_port}/add-manual-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: params
  }}).then(function(res) {{
    if (res.ok) {{
      // [수정: 2026-07-29] 버그 수정 — disabled로 막아버리면 담아둠 재클릭으로
      // 취소하는 토글이 새로고침 전까진 안 먹혔다. 담아둠 상태의 서버 렌더링과
      // 똑같이 계속 눌러서 취소할 수 있게 유지한다(unpinFromLive와 동일한 최종 상태).
      btn.classList.add("added", "is-pinned-btn");
      btn.textContent = "📌 담아둠";
      btn.title = "직접 추가한 기사(임시보드)에 담겨 있어요 — 눌러서 담아두기를 취소할 수 있어요";
      btn.onclick = function() {{ unpinFromLive(btn); }};
      var row = btn.closest(".live-row");
      if (row) {{ row.classList.add("is-pinned"); }}
    }} else {{
      alert("스크랩에 추가하지 못했습니다. 다시 시도해주세요.");
    }}
  }}).catch(function() {{
    alert("스크랩에 추가하지 못했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function hideFromLive(btn) {{
  var params = new URLSearchParams({{
    outlet: btn.dataset.outlet, title: btn.dataset.title,
    url: btn.dataset.url, pubDate: btn.dataset.pubDate
  }});
  fetch("http://{settings_host}:{settings_port}/hide-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: params
  }}).then(function(res) {{
    if (res.ok) {{
      var row = btn.closest(".live-row");
      row.classList.remove("is-pinned", "is-scrapped", "is-auto-drafted");
      row.classList.add("is-hidden");
      var addBtn = row.querySelector(".add-btn");
      // [수정: 2026-07-29] 숨김 상태 버튼은 재클릭으로 되돌릴 수 있어야 하므로 비활성화하지
      // 않는다 — onclick도 unhideFromLive로 바꿔줘야 재클릭이 실제로 되돌리기를 호출한다.
      addBtn.disabled = false;
      addBtn.className = "add-btn added";
      addBtn.textContent = "🗑️ 숨김";
      addBtn.title = "숨긴 기사입니다 — 눌러서 되돌릴 수 있어요(숨긴 기사 관리에서도 가능)";
      addBtn.onclick = function() {{ unhideFromLive(addBtn); }};
      btn.remove();
    }} else {{
      alert("숨기지 못했습니다. 다시 시도해주세요.");
    }}
  }}).catch(function() {{
    alert("숨기지 못했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
// [추가: 2026-08-05] app.renderer.render_article과 동일 — 원문에서 다시 가져오기.
// 실시간 현황은 평소 hideFromLive처럼 DOM만 갈아끼우는 화면이지만, 여기서는 새로고침을
// 쓴다 — 하이라이트 적용된 HTML을 자바스크립트로 다시 만드는 것보다 훨씬 간단하고,
// 어차피 자주 누를 버튼이 아니라 새로고침 한 번(=실시간 재검색 한 번)의 비용이 크지 않다.
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
// [추가: 2026-08-05] app.renderer와 동일 — ✏️ 직접 수정. live.html은 카드 구조가
// 달라서(.article/.article-summary가 아니라 .live-row/.live-summary) 그 부분만 다르다.
function editSummary(btn) {{
  var row = btn.closest(".live-row");
  if (row.querySelector(".edit-summary-form")) {{ return; }}
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
  var details = row.querySelector(".live-title");
  details.open = true;
  row.querySelector(".live-summary").insertAdjacentElement("afterend", form);
}}
function unpinFromLive(btn) {{
  var params = new URLSearchParams({{url: btn.dataset.url}});
  fetch("http://{settings_host}:{settings_port}/unpin-manual-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: params
  }}).then(function(res) {{
    if (res.ok) {{
      var row = btn.closest(".live-row");
      row.classList.remove("is-pinned");
      // [수정: 2026-07-29] 버그 수정 — 🗑️ 아이콘은 이제 담아둠 상태만이 아니라 평범한
      // 상태에도 나오므로(숨김 상태만 빼고 항상 표시), 담아두기를 취소해도 지우면 안 된다.
      btn.classList.remove("added", "is-pinned-btn");
      btn.textContent = "📌";
      btn.title = "스크랩에 추가";
      btn.onclick = function() {{ addToScrap(btn); }};
    }} else {{
      alert("담아두기를 취소하지 못했습니다. 다시 시도해주세요.");
    }}
  }}).catch(function() {{
    alert("담아두기를 취소하지 못했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function unhideFromLive(btn) {{
  var params = new URLSearchParams({{url: btn.dataset.url}});
  fetch("http://{settings_host}:{settings_port}/unhide-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: params
  }}).then(function(res) {{
    if (res.ok) {{
      // 되돌린 뒤 실제 상태(담아둠 or 평범)는 서버가 다시 정확히 판단하므로 새로고침한다.
      location.reload();
    }} else {{
      alert("숨기기를 취소하지 못했습니다. 다시 시도해주세요.");
    }}
  }}).catch(function() {{
    alert("숨기기를 취소하지 못했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
var SCRAP_OUTLETS = {outlet_order_json};
function applyOutletFilter() {{
  var checkbox = document.getElementById("scrap-outlet-only");
  var onlyScrap = checkbox.checked;
  var rows = document.querySelectorAll(".live-row");
  var visible = 0;
  rows.forEach(function (row) {{
    var isScrapOutlet = SCRAP_OUTLETS.indexOf(row.dataset.outlet) !== -1;
    var hide = onlyScrap && !isScrapOutlet;
    row.classList.toggle("is-hidden-by-filter", hide);
    if (!hide) visible++;
  }});
  var statCount = document.getElementById("stat-count");
  if (statCount) statCount.textContent = visible + "건";
  var statScope = document.getElementById("stat-scope");
  if (statScope) statScope.textContent = onlyScrap ? "스크랩 언론사만" : "전체 언론사";
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
</script>
</body>
</html>
"""


def _theme() -> dict:
    return {
        "font_stack": FONT_STACK,
        "bg": COLOR_BG,
        "card": COLOR_CARD,
        "header": COLOR_HEADER,
        "accent": COLOR_ACCENT,
        "text": COLOR_TEXT,
        "muted": COLOR_TEXT_MUTED,
        "border": COLOR_BORDER,
        "hover": COLOR_LIVE_BG,
        "error": COLOR_ERROR,
        # [추가: 2026-07-27] "스크랩됨" 표시 전용 색 — 이 화면에서만 쓰는 상태 표시라
        # app.config의 공용 COLOR_* 팔레트에는 넣지 않고 여기서만 상수로 둔다.
        "scrap_green": "#15803D",
        "scrap_green_bg": "#F0FDF4",
        # [수정: 2026-07-27] "📌 담아둠"(임시보드) 표시 전용 색 — 처음엔 호박색이었는데,
        # "스크랩됨"(초록)보다 더 튀어 보여 위계가 뒤바뀌는 느낌이 있었다. "아직 결정
        # 안 됨"이라는 의미에 맞는 차분한 회색으로 바꿨다.
        "pin_gray": "#9AA4B2",
        "pin_gray_bg": "#F3F5F7",
        # [추가: 2026-07-29] "🤖 자동 선별"(초안에 시스템이 알아서 넣었을 근사치) 표시 전용
        # 색 — 스크랩됨(초록)·담아둠(회색)·읽음(보라, app.renderer)과 안 겹치는 청록 계열.
        "auto_teal": "#0D9488",
        "auto_teal_bg": "#F0FDFA",
    }


def _highlight_words_json(highlight_words: list) -> str:
    """하단바 🖍️ 팝오버에 실어 보낼 형광펜 단어 목록 — app.renderer/app.preview_renderer와
    동일한 형식({"word":..., "color_hex":...})."""
    return json.dumps(
        [
            {"word": item["word"], "color_hex": HIGHLIGHT_COLORS[item.get("color", 0) % len(HIGHLIGHT_COLORS)]}
            for item in highlight_words
        ],
        ensure_ascii=False,
    ).replace("</", "<\\/")


def _home_href() -> str:
    return f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/home.html"


def _scrap_href() -> str:
    return f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/index.html"


def _preview_href() -> str:
    return f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/preview.html"


def _hidden_href() -> str:
    return f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/hidden"


def _scrapped_urls() -> set:
    """오늘 이미 정식 회차(자동 정기 스크랩)에 실제로 들어간 기사 URL을 모은다.

    [수정: 2026-07-27] 예전엔 "직접 추가한 기사"(manual_articles.json)까지 여기 합쳐서
    똑같이 "✓ 스크랩됨"(초록)으로 표시했는데, 그러면 "임시보드에 담아두기만 한 것"과
    "정식 스크랩 결과에 실제로 포함된 것"이 화면에서 구분이 안 됐다 — "스크랩됨"이라는
    말의 의미가 두 가지로 겹쳐 헷갈린다는 피드백에 따라 분리했다(_pinned_urls 참고).
    """
    return {a["url"] for run in load_today_runs() for a in run["articles"]}


def _pinned_urls() -> set:
    """오늘 "📌"으로 "직접 추가한 기사"(임시보드, app.manual_articles)에 담아둔 기사 URL을 모은다.

    아직 정식 스크랩 결과에 들어간 게 아니라(승격 전) 별도의 임시 목록에 있을 뿐이므로
    _scrapped_urls()와는 다른 상태로 표시한다(app.live_renderer._render_row).
    """
    return {a["url"] for a in load_manual_articles()}


def _auto_drafted_urls(articles: list, settings: dict) -> set:
    """지금 [초안]을 열면 컴퓨터가 자동으로 담았을 것으로 보이는 기사 URL을 근사치로 추정한다.

    정확히 확인하려면 app.preview_renderer._compute_preview_articles와 똑같이 네이버를
    다시 검색해야 하는데, 실시간 현황을 열 때마다 그 검색을 한 번 더 돌리면 네이버 API
    호출이 두 배로 늘어난다(느려짐). 대신 실시간 현황이 이미 받아온 데이터(제목·요약·
    언론사·게시시각)와 설정값만으로 초안 파이프라인의 필터 규칙을 그대로 흉내낸다.

    근사치라 정확히 일치하지 않을 수 있는 부분: 그룹의 AND 모드(그룹 안 키워드를 전부
    포함해야 함)는 재현하지 않고 "스크랩 포함 그룹의 키워드 중 하나라도 제목·요약에
    있으면 매치"로 단순화했고, 완전 동일 제목 기준 중복 제거(deduplicate_by_title)도
    다른 기사와의 비교가 필요해 생략했다 — 실제 초안 화면과 100% 같지 않을 수 있다는
    전제로, "컴퓨터가 이미 선별해서 가져갔겠구나"를 미리 가늠하는 용도로만 쓴다.
    """
    slot = next_pending_slot(datetime.now(), active_schedule_times(settings))
    if slot is None:
        return set()
    slot_start = kst_today_at(slot["start"])

    scrap_groups = active_search_groups(
        settings, [g for g in settings.get("keyword_groups", []) if g.get("include_in_scrap")]
    )
    scrap_keywords = []
    seen_keywords = set()
    for group in scrap_groups:
        for keyword in group["keywords"]:
            lowered = keyword.lower()
            if lowered not in seen_keywords:
                seen_keywords.add(lowered)
                scrap_keywords.append(lowered)
    if not scrap_keywords:
        return set()

    outlet_order = settings.get("outlet_order", [])
    include_photo = settings.get("include_photo_in_scrap", False)
    include_personnel = settings.get("include_personnel_in_scrap", False)

    result = set()
    for article in articles:
        pub_date = article.get("pub_date")
        if not pub_date:
            continue
        try:
            parsed = datetime.fromisoformat(pub_date)
        except ValueError:
            continue
        if parsed <= slot_start:
            continue
        if outlet_order and article["outlet"] not in outlet_order:
            continue
        if not include_photo and not exclude_photo_articles([article]):
            continue
        if not include_personnel and not exclude_personnel_articles([article]):
            continue
        haystack = (article["title"] + " " + article.get("summary", "")).lower()
        if not any(keyword in haystack for keyword in scrap_keywords):
            continue
        result.add(article["url"])
    return result


def _render_row(
    article: dict,
    can_add: bool,
    highlight_words: list,
    scrapped_urls: set,
    hidden_urls: set,
    pinned_urls: set,
    auto_drafted_urls: set,
) -> str:
    """기사 한 줄을 렌더링한다.

    [추가: 2026-07-26] 제목을 <details>/<summary>로 감싸 클릭하면 요약이 펼쳐지도록
    했다(자바스크립트 불필요, 스크랩 화면 app.renderer.render_article과 같은 방식).
    요약 텍스트는 애초에 "→ 스크랩" 버튼에 넘기려고 이미 읽어오던 값이라(data-summary),
    추가 네이버 API 호출 없이 그대로 화면에 보여주기만 하면 된다. 제목·요약 둘 다
    형광펜 단어 하이라이트를 적용해 스크랩 화면과 동일한 시각 언어를 유지한다.

    [추가: 2026-07-27] 이미 스크랩된(자동이든 수동이든) 기사는 회색으로 흐리게
    표시하고 버튼도 눌러도 소용없는 "✓ 스크랩됨" 상태로 보여준다 — 페이지를
    새로고침해도 유지된다(수동 추가 직후의 "✓ 추가됨" 표시는 이 클릭의 그 순간에만
    보이던 임시 상태였는데, 이제 저장된 데이터를 기준으로 매번 다시 판단하므로
    새로고침에도 그대로 유지된다).

    [추가: 2026-07-27] 스크랩(초안·완성본 어느 쪽이든)에 있다가 🗑️로 숨겨진 기사는
    "✓ 스크랩됨"과 구분해 "🗑️ 숨김"으로 보여준다 — 원본은 실제로 지워지지 않고
    hidden_articles.json에 URL만 별도로 기록될 뿐이라(app.curation.hide_article),
    실시간 현황에서도 "삭제된 게 아니라 숨김 처리됐을 뿐"이라는 걸 그대로 드러낸다.
    같은 기사가 scrapped_urls에도 있을 수 있는데(숨기기 전엔 스크랩돼 있었을 것이므로),
    이 경우 "숨김" 쪽이 더 최신 상태라 우선한다.

    [추가: 2026-07-27] "📌 직접 추가한 기사"(임시보드)에만 담겨 있고 아직 정식 스크랩에는
    안 들어간 기사는 "✓ 스크랩됨"(초록)이 아니라 "📌 담아둠"(노란색 계열)으로 따로
    보여준다 — "스크랩됨"이라는 말이 "임시보드에 담아만 둠"과 "정식 결과에 실제로
    포함됨" 두 가지 뜻으로 겹쳐 보인다는 피드백에 따른 구분이다.

    [추가: 2026-07-29] 담아둔 적도 없고 정식 스크랩에도 없지만, 지금 [초안]을 열면
    컴퓨터가 검색 조건에 맞춰 자동으로 담았을 것으로 보이는 기사는(_auto_drafted_urls,
    근사치) 기본 "📌" 버튼 대신 "🤖 자동 선별"로 보여주고 눌러도 소용없게 막는다 —
    "내가 직접 안 담아도 컴퓨터가 이미 가져갔구나"를 실시간 현황에서 바로 알 수 있게
    하기 위해서다. 우선순위는 숨김 > 정식 스크랩됨 > 임시보드에 담아둠(사용자가 직접
    한 행동이라 더 우선) > 자동 선별(추정) > 평범.
    """
    is_hidden = article["url"] in hidden_urls
    is_scrapped = article["url"] in scrapped_urls
    is_pinned = article["url"] in pinned_urls
    is_auto_drafted = article["url"] in auto_drafted_urls
    outlet = html.escape(article["outlet"])
    # [추가: 2026-07-29] 화면에 보이는 이름(outlet_display)만 outlet_display_label을 거친다
    # (매일경제만 빨간 글자로) — outlet(위)은 원본 그대로 둬서 언론사 필터 JS(SCRAP_OUTLETS
    # 비교)와 "→ 스크랩" 버튼 파라미터가 그대로 정확히 매칭되게 한다.
    # [수정: 2026-07-30] outlet_display_label이 이미 이스케이프된 HTML을 돌려주므로
    # 여기서 다시 html.escape()하지 않는다.
    outlet_display = outlet_display_label(article["outlet"])
    raw_title = article["title"]
    title_html = highlight_keywords(raw_title, highlight_words)
    title_attr_escaped = html.escape(raw_title)
    url = html.escape(article["url"])
    raw_summary = article.get("summary", "")
    summary_html = highlight_keywords(raw_summary, highlight_words)
    summary_attr = html.escape(raw_summary)
    # [추가: 2026-07-29] 담아둠/숨김 상태의 버튼은 눌러도 소용없게 막는(disabled) 대신,
    # 각자 자기가 한 일만 되돌리는 토글로 만든다 — 담아둠 재클릭 -> 담아두기 취소(평범
    # 상태로), 숨김 재클릭 -> 숨기기 취소(unhide만, pin 여부는 안 건드림). 두 상태가 서로
    # 독립적이라 "숨김이었다가 되돌리면" 원래 담아둠 상태였으면 담아둠으로, 아니었으면
    # 평범 상태로 돌아간다(둘 다 새로고침으로 서버가 다시 정확히 판단하게 한다 — JS에서
    # 우선순위 로직을 따로 재현하지 않기 위해).
    btn_onclick = "addToScrap(this)"
    if is_hidden:
        row_class = "live-row is-hidden"
        disabled = ""
        title_attr = "숨긴 기사입니다 — 눌러서 되돌릴 수 있어요(숨긴 기사 관리에서도 가능)"
        btn_class = "add-btn added"
        btn_label = "🗑️ 숨김"
        btn_onclick = "unhideFromLive(this)"
    elif is_scrapped:
        row_class = "live-row is-scrapped"
        disabled = " disabled"
        title_attr = "이미 스크랩된 기사입니다"
        btn_class = "add-btn added is-scrapped-btn"
        btn_label = "✓ 스크랩됨"
    elif is_pinned:
        row_class = "live-row is-pinned"
        disabled = ""
        title_attr = "직접 추가한 기사(임시보드)에 담겨 있어요 — 눌러서 담아두기를 취소할 수 있어요"
        btn_class = "add-btn added is-pinned-btn"
        btn_label = "📌 담아둠"
        btn_onclick = "unpinFromLive(this)"
    elif is_auto_drafted:
        row_class = "live-row is-auto-drafted"
        disabled = " disabled"
        title_attr = "컴퓨터가 검색 조건에 맞춰 [초안]에 이미 담았을 것으로 보여요 (근사치 추정)"
        btn_class = "add-btn added is-auto-drafted-btn"
        btn_label = "🤖 자동 선별"
    else:
        row_class = "live-row"
        disabled = "" if can_add else " disabled"
        title_attr = "스크랩에 추가" if can_add else "아직 오늘 첫 회차가 없어요"
        btn_class = "add-btn"
        btn_label = "📌"
    pub_date = article.get("pub_date")
    pub_time_html = ""
    if pub_date:
        try:
            parsed = datetime.fromisoformat(pub_date)
        except ValueError:
            parsed = None
        if parsed:
            escaped_pub_date = html.escape(pub_date)
            pub_time_html = (
                f'<span class="pub-time">{parsed.strftime("%H:%M")} · '
                f'<span class="pub-time-relative" data-pub-date="{escaped_pub_date}"></span></span>'
            )
    # [수정: 2026-07-29] 이미 숨겨진 행(is_hidden)만 빼고, 나머지 모든 상태(평범·담아둠·
    # 스크랩됨·자동 선별)에 전부 작은 숨기기 아이콘을 보여준다 — 처음엔 담아둠 상태에만
    # 뒀는데, 애초에 담아두지도 않은(평범 상태) 기사도 초안까지 안 가고 바로 숨기고
    # 싶다는 요청(원래 이 기능을 요청한 이유 자체가 "초안까지 안 넘어가도 되게"였다).
    hide_icon_html = ""
    if not is_hidden:
        hide_icon_html = (
            f'<button class="hide-from-live-btn" type="button" data-outlet="{outlet}" '
            f'data-title="{title_attr_escaped}" data-url="{url}" '
            f'data-pub-date="{html.escape(pub_date or "")}" '
            'onclick="hideFromLive(this)" title="이 기사 숨기기 (되돌리기 가능)">🗑️</button>'
        )
    # [추가: 2026-08-05] app.renderer.render_article과 동일 — 원문에서 다시 가져오기.
    refetch_btn_html = (
        f'<button class="refetch-btn" type="button" data-url="{url}" '
        'onclick="refetchSummary(this)" title="원문에서 다시 가져오기">🔄</button>'
    )
    # [추가: 2026-08-05] app.renderer.render_article과 동일 — ✏️ 직접 수정(🔄가 원문에서도
    # 못 찾는 경우의 최후 수단).
    edit_btn_html = (
        f'<button class="refetch-btn" type="button" data-url="{url}" '
        f'data-title="{title_attr_escaped}" data-summary="{summary_attr}" '
        'onclick="editSummary(this)" title="제목·요약 직접 수정">✏️</button>'
    )
    return (
        f'<div class="{row_class}" data-outlet="{outlet}">'
        f'<div class="live-row-text"><span class="outlet-tag">{outlet_display}</span>{pub_time_html}'
        '<details class="live-title">'
        f"<summary>{title_html}</summary>"
        f'<p class="live-summary">{summary_html}</p>'
        "</details>"
        f'<div class="live-url"><a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a></div></div>'
        '<div class="live-row-actions">'
        f'<button class="{btn_class}" type="button" data-outlet="{outlet}" data-title="{title_attr_escaped}" '
        f'data-url="{url}" data-summary="{summary_attr}" data-pub-date="{html.escape(pub_date or "")}" '
        f'onclick="{btn_onclick}"{disabled} title="{title_attr}">{btn_label}</button>'
        f"{refetch_btn_html}{edit_btn_html}{hide_icon_html}"
        "</div>"
        "</div>"
    )


def render_live_page(
    articles: list,
    can_add: bool,
    generated_at: str,
    highlight_words: Optional[list] = None,
    outlet_order: Optional[list] = None,
) -> str:
    """실시간 기사 현황 화면을 렌더링한다. 소제목 분류는 없지만(예정된 회차만의 기능),
    [수정: 2026-07-26] 제목 클릭 시 요약이 펼쳐지고 형광펜 하이라이트가 적용되는 건
    스크랩 화면과 동일하다 — 큐레이션(숨기기/순서변경)은 여전히 없다("스크랩"으로
    옮긴 뒤에만 가능).

    [추가: 2026-07-26] "☑️ 스크랩 언론사만 보기" 체크박스 — 언론사가 너무 다양하게 섞여
    노이즈가 심하다는 피드백으로 추가했다. 실제 검색 범위(전체 언론사 라이브 미러)는
    그대로 두고, 이미 불러온 결과를 화면에서만 /outlets에 등록된 언론사 기준으로
    걸러 보여준다(서버 재검색 없음, 저장 불필요, 새로고침하면 꺼진 상태로 리셋) —
    정기 스크랩처럼 수집 자체를 좁히면 두 화면의 기능이 겹쳐버리기 때문에 화면
    필터로만 구현한다.

    [수정: 2026-07-28] 제목 중복 제외·포토/현장 기사 제외 옵션은 없앴다 — 이 화면은
    "네이버에 지금 실제로 떠 있는 걸 날것 그대로 보여주는" 실시간 미러가 목적이라,
    그런 선별/가공은 스크랩 초안·완성본 단계에서 하는 게 맞다는 판단(사용자 피드백:
    "실시간은 실시간이어야 되거든"). generate_live_page도 더 이상 이 두 필터를
    적용하지 않는다.

    [추가: 2026-07-29] "🤖 자동 선별" 표시 — 컴퓨터가 검색 조건에 맞춰 지금 [초안]에
    이미 담았을 것으로 보이는 기사를 근사치로 추정해 표시한다(_auto_drafted_urls).
    """
    highlight_words = highlight_words if highlight_words is not None else []
    outlet_order = outlet_order if outlet_order is not None else []
    if not articles:
        rows_html = '<p class="empty">💤</p>'
    else:
        scrapped_urls = _scrapped_urls()
        hidden_urls = load_hidden_urls()
        pinned_urls = _pinned_urls()
        auto_drafted_urls = _auto_drafted_urls(articles, load_settings())
        rows_html = "\n".join(
            _render_row(a, can_add, highlight_words, scrapped_urls, hidden_urls, pinned_urls, auto_drafted_urls)
            for a in articles
        )

    filter_disabled = "" if outlet_order else " disabled"
    filter_title_attr = (
        "" if outlet_order else ' title="아직 스크랩 언론사가 지정되지 않았어요 (설정에서 선택하면 활성화됩니다)"'
    )
    body = (
        f'<div class="filter-row"><label{filter_title_attr}>'
        f'<input type="checkbox" id="scrap-outlet-only" onchange="applyOutletFilter()"{filter_disabled}>'
        "☑️ 선택 언론사만 보기</label></div>"
        f'<div class="stat-card"><span class="stat-num" id="stat-count">{len(articles)}건</span>'
        f'<span class="stat-label">당일 0시 이후 ~ 현재({generated_at}) 누적, '
        f'<span id="stat-scope">전체 언론사</span></span></div>'
        f"{rows_html}"
    )
    return _PAGE_TEMPLATE.format(
        **_theme(),
        body=body,
        home_href=_home_href(),
        scrap_href=_scrap_href(),
        preview_href=_preview_href(),
        hidden_href=_hidden_href(),
        settings_host=SETTINGS_SERVER_HOST,
        settings_port=SETTINGS_SERVER_PORT,
        outlet_order_json=json.dumps(outlet_order, ensure_ascii=False),
        highlight_words_json=_highlight_words_json(highlight_words),
    )


def render_live_error_page() -> str:
    body = (
        '<div class="error-box">🔌 지금은 조회할 수 없어요.<br>'
        "잠시 후 새로고침 버튼을 다시 눌러주세요.</div>"
    )
    return _PAGE_TEMPLATE.format(
        **_theme(),
        body=body,
        home_href=_home_href(),
        scrap_href=_scrap_href(),
        preview_href=_preview_href(),
        hidden_href=_hidden_href(),
        settings_host=SETTINGS_SERVER_HOST,
        settings_port=SETTINGS_SERVER_PORT,
        outlet_order_json="[]",
        highlight_words_json=_highlight_words_json(load_settings().get("highlight_keywords", [])),
    )


def _live_keywords_signature(groups: list) -> list:
    """그룹별 keywords+mode 구성을 캐시 유효성 비교용으로 뽑아낸다.

    사용자가 검색 키워드 설정을 고치면(그룹 추가/삭제, 키워드 on-off, OR/AND 변경 등)
    이 값이 바뀌므로 캐시가 자동으로 무효화된다(_load_live_search_base 참고) — 옛
    키워드 기준으로 모아둔 캐시를 새 설정에 그대로 쓰면 안 되기 때문.
    """
    return [{"keywords": group["keywords"], "mode": group.get("mode", "OR")} for group in groups]


def _load_live_search_base(groups: list) -> tuple:
    """캐시가 유효하면(오늘 날짜 + 키워드 구성 동일) 캐시된 기사와 "마지막으로 본 시각"을,
    아니면(캐시 없음/날짜 바뀜/키워드 바뀜) 빈 목록과 당일 0시를 돌려준다.

    (cached_articles, after_dt) 튜플 — after_dt는 search_articles_by_groups에 그대로
    넘겨 "이 시각 이후 것만" 검색하게 한다(app.naver_api._search_one_keyword, after는
    제외·before는 포함 — 이미 캐시에 있는 경계 기사가 중복으로 다시 잡히지 않는다).
    """
    signature = _live_keywords_signature(groups)
    cache = load_live_cache()
    if cache is not None and cache.get("keywords_signature") == signature:
        last_seen = cache.get("last_seen_pub_date")
        if last_seen:
            return cache.get("articles", []), datetime.fromisoformat(last_seen)
    return [], kst_today_at("00:00")


def generate_live_page() -> Path:
    """요청이 올 때마다 등록된 키워드 그룹으로 라이브 재검색해 live.html을 새로 만든다.
    예정된 회차 수집(app.scraper.collect_run)과는 완전히 별개 파이프라인 — 화면
    렌더링 결과 외엔 저장하지 않는다(다만 검색 결과 자체는 app.live_cache가 증분
    캐시로 남긴다, 아래 참고).

    - 언론사 화이트리스트는 무시한다(전체 언론사) — "지금 뭐가 떠 있는지" 파악이
      목적이라, 공식 리포트용 큐레이션(설정의 언론사 선택)과는 무관하다.
    - [수정: 2026-07-27] 숨긴 기사를 목록에서 걸러내지 않는다 — 예전엔(filter_hidden)
      스크랩 초안·완성본에서 🗑️로 숨긴 기사가 실시간 현황에서도 통째로 사라졌는데,
      "실시간 현황은 원본을 그대로 비추는 화면이라 삭제되면 안 된다"는 요청에 따라
      바꿨다. 대신 _render_row가 hidden_urls로 "🗑️ 숨김" 상태를 표시만 한다 —
      실제로는 지워진 적이 없다는 사실이 화면에도 그대로 드러난다.
    - [수정: 2026-07-28] 완전 동일 제목 중복 제거·포토/현장 기사 제외도 더 이상
      적용하지 않는다 — 예전엔 설정 화면에서 독립적으로 켜고 끌 수 있었는데(기본은
      꺼짐), "실시간 현황은 날것 그대로 보여주는 게 맞고, 선별은 초안·완성본 단계의
      일"이라는 판단으로 아예 없앴다. 지금은 네이버가 돌려주는 결과를 그대로 보여준다.
    - 체크 해제(꺼짐)로 표시해둔 키워드는 여기서도 제외한다(app.settings.active_search_groups).

    [추가: 2026-07-29] 증분 캐시 — 새로고침마다 당일 0시부터 매번 다시 검색하면 하루가
    지날수록(모을 기사가 늘수록) 점점 느려지고, 어차피 직전 새로고침 때 이미 받아온
    기사를 거의 그대로 또 받아오는 낭비였다. app.live_cache에 "지난번에 어디까지
    봤는지"(last_seen_pub_date)와 그때까지의 기사 목록을 저장해뒀다가, 이번엔 그
    이후 것만 추가로 검색해 이어붙인다 — 키워드 설정이 바뀌었거나 자정이 지났으면
    캐시를 버리고 당일 0시부터 다시 시작한다(_load_live_search_base). 검색이
    실패해도 캐시는 그대로 두고(성공했을 때만 덮어쓴다), 캐시가 있으면 이번 요청은
    실패해도 마지막으로 성공했던 결과라도 보여준다(완전히 빈 오류 화면보다 낫다).
    """
    settings = load_settings()
    groups = active_search_groups(settings)
    signature = _live_keywords_signature(groups)
    cached_articles, after_dt = _load_live_search_base(groups)

    try:
        new_articles = search_articles_by_groups(groups, after=after_dt, before=None)
    except requests.exceptions.RequestException:
        if not cached_articles:
            html_text = render_live_error_page()
            atomic_write_text(LIVE_HTML_PATH, html_text)
            return LIVE_HTML_PATH
        articles = cached_articles
    else:
        existing_urls = {a["url"] for a in cached_articles}
        articles = cached_articles + [a for a in new_articles if a["url"] not in existing_urls]
        pub_dates = [datetime.fromisoformat(a["pub_date"]) for a in articles if a.get("pub_date")]
        newest = max(pub_dates) if pub_dates else after_dt
        save_live_cache(signature, newest.isoformat(), articles)

    # [수정: 2026-07-27] filter_hidden 제거 — 위 docstring 참고. 숨긴 기사는 이제
    # 목록에서 안 빠지고 _render_row가 "🗑️ 숨김"으로 표시만 한다.
    # [수정: 2026-07-25] 언론사 우선순위 대신 최신순(네이버 뉴스처럼) — pub_date는
    # app.naver_api._search_one_keyword가 KST로 통일해 isoformat() 문자열로 채워주므로,
    # 그대로 문자열 비교만 해도 시간 순서와 일치한다(굳이 다시 파싱할 필요 없음).
    articles = sorted(articles, key=lambda a: a.get("pub_date") or "", reverse=True)
    articles = apply_summary_overrides(articles)

    can_add = load_latest_run() is not None
    html_text = render_live_page(
        articles,
        can_add,
        datetime.now().strftime("%H:%M"),
        settings.get("highlight_keywords", []),
        settings.get("outlet_order", []),
    )
    atomic_write_text(LIVE_HTML_PATH, html_text)
    return LIVE_HTML_PATH
