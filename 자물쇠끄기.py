"""로그인 자물쇠를 끈다 — 아무도 못 들어가게 됐을 때의 탈출구.

    python3 자물쇠끄기.py

`.env`의 DATABASE_URL이 있으면 DB를, 없으면 data/ 파일을 고친다.
바깥에 열려 있으면(SERVER_HOST가 127.0.0.1이 아니면) 앱이 자물쇠를 강제하므로
이 스크립트로도 못 끈다 — 그때는 .env의 SERVER_HOST를 먼저 지운다.
"""
from dotenv import load_dotenv

load_dotenv()

from app import auth_session  # noqa: E402  (load_dotenv 뒤여야 한다)

if auth_session.is_open_to_network():
    print("끄지 못했습니다 — .env의 SERVER_HOST가 바깥 주소라 자물쇠가 강제됩니다.")
    print("먼저 .env에서 SERVER_HOST 줄을 지우고 다시 실행하세요.")
    raise SystemExit(1)

auth_session.set_login_required(False)
print("자물쇠를 껐습니다. 앱을 재시작하세요:")
print("  launchctl kickstart -k gui/$(id -u)/local.press-monitor")
