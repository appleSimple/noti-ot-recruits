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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
    "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

def get_session():
    """재시도 로직이 포함된 requests 세션 생성"""
    session = requests.Session()
    
    # 재시도 전략: 총 8회로 증가, 더 많은 상태 코드 처리
    retry_strategy = Retry(
        total=8,
        backoff_factor=2,  # 2초, 4초, 8초, 16초... 간격으로 재시도
        status_forcelist=[403, 408, 429, 500, 502, 503, 504],  # 403도 재시도
        allowed_methods=["GET", "POST"],
        raise_on_status=False
    )
    
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=20)
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

def fetch_html(url: str, retry_count: int = 0) -> tuple[str, str]:
    """
    returns: (final_url, html_text)
    - 인코딩 보정 포함
    - 재시도 로직 포함
    - retry_count: 수동 재시도 횟수 (내부용)
    """
    session = get_session()
    
    # 사이트별 특별 처리
    headers = HEADERS.copy()
    timeout = (20, 60)  # 기본 타임아웃 증가: (연결 20초, 읽기 60초)
    
    # 지구촌사회복지재단: 403 차단 우회
    if "jwf.or.kr" in url:
        headers["Referer"] = "http://www.jwf.or.kr/"
        headers["Origin"] = "http://www.jwf.or.kr"
        # 다양한 User-Agent 시도
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        ]
        headers["User-Agent"] = user_agents[retry_count % len(user_agents)]
        timeout = (30, 90)
    
    # 화성시장애아동재활센터: 연결 타임아웃 대비
    if "hs4u.or.kr" in url:
        timeout = (45, 90)  # 타임아웃 대폭 증가
        headers["Referer"] = "https://www.hs4u.or.kr/"
    
    # 치매안심센터: ASP.NET 페이지
    if "nid.or.kr" in url:
        timeout = (30, 90)
        headers["Referer"] = "https://www.nid.or.kr/"
    
    # 수원시보건소: ASP 페이지
    if "health.suwon.go.kr" in url:
        timeout = (30, 90)
        headers["Referer"] = "https://health.suwon.go.kr/"
    
    try:
        r = session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        
        # 인코딩 보정 (특히 EUC-KR/CP949 사이트)
        if not r.encoding or (r.encoding.lower() in ["iso-8859-1", "latin-1"]):
            r.encoding = r.apparent_encoding or r.encoding
        
        return r.url, r.text
        
    except requests.exceptions.Timeout as e:
        # 타임아웃 발생 시 한 번 더 재시도 (최대 2회)
        if retry_count < 2:
            print(f"  [재시도 {retry_count + 1}/2] 타임아웃 발생, 재시도 중...")
            time.sleep(5)  # 5초 대기 후 재시도
            return fetch_html(url, retry_count + 1)
        else:
            raise
    
    except requests.exceptions.HTTPError as e:
        # 403 등 HTTP 에러 시 한 번 더 재시도
        if e.response.status_code == 403 and retry_count < 2:
            print(f"  [재시도 {retry_count + 1}/2] 403 오류, User-Agent 변경 후 재시도...")
            time.sleep(3)
            return fetch_html(url, retry_count + 1)
        else:
            raise

def parse_nid_or_kr(soup: BeautifulSoup, base_url: str, latest_n: int, debug: bool = False) -> List[Item]:
    """치매안심센터: recruit_view.aspx?no=XXX 형식"""
    items_by_id: Dict[str, Item] = {}
    
    if debug:
        print(f"  [DEBUG] 치매안심센터 파서 실행")
        all_links = soup.find_all("a", href=True)
        print(f"  [DEBUG] 전체 링크 개수: {len(all_links)}")
        recruit_links = [a for a in all_links if "recruit" in a.get("href", "").lower()]
        print(f"  [DEBUG] recruit 관련 링크: {len(recruit_links)}")
        if recruit_links:
            for i, a in enumerate(recruit_links[:3]):
                print(f"  [DEBUG]   링크 {i+1}: {a.get('href', '')[:100]}")
    
    # recruit_view.aspx?no= 링크 찾기
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "recruit_view.aspx" in href and "no=" in href:
            # no= 파라미터 추출
            no_match = re.search(r'[?&]no=(\d+)', href)
            if not no_match:
                continue
            
            item_id = no_match.group(1)
            title = a.get_text(strip=True)
            
            # [채용중] 같은 태그 제거
            title = re.sub(r'\[채용중\]|\[채용종료\]', '', title).strip()
            
            if not title:
                continue
            
            full_url = urljoin(base_url, href)
            items_by_id[item_id] = Item(item_id=item_id, title=title, url=full_url)
    
    if debug:
        print(f"  [DEBUG] 치매안심센터: {len(items_by_id)}개 항목 발견")
        if items_by_id:
            print(f"  [DEBUG] 첫 번째 항목: {list(items_by_id.values())[0]}")
    
    items = sorted(items_by_id.values(), key=lambda it: int(it.item_id), reverse=True)
    return items[:latest_n]

def parse_health_suwon(soup: BeautifulSoup, base_url: str, latest_n: int, debug: bool = False) -> List[Item]:
    """수원시보건소: URL의 no= 파라미터 추출"""
    items_by_id: Dict[str, Item] = {}
    
    if debug:
        print(f"  [DEBUG] 수원시보건소 파서 실행")
    
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "board_view.asp" in href and "no=" in href:
            # no= 파라미터 추출
            no_match = re.search(r'[?&]no=(\d+)', href)
            if not no_match:
                continue
            
            item_id = no_match.group(1)
            title = a.get_text(strip=True)
            
            if not title:
                continue
            
            full_url = urljoin(base_url, href)
            items_by_id[item_id] = Item(item_id=item_id, title=title, url=full_url)
    
    if debug:
        print(f"  [DEBUG] 수원시보건소: {len(items_by_id)}개 항목 발견")
    
    items = sorted(items_by_id.values(), key=lambda it: int(it.item_id), reverse=True)
    return items[:latest_n]

def parse_hs4u(soup: BeautifulSoup, base_url: str, latest_n: int, debug: bool = False) -> List[Item]:
    """화성시장애아동재활센터: seq= 파라미터 추출"""
    items_by_id: Dict[str, Item] = {}
    
    if debug:
        print(f"  [DEBUG] 화성시장애아동재활센터 파서 실행")
    
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "subAct=view" in href and "seq=" in href:
            # seq= 파라미터 추출
            seq_match = re.search(r'[?&]seq=(\d+)', href)
            if not seq_match:
                continue
            
            item_id = seq_match.group(1)
            title = a.get_text(strip=True)
            
            # 아이콘 텍스트 제거
            title = title.replace('[새글]', '').replace('[이미지]', '').replace('[다운로드]', '').strip()
            
            if not title:
                continue
            
            full_url = urljoin(base_url, href)
            items_by_id[item_id] = Item(item_id=item_id, title=title, url=full_url)
    
    if debug:
        print(f"  [DEBUG] 화성시장애아동재활센터: {len(items_by_id)}개 항목 발견")
    
    items = sorted(items_by_id.values(), key=lambda it: int(it.item_id), reverse=True)
    return items[:latest_n]

def parse_html_list_number_id(target_url: str, latest_n: int, debug: bool = False) -> List[Item]:
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
    
    # 사이트별 특별 파서
    if "nid.or.kr" in target_url:
        # 치매안심센터: recruit_view.aspx?no=XXX 형식
        return parse_nid_or_kr(soup, final_url, latest_n, debug)
    elif "health.suwon.go.kr" in target_url:
        # 수원시보건소: URL에서 no= 파라미터 추출
        return parse_health_suwon(soup, final_url, latest_n, debug)
    elif "hs4u.or.kr" in target_url:
        # 화성시장애아동재활센터: seq= 파라미터 추출
        return parse_hs4u(soup, final_url, latest_n, debug)

    # 디버그 모드: HTML 구조 출력
    if debug:
        print(f"  [DEBUG] HTML 길이: {len(html)}")
        trs = soup.find_all("tr")
        print(f"  [DEBUG] 총 tr 개수: {len(trs)}")
        for i, tr in enumerate(trs[:10]):  # 처음 10개만
            tds = tr.find_all("td")
            if tds:
                td_texts = [td.get_text(strip=True)[:50] for td in tds[:5]]
                print(f"  [DEBUG] tr[{i}] - td 개수: {len(tds)}, 내용: {td_texts}")

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

    # 3) 여전히 비어있으면 다른 패턴 시도: td의 순서가 다를 수 있음
    if not items_by_id and debug:
        print(f"  [DEBUG] 패턴 1, 2 실패. 다른 패턴 탐색 중...")

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

    try:
        items = parse_html_list_number_id(url, latest_n, debug=False)
        
        print(f"[{name}] fetched={len(items)} first5={[ (it.item_id, it.title) for it in items[:5] ]}")

        # 파싱 실패 감지 - 디버그 모드로 재시도
        if not items:
            print(f"⚠️ [{name}] 파싱 실패: 글 목록을 찾을 수 없습니다. 디버그 모드로 재시도...")
            items = parse_html_list_number_id(url, latest_n, debug=True)
            
            if not items:
                print(f"⚠️ [{name}] 디버그 모드에서도 파싱 실패.")
                raise RuntimeError(f"파싱 실패: 글 목록을 찾을 수 없습니다")

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
        
    except requests.exceptions.Timeout as e:
        # 타임아웃 에러를 명확히 표시
        raise requests.exceptions.Timeout(f"연결 타임아웃: {url}") from e
    except requests.exceptions.HTTPError as e:
        # HTTP 에러를 명확히 표시
        status_code = e.response.status_code if hasattr(e, 'response') and e.response else 'unknown'
        raise requests.exceptions.HTTPError(f"HTTP {status_code} 오류: {url}") from e

def main():
    config = load_config()
    targets = config.get("targets", [])
    if not targets:
        raise RuntimeError("targets.json에 targets가 비어 있습니다.")

    state = load_state()
    errors = []

    for i, target in enumerate(targets):
        try:
            run_target(target, state)
            
            # 각 사이트 크롤링 사이에 딜레이 추가 (마지막 제외)
            if i < len(targets) - 1:
                time.sleep(2)
                
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