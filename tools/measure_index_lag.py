# Design Ref: EXPERT_QUESTIONS.md "4. 늦게 색인된 기사를 제 회차에 담을 것인가"
"""네이버 검색 API의 **색인 지연을 직접 잰다** — 기사가 발행 후 몇 분에 검색에 나타나는가.

**왜 있나.** 회차가 마감 정각에 수집하면 마감 몇 분 전에 나온 기사를 놓치고, 그 기사는
다음 회차가 되돌아보기(COLLECT_LOOKBACK_MIN)로 주워 담는다 — 담당자가 보는 「앞 회차
기사가 섞여 들어옴」이 그것이다. 수집을 마감 뒤로 몇 분 늦추면 메워지는데, **몇 분이
적당한지는 색인 지연을 재야 안다.**

재는 법: 앱이 쓰는 검색어로 1분마다 검색해, 새로 나타난 기사의 발행 시각과 지금 시각의
차이를 적는다. 첫 회차에 받은 것은 「이미 쌓여 있던 것」이라 지연을 알 수 없어 집계에서
뺀다(round > 0만 센다).

**재기만 한다** — 앱 데이터는 설정(검색어)만 읽고 아무것도 쓰지 않는다. 결과는
lag_rows.json에 쌓인다. 네이버 호출은 1분마다 검색어 수(지금 8개)만큼이다.

쓰는 법:  python3 tools/measure_index_lag.py          # 1분 간격 10회(9분)
          python3 tools/measure_index_lag.py 30       # 1분 간격 30회(29분)

표본이 작으면(기사가 드문 시간대) 지연 분포가 흔들린다 — 기사가 많이 나오는 평일
오전·오후에 30분 이상 돌리는 쪽이 믿을 만하다.
"""
import sys, json, time, re, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import requests
from datetime import datetime
from app.credentials import naver_client_id, naver_client_secret
from app.naver_api import _KST
from app.settings import load_settings, active_search_groups, group_in_scrap
from app.search_terms import naver_query

H={"X-Naver-Client-Id":naver_client_id(),"X-Naver-Client-Secret":naver_client_secret()}
API="https://openapi.naver.com/v1/search/news.json"
s=load_settings()
groups=active_search_groups(s,[g for g in s.get("keyword_groups",[]) if group_in_scrap(g)])
kws=[]
for g in groups:
    for k in g.get("keywords",[]):
        if k not in kws: kws.append(k)

OUT = str(pathlib.Path(__file__).resolve().parent.parent / "data" / "measure_topup" / "lag_rows.json")

seen={}   # url -> 처음 본 시각
rows=[]   # 새로 나타난 기사 기록
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 10
for n in range(ROUNDS):
    tick=datetime.now(_KST)
    for k in kws:
        try:
            r=requests.get(API,headers=H,params={"query":naver_query(k),"display":100,"sort":"date"},timeout=12)
            if r.status_code!=200: continue
            for it in r.json().get("items",[]):
                u=it.get("link") or it.get("originallink")
                if not u or u in seen: continue
                try: pub=datetime.strptime(it.get("pubDate"),"%a, %d %b %Y %H:%M:%S %z").astimezone(_KST)
                except Exception: continue
                if pub.date()!=tick.date(): continue
                seen[u]=tick
                lag=(tick-pub).total_seconds()/60
                rows.append({"url":u,"kw":k,"pub":pub.isoformat(),"seen":tick.isoformat(),
                             "lag_min":round(lag,1),"round":n,
                             "title":re.sub(r"<[^>]+>","",it.get("title",""))[:60]})
        except Exception:
            pass
    json.dump(rows, open(OUT,"w"), ensure_ascii=False, indent=1)
    print(f"[{tick:%H:%M:%S}] {n+1}/{ROUNDS}회 — 누적 기사 {len(seen)}건", flush=True)
    if n < ROUNDS-1: time.sleep(60)
print("끝")
