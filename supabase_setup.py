#!/usr/bin/env python3
"""Supabase(Postgres)에 붙이고 지금 `data/`를 올리는 명령 — **한 번 하는 이사**다.

쓰는 법 (앱은 멈춰 둔 채로 하세요):

    python3 supabase_setup.py 확인      # 붙는지만 본다. 아무것도 안 바꾼다
    python3 supabase_setup.py 미리보기  # 무엇이 올라갈지만 센다. 아무것도 안 올린다
    python3 supabase_setup.py 올리기    # 실제로 올린다(이미 있는 키는 건드리지 않는다)
    python3 supabase_setup.py 대조      # 올린 것과 디스크의 것이 같은지 본다

백업 (이사가 끝난 뒤, 가끔 돌린다 — 앱을 멈추지 않아도 된다):

    python3 supabase_setup.py 내려받기          # DB → 날짜 폴더 (data/는 안 건드린다)
    python3 supabase_setup.py 되돌리기 "<폴더>"  # 그 폴더로 DB를 덮는다(한 번 묻는다)

**DB가 진짜 데이터가 된 뒤로는 `data/` 폴더 백업만으로는 모자란다** — 그 폴더는
이사한 날에 얼어붙었다. `내려받기`가 그 자리를 메운다.

연결 주소는 `.env`의 `DATABASE_URL`에서 읽는다. Supabase 프로젝트의
**Settings › Database › Connection string › URI**를 그대로 넣으면 된다
(비밀번호 자리 `[YOUR-PASSWORD]`를 실제 비밀번호로 바꿔야 한다).

`DATABASE_URL`을 넣는 순간부터 앱은 **DB를 본다** — 그래서 순서가 중요하다:
**먼저 올리고, 그다음에 앱을 재시작한다.** 거꾸로 하면 빈 DB를 보고 화면이 비어 보인다.
"""
import sys
from datetime import datetime
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
        key = path.relative_to(DATA_DIR).as_posix()
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


BACKUP_ROOT = BASE_DIR.parent / "press-monitor-백업"


def _backup_dir() -> Path:
    """이번 백업이 들어갈 **빈 새 폴더**. 이미 있으면 뒤에 번호를 붙인다."""
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    base = BACKUP_ROOT / f"db백업_{stamp}"
    target, n = base, 2
    while target.exists():
        target = BACKUP_ROOT / f"db백업_{stamp} ({n})"
        n += 1
    return target


def cmd_download() -> None:
    """DB를 날짜 폴더에 받는다 — **`data/`는 건드리지 않는다.**"""
    _need_url()
    dest = _backup_dir()
    print("받는 곳:", dest)
    print("(data/ 폴더는 건드리지 않습니다 — 거기가 마지막 구명줄입니다)")
    result = store_pg.download_to_files(dest)
    n = len(result["downloaded"])
    print(f"  받음 {n}개")
    if result["skipped"]:
        print(f"  비켜 둠(폴더 밖을 가리키는 키) {len(result['skipped'])}개")
    size = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
    print(f"  크기 {size / 1024 / 1024:.1f}MB")
    print()
    print("되돌릴 일이 생기면:")
    print(f'  python3 supabase_setup.py 되돌리기 "{dest}"')


def cmd_restore() -> None:
    """백업 폴더로 DB를 되돌린다 — **덮어쓴다.** 사람에게 한 번 묻는다."""
    _need_url()
    if len(sys.argv) < 3:
        print("어느 폴더로 되돌릴지 적어주세요:")
        print('  python3 supabase_setup.py 되돌리기 "~/press-monitor-백업/db백업_..."')
        sys.exit(1)
    src = Path(sys.argv[2]).expanduser()
    if not src.is_dir():
        print("그런 폴더가 없습니다:", src)
        sys.exit(1)
    plan = store_pg.restore_from_files(src, dry_run=True)
    print("되돌릴 폴더:", src)
    print(f"덮어쓸 줄: {len(plan['restored'])}개")
    print()
    print("※ 지금 DB의 그 줄들은 사라지고 이 폴더의 내용으로 바뀝니다.")
    print("※ 폴더에 없는 줄은 DB에 그대로 둡니다(지우지 않습니다).")
    print("※ 앱을 먼저 멈추세요.")
    print()
    if input('정말 되돌리려면 "되돌리기"라고 쳐주세요: ').strip() != "되돌리기":
        print("아무것도 바꾸지 않았습니다.")
        return
    result = store_pg.restore_from_files(src)
    print(f"  되돌림 {len(result['restored'])}개")
    print()
    print("이제 앱을 재시작하세요:")
    print("  launchctl kickstart -k gui/$(id -u)/local.press-monitor")


_COMMANDS = {
    "확인": cmd_check, "check": cmd_check,
    "미리보기": cmd_preview, "preview": cmd_preview,
    "올리기": cmd_upload, "upload": cmd_upload,
    "대조": cmd_verify, "verify": cmd_verify,
    "내려받기": cmd_download, "download": cmd_download,
    "되돌리기": cmd_restore, "restore": cmd_restore,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in _COMMANDS:
        print(__doc__)
        sys.exit(1)
    _COMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    main()
