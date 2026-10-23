"""Public-page novel extraction for the separate reader app.

The browser renders the site normally; this module does not call private APIs
or bypass locked pages.
"""

import json
import os
import re
import time
from contextlib import contextmanager, nullcontext
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


SHUBA_HOSTS = {"www.69shuba.com", "69shuba.com"}
HOSTS = {"newtoki1.org", "toki32.com", "www.69shuba.com"}
WORK_ID_PATTERN = r"(?:[0-9]{1,128}|69shuba-[0-9]{1,100})"
USER_AGENT = "ReaderSync/1.0"
EPISODE_PATH = re.compile(r"^/novel/([0-9]+)/([0-9]+)$")
WORK_PATH = re.compile(r"^/novel/([0-9]+)(?:/([0-9]+))?/?$")
VERIFICATION_WAIT_MS = 30000
BROWSER_CONTENT_READY = r"""() => {
  if (/Just a moment/i.test(document.title)) return false;
  const path = location.pathname;
  if (/^\/book\/\d+(?:\/|\.htm)?$/.test(path)) {
    const id = path.match(/\d+/)[0];
    return [...document.querySelectorAll('a[href]')].some(a => {
      const url = new URL(a.href, location.href);
      return url.origin === location.origin && new RegExp('^/txt/' + id + '/[0-9]+/?$').test(url.pathname);
    });
  }
  if (/^\/novel\/\d+\/?$/.test(path))
    return !!document.querySelector('a.item-subject, a.novel-ep-link');
  if (/^\/txt\/\d+\/\d+\/?$/.test(path))
    return (document.querySelector('.txtnav')?.textContent || '').trim().length > 80;
  if (!/^\/novel\/\d+\/\d+\/?$/.test(path)) return false;
  try {
    if (JSON.parse(document.querySelector('#theme-novel-viewer-data')?.textContent || '{}').paidGate?.locked) return true;
  } catch (_) {}
  const host = document.querySelector('[data-theme-novel-content], .theme-novel-content, .novel-viewer > div:last-child');
  const root = window.__readerShadows?.get(host) || host?.shadowRoot || host;
  return (root?.textContent || '').trim().length > 80;
}"""


def supported_host(host):
    return isinstance(host, str) and (host in SHUBA_HOSTS or re.fullmatch(r'(?:newtoki[0-9]{1,6}\.org|toki[0-9]{1,6}\.com)', host) is not None)


def shuba_work(work_id):
    return isinstance(work_id, str) and re.fullmatch(r'69shuba-[0-9]{1,100}', work_id) is not None


def valid_work_id(work_id):
    return isinstance(work_id, str) and re.fullmatch(WORK_ID_PATTERN, work_id) is not None


def work_url(host, work_id, episode_id=None):
    if not valid_work_id(work_id) or (host in SHUBA_HOSTS) != shuba_work(work_id) or not supported_host(host):
        raise ValueError('같은 사이트 계열의 출처만 선택할 수 있습니다.')
    if episode_id is not None and not re.fullmatch(r'[0-9]{1,128}', episode_id):
        raise ValueError('회차 ID가 올바르지 않습니다.')
    if shuba_work(work_id):
        upstream = work_id.removeprefix('69shuba-')
        return f'https://{host}/txt/{upstream}/{episode_id}' if episode_id is not None else f'https://{host}/book/{upstream}/'
    return f'https://{host}/novel/{work_id}' + (f'/{episode_id}' if episode_id is not None else '')


def checked_host(value):
    if not isinstance(value, str):
        raise ValueError('수집 도메인을 입력하세요.')
    host = value.strip().lower()
    if not supported_host(host):
        raise ValueError('newtoki숫자.org, toki숫자.com 또는 www.69shuba.com 도메인만 입력하세요. URL·포트·내부 주소는 사용할 수 없습니다.')
    return host


class CrawlUnavailable(Exception):
    pass


class LockedChapter(Exception):
    pass


class HumanVerificationRequired(CrawlUnavailable):
    def __init__(self):
        super().__init__("사이트의 접속 확인이 아직 끝나지 않았습니다.")


def browser_endpoint():
    endpoint = os.getenv("READER_BROWSER_CDP", "")
    if endpoint:
        parsed = urlparse(endpoint)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise CrawlUnavailable("브라우저 연결은 로컬 HTTP 주소만 허용합니다.")
    return endpoint


@contextmanager
def source_browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CrawlUnavailable("Playwright가 설치되지 않았습니다.") from exc
    endpoint = browser_endpoint()
    with sync_playwright() as playwright:
        browser = (playwright.chromium.connect_over_cdp(endpoint, no_defaults=True) if endpoint
                   else playwright.chromium.launch(headless=True, executable_path=os.getenv("READER_BROWSER") or None))
        try:
            yield browser
        finally:
            # Exiting Playwright disconnects CDP without closing the user's verification browser.
            if not endpoint:
                browser.close()


@contextmanager
def source_page(browser, url, work_id):
    shared = bool(browser_endpoint())
    proxy = source_proxy()
    context = (browser.contexts[0] if shared else browser.new_context(
        service_workers="block", **({"proxy": {"server": proxy}} if proxy else {})))
    host = urlparse(url).hostname
    pages = [p for p in context.pages if urlparse(p.url).hostname == host] if shared else []
    page = next((p for p in pages if urlparse(p.url).path.rstrip('/') == urlparse(url).path.rstrip('/')),
                pages[0] if pages else None)
    borrowed_page = page is not None
    page = page or context.new_page()
    def restrict_request(route):
        request = route.request
        try:
            parsed = urlparse(request.url)
            allowed = (parsed.scheme == "https" and parsed.port is None and not (parsed.username or parsed.password)
                       and (supported_host(parsed.hostname) or parsed.hostname in {"challenges.cloudflare.com", "toki.peertrk.com"}))
        except ValueError:
            allowed = False
        if not allowed:
            route.abort()
            return
        if request.is_navigation_request() and request.frame == page.main_frame:
            try:
                checked_url(request.url, work_id)
            except ValueError:
                route.abort()
                return
        route.fallback()
    page.route("**/*", restrict_request)
    keep_open = False
    try:
        yield page
    except HumanVerificationRequired:
        keep_open = shared
        raise
    finally:
        page.unroute("**/*", restrict_request)
        if not shared:
            context.close()
        elif not keep_open and not borrowed_page and len(context.pages) > 1:
            page.close()


def check_browser_response(response, page):
    if (response and response.headers.get("cf-mitigated") == "challenge"
            or "Just a moment" in page.title()):
        from playwright.sync_api import TimeoutError as BrowserTimeout
        try:
            # The original 403/header remains stale after the browser's automatic check.
            page.wait_for_function(BROWSER_CONTENT_READY, timeout=VERIFICATION_WAIT_MS)
        except BrowserTimeout:
            raise HumanVerificationRequired() from None
        return
    if not response or response.status >= 400:
        raise CrawlUnavailable(f"원본 페이지 응답 {response.status if response else '없음'}")


def ready_verification_pages(hosts):
    if not hosts or not browser_endpoint():
        return set()
    from playwright.sync_api import Error as BrowserError
    ready = set()
    try:
        with source_browser() as browser:
            for context in browser.contexts:
                for page in context.pages:
                    try:
                        url = page.url
                        host, work_id, episode_id = checked_url(url)
                        if host in hosts and page.evaluate(BROWSER_CONTENT_READY) and page.url == url:
                            ready.add((host, work_id, episode_id or ''))
                    except (ValueError, BrowserError):
                        continue
    except (CrawlUnavailable, BrowserError):
        pass  # A browser restart must not block collection from other sources.
    return ready


def checked_url(url, work_id=None):
    parsed = urlparse(url)
    is_shuba = parsed.hostname in SHUBA_HOSTS
    match = (re.fullmatch(r'/book/([0-9]{1,100})(?:\.htm|/)?', parsed.path)
             or re.fullmatch(r'/txt/([0-9]{1,100})/([0-9]{1,128})/?', parsed.path)) if is_shuba else WORK_PATH.fullmatch(parsed.path)
    if (parsed.scheme != "https" or not supported_host(parsed.hostname) or parsed.port is not None
            or parsed.username or parsed.password or not match):
        raise ValueError("허용된 소설 주소만 입력하세요.")
    key = '69shuba-' + match.group(1) if is_shuba else match.group(1)
    if work_id and key != work_id:
        raise ValueError("다른 작품으로 이동했습니다.")
    if not valid_work_id(key):
        raise ValueError('작품 ID가 올바르지 않습니다.')
    return parsed.hostname, key, match.group(2) if match.lastindex == 2 else None


class SameSiteRedirect(HTTPRedirectHandler):
    def __init__(self, work_id):
        self.work_id = work_id

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        checked_url(newurl, self.work_id)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def source_proxy():
    proxy = os.getenv("READER_SOURCE_PROXY", "")
    if not proxy:
        return None
    parsed = urlparse(proxy)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or not port or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise CrawlUnavailable("수집 프록시는 로컬 HTTP 주소만 허용합니다.")
    return proxy


def source_opener(redirect_handler):
    proxy = source_proxy()
    handlers = [redirect_handler]
    if proxy:
        handlers.append(ProxyHandler({"https": proxy}))
    return build_opener(*handlers)


class NovelLinks(HTMLParser):
    def __init__(self, work_id):
        super().__init__(convert_charrefs=True)
        self.work_id = work_id
        self.links = []
        self.pages = set()
        self.title = ""
        self._anchor = None
        self._in_title = False
        self._in_item = False
        self._in_number = False
        self._in_episode_title = False
        self._number = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "li" and {"list-item", "novel-ep-row"}.intersection(attrs.get("class", "").split()):
            self._in_item = True
            self._number = attrs.get("data-ep", "")
        if tag == "span" and "ne-title" in attrs.get("class", "").split():
            self._in_episode_title = True
        if self._in_item and tag == "div" and "wr-num" in attrs.get("class", "").split():
            self._in_number = True
        if tag == "a" and attrs.get("href"):
            self._anchor = [attrs["href"], attrs.get("class", ""), []]

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_number:
            self._number += data
        if self._anchor and ("novel-ep-link" not in self._anchor[1].split() or self._in_episode_title):
            self._anchor[2].append(data)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "div":
            self._in_number = False
        if tag == "span":
            self._in_episode_title = False
        if tag == "li":
            self._in_item = False
        if tag != "a" or not self._anchor:
            return
        href, css_class, parts = self._anchor
        self._anchor = None
        parsed = urlparse(href)
        match = EPISODE_PATH.fullmatch(parsed.path)
        classes = css_class.split()
        if match and match.group(1) == self.work_id and {"item-subject", "novel-ep-link"}.intersection(classes):
            title = " ".join((" ".join(parts) if "novel-ep-link" in classes else "".join(parts)).split())
            number = self._number.strip() if self._in_item else ""
            if number.isdigit() and not re.match(rf"^{number}\s*화", title):
                title = f"{number}화 · {title}" if title else f"{number}화"
            self.links.append((match.group(2), title))
        elif parsed.path.rstrip("/") == f"/novel/{self.work_id}":
            page = parse_qs(parsed.query).get("epage", [""])[0]
            if page.isdigit() and int(page) > 0:
                self.pages.add(int(page))


def fetch_index_page(url, work_id, browser=None):
    checked_url(url, work_id)
    if browser is not None:
        with source_page(browser, url, work_id) as page:
            response = page.goto(url, wait_until="load", timeout=30000)
            check_browser_response(response, page)
            _, _, final_episode = checked_url(page.url, work_id)
            if final_episode:
                raise CrawlUnavailable("작품 목록이 다른 페이지로 이동했습니다.")
            page.wait_for_selector("a.item-subject, a.novel-ep-link", state="attached", timeout=30000)
            parser = NovelLinks(work_id)
            parser.feed(page.content())
            links = dict(parser.links)
            more = page.get_by_role("button", name="이전 회차 더 보기", exact=True)
            loads = 0
            while more.count():
                if loads >= 999:
                    raise CrawlUnavailable("목록이 1000페이지를 넘어 중단했습니다.")
                last_href = page.locator("a.novel-ep-link").last.get_attribute("href")
                time.sleep(0.7)
                more.click(timeout=30000)
                # Toki retains only a window of rows, so the count stops growing at 300.
                page.wait_for_function("last => { const rows = document.querySelectorAll('a.novel-ep-link'); "
                                       "return rows.length && rows[rows.length - 1].getAttribute('href') !== last; }",
                                       arg=last_href, timeout=30000)
                checked_url(page.url, work_id)
                window = NovelLinks(work_id)
                window.feed(page.content())
                links.update(window.links)
                loads += 1
            parser.links = list(links.items())
            return parser, page.url
    opener = source_opener(SameSiteRedirect(work_id))
    try:
        response = opener.open(Request(url, headers={"User-Agent": USER_AGENT}), timeout=20)
    except HTTPError as exc:
        if exc.headers.get("cf-mitigated") == "challenge":
            exc.close()
            raise HumanVerificationRequired() from exc
        raise
    with response:
        _, _, final_episode = checked_url(response.url, work_id)
        if final_episode:
            raise CrawlUnavailable("작품 목록이 다른 페이지로 이동했습니다.")
        html = response.read(2_000_000).decode("utf-8", "replace")
    if response.headers.get("cf-mitigated") == "challenge" or "Just a moment" in html[:2000]:
        raise HumanVerificationRequired()
    parser = NovelLinks(work_id)
    parser.feed(html)
    return parser, response.url


def collect_episode_list(source_url, work_id, delay=0.7):
    with source_browser() if browser_endpoint() or shuba_work(work_id) else nullcontext(None) as browser:
        if shuba_work(work_id):
            return collect_shuba_list(source_url, work_id, browser)
        return _collect_episode_list(source_url, work_id, delay, browser)


class ShubaLinks(HTMLParser):
    def __init__(self, work_id, base):
        super().__init__(convert_charrefs=True)
        self.work_id, self.base, self.links = work_id, base, {}
        self.title, self.anchor, self.in_title = '', None, False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'title':
            self.in_title = True
        if tag == 'a' and attrs.get('href'):
            self.anchor = [urljoin(self.base, attrs['href']), []]

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.anchor:
            self.anchor[1].append(data)

    def handle_endtag(self, tag):
        if tag == 'title':
            self.in_title = False
        if tag == 'a' and self.anchor:
            url, parts = self.anchor
            self.anchor = None
            try:
                _, _, eid = checked_url(url, self.work_id)
            except ValueError:
                return
            title = ' '.join(''.join(parts).split())
            if eid and title:
                self.links.setdefault(eid, (eid, title, url))


def collect_shuba_list(source_url, work_id, browser):
    host, _, _ = checked_url(source_url, work_id)
    url = work_url(host, work_id)
    with source_page(browser, url, work_id) as page:
        response = page.goto(url, wait_until='domcontentloaded', timeout=30000)
        check_browser_response(response, page)
        _, _, episode = checked_url(page.url, work_id)
        if episode:
            raise CrawlUnavailable('작품 목록이 다른 페이지로 이동했습니다.')
        page.wait_for_selector(f'a[href*="/txt/{work_id.removeprefix("69shuba-")}/"]', state='attached', timeout=30000)
        parser = ShubaLinks(work_id, page.url)
        parser.feed(page.content())
        if not parser.links:
            raise CrawlUnavailable('회차 목록을 찾지 못했습니다.')
        title = (page.locator('h1').first.text_content() or '').strip() if page.locator('h1').count() else ''
        title = title or re.split(r'最新章节|无弹窗|[-_]', parser.title)[0].strip()
        return title, list(parser.links.values())


def _collect_episode_list(source_url, work_id, delay, browser):
    host, _, _ = checked_url(source_url, work_id)
    base = f"https://{host}/novel/{work_id}"
    pending = {1}
    visited = set()
    pages = {}
    title = ""
    while pending:
        page_num = min(pending)
        pending.remove(page_num)
        if page_num in visited:
            continue
        if len(visited) >= 1000:
            raise CrawlUnavailable("목록이 1000페이지를 넘어 중단했습니다.")
        if visited:
            time.sleep(delay)
        parser, final_url = fetch_index_page(base + (f"?epage={page_num}" if page_num != 1 else ""), work_id, browser)
        visited.add(page_num)
        pages[page_num] = [(episode_id, episode_title, urljoin(final_url, f"/novel/{work_id}/{episode_id}"))
                           for episode_id, episode_title in parser.links]
        title = title or parser.title.split(" - ")[0].strip()
        pending.update(parser.pages - visited)
    seen = set()
    newest_first = []
    for page_num in sorted(pages):
        for episode in pages[page_num]:
            if episode[0] not in seen:
                newest_first.append(episode)
                seen.add(episode[0])
    if not newest_first:
        raise CrawlUnavailable("회차 목록을 찾지 못했습니다.")
    return title, list(reversed(newest_first))


def extract_episode(browser, url, work_id, episode_id):
    checked_url(url, work_id)
    with source_page(browser, url, work_id) as page:
        page.add_init_script("""
      (() => { const original = Element.prototype.attachShadow;
        Element.prototype.attachShadow = function(options) {
          const root = original.call(this, options);
          (window.__readerShadows ||= new Map()).set(this, root);
          return root;
        };
      })();
        """)
        loaded = False
        try:
            loaded = checked_url(page.url) == checked_url(url) and page.evaluate(BROWSER_CONTENT_READY)
        except ValueError:
            pass
        if not loaded:
            response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            check_browser_response(response, page)
        _, _, final_episode = checked_url(page.url, work_id)
        if final_episode != episode_id:
            raise CrawlUnavailable("요청한 회차가 다른 페이지로 이동했습니다.")
        if shuba_work(work_id):
            page.wait_for_selector('.txtnav', state='attached', timeout=30000)
            paragraphs = page.evaluate(r'''() => {
              const root=document.querySelector('.txtnav')?.cloneNode(true);
              if(!root)return [];
              root.querySelectorAll('script,style,button,iframe,ins,nav,h1,.txtinfo,.bottom-ad,.ads,.ad').forEach(el=>el.remove());
              root.querySelectorAll('br').forEach(el=>el.replaceWith('\n'));
              root.querySelectorAll('p,div').forEach(el=>el.append('\n'));
              return root.textContent.split(/\n+/).map(s=>s.trim()).filter(s=>s && !/^[(（]本章完[)）]$/.test(s));
            }''')
            if not paragraphs or sum(map(len, paragraphs)) < 80:
                raise CrawlUnavailable('본문을 찾지 못했습니다.')
            return paragraphs
        cfg = page.locator("#theme-novel-viewer-data")
        if cfg.count():
            data = json.loads(cfg.text_content() or "{}")
            if data.get("paidGate", {}).get("locked"):
                raise LockedChapter("로그인 또는 포인트가 필요한 회차입니다.")
        page.wait_for_function("""() => {
          const host = document.querySelector('[data-theme-novel-content], .theme-novel-content, .novel-viewer > div:last-child');
          const root = window.__readerShadows?.get(host) || host?.shadowRoot || host;
          return (root?.textContent || '').trim().length > 80;
        }""", timeout=30000)
        paragraphs = page.evaluate("""() => {
          const host = document.querySelector('[data-theme-novel-content], .theme-novel-content, .novel-viewer > div:last-child');
          const root = window.__readerShadows?.get(host) || host?.shadowRoot || host;
          if (!root) return [];
          const ps = [...root.querySelectorAll('p')].map(p => p.textContent.trim()).filter(Boolean);
          if (ps.length) return ps;
          const clone = root.cloneNode(true);
          clone.querySelectorAll('script,style,button').forEach(el => el.remove());
          clone.querySelectorAll('br').forEach(br => br.replaceWith('\\n'));
          return clone.textContent.split(/\\n+/).map(s => s.trim()).filter(Boolean);
        }""")
        if not paragraphs or sum(map(len, paragraphs)) < 80:
            raise CrawlUnavailable("본문을 찾지 못했습니다.")
        return paragraphs
