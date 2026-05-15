# Signal Catcher

기술 생태계의 **가속 패턴**을 주류 미디어보다 수개월 앞서 자동 감지하는 투자 신호 시스템.

NVIDIA의 2012년 ImageNet 우승 같은 거대한 투자 기회 신호를 포착하는 것이 목표.

## 핵심 기능

- **일일 파이프라인** — 5개 소스(HN, arXiv, GitHub, RSS, YouTube) 수집 → LLM 스코어링(0-100) → 다이제스트 → Discord 전송
- **트렌드 감지** — z-score 기반 키워드 가속도 감지 (notable >2.0, urgent >3.0)
- **Silent Signals** — 컨퍼런스에서 예상했으나 미발표된 항목의 투자적 의미 분석
- **주간 키워드 제안** — 미추적 빈출 구문을 LLM이 평가 → 신규 키워드 추천
- **장기 가속 감지** — 4주 연속 상승 키워드 자동 탐지
- **키워드 동시출현 네트워크** — 함께 언급되는 키워드 쌍의 관계 시각화
- **대시보드** — Next.js 16, Cloudflare Tunnel 외부 접속, read-only SQLite, 자동 새로고침

## 요구사항

- macOS (launchd 스케줄링)
- Python 3.12+
- Node.js 22+ (대시보드)
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
# 일일 실행 (수집 → 스코어링 → 다이제스트 → Discord)
python -m pipeline daily

# 30일 백필 (HN + GitHub만)
python -m pipeline backfill --days 30

# 주간 키워드 제안
python -m pipeline weekly

# 컨퍼런스 이벤트 (conferences.yaml 기반)
python -m pipeline event
python -m pipeline event --target-date 2026-01-15

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

5개 페이지: 다이제스트(캘린더 날짜 선택), 시그널 탐색, 트렌드 차트(멀티타임프레임+동시출현 네트워크), 컨퍼런스 브리핑, 설정

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
| daily | 매일 07:00 | `com.signalcatcher.daily.plist` |
| weekly | 일요일 09:00 | `com.signalcatcher.weekly.plist` |
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
├── config/       # YAML 설정 (keywords, sources, conferences, scoring_prompt)
├── pipeline/     # Python 백엔드 (수집, 분석, 생성, 전송)
├── dashboard/    # Next.js 16 + Tailwind (read-only SQLite)
├── launchd/      # macOS 서비스 (5개 plist + install.sh)
├── scripts/      # 대시보드·터널 시작 스크립트
├── docs/         # 프로젝트 문서 (PRD, 스키마, 아키텍처, ADR)
└── data/         # gitignored: signalcatcher.db, logs/
```

## 설정 파일

| 파일 | 용도 |
|------|------|
| `config/keywords.yaml` | 추적 키워드 (카테고리별) |
| `config/sources.yaml` | RSS URL, YouTube 채널, GitHub 쿼리 |
| `config/conferences.yaml` | 컨퍼런스 일정 + 검색어 |
| `config/scoring_prompt.txt` | Claude 스코어링 시스템 프롬프트 |

## 기술 스택

| 계층 | 기술 |
|------|------|
| 수집/분석 | Python 3.12, asyncio, httpx, feedparser |
| LLM | Claude Haiku (스코어링/다이제스트), Sonnet (컨퍼런스) |
| 저장 | SQLite WAL, 8개 테이블 |
| 대시보드 | Next.js 16, Tailwind, better-sqlite3 |
| 전송 | Discord webhook (색상 코드 embed) |
| 외부 접속 | Cloudflare Quick Tunnel |
| 스케줄 | macOS launchd (5개 서비스) |

## 문서

- [`docs/prd.md`](docs/prd.md) — 제품 요구사항
- [`docs/data-schema.md`](docs/data-schema.md) — 데이터 스키마
- [`docs/code-architecture.md`](docs/code-architecture.md) — 코드 아키텍처
- [`docs/adr.md`](docs/adr.md) — 기술 결정 기록 (ADR)

## 라이선스

Private — 비공개 프로젝트
