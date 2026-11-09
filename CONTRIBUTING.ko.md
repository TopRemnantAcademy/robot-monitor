# RobotMonitor 기여 가이드

RobotMonitor 개선에 관심을 가져주셔서 감사합니다! 이 가이드는 개발 환경 구성, 브랜치 전략, PR 방법을 안내합니다.

## 행동 강령 (Code of Conduct)

참여하는 모든 분은 [행동 강령](./CODE_OF_CONDUCT.md)을 준수해야 합니다.

## 언어

- **토론·이슈:** 영어가 공용어(lingua franca)지만, 한국어와 중국어도 환영합니다(기여자 다수가 한국 휴머노이드 공장 현장 운영자와 중국 디아스포라 운영 커뮤니티입니다).
- **코드·주석:** 검토자가 최대한 많아야 하므로 **영어**로 작성하세요.
- **UI 문자열:** 화면 텍스트를 절대 하드코딩하지 마세요. [`frontend_dashboard/src/i18n/locales.ts`](./frontend_dashboard/src/i18n/locales.ts)에 추가(영어 키 + 한국어 + 중국어)하고 `useI18n()`으로 해석하세요. 서버가 내려주는 데이터(경보 메시지, 로봇 이름)는 번역하지 않습니다(의도적).

## 개발 환경 구성

### 풀스택 (Docker)

```bash
docker compose -f infra/docker-compose.yml up -d
bash test_integration.sh
```

### 프론트엔드만 (Docker 불필요)

```bash
cd frontend_dashboard
npm install
node mock_backend.mjs            # :8787 에 목 WS+REST 서버 기동
VITE_API_BASE=http://localhost:8787 VITE_WS_HOST=localhost VITE_WS_PORT=8787 npm run dev   # :5173
```

상세 내용은 [docs/developer-guide.md](./docs/developer-guide.md)를 참고하세요(목 백엔드가 EMQX/TimescaleDB/Redis를 어떻게 대체하는지 포함).

## 브랜치 & 커밋

- `main`에서 분기: `feat/...`, `fix/...`, `docs/...`, `chore/...`.
- 커밋은 집중되고 설명이 충분해야 합니다.
- **비밀 정보 금지.** `.env`, `.env.local`, `*.token`은 git 무시 대상입니다. 절대 강제 추가하지 마세요.
- CI는 그린이어야 합니다([`.github/workflows/ci.yml`](./.github/workflows/ci.yml)): 백엔드 pytest, 프론트 `npm test` + typecheck + `vite build` + 번들 크기 가드, 계약 게이트, 실DB e2e(30k landing).

## 풀 리퀘스트

- [PR 템플릿](./.github/PULL_REQUEST_TEMPLATE.md)을 작성하세요.
- 관련 이슈를 연결하세요.
- 무엇을 왜 바꿨는지 증거(테스트 출력, 스크린샷, before/after)와 함께 설명하세요.
- UI 변경 시 `npm test`와 `npm run typecheck`가 로컬에서 통과하는지 확인하세요.

## 버그 신고 & 기능 제안

[`.github/ISSUE_TEMPLATE/`](./.github/ISSUE_TEMPLATE/)의 템플릿을 사용하세요:

- **Bug report** — 재현 단계, 기대 vs 실제, 환경.
- **Feature request** — 운영자 페인포인트와 제안 동작.
- **Good first issue** — 초보자를 위한 작고 명확한 작업.

## 보안

취약점을 발견하셨나요? **공개 이슈로 올리지 마세요.** [SECURITY.md](./SECURITY.md)를 따르고 비공개로 신고하세요.

## 라이선스

기여함으로써 귀하의 기여가 [MIT 라이선스](./LICENSE) 하에 허여됨에 동의합니다.
