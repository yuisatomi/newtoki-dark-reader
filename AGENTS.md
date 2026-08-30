# 저장소 및 배포 지침

- `source` 브랜치는 전체 소스의 기준 브랜치다. 사용자 스크립트, 서버 코드, 테스트와 문서를 모두 여기에서 관리한다.
- `main` 브랜치는 Tampermonkey 배포 전용이다. `README.md`와 `newtoki-dark-reader.user.js`만 유지한다.
- 전체 소스 브랜치를 `main`에 직접 푸시하지 않는다. 서버 코드나 테스트를 배포 브랜치에 섞지 않는다.
- 설치 및 자동 업데이트 경로는 항상 다음 주소다.
  - `https://raw.githubusercontent.com/yuisatomi/newtoki-dark-reader/main/newtoki-dark-reader.user.js`
- 배포 전 `newtoki-dark-reader.user.js`의 `@version`을 올리고 `test-userscript.ps1`을 통과시킨다.
- 소스 변경을 먼저 `source`에 커밋·푸시한 다음, 배포용 `main`의 기존 이력 위에 `newtoki-dark-reader.user.js`만 반영해 푸시한다.
- 배포 후 raw 주소의 `@version`이 소스 버전과 같은지 확인한다.
- 배포가 실패하면 강제 푸시하지 않는다. 원격 `main`을 다시 가져와 그 위에 스크립트만 재적용한다.

## 동기화 서버

- 서버 소스는 `server/reader_sync.py`, 서비스 파일은 `server/reader-sync.service`에서 관리한다.
- LXC 실행 경로는 `/opt/reader-sync/reader_sync.py`, systemd 서비스 이름은 `reader-sync`다.
- 공개 API 주소는 `https://reader-sync.flolim.com`이며 `/health`로 상태를 확인한다.
- 서버 변경 전 `python server/reader_sync.py --self-test`와 `test-server.ps1`을 모두 통과시킨다.
- 서버 파일을 교체한 뒤 `reader-sync` 서비스를 재시작하고, 기존 DB를 보존한 상태에서 `/health`와 실제 API를 확인한다.
