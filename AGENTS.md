# 저장소 및 배포 지침

- `source`는 GitHub 기본 브랜치다. 사용자 스크립트, 서버 코드, 테스트, 문서와 Tampermonkey 배포를 여기에서 관리한다.
- 설치 및 업데이트 주소는 `https://raw.githubusercontent.com/yuisatomi/newtoki-dark-reader/source/newtoki-dark-reader.user.js`다.
- 사용자 스크립트의 새 버전을 배포할 때 `@version`을 올리고 `test-userscript.ps1`을 통과시킨 후 `source`에 푸시한다. 배포 후 raw 주소의 버전을 확인한다.
- `main` 태그는 이전 Tampermonkey 설치 주소에서 5.18을 계속 제공하기 위한 고정 참조다. 태그를 이동하거나 같은 이름의 브랜치를 다시 만들지 않는다.

## 동기화 서버

- 서버 소스는 `server/reader_sync.py`, 서비스 파일은 `server/reader-sync.service`에서 관리한다.
- LXC 실행 경로는 `/opt/reader-sync/reader_sync.py`, systemd 서비스 이름은 `reader-sync`다.
- 공개 API 주소는 `https://reader-sync.flolim.com`이며 `/health`로 상태를 확인한다.
- 서버 변경 전 `python server/reader_sync.py --self-test`와 `test-server.ps1`을 모두 통과시킨다.
- 서버 파일을 교체한 뒤 `reader-sync` 서비스를 재시작하고, 기존 DB를 보존한 상태에서 `/health`와 실제 API를 확인한다.

## 기존 소설 추출 도구 (참고)

- 별도로 만든 로컬 도구는 `999/newtoki_scraper/`에 있다. 서버형 본문 캐시나 독립 뷰어를 검토할 때 먼저 이 구현을 확인한다.
- `scraper.py`는 소설 회차 목록·본문을 수집해 `progress_[ID].json`과 `output/[ID]/*.txt`로 저장하며, `tui.py`는 선택·이어받기 UI, `make_epub.py`는 EPUB 생성을 담당한다.
- 이 도구는 현재 사용자 스크립트 및 `reader-sync` 서버와 연동되지 않는다. `999/`는 현재 Git 미추적 로컬 자료이므로 `source`에 포함되거나 배포된 것으로 가정하지 않는다.
- 실제 사이트에서의 동작, 접근 권한, 저장 데이터의 적합성은 재사용 전에 별도로 검증한다.
