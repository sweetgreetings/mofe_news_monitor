# Design Ref: PRD.md 기능11 "데이터 내보내기" — 보관된 기사를 표 형태 파일로 뽑아
# 담당자가 엑셀에서 직접 가공할 수 있게 한다. 기존 복사/.txt 내보내기(보고서 문장 형식)와
# 달리 이쪽은 "데이터"라 열 구성이 고정된 표다.
#
# 5곳(확정본·정기 보관함·수집 확정본·수시 보관함·라벨 보관함)이 전부 이 모듈 하나를 공유한다
# — 화면마다 다른 건 "rows를 어떻게 모으는가"뿐이고, "rows를 엑셀로 만드는 방법"은 하나다.
import re
from datetime import datetime
from io import BytesIO
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

# PRD.md 기능11 규칙3 — 열 구성 8개, 이 순서 그대로.
COLUMNS = (
    "스크랩일자",
    "스크랩종료시간",
    "발행일 발행시간",
    "언론사명",
    "기사제목",
    "URL",
    "소제목",
    "라벨명",
)

# 열 너비는 내용 길이에 비례해 대충 맞춘다(자동 맞춤은 openpyxl이 못 하므로) — 정확할
# 필요는 없고, 담당자가 처음 열었을 때 잘려서 안 보이는 열이 없으면 충분하다.
_COLUMN_WIDTHS = {
    "스크랩일자": 12,
    "스크랩종료시간": 14,
    "발행일 발행시간": 17,
    "언론사명": 12,
    "기사제목": 60,
    "URL": 42,
    "소제목": 20,
    "라벨명": 16,
}

_HEADER_FILL = PatternFill(start_color="1E3A5F", end_color="1E3A5F", fill_type="solid")
_HEADER_FONT = Font(color="FFFFFF", bold=True)


def format_pub_datetime(pub_date: Optional[str]) -> str:
    """저장된 ISO 8601(예: "2026-08-18T16:59:00+09:00")을 "2026-08-18 16:59"로.

    실시간 화면이 include_unparsed_dates로 담아오는 pub_date=None 기사(발행시각을 못
    읽은 기사, CLAUDE.md 실시간 화면 절 참고)는 빈 칸으로 둔다 — 없는 값을 지어내지 않는다.
    """
    if not pub_date:
        return ""
    try:
        return datetime.fromisoformat(pub_date).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return pub_date  # 형식이 어긋난 값도 지어내지 않고 원본 그대로 내보낸다


def sanitize_filename_part(text: str) -> str:
    """파일명 금지 문자(`/:*?"<>|`)를 밑줄로 치환한다.

    사안명·라벨명처럼 담당자가 자유 입력한 문자열이 파일명 일부로 들어가는 자리에서
    쓴다(PRD.md 기능11 규칙7 — app.adhoc.renderer.download_filename과 같은 규칙).
    """
    return "".join(c if c not in '/:*?"<>|' else "_" for c in text)


def build_workbook_bytes(rows: list[dict]) -> bytes:
    """rows(각 항목은 COLUMNS를 키로 하는 dict)를 .xlsx 바이트로 만든다.

    빈 열은 빈 문자열로 채운다(소제목이 없는 행 등 — PRD.md 기능11 규칙4, "소제목은
    분류 도우미일 뿐 절대적 값이 아니므로 빈 값도 정상"). 헤더 행 고정(틀고정)과 자동
    필터를 걸어 1년치처럼 행이 수만 개인 경우에도 바로 훑어볼 수 있게 한다(규칙6).

    수식 오인식 방어는 따로 하지 않는다 — CSV와 달리 .xlsx는 셀 타입을 명시해 저장하므로
    "-3.2% 하락…"같은 제목이 수식으로 오인되지 않는다(PRD.md 기능11 규칙6의 채택 이유).
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "기사"

    ws.append(COLUMNS)
    for cell in ws[1]:
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL

    for row in rows:
        ws.append([row.get(col, "") for col in COLUMNS])

    for i, col in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = _COLUMN_WIDTHS[col]

    ws.freeze_panes = "A2"
    last_row = max(ws.max_row, 1)
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{last_row}"

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


_DATE_RANGE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def date_range_label(dates: list[str]) -> str:
    """날짜 문자열 목록("YYYY-MM-DD")을 파일명용 라벨로 압축한다.

    하나면 그대로, 여럿이면 "가장 오래된~가장 최근"(월-일만, PRD.md 기능11 규칙7의
    "2026-08-11~08-18_언론모니터링.xlsx" 예시 형식)으로 줄인다 — 보관함처럼 여러 날짜를
    펼쳐 한 번에 뽑을 때 쓴다.
    """
    valid = sorted(d for d in dates if _DATE_RANGE_RE.match(d))
    if not valid:
        return datetime.now().strftime("%Y-%m-%d")
    if len(valid) == 1:
        return valid[0]
    return f"{valid[0]}~{valid[-1][5:]}"
