// ==UserScript==
// @name         뉴토끼 다크 리더 (본문 전용 뷰어)
// @namespace    nt-dark-reader
// @version      5.0
// @description  뉴토끼 소설/웹툰: 야간 다크/주간 종이색 본문 뷰어. 도메인이 바뀌어도 자동 인식 + 메뉴에서 도메인 직접 추가 가능
// @homepageURL  https://github.com/yuisatomi/newtoki-dark-reader
// @updateURL    https://raw.githubusercontent.com/yuisatomi/newtoki-dark-reader/main/newtoki-dark-reader.user.js
// @downloadURL  https://raw.githubusercontent.com/yuisatomi/newtoki-dark-reader/main/newtoki-dark-reader.user.js
// @match        *://*/*
// @run-at       document-end
// @grant        GM_registerMenuCommand
// @grant        GM_setValue
// @grant        GM_getValue
// ==/UserScript==

(function () {
  'use strict';

  /* ============================================================
     도메인 매칭 (옵션1: 자동 인식 + 옵션2: 사용자 추가 도메인)
     - 호스트명에 "newtoki" 포함 → 자동 인식 (번호/도메인 변경 무관)
     - 사용자가 추가한 도메인 → GM_setValue로 영구 저장
     ============================================================ */
  const USER_DOMAINS_KEY = 'ntReaderUserDomains';

  function getUserDomains() {
    try { return GM_getValue(USER_DOMAINS_KEY, []) || []; } catch (e) { return []; }
  }
  function hostMatches(host) {
    if (!host) return false;
    if (host.includes('newtoki')) return true;               // 자동 인식
    return getUserDomains().some(d =>                        // 사용자 추가 (B4: 정확 매칭만)
      host === d || host.endsWith('.' + d)
    );
  }
  // Tampermonkey 메뉴: 도메인 추가/관리
  if (typeof GM_registerMenuCommand === 'function') {
    GM_registerMenuCommand('➕ 현재 사이트를 뷰어 대상에 추가', () => {
      const d = location.hostname.replace(/^www\./, '');
      const list = getUserDomains();
      if (list.includes(d)) { alert('이미 등록된 도메인입니다: ' + d); return; }
      list.push(d);
      GM_setValue(USER_DOMAINS_KEY, list);
      alert('등록 완료: ' + d + '\n페이지를 새로고침하면 뷰어가 활성화됩니다.');
    });
    GM_registerMenuCommand('📋 등록된 도메인 보기/삭제', () => {
      const list = getUserDomains();
      if (!list.length) { alert('사용자 추가 도메인이 없습니다.\n("newtoki" 포함 도메인은 자동 인식됩니다.)'); return; }
      const del = prompt('등록된 도메인:\n' + list.map((x, i) => (i + 1) + '. ' + x).join('\n')
        + '\n\n삭제할 번호를 입력 (취소는 그냥 닫기):');
      const n = parseInt(del, 10);
      if (n >= 1 && n <= list.length) {
        list.splice(n - 1, 1);
        GM_setValue(USER_DOMAINS_KEY, list);
        alert('삭제 완료.');
      }
    });
  }
  if (!hostMatches(location.hostname)) return;   // 대상 사이트가 아니면 종료

  /* ============================================================ */

  const path = location.pathname;
  const isNovelEp   = /^\/novel\/[^/]+\/[^/]+/.test(path) && /\d/.test(path.split('/').pop() || '');
  const isWebtoonEp = /^\/webtoon\/[^/]+\/[^/]+/.test(path) && /\d/.test(path.split('/').pop() || '');
  // 작품 ID가 숫자든 slug든 관계없이 "카테고리/작품/회차" 3단 구조 + 회차 ID에 숫자 포함이면 회차 페이지로 인식
  const isEpisodePage = isNovelEp || isWebtoonEp;
  const VIEW_FLAG = 'ntDarkReaderOn';
  const VIEW_FLAG_TS = 'ntDarkReaderOnTs';

  function injectFloatingBtn() {
    const s = document.createElement('style');
    s.textContent = `
      #nt-float-btn {
        position: fixed; right: 18px; bottom: 18px; z-index: 2147483000;
        padding: 12px 18px; border-radius: 999px; border: 1px solid #30363d;
        background:#1f6feb; color:#fff; font-size:14px; font-weight:700;
        cursor:pointer; box-shadow:0 6px 18px rgba(0,0,0,.45); opacity:.85;
      }
      #nt-float-btn:hover { opacity:1; }
    `;
    document.head.appendChild(s);
    const b = document.createElement('button');
    b.id = 'nt-float-btn';
    b.textContent = '📖 뷰어 모드';
    b.addEventListener('click', () => {
      setViewerFlag(true);
      location.reload();
    });
    document.body.appendChild(b);
  }

  let viewerWanted = false;
  try { viewerWanted = sessionStorage.getItem(VIEW_FLAG) === '1'; } catch (e) {}
  // 화면 꺼짐 등으로 탭이 완전히 재시작된 경우 대비: localStorage 플래그로도 복원
  try {
    if (!viewerWanted && localStorage.getItem(VIEW_FLAG) === '1' &&
        Date.now() - (+(localStorage.getItem(VIEW_FLAG_TS) || 0)) < 6 * 60 * 60 * 1000) {
      viewerWanted = true;
      sessionStorage.setItem(VIEW_FLAG, '1');   // 세션 동기화
    }
  } catch (e) {}
  if (/[?&]ntview=1/.test(location.search)) {
    viewerWanted = true;
    try { sessionStorage.setItem(VIEW_FLAG, '1'); } catch (e) {}
  }
  function setViewerFlag(on) {
    try {
      if (on) {
        sessionStorage.setItem(VIEW_FLAG, '1');
        localStorage.setItem(VIEW_FLAG, '1');                       // 탭 재시작 대비
        localStorage.setItem(VIEW_FLAG_TS, String(Date.now()));
      } else {
        sessionStorage.removeItem(VIEW_FLAG);
        localStorage.removeItem(VIEW_FLAG);
        localStorage.removeItem(VIEW_FLAG_TS);
      }
    } catch (e) {}
  }

  /* ---------- 뷰어 전용 목록 페이지 (?ntlist=1) ---------- */
  const isListPage = /^\/(webtoon|novel)\/[^/]+\/?$/.test(path) && /[?&]ntlist=1/.test(location.search);
  if (isListPage && !/[?&]ntview=1/.test(location.search)) {
    // 회차 목록 ul.list-body 만 추려서 다크 화면으로 재구성
    const listBody = document.querySelector('ul.list-body');
    const workTitle = document.querySelector('.theme-detail-title-line');
    if (listBody) {
      document.head.querySelectorAll('link[rel="stylesheet"], style').forEach(el => el.remove());
      const root = document.createElement('div');
      root.id = 'nt-dark-root';
      root.innerHTML = `
        <style>
          html, body { margin:0!important; padding:0!important; background:#0d1117!important; }
          #nt-dark-root { max-width:720px; margin:0 auto; padding:20px 16px 120px;
            background:#0d1117; color:#d7dde7; font-family:'Noto Sans KR','Malgun Gothic',sans-serif; }
          .nt-list-title { font-size:20px; font-weight:700; color:#e6edf3; padding-bottom:12px;
            margin-bottom:14px; border-bottom:1px solid #21262d; }
          #nt-dark-root ul.list-body { list-style:none!important; margin:0!important; padding:0!important; }
          #nt-dark-root li.list-item { display:flex!important; gap:10px; align-items:baseline;
            padding:11px 6px!important; border-bottom:1px solid #161b22!important; background:transparent!important; }
          #nt-dark-root li.list-item > div:not(.wr-subject) { display:none!important; }
          #nt-dark-root .wr-num { color:#8b949e!important; font-size:13px!important; min-width:34px; }
          #nt-dark-root a.item-subject { color:#d7dde7!important; font-size:16px!important; text-decoration:none!important;
            line-height:1.5!important; }
          #nt-dark-root a.item-subject:active { color:#58a6ff!important; }
          /* 링크 클릭 시 자동으로 뷰어 모드로 진입 */
          #nt-dark-root li.list-item a { pointer-events:auto; }
        </style>`;
      const t = document.createElement('div');
      t.className = 'nt-list-title';
      t.textContent = workTitle ? workTitle.textContent.trim() : '회차 목록';
      root.appendChild(t);
      root.appendChild(listBody);
      document.body.innerHTML = '';
      document.body.appendChild(root);

      // P3: 읽은 회차 흐리게 표시
      try {
        const wm = path.match(/^\/(webtoon|novel)\/([^/]+)/);
        if (wm) {
          const readEp = localStorage.getItem('ntRead:' + wm[1] + ':' + wm[2]);
          if (readEp) {
            listBody.querySelectorAll('a.item-subject').forEach(a => {
              const ep = new URL(a.href, location.href).pathname.split('/').filter(Boolean).pop();
              if (ep === readEp) a.style.opacity = '.45';
            });
          }
        }
      } catch (e) {}

      // 목록에서 회차 클릭 → 세션 플래그 켜서 이동 (뷰어로 바로 진입, B8: 새 탭 클릭 허용)
      listBody.addEventListener('click', e => {
        const a = e.target.closest('a[href]');
        if (!a) return;
        if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey) return;
        e.preventDefault();
        setViewerFlag(true);
        location.href = a.getAttribute('href');
      });

      // 하단 최소 내비: 원본으로 나가기 (B2: 뷰어 플래그 제거 후 이동)
      const back = document.createElement('a');
      back.href = location.pathname;   // 플래그 없이 원본 목록으로
      back.style.cssText = 'position:fixed;left:0;right:0;bottom:0;padding:12px;text-align:center;'
        + 'background:rgba(13,17,23,.95);border-top:1px solid #21262d;color:#3fb950;'
        + 'text-decoration:none;font-size:15px;';
      back.textContent = '↩ 원본 목록';
      back.addEventListener('click', () => {
        setViewerFlag(false);
      });
      document.body.appendChild(back);
      return;
    }
    // list-body를 못 찾으면 원본 유지
    return;
  }

  if (!isEpisodePage || !viewerWanted) {
    if (isEpisodePage) injectFloatingBtn();
    return;
  }

  /* ================= 뷰어 모드 ================= */

  let titleEl = document.querySelector('.theme-viewer-title, .theme-novel-title, h3');
  function getBodyEl() {
    return isNovelEp
      ? document.querySelector('[data-theme-novel-content], .theme-novel-content')
      : document.querySelector('[data-theme-viewer-images], .theme-viewer-images');
  }
  let bodyEl = getBodyEl();

  /* ---------- 옵션 A: 광고 검증(ack) 완료까지 대기 후 뷰어 전환 ----------
     사이트는 광고 챌린지 성공(ntk-ad-ack-ready / __ntk_ad_ack_scope 세팅) 후에야
     /api/*-images 등에서 본문 데이터를 받아 렌더링한다.
     DOM을 미리 지우면 챌린지 스크립트가 죽어 첫 로딩 회차의 본문이 영원히 안 온다.
     → 원본을 남겨두고 "본문 준비 완료" 신호를 기다린 뒤 뷰어로 교체. */
  function bodyReady() {
    const el = getBodyEl() || bodyEl;
    if (!el) return false;
    if (isNovelEp) {
      // 소설 본문은 로딩 완료 후 open Shadow DOM 안에 렌더링된다.
      return !!el.shadowRoot && (el.shadowRoot.textContent || '').trim().length > 0;
    }
    // 웹툰: 모바일/데스크톱 래퍼 구조와 무관하게 실제 로드된 이미지를 확인
    if (el.querySelector('.is-error, .theme-viewer-error')) return true;
    const images = [...el.querySelectorAll('img')];
    const loaded = images.filter(img => img.complete && img.naturalWidth > 0).length;
    return images.length > 0 && loaded >= Math.min(images.length, 3);
  }

  function waitForBody(timeoutMs) {
    return new Promise(resolve => {
      if (bodyReady()) { resolve(true); return; }
      const started = Date.now();
      // 사이트 이벤트 기반 조기 완료
      const onAck = () => setTimeout(check, 400);
      const onNovelReady = () => check();
      window.addEventListener('ntk-ad-ack-ready', onAck, { once: true });
      window.addEventListener('novel-content-ready', onNovelReady, { once: true });
      const timer = setInterval(check, 300);
      function check() {
        if (bodyReady()) { cleanup(); resolve(true); return; }
        if (Date.now() - started > timeoutMs) { cleanup(); resolve(false); }
      }
      function cleanup() {
        clearInterval(timer);
        window.removeEventListener('ntk-ad-ack-ready', onAck);
        window.removeEventListener('novel-content-ready', onNovelReady);
      }
    });
  }

  async function startViewerWhenReady() {
    // 광고 확인 UI를 가리지 않고 원본 화면에서 본문 로딩을 기다린다.
    const ok = await waitForBody(120000);
    if (!ok) {
      // 실패: 원본 유지 + 안내 (플래그는 끔 → 새로고침하면 원본)
      setViewerFlag(false);
      const note = document.createElement('div');
      note.style.cssText = 'position:fixed;left:12px;right:12px;bottom:16px;z-index:2147483000;'
        + 'background:#1c1917;border:1px solid #7f1d1d;color:#fca5a5;padding:10px 14px;'
        + 'border-radius:8px;font-size:13px;';
      note.textContent = '뷰어: 본문 로딩 시간 초과 — 원본으로 표시합니다. 새로고침 후 📖 버튼으로 재시도하세요.';
      document.body.appendChild(note);
      setTimeout(() => note.remove(), 8000);
      injectFloatingBtn();
      return;
    }

    bodyEl = getBodyEl() || bodyEl;
    titleEl = document.querySelector('.theme-viewer-title, .theme-novel-title, h3') || titleEl;
    prevUrl = findNavBtn('이전화') || prevUrl;
    nextUrl = findNavBtn('다음화') || nextUrl;
    listUrl = findNavBtn('목록') || listUrl;
    titleText = titleEl ? titleEl.textContent.trim() : document.title;
    buildViewer();   // 아래 정의된 실제 뷰어 구성 함수
  }

  function findNavBtn(label) {
    const a = [...document.querySelectorAll('a')].find(
      x => x.textContent.trim() === label && x.getAttribute('href')
    );
    return a ? a.getAttribute('href') : null;
  }
  let prevUrl = findNavBtn('이전화');
  let nextUrl = findNavBtn('다음화');
  let listUrl = findNavBtn('목록');
  let titleText = titleEl ? titleEl.textContent.trim() : document.title;

  /* P3: 읽은 회차 기록 (목록 페이지에서 흐리게 표시용) */
  try {
    const wm = path.match(/^\/(webtoon|novel)\/([^/]+)\//);
    const ep = path.split('/').filter(Boolean).pop();
    if (wm && ep) localStorage.setItem('ntRead:' + wm[1] + ':' + wm[2], ep);
  } catch (e) {}

  /* ---------- 실제 뷰어 구성 (본문 준비 완료 후 호출) ---------- */
  function buildViewer() {
  document.head.querySelectorAll('link[rel="stylesheet"], style').forEach(el => el.remove());

  const root = document.createElement('div');
  root.id = 'nt-dark-root';

  const style = document.createElement('style');
  style.textContent = `
    html, body { margin:0 !important; padding:0 !important; background:var(--nt-bg, #0d1117) !important; }
    #nt-dark-root {
      max-width: var(--nt-width, 720px); margin: 0 auto; padding: 24px 20px 160px;
      background:var(--nt-bg, #0d1117); color:var(--nt-text, #d7dde7);
      font-family: 'Noto Sans KR', 'Malgun Gothic', sans-serif;
      font-size: var(--nt-font, 18px); line-height: var(--nt-lh, 2.0); letter-spacing: 0.01em;
    }
    #nt-dark-root .nt-title {
      font-size: calc(var(--nt-font, 18px) * 1.25); font-weight: 700; color:var(--nt-title, #e6edf3);
      padding-bottom: 14px; margin-bottom: 22px;
      border-bottom: 1px solid var(--nt-border, #21262d);
    }
    #nt-dark-root .nt-body p { margin: 0 0 1.2em; }
    #nt-dark-root .nt-body, #nt-dark-root .nt-body * {
      width: auto !important;
      max-width: 100% !important;
      white-space: normal !important;
      word-break: break-word;
      overflow-wrap: break-word;
    }
    #nt-dark-root .nt-body table { width: 100% !important; table-layout: auto !important; }
    #nt-dark-root .nt-body img { max-width: 100%; height: auto; display: block; margin: 0 auto 8px; }
    #nt-dark-root .nt-body a { color:var(--nt-accent, #58a6ff); }
    #nt-dark-root .nt-body, #nt-dark-root .nt-body * {
      background: transparent !important; color: var(--nt-text, #d7dde7) !important;
      border-color: var(--nt-border, #21262d) !important; text-shadow: none !important;
      font-size: inherit !important; line-height: var(--nt-lh, 2.0) !important;
    }
    #nt-dark-root .nt-body h1, #nt-dark-root .nt-body h2, #nt-dark-root .nt-body h3 {
      font-size: calc(var(--nt-font, 18px) * 1.15) !important;
    }
    #nt-dark-root.nt-webtoon { padding-bottom: 90px; }
    /* 웹툰: 모바일은 꽉 차게, 데스크톱은 웹툰 비율에 맞는 고정 너비 */
    #nt-dark-root.nt-webtoon { max-width: 800px !important; padding-left: 0 !important; padding-right: 0 !important; }
    @media (max-width: 600px) {
      /* 모바일: 화면 폭 꽉 차게 */
      #nt-dark-root.nt-webtoon { max-width: none !important; margin: 0 !important; padding: 0 0 90px !important; }
      html, body { overflow-x: hidden !important; }
    }
    #nt-dark-root.nt-webtoon .nt-title { margin-bottom: 0; }
    #nt-dark-root.nt-webtoon .nt-body,
    #nt-dark-root.nt-webtoon .nt-body * {
      margin: 0 !important; padding: 0 !important;
      border: none !important; box-shadow: none !important;
      line-height: 0 !important; font-size: 0 !important;
      width: 100% !important;
    }
    #nt-dark-root.nt-webtoon .nt-body img {
      display: block !important; width: 100% !important;
      max-width: 100% !important; height: auto !important;
      vertical-align: top !important;
    }

    #nt-dark-nav {
      position: fixed; left:0; right:0; bottom:0; z-index: 99999;
      display:flex; flex-wrap: wrap; gap:8px; justify-content:center; align-items:center;
      padding: 8px 12px calc(8px + env(safe-area-inset-bottom, 0px));
      background: var(--nt-nav-bg, rgba(13,17,23,.92));
      border-top: 1px solid var(--nt-border, #21262d); backdrop-filter: blur(6px);
      transition: transform .25s ease;
    }
    /* 스크롤 내릴 때 숨김 */
    #nt-dark-nav.nt-hidden { transform: translateY(110%); }
    #nt-dark-nav a, #nt-dark-nav button {
      min-width: 0; flex: 1 1 auto; max-width: 150px;
      padding: 9px 14px; border-radius: 8px;
      border: 1px solid var(--nt-border, #30363d); background:var(--nt-button, #21262d); color:var(--nt-title, #e6edf3);
      font-size: 15px; cursor: pointer; text-align:center; text-decoration:none;
      white-space: nowrap;
    }
    @media (max-width: 600px) {
      /* 모바일: 아이콘-only 한 줄, 고정폭 균등 분배 */
      #nt-dark-nav { gap:6px; padding: 6px 8px calc(6px + env(safe-area-inset-bottom, 0px)); }
      #nt-dark-nav a, #nt-dark-nav button {
        min-width: 0; flex: 1 1 0; max-width: none;
        padding: 10px 4px; font-size: 17px;
      }
      #nt-dark-nav .nt-label { display:none; }   /* 라벨 숨기고 아이콘만 */
    }
    #nt-dark-nav .primary { background:var(--nt-primary, #1f6feb); border-color:var(--nt-primary, #1f6feb); color:#fff; font-weight:700; }
    #nt-dark-nav a.origin { border-color:var(--nt-accent, #238636); color:var(--nt-accent, #3fb950); }
    #nt-dark-nav a:disabled, #nt-dark-nav button:disabled { opacity:.35; cursor:default; }
    #nt-dark-nav .nt-pos { color:var(--nt-muted, #8b949e); font-size:13px; margin:0 6px; }
    #nt-dark-root .nt-hint { color:var(--nt-muted, #6e7681); font-size:12px; text-align:center; margin-top:40px; }
  `;
  root.appendChild(style);

  if (!isNovelEp) root.classList.add('nt-webtoon');

  const DEFAULTS = { font: 18, width: 720, lh: 2.0, warm: 45, theme: 'dark' };
  let cfg = Object.assign({}, DEFAULTS);
  try {
    const saved = localStorage.getItem('ntDarkReaderCfg');
    if (saved) cfg = Object.assign({}, DEFAULTS, JSON.parse(saved));
  } catch (e) {}
  function applyCfg() {
    cfg.warm = Math.max(0, Math.min(100, Number(cfg.warm) || 0));
    cfg.theme = cfg.theme === 'paper' ? 'paper' : 'dark';
    const paper = cfg.theme === 'paper';
    const bg = paper
      ? `color-mix(in srgb, #f7f1e5, #ead09e ${cfg.warm}%)`
      : `color-mix(in srgb, #0d1117, #1b1208 ${cfg.warm}%)`;
    const text = paper
      ? `color-mix(in srgb, #28231e, #49351f ${cfg.warm}%)`
      : `color-mix(in srgb, #d7dde7, #d8b98a ${cfg.warm}%)`;
    const title = paper
      ? `color-mix(in srgb, #1f1b17, #3f2b17 ${cfg.warm}%)`
      : `color-mix(in srgb, #e6edf3, #ead0a8 ${cfg.warm}%)`;
    const ui = paper
      ? { surface: '#e8dcc6', button: '#f5ead5', border: '#cbb996', muted: '#756855', accent: '#76512c', primary: '#8a5a2b', nav: 'rgba(242,232,213,.94)' }
      : { surface: '#161b22', button: '#21262d', border: '#30363d', muted: '#8b949e', accent: '#58a6ff', primary: '#1f6feb', nav: 'rgba(13,17,23,.92)' };
    root.style.setProperty('--nt-font', cfg.font + 'px');
    root.style.setProperty('--nt-width', cfg.width + 'px');
    root.style.setProperty('--nt-lh', cfg.lh);
    root.style.setProperty('--nt-bg', bg);
    root.style.setProperty('--nt-text', text);
    root.style.setProperty('--nt-title', title);
    document.documentElement.style.setProperty('--nt-bg', bg);
    document.documentElement.style.setProperty('--nt-text', text);
    document.documentElement.style.setProperty('--nt-title', title);
    Object.entries(ui).forEach(([name, value]) => {
      root.style.setProperty('--nt-' + (name === 'nav' ? 'nav-bg' : name), value);
      document.documentElement.style.setProperty('--nt-' + (name === 'nav' ? 'nav-bg' : name), value);
    });
    bodyEl.style.setProperty('--theme-novel-text-color', text);
  }
  applyCfg();

  const h1 = document.createElement('div');
  h1.className = 'nt-title';
  h1.textContent = titleText;
  root.appendChild(h1);

  const bodyWrap = document.createElement('div');
  bodyWrap.className = 'nt-body';
  bodyWrap.appendChild(bodyEl);
  root.appendChild(bodyWrap);

  const hint = document.createElement('div');
  hint.className = 'nt-hint';
  hint.textContent = '화면 좌우 가장자리 클릭으로 이전/다음 화 이동';
  root.appendChild(hint);

  document.body.innerHTML = '';
  document.body.appendChild(root);

  const nav = document.createElement('div');
  nav.id = 'nt-dark-nav';

  function navBtn(href, label, icon, cls) {
    if (href) {
      const b = document.createElement('button');
      b.innerHTML = '<span class="nt-ico">' + icon + '</span><span class="nt-label"> ' + label + '</span>';
      if (cls) b.className = cls;
      b.addEventListener('click', e => {
        e.preventDefault();
        e.stopImmediatePropagation();
        go(href);   // 사이트 SPA 라우터를 건너뛰고 새 원본 문서를 로드
      });
      nav.appendChild(b);
    } else {
      const b = document.createElement('button');
      b.disabled = true;
      b.innerHTML = '<span class="nt-ico">' + icon + '</span><span class="nt-label"> ' + label + '</span>';
      nav.appendChild(b);
    }
  }
  navBtn(prevUrl, '이전화', '◀', '');
  navBtn(nextUrl, '다음화', '▶', 'primary');
  if (listUrl) navBtn(listUrl + (listUrl.includes('?') ? '&' : '?') + 'ntlist=1', '목록', '☰', '');

  const exit = document.createElement('a');
  exit.href = '#'; exit.className = 'origin';
  exit.innerHTML = '<span class="nt-ico">↩</span><span class="nt-label"> 원본</span>';
  exit.addEventListener('click', e => {
    e.preventDefault();
    setViewerFlag(false);
    location.reload();
  });
  nav.appendChild(exit);

  const m = titleText.match(/(\d+)화/);
  if (m) {
    const pos = document.createElement('span');
    pos.className = 'nt-pos';
    pos.textContent = m[1] + '화';
    nav.appendChild(pos);
  }

  const gear = document.createElement('button');
  gear.innerHTML = '<span class="nt-ico">⚙</span><span class="nt-label"> 설정</span>';
  nav.appendChild(gear);

  const panel = document.createElement('div');
  panel.id = 'nt-dark-panel';
  panel.innerHTML = `
    <style>
      #nt-dark-panel {
        position: fixed; right: 16px; bottom: 70px; z-index: 100000;
        background:var(--nt-surface, #161b22); border:1px solid var(--nt-border, #30363d); border-radius:10px;
        padding:14px 16px; width: 250px; max-width: calc(100vw - 24px); display:none;
        color:var(--nt-title, #e6edf3); font-size:13px; box-shadow:0 8px 24px rgba(0,0,0,.5);
      }
      #nt-dark-panel.open { display:block; }
      #nt-dark-panel .row { margin-bottom:12px; }
      #nt-dark-panel label { display:flex; justify-content:space-between; margin-bottom:4px; color:var(--nt-muted, #8b949e); }
      #nt-dark-panel input[type=range] { width:100%; accent-color:var(--nt-primary, #1f6feb); }
      #nt-dark-panel .val { color:var(--nt-accent, #58a6ff); font-weight:700; }
      #nt-dark-panel .presets { display:flex; gap:8px; margin-bottom:14px; }
      #nt-dark-panel .presets button { flex:1; }
      #nt-dark-panel .presets button.active { background:var(--nt-primary, #1f6feb); border-color:var(--nt-primary, #1f6feb); color:#fff; font-weight:700; }
      #nt-dark-panel .btns { display:flex; gap:8px; justify-content:flex-end; margin-top:4px; }
      #nt-dark-panel button {
        padding:6px 12px; border-radius:6px; border:1px solid var(--nt-border, #30363d);
        background:var(--nt-button, #21262d); color:var(--nt-title, #e6edf3); cursor:pointer; font-size:12px;
      }
    </style>
    <div class="presets">
      <button type="button" data-theme="dark">🌙 야간 다크</button>
      <button type="button" data-theme="paper">☀ 주간 종이색</button>
    </div>
    <div class="row">
      <label>글자 크기 <span class="val" data-v="font"></span></label>
      <input type="range" id="nt-f" min="12" max="32" step="1">
    </div>
    <div class="row">
      <label>본문 너비 <span class="val" data-v="width"></span></label>
      <input type="range" id="nt-w" min="360" max="1400" step="20">
    </div>
    <div class="row">
      <label>줄 간격 <span class="val" data-v="lh"></span></label>
      <input type="range" id="nt-lh" min="1.4" max="3" step="0.1">
    </div>
    <div class="row">
      <label>따뜻한 색감 <span class="val" data-v="warm"></span></label>
      <input type="range" id="nt-warm" min="0" max="100" step="5">
    </div>
    <div class="btns"><button id="nt-reset">기본값</button><button id="nt-close">닫기</button></div>
  `;
  document.body.appendChild(panel);

  function syncPanel() {
    panel.querySelector('#nt-f').value = cfg.font;
    panel.querySelector('#nt-w').value = cfg.width;
    panel.querySelector('#nt-lh').value = cfg.lh;
    panel.querySelector('#nt-warm').value = cfg.warm;
    panel.querySelector('[data-v=font]').textContent = cfg.font + 'px';
    panel.querySelector('[data-v=width]').textContent = cfg.width + 'px';
    panel.querySelector('[data-v=lh]').textContent = cfg.lh.toFixed(1);
    panel.querySelector('[data-v=warm]').textContent = cfg.warm + '%';
    panel.querySelectorAll('[data-theme]').forEach(button => {
      button.classList.toggle('active', button.dataset.theme === cfg.theme);
    });
  }
  syncPanel();

  function saveCfg() {
    applyCfg(); syncPanel();
    try { localStorage.setItem('ntDarkReaderCfg', JSON.stringify(cfg)); } catch (e) {}
  }
  panel.querySelector('#nt-f').addEventListener('input', e => { cfg.font = +e.target.value; saveCfg(); });
  panel.querySelector('#nt-w').addEventListener('input', e => { cfg.width = +e.target.value; saveCfg(); });
  panel.querySelector('#nt-lh').addEventListener('input', e => { cfg.lh = +e.target.value; saveCfg(); });
  panel.querySelector('#nt-warm').addEventListener('input', e => { cfg.warm = +e.target.value; saveCfg(); });
  panel.querySelectorAll('[data-theme]').forEach(button => {
    button.addEventListener('click', () => { cfg.theme = button.dataset.theme; saveCfg(); });
  });
  panel.querySelector('#nt-reset').addEventListener('click', () => { cfg = Object.assign({}, DEFAULTS); saveCfg(); });
  panel.querySelector('#nt-close').addEventListener('click', () => panel.classList.remove('open'));
  gear.addEventListener('click', e => { e.stopPropagation(); panel.classList.toggle('open'); });
  document.addEventListener('click', e => {
    if (!panel.contains(e.target) && e.target !== gear) panel.classList.remove('open');
  });

  document.body.appendChild(nav);

  /* ---------- 스크롤 내리면 하단바 숨김, 올리거나 바닥에 닿으면 표시 + 읽던 위치 저장 ---------- */
  // 스크롤 위치 저장 키 (회차 단위)
  const scrollKey = (() => {
    try {
      const wm = path.match(/^\/(webtoon|novel)\/([^/]+)\//);
      const ep = (path.split('/').pop() || '').match(/(\d+)/);
      return wm && ep ? 'ntScroll:' + wm[1] + ':' + wm[2] + ':' + ep[1] : null;
    } catch (e) { return null; }
  })();

  let lastY = window.scrollY;
  let saveTimer = null;
  window.addEventListener('scroll', () => {
    const y = window.scrollY;
    const atBottom = window.innerHeight + y >= document.documentElement.scrollHeight - 120;
    if (atBottom) {
      nav.classList.remove('nt-hidden');
    } else if (y > lastY + 8 && y > 120) {
      nav.classList.add('nt-hidden');       // 아래로 스크롤 → 숨김
      panel.classList.remove('open');
    } else if (y < lastY - 8) {
      nav.classList.remove('nt-hidden');    // 위로 스크롤 → 표시
    }
    lastY = y;

    // 읽던 위치 저장 (비율 기반 — 웹툰 이미지 로딩으로 높이 변해도 복원 가능), 500ms 디바운스
    if (scrollKey) {
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => {
        try {
          const max = document.documentElement.scrollHeight - window.innerHeight;
          if (max > 0) localStorage.setItem(scrollKey, String(y / max));
        } catch (e) {}
      }, 500);
    }
  }, { passive: true });

  /* ---------- 읽던 위치 복원 ---------- */
  (function restoreScroll() {
    if (!scrollKey) return;
    let ratio = NaN;
    try { ratio = parseFloat(localStorage.getItem(scrollKey)); } catch (e) {}
    if (!isFinite(ratio) || ratio <= 0) return;
    // 콘텐츠 높이 안정화를 대기하며 비율 위치로 이동 (최대 8초)
    const started = Date.now();
    let lastApplied = -1;
    const t = setInterval(() => {
      const max = document.documentElement.scrollHeight - window.innerHeight;
      if (max > 0) {
        const target = Math.round(ratio * max);
        if (Math.abs(window.scrollY - target) > 40 && target !== lastApplied) {
          lastApplied = target;
          window.scrollTo(0, target);
        }
        // 도달했거나 목표가 더 이상 변하지 않으면 종료
        if (Math.abs(window.scrollY - target) <= 40 || Date.now() - started > 8000) {
          clearInterval(t);
        }
      }
      if (Date.now() - started > 8000) clearInterval(t);
    }, 250);
  })();

  function go(url) { if (url) location.href = url; }
  /* 좌우 가장자리 클릭 → 이전/다음 화 (소설·웹툰 공통, P2)
     웹툰 본문(이미지 영역) 중앙 클릭은 무시 */
  document.addEventListener('click', e => {
    if (e.target.closest('#nt-dark-nav') || e.target.closest('#nt-dark-panel')) return;
    if (!isNovelEp && e.target.closest('.nt-body')) return;
    const x = e.clientX / window.innerWidth;
    if (x < 0.15) go(prevUrl);
    else if (x > 0.85) go(nextUrl);
  });
  } /* end buildViewer */

  /* ---------- 진입: 본문 준비 대기 후 뷰어 구성 ---------- */
  startViewerWhenReady();
})();

