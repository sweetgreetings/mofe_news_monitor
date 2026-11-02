#!/usr/bin/env python3
"""Supabase(Postgres)에 붙이고 지금 `data/`를 올리는 명령 — **한 번 하는 이사**다.

쓰는 법 (앱은 멈춰 둔 채로 하세요):

    python3 supabase_setup.py 확인      # 붙는지만 본다. 아무것도 안 바꾼다
    python3 supabase_setup.py 미리보기  # 무엇이 올라갈지만 센다. 아무것도 안 올린다
    python3 supabase_setup.py 올리기    # 실제로 올린다(이미 있는 키는 건드리지 않는다)
    python3 supabase_setup.py 대조      # 올린 것과 디스크의 것이 같은지 본다

연결 주소는 `.env`의 `DATABASE_URL`에서 읽는다. Supabase 프로젝트의
**Settings › Database › Connection string › URI**를 그대로 넣으면 된다
(비밀번호 자리 `[YOUR-PASSWORD]`를 실제 비밀번호로 바꿔야 한다).

`DATABASE_URL`을 넣는 순간부터 앱은 **DB를 본다** — 그래서 순서가 중요하다:
**먼저 올리고, 그다음에 앱을 재시작한다.** 거꾸로 하면 빈 DB를 보고 화면이 비어 보인다.
"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE_DIR / ".env")

from app import store_pg  # noqa: E402
from app.config import DATA_DIR  # noqa: E402


def _need_url() -> str:
    url = store_pg.database_url()
    if not url:
        print("DATABASE_URL이 .env에 없습니다.")
        print()
        print("Supabase 프로젝트의 Settings › Database › Connection string › URI를")
        print("복사해 .env에 이렇게 한 줄 넣으세요(비밀번호 자리를 실제 값으로):")
        print()
        print('  DATABASE_URL=postgresql://postgres:비밀번호@db.xxxx.supabase.co:5432/postgres')
        sys.exit(1)
    return url


def _safe(url: str) -> str:
    """화면에 보일 주소 — 비밀번호를 가린다."""
    if "@" not in url:
        return url
    head, tail = url.rsplit("@", 1)
    if ":" in head:
        head = head.rsplit(":", 1)[0] + ":••••••"
    return f"{head}@{tail}"


def cmd_check() -> None:
    url = _need_url()
    print("연결 주소:", _safe(url))
    store_pg.ensure_schema()
    rows = store_pg._run(f"select count(*) from {store_pg._TABLE}", fetch="one")
    print("붙었습니다. 표에 든 줄:", rows[0] if rows else 0, "개")


def cmd_preview() -> None:
    _need_url()
    result = store_pg.upload_from_files(DATA_DIR, dry_run=True)
    print(f"올라갈 것 {len(result['uploaded'])}개 · 건너뛸 것 {len(result['skipped'])}개")
    print()
    print("올라갈 것(앞 10개):")
    for key in result["uploaded"][:10]:
        print("  ", key)
    if len(result["uploaded"]) > 10:
        print(f"   … 외 {len(result['uploaded']) - 10}개")
    print()
    print("건너뛸 것:  로그(logs/)와 임시 파일(.tmp)")


def cmd_upload() -> None:
    _need_url()
    print("올립니다 — 이미 DB에 있는 키는 건드리지 않습니다(두 번 돌려도 안전).")
    result = store_pg.upload_from_files(DATA_DIR)
    print(f"  올림 {len(result['uploaded'])}개")
    print(f"  이미 있어서 그대로 둠 {len(result['already'])}개")
    print(f"  건너뜀(로그·임시) {len(result['skipped'])}개")
    print()
    print("이제 앱을 재시작하면 DB를 봅니다:")
    print("  launchctl kickstart -k gui/$(id -u)/local.press-monitor")


def cmd_verify() -> None:
    _need_url()
    store_pg.ensure_schema()
    same = diff = missing = 0
    bad = []
    for path in sorted(Path(DATA_DIR).rglob("*")):
        if not path.is_file():
            continue
        key = str(path.relative_to(DATA_DIR))
        if key.split("/")[0] == "logs" or path.name.endswith(".tmp"):
            continue
        try:
            body = store_pg.read_text(key)
        except FileNotFoundError:
            missing += 1
            bad.append(("DB에 없음", key))
            continue
        if body == path.read_text(encoding="utf-8"):
            same += 1
        else:
            diff += 1
            bad.append(("내용이 다름", key))
    print(f"같음 {same}개 · 다름 {diff}개 · DB에 없음 {missing}개")
    for why, key in bad[:10]:
        print(f"  {why}: {key}")
    if not bad:
        print("디스크와 DB가 같습니다.")


_COMMANDS = {
    "확인": cmd_check, "check": cmd_check,
    "미리보기": cmd_preview, "preview": cmd_preview,
    "올리기": cmd_upload, "upload": cmd_upload,
    "대조": cmd_verify, "verify": cmd_verify,
}


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in _COMMANDS:
        print(__doc__)
        sys.exit(1)
    _COMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    main()
