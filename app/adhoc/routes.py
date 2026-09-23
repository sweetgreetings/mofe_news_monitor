# Design Ref: app/adhoc/renderer.py 상단 "라우팅 계약" — 여기서는 그 계약을 그대로
# 구현만 한다. app.settings_server.SettingsHandler가 do_GET/do_POST에서 이 모듈의
# handle_get/handle_post를 부른다(연결부 두 줄 — CLAUDE.md "기존 코드에 손대야 하는
# 지점" 참고).
#
# 전부 실제 <form> 제출을 받으므로, 성공 시 handler._redirect(...)로 Post/Redirect/Get
# 패턴을 따른다(settings_server.py의 기존 _redirect 그대로 재사용 — 303, 새로고침해도
# 중복 제출되지 않는다). 실패는 카드 화면으로 되돌리며 error 쿼리로 메시지를 싣는다
# (ADHOC_DESIGN.md §6.9 — 즉시 표시, 자동 재시도 없음).
from datetime import datetime
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from app.adhoc import archive_renderer, card, renderer, undo
from app.adhoc.card import AdhocCardError
from app.adhoc.classifier import classify_card
from app.adhoc.send import AdhocSendError, send_bundle
from app.adhoc.collector import AdhocCollectError, run_collect
from app.excel_export import build_workbook_bytes
from app.label_undo import push as label_undo_push
from app.labels import attach_label, detach_label, snapshot_source
from app.naver_api import fetch_full_title_and_summary
from app.summary_overrides import set_summary_override

_NEW_ISSUE_SENTINEL = "__new__"
# 수집 원본의 「확정본으로」가 싣는 값 — 「이 원본의 확정본(없으면 새로)」을 서버가 고른다.
_AUTO_BUNDLE = "__auto__"


def _field(form: dict, name: str, default: str = "") -> str:
    return form.get(name, [default])[0]


def _query(path: str) -> dict:
    return parse_qs(urlsplit(path).query)


def _id_list(value: str) -> list:
    """주소의 `deleted=id1,id2` → [id1, id2]."""
    return [v for v in value.split(",") if v]


def _archive_redirect(handler, back: str, deleted: list, restored: str = "") -> None:
    """수시 보관함으로 되돌린다 — 보던 조회 조건(back)을 그대로 두고, 제자리 「삭제함」 줄로
    그릴 id(deleted)와 방금 되살린 id(restored)만 갈아 끼운다.

    back은 화면이 실어 보낸 자기 주소다. /adhoc 밖으로는 절대 안 보낸다(열린 리다이렉트 방어).
    """
    parts = urlsplit(back or "/adhoc")
    if parts.path != "/adhoc" or parts.netloc or parts.scheme:
        parts = urlsplit("/adhoc")
    query = {k: v for k, v in parse_qs(parts.query).items() if k not in ("deleted", "restored")}
    if deleted:
        query["deleted"] = [",".join(deleted)]
    if restored:
        query["restored"] = [restored]
    handler._redirect("/adhoc" + (f"?{urlencode(query, doseq=True)}" if query else ""))


def _card_redirect(handler, card_id: str, error: str = "", notice: str = "", notice_card: str = "") -> None:
    target = f"/adhoc/card?id={quote(card_id)}"
    if error:
        target += f"&error={quote(error)}"
    if notice:
        target += f"&notice={quote(notice)}"
    if notice and notice_card:
        target += f"&notice_card={quote(notice_card)}"
    handler._redirect(target)


# --- GET ------------------------------------------------------------------


def handle_get(handler, path: str) -> bool:
    """settings_server.py의 do_GET이 /adhoc로 시작하는 요청을 넘길 때 호출한다.
    이 모듈이 처리할 경로가 아니면 False를 돌려줘 호출부가 평소처럼(404 등) 이어가게 한다.
    """
    base = path.split("?", 1)[0]

    if base == "/adhoc":
        query = _query(path)
        handler._respond(
            archive_renderer.render_archive_page(
                q=query.get("q", [""])[0],
                start=query.get("start", [""])[0],
                end=query.get("end", [""])[0],
                preset=query.get("preset", [""])[0],
                deleted_ids=_id_list(query.get("deleted", [""])[0]),
                restored_id=query.get("restored", [""])[0],
                view=query.get("view", [""])[0],
                sort=query.get("sort", [""])[0],
            )
        )
        return True

    if base == "/adhoc/new":
        # ADHOC_DESIGN.md §6.13 — 같은 경로의 두 번째 갈래(빈 모음 만들기).
        # 별도 URL을 파지 않은 이유: 담당자에게는 "새로 만든다"는 한 가지 동작이고,
        # 무엇을 만드는지만 갈린다.
        query = _query(path)
        kind = query.get("kind", [""])[0]
        handler._respond(renderer.render_new_card_page(kind=kind, pick=query.get("from", [""])[0]))
        return True

    if base == "/adhoc/card":
        card_id = _query(path).get("id", [""])[0]
        c = card.load_card(card_id)
        if c is None:
            handler.send_response(404)
            handler.end_headers()
            return True
        query = _query(path)
        error = query.get("error", [""])[0]
        notice = query.get("notice", [""])[0]
        notice_card = query.get("notice_card", [""])[0]
        handler._respond(
            renderer.render_card_page(
                c, error=error or None, notice=notice or None, notice_card=notice_card or None
            )
        )
        return True

    if base == "/adhoc/card/download":
        card_id = _query(path).get("id", [""])[0]
        c = card.load_card(card_id)
        if c is None:
            handler.send_response(404)
            handler.end_headers()
            return True
        handler._respond_download(renderer.build_adhoc_plain_text(c), renderer.download_filename(c))
        return True

    if base == "/adhoc/card/download-excel":
        card_id = _query(path).get("id", [""])[0]
        c = card.load_card(card_id)
        if c is None:
            handler.send_response(404)
            handler.end_headers()
            return True
        handler._respond_excel_download(
            build_workbook_bytes(renderer.build_adhoc_excel_rows(c)), renderer.download_excel_filename(c)
        )
        return True

    return False


# --- POST -------------------------------------------------------------------


def handle_post(handler, path: str, form: dict) -> bool:
    """settings_server.py의 do_POST이 이미 파싱해둔 form(parse_qs 결과)을 그대로 받는다."""
    if path == "/adhoc/collect":
        _handle_collect(handler, form)
        return True
    if path == "/adhoc/card/recollect":
        _handle_recollect(handler, form)
        return True
    if path == "/adhoc/card/rename":
        _handle_rename(handler, form)
        return True
    if path == "/adhoc/card/hide":
        _handle_set_hidden(handler, form, hidden=True)
        return True
    if path == "/adhoc/card/unhide":
        _handle_set_hidden(handler, form, hidden=False)
        return True
    if path == "/adhoc/card/raw-hide":
        _handle_raw_mark(handler, form, hide=True)
        return True
    if path == "/adhoc/card/raw-unhide":
        _handle_raw_mark(handler, form, hide=False)
        return True
    if path == "/adhoc/card/raw-unsend":
        _handle_raw_unsend(handler, form)
        return True
    if path == "/adhoc/card/hide-group":
        _handle_hide_group(handler, form)
        return True
    if path == "/adhoc/card/bulk-hide":
        _handle_bulk_hide(handler, form)
        return True
    if path == "/adhoc/bundle/create":
        _handle_create_bundle(handler, form)
        return True
    if path == "/adhoc/card/send-to-bundle":
        _handle_send_to_bundle(handler, form)
        return True
    if path == "/adhoc/card/send":
        _handle_send(handler, form)
        return True
    if path == "/adhoc/card/move-article":
        _handle_move_article(handler, form)
        return True
    if path == "/adhoc/card/move-order":
        _handle_move_order(handler, form)
        return True
    if path == "/adhoc/card/move-to-bundle":
        _handle_move_to_bundle(handler, form)
        return True
    if path == "/adhoc/card/bulk-move":
        _handle_bulk_move(handler, form)
        return True
    if path == "/adhoc/card/classify":
        _handle_classify(handler, form)
        return True
    if path == "/adhoc/card/add-custom-group":
        _handle_add_custom_group(handler, form)
        return True
    if path == "/adhoc/card/remove-custom-group":
        _handle_remove_custom_group(handler, form)
        return True
    if path == "/adhoc/card/move-group-order":
        _handle_move_group_order(handler, form)
        return True
    if path == "/adhoc/card/delete":
        _handle_delete(handler, form)
        return True
    if path == "/adhoc/archive/delete":
        _handle_archive_delete(handler, form)
        return True
    if path == "/adhoc/archive/restore":
        _handle_archive_restore(handler, form)
        return True
    if path == "/adhoc/card/undo":
        _handle_undo(handler, form)
        return True
    if path == "/adhoc/card/add-label":
        _handle_add_label(handler, form)
        return True
    if path == "/adhoc/card/remove-label":
        _handle_remove_label(handler, form)
        return True
    if path == "/adhoc/card/refetch-summary":
        _handle_refetch_summary(handler, form)
        return True
    if path == "/adhoc/card/edit-summary":
        _handle_edit_summary(handler, form)
        return True
    return False


def _handle_refetch_summary(handler, form: dict) -> None:
    """⋯ 더보기 > "원문 다시 불러오기" — 그 기사 원문 페이지의 og:title/og:description으로
    제목·요약을 다시 가져와 app.summary_overrides에 저장한다(정기의 /refetch-summary와
    같은 함수·같은 저장소).

    저장소가 URL 키 전역이라 여기서 고친 제목은 정기 확정본·정기 보관함에서도 같이
    보인다 — 담기는 값이 "이 URL의 진짜 제목"이라는 사실 하나뿐이라 흐름을 가로질러
    공유하는 게 맞다(app/adhoc/* → app/* 단방향 의존 규칙에도 어긋나지 않는다:
    summary_overrides.json은 labels.json과 함께 두 흐름이 같이 쓰라고 app/ 최상위에
    둔 저장소다). 실패는 조용히 넘기지 않고 카드 화면에 오류 배너로 알린다 — 자동이
    아니라 담당자가 직접 누른 동작이기 때문이다.
    """
    card_id = _field(form, "id")
    url = _field(form, "url")
    if not url:
        _card_redirect(handler, card_id, error="기사 주소가 없어 원문을 불러올 수 없습니다.")
        return
    result = fetch_full_title_and_summary(url)
    if not result or not (result.get("title") or result.get("summary")):
        _card_redirect(
            handler,
            card_id,
            error="원문에서 제목·요약을 찾지 못했습니다. 「기사제목 직접 수정」으로 고쳐주세요.",
        )
        return
    set_summary_override(url, result.get("title"), result.get("summary"))
    _card_redirect(handler, card_id)


def _handle_edit_summary(handler, form: dict) -> None:
    """⋯ 더보기 > "기사제목 직접 수정" — 🔄가 원문에서도 못 찾을 때의 최후 수단.
    저장 형식·적용 범위는 🔄와 완전히 같다(빈 칸은 기존 값을 그대로 둔다).
    """
    card_id = _field(form, "id")
    url = _field(form, "url")
    title = _field(form, "title").strip()
    summary = _field(form, "summary").strip()
    if not url or not (title or summary):
        _card_redirect(handler, card_id, error="제목이나 요약 중 하나는 입력해주세요.")
        return
    set_summary_override(url, title or None, summary or None)
    _card_redirect(handler, card_id)


def _handle_collect(handler, form: dict) -> None:
    """원본 화면의 [수집] 버튼 — 카드 생성과 첫 수집을 한 번에 한다
    (ADHOC_DESIGN.md §5.2, "담당자가 세팅하는 건 사안·검색어·시간대뿐"이라는 원칙대로
    이 한 번의 제출로 카드가 만들어지고 바로 채워진다).
    """
    issue_id = _field(form, "issue_id")
    new_issue_name = _field(form, "new_issue_name").strip()
    keywords = form.get("keywords", [])
    # "검색어가 모두 있는 기사만" — 같은 목록을 must_keywords에도 그대로 넣는다.
    # 일부만(예: 첫 단어는 빼고) 넣으면 1,000건 상한 검사(§6.4b, must_keywords만 본다)가
    # 그 단어를 안 덮어, 잘린 검색 결과의 교집합에서 기사가 조용히 빠진다.
    match_all = _field(form, "match_mode") == "all"
    must_keywords = list(keywords) if match_all else []
    window_start = _field(form, "window_start")
    window_end = _field(form, "window_end")
    options = {
        "use_outlet_whitelist": "use_outlet_whitelist" in form,
        "exclude_photo": "exclude_photo" in form,
        "exclude_personnel": "exclude_personnel" in form,
    }

    try:
        if issue_id == _NEW_ISSUE_SENTINEL or not issue_id:
            issue = card.get_or_create_issue(new_issue_name)
        else:
            issue = card.find_issue(issue_id)
            if issue is None:
                raise AdhocCardError("사안을 찾을 수 없습니다. 새로고침 후 다시 시도해주세요.")

        new_card = card.new_card(
            issue_id=issue["id"],
            report_title=issue["name"],
            window_start=window_start,
            window_end=window_end,
            keywords=keywords,
            options=options,
            must_keywords=must_keywords,
        )
    except AdhocCardError as error:
        # 아직 카드가 안 만들어진 단계의 실패라 되돌아갈 카드가 없다 — 수집 원본 폼
        # 자체에 에러를 얹어 다시 보여준다(입력값은 비워짐: ADHOC_DESIGN.md가 요구하는
        # 30초 입력 목표상 재입력 비용이 크지 않다고 판단했다).
        handler._respond(renderer.render_new_card_page(error=str(error)), status=400)
        return

    card.update_issue_last_keywords(issue["id"], new_card["keywords"])

    try:
        run_collect(new_card["id"])
    except AdhocCollectError as error:
        # 카드는 이미 저장돼 있으므로("이미 모아둔 기사는 그대로 있습니다"가 자연히
        # 성립 — 이 시점엔 0건이지만) 카드 화면으로 보내고 에러만 얹는다.
        _card_redirect(handler, new_card["id"], error=str(error))
        return

    _card_redirect(handler, new_card["id"])


def _handle_recollect(handler, form: dict) -> None:
    card_id = _field(form, "id")
    window_start = _field(form, "window_start")
    window_end = _field(form, "window_end")
    # [추가: 2026-09-15] 헤더의 「지금까지 불러오기」 — 시작은 카드에 저장된 값 그대로,
    # 끝만 **누른 순간의 서버 시각**으로. 화면이 렌더링 때 시각을 구워 보내면 탭을 오래
    # 열어둔 뒤 누를 때 옛 시각으로 불러오게 되므로 시각은 여기서 정한다.
    to_now = _field(form, "to_now") == "1"

    with card.card_lock(card_id):
        c = card.load_card(card_id)
        if c is None:
            handler.send_response(404)
            handler.end_headers()
            return
        if to_now:
            window_start = card.window_of(c)["start"]
            window_end = datetime.now().strftime("%H:%M")
        try:
            # 창 검증은 card.py가 새 카드를 만들 때 쓰는 것과 같은 규칙(시작<끝) —
            # 카드 하나에 규칙이 두 벌 생기지 않도록 그대로 재사용한다.
            card._validate_window(window_start, window_end)
        except AdhocCardError as error:
            _card_redirect(handler, card_id, error=str(error))
            return
        c["window"] = {"start": window_start, "end": window_end}
        card.save_card(c)

    try:
        run_collect(card_id)
    except AdhocCollectError as error:
        _card_redirect(handler, card_id, error=str(error))
        return

    _card_redirect(handler, card_id)


def _handle_rename(handler, form: dict) -> None:
    card_id = _field(form, "id")
    report_title = _field(form, "report_title").strip()
    with card.card_lock(card_id):
        c = card.load_card(card_id)
        if c is None:
            handler.send_response(404)
            handler.end_headers()
            return
        if report_title:  # 빈 값으로 지우려는 시도는 조용히 무시 — 헤더가 빈칸이 되면 안 됨
            c["report_title"] = report_title
            card.save_card(c)
    _card_redirect(handler, card_id)


def _handle_set_hidden(handler, form: dict, hidden: bool) -> None:
    card_id = _field(form, "id")
    url = _field(form, "url")
    undo.push(card_id, "기사 숨기기" if hidden else "기사 숨김 해제")
    with card.card_lock(card_id):
        c = card.load_card(card_id)
        if c is None:
            handler.send_response(404)
            handler.end_headers()
            return
        for article in c["articles"]:
            if article["url"] == url:
                if hidden:
                    card.mark_hidden(article)
                else:
                    article["hidden"] = False
                    article.pop("hidden_at", None)
                break
        card.save_card(c)
    _card_redirect(handler, card_id)


def _handle_raw_mark(handler, form: dict, hide: bool) -> None:
    """[추가: 2026-09-15] 로데이터 원본의 🗑 / 「🗑 숨김」 다시 누르기 — 실시간 현황의 🗑와 같은
    동작이다: 기사를 목록에서 빼지 않고 「숨김」 표시만 붙이고 뗀다(card.set_raw_mark).

    이 카드 스택에 되돌리기를 쌓는다(표시가 이 카드 안에만 남으므로). 보낸 기사엔 🗑가
    없지만 옛 탭에서 눌릴 수 있어 서버가 한 번 더 막는다 — 보낸 기사를 빼는 곳은 확정본이다.
    """
    card_id = _field(form, "id")
    url = _field(form, "url")
    c = card.load_card(card_id)
    if c is None:
        handler.send_response(404)
        handler.end_headers()
        return
    if not card.is_raw(c) or not url:
        _card_redirect(handler, card_id)
        return
    bundles = card.bundles_for_date(c["collect_date"])
    if hide and card.raw_row_states(c, bundles).get(url, ("",))[0] == "sent":
        _card_redirect(handler, card_id)
        return
    undo.push(card_id, "기사 숨김 표시" if hide else "기사 숨김 표시 해제")
    card.set_raw_mark(card_id, url, hide, bundles)
    _card_redirect(handler, card_id)


def _handle_raw_unsend(handler, form: dict) -> None:
    """[추가: 2026-09-22] 원본의 「✓ 보냄」 다시 누르기 = 보냄 취소(card.unsend_raw).

    확정본의 사본을 숨기고 원본 행은 다시 「확정본으로」가 된다. 되돌리기는 **두 스택에**
    쌓는다(다른 사안으로 옮기기와 같은 이유): 원본 ↩는 원본 스냅샷 + 확정본 사본을 다시
    보이게 하는 흔적(undo.attach_link의 pulled), 확정본 ↩는 그 확정본 스냅샷. 어제 카드는
    다시 보낼 수 없어 막는다(자정 잠금).
    """
    card_id = _field(form, "id")
    url = _field(form, "url")
    c = card.load_card(card_id)
    if c is None:
        handler.send_response(404)
        handler.end_headers()
        return
    today = datetime.now().strftime("%Y-%m-%d")
    if not card.is_raw(c) or not url or c.get("collect_date") != today:
        _card_redirect(handler, card_id)
        return
    # 같은 사안의 다른 판에서 보낸 사본도 대상이다 — 이 판에도 「✓ 보냄」으로 보였으니까.
    ids = card.version_ids(c)
    bundles = [
        b for b in card.bundles_for_date(today)
        if any(
            a["url"] == url and not a.get("hidden")
            and (a.get("sent_from") or {}).get("card_id") in ids
            for a in b["articles"]
        )
    ]
    if not bundles:
        _card_redirect(handler, card_id)
        return
    label = "보냄 취소"
    undo.push(card_id, label)
    for b in bundles:
        undo.push(b["id"], "원본에서 보냄 취소")
    touched = card.unsend_raw(card_id, url, bundles)
    for bundle_id in touched:
        undo.attach_link(card_id, label, bundle_id, [], [], pulled=[url])
    names = "·".join(f"「{card.bundle_label(b)}」" for b in bundles if b["id"] in touched)
    _card_redirect(handler, card_id, notice=f"{names} 확정본에서 뺐어요." if names else "")


def _handle_hide_group(handler, form: dict) -> None:
    """소제목 헤더의 🗑️ — 그 소제목에서 화면에 보이는 기사를 한 번에 전부 숨긴다.
    정기 확정본·초안의 hideGroup과 같은 동작이되, 카드가 파일 하나라 기사마다 요청을
    나눌 필요 없이 한 번의 락 안에서 끝난다(app.adhoc.card.hide_group 주석 참고).

    되돌리기는 카드 스냅샷 한 장이라 ↩ 한 번으로 통째로 돌아온다 — 정기가 같은 동작을
    3초 coalesce로 억지로 한 스텝에 묶는 것과 달리 자연히 1스텝이다."""
    card_id = _field(form, "id")
    name = _field(form, "name")
    c = card.load_card(card_id)
    if c is None:
        handler.send_response(404)
        handler.end_headers()
        return
    # 숨길 게 실제로 있을 때만 되돌리기를 쌓는다 — 버튼은 보이는 기사가 있을 때만
    # 그려지지만, 다른 탭에서 먼저 숨긴 뒤 이 탭의 옛 화면에서 누르면 0건이 될 수
    # 있다. 그때 ↩에 "소제목 통째로 숨기기" 한 칸이 쌓이면 눌러도 아무 일이 안
    # 일어나는 되돌리기가 된다(2026-08-24 회차 파일 누락과 같은 종류의 빈틈).
    if not card.visible_group_urls(c, name):
        _card_redirect(handler, card_id)
        return
    undo.push(card_id, "소제목 통째로 숨기기")
    card.hide_group(card_id, name)
    _card_redirect(handler, card_id)


def _handle_bulk_hide(handler, form: dict) -> None:
    """[추가: 2026-09-15] 선택 바의 「🗑 숨기기」 — 체크한 기사를 한 번에 숨긴다
    (app.adhoc.card.hide_articles). 소제목 통째 숨기기와 같은 규칙으로, 숨길 게 실제로
    있을 때만 되돌리기를 쌓는다(옛 탭에서 눌러 0건이 되면 ↩가 빈 칸이 된다)."""
    card_id = _field(form, "id")
    urls = form.get("urls", [])
    c = card.load_card(card_id)
    if c is None:
        handler.send_response(404)
        handler.end_headers()
        return
    if not card.visible_urls_among(c, urls):
        _card_redirect(handler, card_id)
        return
    undo.push(card_id, "기사 여러 건 숨기기")
    card.hide_articles(card_id, urls)
    _card_redirect(handler, card_id)


def _handle_send(handler, form: dict) -> None:
    """[추가: 2026-09-17] 수시 확정본의 (발송) — 확인창에서 체크를 푼 사람(exclude)만 빼고
    텔레그램 명단에 보낸다(app.adhoc.send). 뺀 사람은 저장하지 않는다(이번 한 번만)."""
    card_id = _field(form, "id")
    try:
        result = send_bundle(card_id, excluded_chat_ids=set(form.get("exclude", [])))
    except AdhocSendError as error:
        _card_redirect(handler, card_id, error=str(error))
        return
    if result["failed"]:
        sent_part = f'{result["sent"]}명에게는 보냈지만, ' if result["sent"] else ""
        _card_redirect(
            handler, card_id,
            error=f'{sent_part}{", ".join(result["failed"])}에게는 보내지 못했어요 — 텔레그램 받는 사람의 chat id를 확인해주세요.',
        )
        return
    _card_redirect(handler, card_id, notice=f'텔레그램으로 {result["sent"]}명에게 보냈어요.')


def _handle_create_bundle(handler, form: dict) -> None:
    """「빈 모음 만들기」 — 입력이 이름 한 줄뿐이라 만들자마자 그 모음 화면으로 보낸다
    (ADHOC_DESIGN.md §6.13). 수집이 없으므로 _handle_collect처럼 뒤이어 할 일도 없다."""
    try:
        bundle = card.new_bundle_card(_field(form, "name"))
    except AdhocCardError as error:
        handler._respond(
            renderer.render_new_card_page(kind="bundle", error=str(error)), status=400
        )
        return
    _card_redirect(handler, bundle["id"])


def _handle_send_to_bundle(handler, form: dict) -> None:
    """원본 카드 → 모음 카드로 기사를 **복사**한다 (ADHOC_DESIGN.md §6.13).

    낱개(체크박스·행 드롭다운)와 소제목 통째 보내기가 같은 라우트를 쓴다 — 다른 것은
    "무엇을 보낼지 고르는 방법"뿐이고, 보내는 동작 자체는 완전히 같기 때문이다.
    `group`이 오면 그 소제목에서 **화면에 보이는** 기사를 서버가 직접 추린다
    (visible_group_urls — 화면이 세어 보여준 건수와 실제로 보내는 건수가 어긋나면 안 된다).

    되돌리기는 **받는 카드 스택에만** 쌓는다(규칙 5) — 복사라 원본 카드는 안 변하므로
    거기 쌓아봐야 되돌릴 것이 없다(2026-08-24 회차 파일 누락과 같은 빈틈이 된다).
    """
    card_id = _field(form, "id")
    source = card.load_card(card_id)
    if source is None:
        handler.send_response(404)
        handler.end_headers()
        return
    if card.is_bundle(source):
        # 모음 → 모음은 1차 범위 밖(§6.13 "안 만드는 것") — 화면에 버튼 자체가 없지만,
        # 열어둔 옛 탭에서 눌릴 수 있으므로 서버가 최종 방어선이다.
        _card_redirect(handler, card_id, error="확정본에서 다른 확정본으로는 보낼 수 없습니다.")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    if source.get("collect_date") != today:
        _card_redirect(
            handler,
            card_id,
            error=f"{source.get('collect_date')} 기준으로 수집한 카드입니다 (오늘은 {today}). "
            "지난 날짜의 카드에서는 확정본으로 보낼 수 없어요.",
        )
        return

    group_name = _field(form, "group")
    today_bundles = card.bundles_for_date(today)
    if _field(form, "all_open") and card.is_raw(source):
        # [추가: 2026-09-15] 원본 툴바의 「처리 안 한 N건 전부 확정본으로」 — 무엇을 보낼지는
        # 화면이 아니라 서버가 센다(소제목 통째 보내기와 같은 이유: 화면이 센 건수와 실제로
        # 보내는 건수가 어긋나면 안 된다). 보냄·숨김 표시가 붙은 기사는 빠진다.
        urls = card.open_raw_urls(source, today_bundles)
    elif group_name:
        urls = card.visible_group_urls(source, group_name)
    else:
        urls = form.get("urls", [])
    # 이미 보낸 기사는 조용히 건너뛴다(규칙 10 "다시 누르면 남은 것만") — 여기서 미리
    # 빼두면 아래 send_to_bundle의 URL 중복 제거와 이중으로 안전하다.
    already_sent = card.sent_index(card.version_ids(source), today_bundles)
    urls = [u for u in urls if u not in already_sent]
    if not urls:
        _card_redirect(handler, card_id)
        return

    basis = card.window_of(source)["end"]
    try:
        target = _field(form, "bundle")
        if target == _AUTO_BUNDLE:
            # [추가: 2026-09-15] 수집 원본의 「확정본으로」 — 같은 사안이면서 **원본 기준 시각이
            # 같은** 오늘 확정본으로 보내고, 없으면 이 순간 원본과 같은 이름·같은 사안으로
            # 만든다. 「지금까지 불러오기」를 누른 뒤 보내면 그래서 새 확정본이 생긴다
            # (card.default_bundle_for — 정기의 회차 마감과 같은 경계).
            bundle = card.default_bundle_for(source, today_bundles) or card.new_bundle_card(
                source["report_title"], issue_id=source.get("issue_id") or "", basis_time=basis,
                source_card_id=source["id"], source_version=card.version_of(source),
            )
        elif target == _NEW_ISSUE_SENTINEL:
            bundle = card.new_bundle_card(_field(form, "bundle_name"), basis_time=basis)
        else:
            bundle = card.load_card(_field(form, "bundle"))
            if bundle is None:
                raise AdhocCardError("확정본을 찾을 수 없습니다. 새로고침 후 다시 시도해주세요.")
        undo.push(bundle["id"], "확정본으로 보내기")
        card.send_to_bundle(bundle["id"], source, urls, group_name=group_name)
    except AdhocCardError as error:
        _card_redirect(handler, card_id, error=str(error))
        return
    if card.is_raw(source):
        card.clear_raw_marks(card_id, urls)
    _card_redirect(handler, card_id)


def _handle_add_label(handler, form: dict) -> None:
    """확정본 화면의 🏷 팝오버(붙은 라벨 없음 → addbox, 또는 이미 쓴 라벨 칩)가
    실제 form 제출로 호출한다. app.labels는 정기·수시를 가로지르는 전역 저장소라
    (app/labels.py 상단 설계 배경), 카드별 되돌리기(app.adhoc.undo)가 아니라 라벨
    전용 전역 스택(app.label_undo)에 쌓는다 — 라벨은 이 카드에만 속한 게 아니다."""
    card_id = _field(form, "id")
    url = _field(form, "url")
    label = _field(form, "label").strip()
    if url and label:
        c = card.load_card(card_id)
        article = {
            "url": url,
            "outlet": _field(form, "outlet"),
            "title": _field(form, "title"),
            "pub_date": _field(form, "pubDate"),
            "scrap_date": c["collect_date"] if c else "",
            "scrap_end": card.window_of(c)["end"] if c else "",
        }
        source = snapshot_source("adhoc", issue=c["report_title"] if c else "")
        label_undo_push(f'라벨 "{label}" 붙이기')
        attach_label(article, label, source, group=_field(form, "group"))
    _card_redirect(handler, card_id)


def _handle_remove_label(handler, form: dict) -> None:
    """확정본 화면의 라벨 칩 × 버튼(form 제출)이 호출한다."""
    card_id = _field(form, "id")
    url = _field(form, "url")
    label = _field(form, "label").strip()
    if url and label:
        label_undo_push(f'라벨 "{label}" 떼기')
        detach_label(url, label)
    _card_redirect(handler, card_id)


def _handle_move_article(handler, form: dict) -> None:
    card_id = _field(form, "id")
    url = _field(form, "url")
    group = _field(form, "group")
    undo.push(card_id, "소제목 이동")
    with card.card_lock(card_id):
        c = card.load_card(card_id)
        if c is None:
            handler.send_response(404)
            handler.end_headers()
            return
        for article in c["articles"]:
            if article["url"] == url:
                article["group"] = group or None
                break
        card.save_card(c)
    _card_redirect(handler, card_id)


def _handle_move_to_bundle(handler, form: dict) -> None:
    """[추가: 2026-09-17] 확정본의 「옮기기 ▾」 → 「다른 사안 확정본으로」 (행 하나 · 선택 바 여러 건).

    이동이다 — 이 확정본에선 숨겨지고 받는 확정본에 들어간다(card.move_to_bundle). 받을 수
    있는 곳은 화면 메뉴와 같은 card.move_targets(오늘 · 다른 사안)로 서버가 다시 확인한다 —
    열어둔 옛 탭에서 눌려도 같은 사안·지난 날짜로 새지 않게.

    되돌리기는 두 스택에 쌓는다: 보낸 쪽 ↩는 받는 쪽 흔적까지 거두고(undo.attach_link),
    받는 쪽 ↩는 그 확정본 안에서만 되돌린다(원본에서 보낼 때 받는 카드에 쌓는 것과 같다 —
    안 쌓으면 받는 쪽 ↩가 이 동작을 건너뛰고 그 전 동작을 되돌린다).
    """
    card_id = _field(form, "id")
    target_id = _field(form, "bundle")
    urls = form.get("urls", [])
    source = card.load_card(card_id)
    if source is None:
        handler.send_response(404)
        handler.end_headers()
        return
    today = datetime.now().strftime("%Y-%m-%d")
    if not card.is_bundle(source) or source.get("collect_date") != today:
        _card_redirect(handler, card_id, error="오늘 확정본에서만 다른 사안으로 옮길 수 있어요.")
        return
    targets = {b["id"]: b for b in card.move_targets(source, card.bundles_for_date(today))}
    target = targets.get(target_id)
    if target is None:
        _card_redirect(handler, card_id, error="옮길 확정본을 찾을 수 없어요. 새로고침 후 다시 시도해주세요.")
        return
    if not card.visible_urls_among(source, urls):
        _card_redirect(handler, card_id)
        return
    label = "다른 사안으로 옮기기"
    undo.push(card_id, label)
    undo.push(target_id, "다른 사안에서 옮겨오기")
    try:
        result = card.move_to_bundle(card_id, target_id, urls)
    except AdhocCardError as error:
        _card_redirect(handler, card_id, error=str(error))
        return
    undo.attach_link(card_id, label, target_id, result["added"], result["revived"])
    _card_redirect(
        handler, card_id,
        notice=f'{result["moved"]}건을 「{card.bundle_label(card.load_card(target_id) or target)}」 확정본으로 옮겼어요.',
        notice_card=target_id,
    )


def _handle_move_order(handler, form: dict) -> None:
    card_id = _field(form, "id")
    url = _field(form, "url")
    direction = _field(form, "direction")
    undo.push(card_id, "기사 순서 변경")
    updated = card.move_article_order(card_id, url, direction)
    if updated is None:
        handler.send_response(404)
        handler.end_headers()
        return
    _card_redirect(handler, card_id)


def _handle_bulk_move(handler, form: dict) -> None:
    card_id = _field(form, "id")
    group = _field(form, "group")
    urls = form.get("urls", [])
    if not group or not urls:
        _card_redirect(handler, card_id)
        return
    undo.push(card_id, "일괄 이동")
    updated = card.bulk_move_articles(card_id, urls, group)
    if updated is None:
        handler.send_response(404)
        handler.end_headers()
        return
    _card_redirect(handler, card_id)


def _handle_classify(handler, form: dict) -> None:
    """소제목 배정 — app.adhoc.classifier.classify_card가 카드 락까지 스스로 쥔다.
    여기서 또 잠그면 안 된다(threading.Lock은 재진입 불가 — 교착 상태가 된다).

    실패(설정 없음·API 오류·미분류 없음)해도 classifier가 카드를 원래 그대로 돌려주므로
    (조용히 규칙 기반으로 폴백하는 정기와 달리 대체 분류가 없어 "그대로 둔다"), 에러
    쿼리 없이 그냥 카드 화면으로 돌아간다 — 바뀐 게 없으면 화면도 그대로다.
    """
    card_id = _field(form, "id")
    undo.push(card_id, "AI 소제목 분류")
    updated = classify_card(card_id)
    if updated is None:
        handler.send_response(404)
        handler.end_headers()
        return
    _card_redirect(handler, card_id)


def _handle_add_custom_group(handler, form: dict) -> None:
    card_id = _field(form, "id")
    name = _field(form, "name")
    undo.push(card_id, "소제목 추가")
    try:
        card.add_custom_group(card_id, name)
    except AdhocCardError as error:
        _card_redirect(handler, card_id, error=str(error))
        return
    _card_redirect(handler, card_id)


def _handle_remove_custom_group(handler, form: dict) -> None:
    card_id = _field(form, "id")
    name = _field(form, "name")
    undo.push(card_id, "소제목 삭제")
    try:
        card.remove_custom_group(card_id, name)
    except AdhocCardError as error:
        _card_redirect(handler, card_id, error=str(error))
        return
    _card_redirect(handler, card_id)


def _handle_move_group_order(handler, form: dict) -> None:
    card_id = _field(form, "id")
    name = _field(form, "name")
    direction = _field(form, "direction")
    undo.push(card_id, "소제목 순서 변경")
    updated = card.move_group_order(card_id, name, direction)
    if updated is None:
        handler.send_response(404)
        handler.end_headers()
        return
    _card_redirect(handler, card_id)


def _handle_delete(handler, form: dict) -> None:
    card_id = _field(form, "id")
    if not card.delete_card(card_id):
        handler.send_response(404)
        handler.end_headers()
        return
    # 삭제된 카드 화면으로 되돌아갈 수 없으니 목록으로 — [수정: 2026-09-11] 그 카드가 있던
    # 자리에 「삭제함 · 되살리기」 줄이 보이도록 방금 지운 id를 실어 보낸다.
    _archive_redirect(handler, "/adhoc", [card_id])


def _handle_archive_delete(handler, form: dict) -> None:
    """수시 보관함의 🗑(하나)와 [선택 삭제](여러 개)가 같이 쓴다 — 확인창은 화면이 선택
    삭제일 때만 띄우고, 서버는 둘을 가르지 않는다. 한 요청에서 지운 것은 같은 시각(stamp)을
    붙여 「최근 삭제」에서 한 묶음으로 보이게 한다.

    앞서 지워 제자리 줄로 떠 있던 것(back의 deleted)은 그대로 이어 붙인다 — 하나 더 지웠다고
    방금 전 「삭제함」 줄이 사라지면 되살릴 자리가 옮겨가 헷갈린다.
    """
    back = _field(form, "back", "/adhoc")
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    deleted = [cid for cid in form.get("id", []) if cid and card.delete_card(cid, stamp=stamp)]
    earlier = _id_list(parse_qs(urlsplit(back).query).get("deleted", [""])[0])
    _archive_redirect(handler, back, earlier + [cid for cid in deleted if cid not in earlier])


def _handle_archive_restore(handler, form: dict) -> None:
    """제자리 「되살리기」·맨 아래 「최근 삭제」의 되살리기·모두 되살리기가 같이 쓴다."""
    back = _field(form, "back", "/adhoc")
    restored = [cid for cid in (card.restore_card(name) for name in form.get("name", [])) if cid]
    earlier = _id_list(parse_qs(urlsplit(back).query).get("deleted", [""])[0])
    _archive_redirect(
        handler, back, [cid for cid in earlier if cid not in restored], restored[0] if restored else ""
    )


def _handle_undo(handler, form: dict) -> None:
    card_id = _field(form, "id")
    undo.undo(card_id)  # 되돌릴 게 없으면 조용히 아무 일도 안 함 — 버튼이 애초에 그럴 때만 보인다
    _card_redirect(handler, card_id)
