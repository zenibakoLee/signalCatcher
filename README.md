# Signal Catcher

기술 생태계의 **가속 패턴**을 주류 미디어보다 수개월 앞서 자동 감지하는 투자 신호 시스템.

NVIDIA의 2012년 ImageNet 우승 같은 거대한 투자 기회 신호를 포착하는 것이 목표.

## 핵심 기능

- **일일 파이프라인** — 6개 소스(HN, arXiv, GitHub, RSS, YouTube, Reddit) 수집 → LLM 스코어링(0-100) → 관련종목 매핑 → 다이제스트 → Discord 전송
- **기업 모멘텀 분석** — 시그널 누적 종목을 자동 발견, Claude Sonnet으로 5대 질문 프레임워크 기반 모멘텀 레포트 생성 (Google News 보강)
- **트렌드 감지** — z-score 기반 키워드 가속도 감지 (notable >2.0, urgent >3.0)
- **Silent Signals** — 컨퍼런스에서 예상했으나 미발표된 항목의 투자적 의미 분석
- **자동 키워드 관리** — 매일 신규 키워드 자동 발견/활성화, 스파이크 감지, 30일 무언급 은퇴
- **장기 가속 감지** — 4주 연속 상승 키워드 자동 탐지
- **키워드 동시출현 네트워크** — 함께 언급되는 키워드 쌍의 관계 시각화
- **YouTube 자막 보강** — yt-dlp로 YouTube 영상 자막을 추출하여 스코어링 정확도 향상
- **대시보드** — Next.js 16, Cloudflare Tunnel 외부 접속, read-only SQLite, 자동 새로고침, 시각 차트

## 요구사항

- macOS (launchd 스케줄링)
- Python 3.12+
- Node.js 22+ (대시보드)
- yt-dlp (YouTube 자막 추출, 선택)
- API 키: Anthropic, Discord Webhook, GitHub, YouTube

## 설치

```bash
# 1. 클론
git clone git@github.com:zenibakoLee/signalCatcher.git
cd signalCatcher

# 2. Python 환경
python -m venv .venv
source .venv/bin/activate
pip install -e .

# 3. 환경변수
cp .env.example .env
# .env에 API 키 입력:
#   ANTHROPIC_API_KEY, DISCORD_WEBHOOK_URL,
#   GITHUB_TOKEN, YOUTUBE_API_KEY

# 4. 대시보드
cd dashboard
npm install
cd ..

# 5. launchd 등록 (선택)
bash launchd/install.sh install
```

## 사용법

### 파이프라인

```bash
# 일일 실행 (수집 → 자막보강 → 키워드관리 → 스코어링 → 다이제스트 → 기업분석 → Discord)
python -m pipeline daily

# 30일 백필 (HN + GitHub만)
python -m pipeline backfill --days 30

# 컨퍼런스 이벤트 (conferences.yaml 기반)
python -m pipeline event
python -m pipeline event --target-date 2026-01-15

# 기업 모멘텀 분석 (자동 상위 종목 또는 특정 종목)
python -m pipeline analyze
python -m pipeline analyze --ticker NVDA

# 미스코어링 항목 일괄 스코어링
python -m pipeline score-all --batch-limit 200

# 한글 제목 번역 backfill
python -m pipeline translate-titles --batch-limit 500
```

### 대시보드

```bash
cd dashboard
npm run dev
# http://localhost:3000

# 또는 프로덕션 빌드
npm run build && npm start
```

7개 페이지: 다이제스트(시각 대시보드+캘린더), 시그널 탐색(차트+필터), 트렌드(멀티타임프레임+스파크라인+동시출현 네트워크), 기업 분석(모멘텀 레포트+온디맨드 생성), 컨퍼런스 브리핑, 설정(키워드 자동관리), 에디토리얼(기사 초안 관리)

DB 데이터 변경 시 자동 새로고침 (3초 폴링)

### 외부 접속 (Cloudflare Quick Tunnel)

```bash
# 수동 실행
cloudflared tunnel --url http://localhost:3000

# launchd 서비스로 자동 시작 시 Discord에 URL 자동 전송
```

### launchd 스케줄

| 작업 | 시간 | plist |
|------|------|-------|
| daily | 매일 07:00 | `com.signalcatcher.daily.plist` (run-daily.sh 래퍼, 최대 3회 재시도) |
| event | 매일 20:00 | `com.signalcatcher.event.plist` |
| dashboard | 상시 (KeepAlive) | `com.signalcatcher.dashboard.plist` |
| tunnel | 상시 (KeepAlive) | `com.signalcatcher.tunnel.plist` |

```bash
# 상태 확인
bash launchd/install.sh status

# 해제
bash launchd/install.sh uninstall
```

## 프로젝트 구조

```
signalCatcher/
├── config/           # YAML 설정 (keywords, sources, conferences, scoring_prompt)
├── pipeline/         # Python 백엔드 (수집, 분석, 생성, 전송)
│   ├── scripts/      # 대시보드 API에서 호출되는 스크립트 (generate_analysis.py)
│   └── ...
├── dashboard/        # Next.js 16 + Tailwind (read-only SQLite)
├── launchd/          # macOS 서비스 (4개 plist + install.sh)
├── scripts/          # 실행 스크립트 (run-daily.sh, start-dashboard.sh, start-tunnel.sh)
├── docs/             # 프로젝트 문서 (PRD, 스키마, 아키텍처, ADR)
└── data/             # gitignored: signalcatcher.db, logs/
```

## 설정 파일

| 파일 | 용도 |
|------|------|
| `config/keywords.yaml` | 추적 키워드 (카테고리별) |
| `config/sources.yaml` | RSS URL, YouTube 채널/검색어, GitHub 쿼리, Reddit 서브레딧 |
| `config/conferences.yaml` | 컨퍼런스 일정 + 검색어 |
| `config/scoring_prompt.txt` | Claude 스코어링 시스템 프롬프트 |

## 기술 스택

| 계층 | 기술 |
|------|------|
| 수집/분석 | Python 3.12, asyncio, httpx, feedparser, yt-dlp (자막) |
| LLM | Claude Haiku (스코어링/다이제스트/키워드), Sonnet (컨퍼런스/기업분석) |
| 저장 | SQLite WAL, 9개 테이블 |
| 대시보드 | Next.js 16, Tailwind, better-sqlite3, SVG 차트 |
| 전송 | Discord webhook (색상 코드 embed) |
| 외부 접속 | Cloudflare Quick Tunnel |
| 스케줄 | macOS launchd (4개 서비스) |

## 문서

- [`docs/prd.md`](docs/prd.md) — 제품 요구사항
- [`docs/data-schema.md`](docs/data-schema.md) — 데이터 스키마
- [`docs/code-architecture.md`](docs/code-architecture.md) — 코드 아키텍처
- [`docs/adr.md`](docs/adr.md) — 기술 결정 기록 (ADR)

## 라이선스

Private — 비공개 프로젝트
