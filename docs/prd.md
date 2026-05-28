# Signal Catcher — PRD

## 핵심 명제

기술 생태계의 **가속 패턴**을 주류 미디어보다 수개월 앞서 자동 감지한다.
NVIDIA의 2012년 ImageNet 우승 같은 거대한 투자 기회 신호를 포착하는 것이 목표.

## 사용자

매크로 투자자 1인. 기술 트렌드의 *초기 가속 신호*를 정량적으로 추적하고자 함.

## 핵심 가치

| 우선순위 | 가치 | 설명 |
|---------|------|------|
| 1 | 조기 감지 | z-score 기반 키워드 가속도 → 주류 미디어 선행 |
| 2 | Silent Signals | 컨퍼런스에서 *예상했으나 미발표*된 항목의 투자적 의미 분석 |
| 3 | 무인 운영 | launchd 스케줄링, 에러 Discord 알림, 멱등성 보장 |
| 4 | 맥락 보존 | 점수/해석/추론 근거를 모두 저장하여 사후 검증 가능 |

## 기능 범위

### 일일 파이프라인 (07:00 KST)
수집(6개 소스) → 중복제거 → 키워드카운팅 → 트렌드감지(z-score) → LLM스코어링(0-100) → 다이제스트생성 → Discord전송

### 주간 파이프라인 (일요일 09:00)
미추적 빈출 구문 추출 → LLM 키워드 제안 → Discord 전송, 대시보드에서 승인/거부

### 이벤트 파이프라인 (20:00)
conferences.yaml 기반 자동 트리거:
- **시작 2~1일 전**: Claude Sonnet으로 예상 발표 목록 생성 (pre_event)
- **종료 1~3일 후**: 수집 데이터 대비 silent_signals 분석 (post_event)

### 대시보드 (Cloudflare Tunnel 외부 접속)
Next.js 16, read-only SQLite, DB 변경 자동 새로고침. 5개 페이지:
- **다이제스트**: 캘린더 날짜 선택, 한글 제목 + 영어 원문 표시
- **시그널 탐색**: 소스·카테고리·최소점수 필터
- **트렌드 차트**: 멀티타임프레임(7/30/90일/전체), 장기 가속 섹션, 키워드 동시출현 네트워크 그래프
- **컨퍼런스 브리핑**: 관련 수집 콘텐츠 하이퍼링크
- **설정**: 키워드 관리

## 데이터 소스

| 소스 | API | Rate Limit | 비고 |
|------|-----|-----------|------|
| Hacker News | Algolia Search | 2/s | front_page top30 + 키워드검색 |
| arXiv | Atom XML | 0.1/s (10초) | company/infrastructure 카테고리 skip |
| GitHub | Search API | 0.5/s | 인증 필수, trending 근사 |
| RSS | feedparser | 2/s | 12개 피드 (TechCrunch, NVIDIA Blog, SemiAnalysis 등) |
| YouTube | playlistItems + Search | 1/s | 12개 채널 playlistItems(1유닛) + 4개 검색 쿼리 search(100유닛) |
| Reddit | JSON API | 1.5/s | 11개 서브레딧 (technology, MachineLearning, wallstreetbets 등) |

## 비기능 요구사항

- **언어**: 모든 사용자 대면 출력(Discord, 대시보드, 스코어링)은 한국어
- **멱등성**: 같은 날 2회 실행해도 중복 없음 (UNIQUE 제약 + ON CONFLICT)
- **격리**: 수집기별 독립 실패 (HN 실패해도 나머지 진행)
- **인프라**: 로컬 Mac, SQLite WAL, launchd(5개 서비스), Cloudflare Quick Tunnel. 클라우드/CI 없음
