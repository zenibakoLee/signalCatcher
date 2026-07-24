"""SEC Form 4 내부자 거래 수집기.

임원·이사의 자사주 매매 공시(Form 4)를 파싱해 공개시장 매수(code P)를 신호로 낸다.
내부자 집단 매수는 가장 잘 검증된 알파 신호 중 하나 — 실적 발표 전 경영진이 자기 돈으로
사는 것. 대규모 매도(code S)도 주의 신호로 함께 낸다.

data.sec.gov submissions JSON으로 최근 Form 4를 찾고, 각 파일링 XML의 거래 코드를 파싱.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime

import httpx

from pipeline.collectors.base import BaseCollector
from pipeline.models import RawItem
from pipeline.utils.rate_limiter import RateLimiter
from pipeline.utils.retry import with_retry

logger = logging.getLogger(__name__)

_UA = "SignalCatcher research 0oooceanhigh@gmail.com"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{doc}"

# 추적 대상 (티커: CIK) — 8-K 공시와 동일 + 핵심 종목
TRACKED = {
    "NVDA": 1045810, "MU": 723125, "AMD": 2488, "AVGO": 1730168,
    "MSFT": 789019, "INTC": 50863, "TSLA": 1318605, "GOOGL": 1652044,
}
MIN_BUY_VALUE = 50_000  # 소액 노이즈 제외


class SECForm4Collector(BaseCollector):
    def __init__(self, rate_limiter: RateLimiter | None = None):
        super().__init__(rate_limiter or RateLimiter(3.0))

    async def collect(self, keywords: list[str], since: datetime, keyword_categories: dict[str, str] | None = None) -> list[RawItem]:
        items: list[RawItem] = []
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": _UA}) as client:
            for ticker, cik in TRACKED.items():
                await self.rate_limiter.acquire()
                try:
                    items.extend(await self._collect_ticker(client, ticker, cik, since))
                except Exception:
                    logger.exception("Form4: failed for %s", ticker)
        logger.info("SEC Form4: collected %d insider transactions", len(items))
        return items

    async def _collect_ticker(self, client, ticker: str, cik: int, since: datetime) -> list[RawItem]:
        async def _sub():
            r = await client.get(SUBMISSIONS_URL.format(cik=cik))
            r.raise_for_status()
            return r
        resp = await with_retry(_sub)
        recent = resp.json().get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accs = recent.get("accessionNumber", [])
        docs = recent.get("primaryDocument", [])
        dates = recent.get("filingDate", [])

        items: list[RawItem] = []
        for i, form in enumerate(forms):
            if form != "4":
                continue
            try:
                fdate = datetime.strptime(dates[i], "%Y-%m-%d")
            except (ValueError, IndexError):
                continue
            if fdate < since:
                continue

            acc_nodash = accs[i].replace("-", "")
            # 실제 데이터 XML은 xslF345X06/ 프리픽스 제거한 경로
            doc = docs[i].split("/")[-1]
            url = ARCHIVE_URL.format(cik=cik, acc_nodash=acc_nodash, doc=doc)
            await self.rate_limiter.acquire()
            try:
                r = await client.get(url)
                if r.status_code != 200:
                    continue
                signal = self._parse_form4(r.text, ticker, fdate, url, accs[i])
                if signal:
                    items.append(signal)
            except Exception:
                logger.debug("Form4: parse failed %s", url)
        return items

    def _parse_form4(self, xml_text: str, ticker: str, fdate: datetime, url: str, acc: str) -> RawItem | None:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return None

        issuer = (root.findtext(".//issuerName") or ticker).strip()
        owner = (root.findtext(".//rptOwnerName") or "?").strip()
        title = (root.findtext(".//officerTitle") or "").strip()

        buys, sells = [], []
        for tx in root.findall(".//nonDerivativeTransaction"):
            code = (tx.findtext(".//transactionCode") or "").strip()
            if code not in ("P", "S"):
                continue
            shares = _num(tx.findtext(".//transactionShares/value"))
            price = _num(tx.findtext(".//transactionPricePerShare/value"))
            value = shares * price
            if value < MIN_BUY_VALUE:
                continue
            (buys if code == "P" else sells).append((shares, price, value))

        if not buys and not sells:
            return None

        role = f", {title}" if title else ""
        if buys:
            total = sum(v for _, _, v in buys)
            shares = sum(s for s, _, _ in buys)
            head = f"[내부자 매수] {issuer} ({ticker}): {owner}{role} — {shares:,.0f}주 매수 (약 ${total:,.0f})"
            direction = "insider_buy"
        else:
            total = sum(v for _, _, v in sells)
            shares = sum(s for s, _, _ in sells)
            head = f"[내부자 매도] {issuer} ({ticker}): {owner}{role} — {shares:,.0f}주 매도 (약 ${total:,.0f})"
            direction = "insider_sell"

        return RawItem(
            source="sec_form4",
            source_id=f"form4_{acc}",
            title=head,
            url=url,
            author=owner,
            content_snippet=f"{issuer} {direction}. 신고일 {fdate.date()}.",
            published_at=fdate,
            metadata={"ticker": ticker, "direction": direction, "total_value": round(total)},
        )


def _num(s: str | None) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0
