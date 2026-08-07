# media_report(별도 프로젝트, 언론동향 보고서 앱)로 넘겨줄 "완성본" export 계약.
#
# media_report는 이 파일이 만든 JSON만 보고 동작해야 한다 — data/articles/*.json 같은
# my_app 내부 저장 포맷을 직접 읽으면 my_app 리팩터링 때마다 media_report가 같이 깨진다.
# 그래서 이 모듈은 render_page/build_latest_plain_text와 같은 방식으로 최신 회차를
# "이미 다 반영된" 상태(숨김·순서·소제목·수동수정 전부 적용됨)로 완전히 풀어서 내보낸다.
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.atomic_write import atomic_write_text
from app.classifier import classify_articles
from app.config import DATA_DIR
from app.curation import display_group_name, filter_hidden, load_group_labels, load_group_overrides
from app.custom_groups import load_custom_groups
from app.group_order import apply_group_order
from app.manual_keyword_note import load_manual_keyword_note
from app.renderer import format_slot_time_kr
from app.settings import all_search_keywords, load_settings
from app.storage import is_today, load_latest_run
from app.summarizer import extract_keywords, summarize_groups
from app.summary_overrides import apply_summary_overrides

EXPORT_SCHEMA_VERSION = 1
EXPORT_DIR = DATA_DIR / "export"


def _export_file_path(date_str: str, run_slot: str) -> Path:
    safe_slot = run_slot.replace(":", "-")
    return EXPORT_DIR / f"{date_str}_{safe_slot}.json"


def build_export_data(run: dict) -> dict:
    """저장된 회차 데이터(run)를 media_report용 JSON 구조로 변환한다.

    index.html(완성본)을 만들 때와 동일한 순서로 숨김/수동수정/그룹핑/소제목순서를
    적용한다 — "화면에 보이는 것"과 "내보내진 것"이 항상 같아야 하기 때문이다
    (app.renderer.generate_screen과 같은 순서).
    """
    settings = load_settings()
    keywords = all_search_keywords(settings)
    highlight_words = settings["highlight_keywords"]
    line_template = settings["article_line_template"]

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

    subheadings = [
        {
            "name": display_group_name(g["name"], labels),
            "articles": [
                # outlet_display_label()은 HTML <span> 조각(매일경제/매경이코노미 구분
                # 불가 표식, app.naver_api 참고)을 돌려주는 화면 전용 함수라 여기 넣지
                # 않는다 — my_app의 복사/내보내기 텍스트도 같은 이유로 원본 outlet만 쓴다.
                {
                    "outlet": a["outlet"],
                    "title": a["title"],
                    "summary": a.get("summary", ""),
                    "url": a["url"],
                    "pub_date": a.get("pub_date"),
                }
                for a in g["articles"]
            ],
        }
        for g in groups
    ]

    date_str = run["run_at"][:10]
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "date": date_str,
        "run_slot": run["run_slot"],
        "header_label": f"언론 모니터링 {format_slot_time_kr(run['run_slot'])} 기준",
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "manual_keyword_note": load_manual_keyword_note((run["run_at"][:10], run["run_slot"])),
        "subheadings": subheadings,
        # 참고용 — 규칙기반 결과라 media_report의 LLM 요약/키워드를 대체하지 않는다.
        # 프롬프트에 힌트로 넣고 싶을 때만 참고.
        "rule_based_keywords": extract_keywords(articles, keywords) if articles else [],
        "rule_based_summaries": (
            [{"subheading": display_group_name(s["name"], labels), "summary": s["summary"]} for s in summarize_groups(groups)]
            if groups
            else []
        ),
    }


def export_latest_run() -> Optional[Path]:
    """오늘 최신 회차를 media_report용 JSON으로 내보낸다.

    같은 회차(같은 date+run_slot)를 다시 내보내면 덮어쓴다 — 담당자가 추가로 큐레이션한
    뒤 다시 내보내는 경우를 정상 흐름으로 취급하기 위해서다(버전 관리는 media_report
    쪽에서 필요하면 자체적으로 한다).
    """
    run = load_latest_run()
    if run is None or not is_today(run):
        return None
    data = build_export_data(run)
    EXPORT_DIR.mkdir(exist_ok=True)
    path = _export_file_path(data["date"], data["run_slot"])
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
    return path
