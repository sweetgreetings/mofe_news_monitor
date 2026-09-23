# Design Ref: PRD.md 기능10(라벨) — 기사 한 건에 담당자가 직접 붙이는 자유 입력 태그.
# 소제목(회차 안에서만 의미)과 달리 회차를 가로지르므로 URL을 키로 하는 전역 저장소에
# 둔다(summary_overrides.json과 같은 이유·같은 성격). app/adhoc/* → app/* 단방향 의존
# 규칙 때문에 정기·수시 어느 쪽 패키지도 아닌 이 자리(app/ 최상위)에 둔다.
#
# 저장 형태 확정 배경(2026-08-18 목업 대화): 회차 파일이 1년 뒤 삭제돼도 라벨 붙은
# 기사는 살아남아야 한다(규칙6). "회차 파일이 있으면 안 지운다"는 대안은 그 회차의
# 나머지 수백 건까지 함께 남아 채택하지 않았고, 대신 **라벨을 붙이는 순간 기사 정보를
# 여기로 복사**해 회차 파일과 완전히 독립시켰다. 그래서 cleanup_group_overrides
# (app.curation)와 정반대 방향이다 — 그쪽은 "살아있는 기사가 아니면 지운다"이고, 여기는
# "라벨이 있으면 (원본이 사라져도) 산다". 별도의 정리 콜백이 필요 없다: 항목은 사용자가
# 명시적으로 라벨을 전부 뗄 때만 사라진다.
import json
import threading
from datetime import datetime
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import LABELS_FILE
from app.sorter import sort_by_pub_desc

# 파일 하나를 여러 스레드가 읽기-수정-쓰기 하는 구조라, app.curation._hidden_lock과
# 같은 이유로 잠근다(ThreadingHTTPServer 동시 요청 대비).
_lock = threading.Lock()

class LabelCollisionError(Exception):
    """이름 변경이 이미 있는 다른 라벨과 부딪힐 때 — app.curation의 소제목 이름 충돌
    (409 "이미 '{name}'라는 이름의 소제목이 있습니다")과 같은 성격의 거절이다.
    이름 변경은 이름만 바꾸고, 합치기는 별도 동작(merge_labels)으로 둔다 — 이름 변경이
    몰래 병합까지 하면 오타 한 글자로 있는 줄도 모르고 두 라벨이 합쳐질 수 있어서다.
    """

    def __init__(self, name: str, count: int):
        self.name = name
        self.count = count
        super().__init__(f"이미 '{name}'라는 라벨이 있습니다.")


def load_labels() -> dict:
    """{url: {labels, scrap_date, scrap_end, pub_date, outlet, title, group, source,
    labeled_at}} 전체를 읽는다."""
    if not LABELS_FILE.exists():
        return {}
    try:
        data = json.loads(LABELS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    atomic_write_text(LABELS_FILE, json.dumps(data, ensure_ascii=False, indent=2))


def labels_for_url(url: str) -> list:
    """이 기사에 붙은 라벨 이름 목록 — 없으면 빈 리스트."""
    return list(load_labels().get(url, {}).get("labels", []))


def attach_label(article: dict, label: str, source: dict, group: str = "") -> list:
    """기사 하나에 라벨을 붙인다. 이미 붙어 있으면 그대로 둔다(중복 방지).

    article: url·outlet·title·pub_date·scrap_date·scrap_end를 담은 dict(호출부가
    화면에 이미 쓰고 있는 값을 그대로 넘긴다 — 새로 조회하지 않는다).
    group: 라벨 붙이는 시점의 소제목 이름. **스냅샷은 이 최초 붙이는 시점 값으로
    고정되고, 이후 같은 기사에 라벨을 더 붙여도 갱신되지 않는다** — PRD 규칙6-1 "소제목은
    스냅샷 시점 값을 그대로 둔다"를 여러 번 붙이는 경우까지 일관되게 지키려면, 그
    "시점"을 이 항목이 저장소에 처음 생기는 순간 하나로 고정하는 게 가장 덜 헷갈린다
    (붙일 때마다 최신 소제목으로 다시 찍으면 "그때 무슨 소제목이었나"라는 원래 목적과
    어긋난다).

    반환값: 이 기사에 지금 붙어 있는 라벨 전체 목록.
    """
    label = label.strip()
    if not label:
        return labels_for_url(article["url"])
    with _lock:
        data = load_labels()
        url = article["url"]
        entry = data.get(url)
        if entry is None:
            entry = {
                "labels": [],
                "scrap_date": article.get("scrap_date", ""),
                "scrap_end": article.get("scrap_end", ""),
                "pub_date": article.get("pub_date") or "",
                "outlet": article.get("outlet", ""),
                "title": article.get("title", ""),
                "group": group,
                "source": source,
                "labeled_at": datetime.now().isoformat(timespec="seconds"),
            }
        if label not in entry["labels"]:
            entry["labels"].append(label)
            entry["labeled_at"] = datetime.now().isoformat(timespec="seconds")
        data[url] = entry
        _write(data)
        return list(entry["labels"])


def detach_label(url: str, label: str) -> list:
    """기사 하나에서 라벨 하나를 뗀다. 남은 라벨이 없으면 항목 자체를 지운다(=
    "라벨이 있으면 산다"의 반대 방향 — 없으면 더 이상 보관 예외가 아니다).

    반환값: 뗀 뒤 남은 라벨 목록(빈 리스트일 수 있음).
    """
    with _lock:
        data = load_labels()
        entry = data.get(url)
        if entry is None:
            return []
        if label in entry["labels"]:
            entry["labels"].remove(label)
        if entry["labels"]:
            data[url] = entry
        else:
            data.pop(url, None)
        _write(data)
        return list(entry["labels"]) if url in data else []


def label_stats() -> dict:
    """라벨 이름 → {count, orphan_count, last_used} — 건수 많은 순으로 이미 정렬해 돌려준다
    (라벨 보관함 칩·라벨 관리 표 둘 다 이 순서를 그대로 쓴다).

    orphan_count: 이 라벨이 그 기사의 **유일한** 라벨인 기사 수 — 라벨 관리의 삭제
    확인창이 "N건은 라벨 보관함에서도 사라지고 보관 예외가 풀립니다"를 정확한 숫자로
    보여주는 데 쓴다(추측 대신 실제로 세어서 보여준다는 원칙, CODING_CONVENTIONS §1).
    """
    data = load_labels()
    stats: dict = {}
    for entry in data.values():
        for name in entry["labels"]:
            s = stats.setdefault(name, {"count": 0, "orphan_count": 0, "last_used": ""})
            s["count"] += 1
            if len(entry["labels"]) == 1:
                s["orphan_count"] += 1
            if entry.get("labeled_at", "") > s["last_used"]:
                s["last_used"] = entry["labeled_at"]
    return dict(sorted(stats.items(), key=lambda kv: (-kv[1]["count"], kv[0])))


def articles_for_label(name: str) -> list:
    """이 라벨이 붙은 기사 스냅샷 목록. 지금은 합치기 확인창의 건수 세기(merge_labels)만
    쓴다 — 라벨 보관함 화면은 articles_for_labels_and를 쓴다. 최근 붙인 순으로 정렬한다."""
    data = load_labels()
    rows = []
    for url, entry in data.items():
        if name in entry["labels"]:
            rows.append({"url": url, **entry})
    rows.sort(key=lambda r: r.get("labeled_at", ""), reverse=True)
    return rows


def articles_for_labels_and(names: list) -> list:
    """AND 다중 선택 — names에 있는 라벨을 **전부** 가진 기사만 돌려준다.

    [결정: 2026-08-18] 라벨은 한 기사에 여러 축(주제·용도 등)이 동시에 겹쳐 붙는
    직교 태그라 OR로 넓히면 서로 무관한 결과가 섞여 실제로 쓸 데가 없다 — "세제"+
    "보고서"를 같이 고르는 이유 자체가 "보고서 쓸 때 쓰려고 찍어둔 세제 기사"를 좁혀
    보려는 것이므로 AND가 맞다. 실시간 화면의 키워드 그룹 칩(OR)과 모양은 같아도
    뜻이 다른 게 정당한 케이스 — 그룹 칩은 "이 기사가 어느 그룹에서 왔나"를 고르는
    것이고, 라벨은 한 기사에 동시에 여러 개 붙는 것이라 성질 자체가 다르다.

    [수정: 2026-09-15] 순서는 **발행 최신순**이다(예전엔 라벨 붙인 최신순) — 라벨
    보관함에 복사·txt가 생기면서, 붙인 순서 그대로 보고서에 붙으면 읽는 사람에겐
    뒤섞여 보였다. 화면·복사·txt·xlsx가 모두 이 함수 결과 순서를 그대로 쓴다.
    발행시각이 없거나 깨진 기사는 지어내지 않고 맨 뒤로 간다(app.sorter와 같은 규칙).
    """
    if not names:
        return []
    data = load_labels()
    want = set(names)
    rows = []
    for url, entry in data.items():
        if want.issubset(set(entry["labels"])):
            rows.append({"url": url, **entry})
    return sort_by_pub_desc(rows)


def rename_label(old: str, new: str) -> None:
    """라벨 이름만 바꾼다 — 병합은 하지 않는다. 이미 있는 다른 이름과 부딪히면
    LabelCollisionError를 던진다(소제목 이름 변경의 409 거절과 같은 규칙)."""
    old = old.strip()
    new = new.strip()
    if not new or old == new:
        return
    with _lock:
        data = load_labels()
        existing = {n for entry in data.values() for n in entry["labels"]}
        if new in existing:
            count = sum(1 for entry in data.values() if new in entry["labels"])
            raise LabelCollisionError(new, count)
        for entry in data.values():
            if old in entry["labels"]:
                entry["labels"] = [new if n == old else n for n in entry["labels"]]
        _write(data)


def merge_labels(src: str, dst: str) -> int:
    """src 라벨이 붙은 모든 기사에서 src를 떼고 dst를 붙인다(이미 dst가 있으면 중복 없이
    하나로 합쳐진다). src 라벨은 이 동작 뒤 완전히 사라진다.

    반환값: 합친 뒤 dst 라벨의 최종 건수.
    """
    src, dst = src.strip(), dst.strip()
    if not src or not dst or src == dst:
        return len(articles_for_label(dst))
    with _lock:
        data = load_labels()
        for entry in data.values():
            if src in entry["labels"]:
                entry["labels"].remove(src)
                if dst not in entry["labels"]:
                    entry["labels"].append(dst)
        _write(data)
        return sum(1 for entry in data.values() if dst in entry["labels"])


def delete_label(name: str) -> int:
    """이 라벨을 모든 기사에서 뗀다. 남은 라벨이 없어진 기사는 항목째 사라진다(=
    그 기사의 1년 보관 예외도 함께 풀린다 — app.labels의 존재 자체가 예외의 근거이므로).

    반환값: 라벨이 떨어진 기사 수.
    """
    name = name.strip()
    with _lock:
        data = load_labels()
        affected = 0
        for url in list(data.keys()):
            entry = data[url]
            if name in entry["labels"]:
                affected += 1
                entry["labels"].remove(name)
                if not entry["labels"]:
                    data.pop(url, None)
        _write(data)
        return affected


def snapshot_source(kind: str, issue: Optional[str] = None) -> dict:
    """8번째 필드(source) 값 — 정기는 {"kind":"regular"}, 수시는 사안명을 더한다.
    화면의 출처 태그는 수시일 때 사안명만 보여준다([결정: 2026-08-18] q1 — 색으로 이미
    정기/수시가 구분되므로 "수시 ·" 접두어는 글자만 늘린다)."""
    if kind == "adhoc":
        return {"kind": "adhoc", "issue": issue or ""}
    return {"kind": "regular"}
