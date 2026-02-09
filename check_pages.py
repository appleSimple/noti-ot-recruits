import json
import os
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Set
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

CONFIG_FILE = "targets.json"
STATE_FILE = "state.json"

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

def get_session():
    """재시도 로직이 포함된 requests 세션 생성"""
    session = requests.Session()
    
    # 재시도 전략: 총 3회, 연결 에러/읽기 타임아웃 시 재시도
    retry_strategy = Retry(
        total=3,
        backoff_factor=2,  # 1초, 2초, 4초 간격으로 재시도
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    return session

@dataclass
class Item:
    item_id: str   # 목록 글번호(숫자)
    title: str
    url: str       # 딥링크가 있으면 딥링크, 없으면 목록 URL

def load_config() -> Dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def load_state() -> Dict[str, Set[str]]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {k: set(map(str, v)) for k, v in raw.items()}
    except Exception:
        return {}

def save_state(state: Dict[str, Set[str]]):
    compact = {k: list(sorted(v, reverse=True))[:3000] for k, v in state.items()}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(compact, f, ensure_ascii=False, indent=2)

def telegram_send(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("BOT_TOKEN / CHAT_ID 환경변수가 비어 있습니다. (GitHub Secrets 확인)")

    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    r = requests.post(api, json=payload, timeout=20)
    r.raise_for_status()

def fetch_html(url: str) -> tuple[str, str]:
    """
    returns: (final_url, html_text)
    - 인코딩 보정 포함
    - 재시도 로직 포함
    """
    session = get_session()
    
    # 타임아웃 증가: (연결 타임아웃, 읽기 타임아웃)
    r = session.get(url, headers=HEADERS, timeout=(15, 45), allow_redirects=True)
    r.raise_for_status()

    # 인코딩 보정 (특히 EUC-KR/CP949 사이트)
    if not r.encoding or (r.encoding.lower() in ["iso-8859-1", "latin-1"]):
        r.encoding = r.apparent_encoding or r.encoding

    return r.url, r.text

def parse_html_list_number_id(target_url: str, latest_n: int) -> List[Item]:
    """
    목록에서 글번호(숫자)를 item_id로 사용.
    전형적인 테이블 목록:
      <tr>
        <td>376</td>
        <td><a ...>제목</a></td>
        ...
      </tr>

    URL:
    - a[href]가 실링크면 urljoin해서 사용
    - href가 #/javascript면 onclick에서 '...' 형태 URL이 있으면 추출
    - 그마저도 없으면 target_url(목록) 사용
    """
    final_url, html = fetch_html(target_url)
    soup = BeautifulSoup(html, "lxml")

    items_by_id: Dict[str, Item] = {}

    # 1) 가장 안정적인 패턴: tr의 첫 td가 숫자
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue

        no = tds[0].get_text(strip=True)
        if not no.isdigit():
            continue

        a = tr.find("a")
        if not a:
            continue

        title = a.get_text(strip=True)
        if not title:
            continue

        href = (a.get("href") or "").strip()
        onclick = (a.get("onclick") or "").strip()

        full_url = target_url  # fallback은 목록
        if href and href not in ["#", "javascript:void(0);", "javascript:void(0)"] and not href.lower().startswith("javascript:"):
            full_url = urljoin(final_url, href)
        else:
            # onclick에 URL 문자열이 들어있는 경우: 'view.php?...' 또는 "/path/..." 등
            url_m = re.search(r"""['"]([^'"]+)['"]""", onclick)
            if url_m:
                full_url = urljoin(final_url, url_m.group(1))

        items_by_id[no] = Item(item_id=no, title=title, url=full_url)

    # 2) 혹시 테이블 구조가 달라서 1)이 비면: a의 부모 tr에서 첫 td 숫자 찾기
    if not items_by_id:
        for a in soup.find_all("a"):
            title = a.get_text(strip=True)
            if not title:
                continue

            tr = a.find_parent("tr")
            if not tr:
                continue
            tds = tr.find_all("td")
            if not tds:
                continue

            no = tds[0].get_text(strip=True)
            if not no.isdigit():
                continue

            href = (a.get("href") or "").strip()
            full_url = urljoin(final_url, href) if href and not href.lower().startswith("javascript:") else target_url
            items_by_id[no] = Item(item_id=no, title=title, url=full_url)

    items = sorted(items_by_id.values(), key=lambda it: int(it.item_id), reverse=True)
    return items[:latest_n]

def run_target(target: Dict, state: Dict[str, Set[str]]):
    name = str(target.get("name", "unknown"))
    url = target["url"]
    ttype = target.get("type", "html_list_number_id")
    latest_n = int(target.get("latest_n", 30))

    if ttype != "html_list_number_id":
        raise ValueError(f"Unsupported target type (only html_list_number_id): {ttype}")

    seen = state.get(name, set())

    items = parse_html_list_number_id(url, latest_n)

    print(f"[{name}] fetched={len(items)} first5={[ (it.item_id, it.title) for it in items[:5] ]}")

    # 파싱 실패 감지
    if not items:
        print(f"⚠️ [{name}] 파싱 실패: 글 목록을 찾을 수 없습니다. HTML 구조를 확인하세요.")
        # 파싱 실패 시에도 텔레그램으로 알림
        if BOT_TOKEN and CHAT_ID:
            try:
                telegram_send(f"⚠️ 파싱 실패 ({name})\n- URL: {url}\n- 글 목록을 찾을 수 없습니다.")
            except:
                pass
        return

    new_items = [it for it in items if it.item_id not in seen]
    if not new_items:
        print(f"[{name}] No new items.")
        return

    # 오래된 것부터 알림 보내기
    new_items.sort(key=lambda it: int(it.item_id))

    for it in new_items:
        msg = f"🆕 새 글 ({name})\n- {it.title}\n- {it.url}"
        telegram_send(msg)
        print(f"[{name}] Sent: {it.item_id} {it.title}")
        seen.add(it.item_id)
        time.sleep(0.7)

    state[name] = seen

def main():
    config = load_config()
    targets = config.get("targets", [])
    if not targets:
        raise RuntimeError("targets.json에 targets가 비어 있습니다.")

    state = load_state()
    errors = []

    for target in targets:
        try:
            run_target(target, state)
        except Exception as e:
            err_msg = f"⚠️ 크롤러 오류 ({target.get('name','unknown')})\n- {type(e).__name__}: {e}"
            print(err_msg)
            errors.append(err_msg)

    save_state(state)
    
    # 에러가 있으면 텔레그램으로 알림 (선택적)
    if errors and BOT_TOKEN and CHAT_ID:
        try:
            summary = "\n\n".join(errors)
            telegram_send(f"📋 크롤러 실행 완료 ({len(errors)}개 에러 발생)\n\n{summary}")
        except Exception as e:
            print(f"텔레그램 에러 알림 전송 실패: {e}")

if __name__ == "__main__":
    main()