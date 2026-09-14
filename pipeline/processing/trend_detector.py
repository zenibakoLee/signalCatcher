from __future__ import annotations

import json
import logging
import statistics
from datetime import date

from pipeline import llm
from pipeline.db import get_connection, get_active_keywords
from pipeline.models import TrendAlert

logger = logging.getLogger(__name__)

MODEL = llm.LUNA_MODEL
MIN_HISTORY_DAYS = 7
NOTABLE_THRESHOLD = 2.0
URGENT_THRESHOLD = 3.0
ACCELERATION_WEEKS = 4
MIN_WEEKLY_AVG = 1.0


def detect_trends(target_date: date | None = None, *, run_id: str | int) -> list[TrendAlert]:
    if target_date is None:
        target_date = date.today()
    date_str = target_date.isoformat()

    conn = get_connection()
    keywords = get_active_keywords(conn)
    alerts: list[TrendAlert] = []

    for keyword in keywords:
        rows = conn.execute(
            """SELECT mention_date, total_count
               FROM keyword_daily_aggregates
               WHERE keyword = ? AND mention_date <= ? AND mention_date > date(?, '-30 days')
               ORDER BY mention_date""",
            (keyword, date_str, date_str),
        ).fetchall()

        if len(rows) < MIN_HISTORY_DAYS:
            continue

        counts = [r["total_count"] for r in rows]
        today_count = counts[-1] if rows[-1]["mention_date"] == date_str else 0

        if today_count == 0:
            continue

        avg_30d = statistics.mean(counts)
        std_30d = statistics.stdev(counts) if len(counts) > 1 else 0.0
        avg_7d = statistics.mean(counts[-7:]) if len(counts) >= 7 else avg_30d

        if std_30d == 0:
            z_score = URGENT_THRESHOLD if today_count > avg_30d else 0.0
        else:
            z_score = (today_count - avg_30d) / std_30d

        if z_score >= NOTABLE_THRESHOLD:
            severity = "urgent" if z_score >= URGENT_THRESHOLD else "notable"
            alert = TrendAlert(
                keyword=keyword,
                alert_date=date_str,
                z_score=round(z_score, 2),
                severity=severity,
                moving_avg_7d=round(avg_7d, 2),
                moving_avg_30d=round(avg_30d, 2),
                std_dev_30d=round(std_30d, 2),
                today_count=today_count,
            )
            alerts.append(alert)

    accel_alerts = detect_long_term_acceleration(target_date)
    alerts.extend(accel_alerts)
    alerts = _merge_alerts(alerts)

    interpretations = _interpret_alerts(alerts, conn=conn, run_id=run_id, persist=False) if alerts else {}
    try:
        conn.execute("BEGIN")
        _store_alerts(conn, alerts)
        for alert in alerts:
            alert.llm_interpretation = interpretations[alert.keyword]
            conn.execute(
                "UPDATE trend_alerts SET llm_interpretation = ? WHERE keyword = ? AND alert_date = ?",
                (alert.llm_interpretation, alert.keyword, alert.alert_date),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    logger.info("Trend detector: %d alerts (%d accel) for %s", len(alerts), len(accel_alerts), date_str)
    return alerts


def _merge_alerts(alerts: list[TrendAlert]) -> list[TrendAlert]:
    severity_rank = {"notable": 1, "accelerating": 2, "urgent": 3}
    by_keyword: dict[str, TrendAlert] = {}
    for alert in alerts:
        current = by_keyword.get(alert.keyword)
        candidate_key = (
            severity_rank.get(alert.severity, 0), alert.z_score, alert.today_count,
            alert.moving_avg_7d, alert.moving_avg_30d, alert.std_dev_30d,
        )
        if current is None:
            by_keyword[alert.keyword] = alert
            continue
        current_key = (
            severity_rank.get(current.severity, 0), current.z_score, current.today_count,
            current.moving_avg_7d, current.moving_avg_30d, current.std_dev_30d,
        )
        if candidate_key > current_key:
            by_keyword[alert.keyword] = alert
    return [by_keyword[keyword] for keyword in sorted(by_keyword, key=lambda value: (value.casefold(), value))]


def detect_long_term_acceleration(target_date: date | None = None) -> list[TrendAlert]:
    if target_date is None:
        target_date = date.today()
    date_str = target_date.isoformat()

    conn = get_connection()
    keywords = get_active_keywords(conn)
    alerts: list[TrendAlert] = []

    for keyword in keywords:
        rows = conn.execute(
            """SELECT mention_date, total_count
               FROM keyword_daily_aggregates
               WHERE keyword = ? AND mention_date <= ? AND mention_date > date(?, '-35 days')
               ORDER BY mention_date""",
            (keyword, date_str, date_str),
        ).fetchall()

        if len(rows) < ACCELERATION_WEEKS * 7:
            continue

        counts = [r["total_count"] for r in rows]
        weekly_avgs = []
        for i in range(ACCELERATION_WEEKS):
            start = len(counts) - (ACCELERATION_WEEKS - i) * 7
            end = start + 7
            if start < 0:
                break
            week_slice = counts[max(0, start):end]
            if week_slice:
                weekly_avgs.append(statistics.mean(week_slice))

        if len(weekly_avgs) < ACCELERATION_WEEKS:
            continue

        if weekly_avgs[-1] < MIN_WEEKLY_AVG:
            continue

        consecutive_rises = all(
            weekly_avgs[i + 1] > weekly_avgs[i]
            for i in range(len(weekly_avgs) - 1)
        )

        if not consecutive_rises:
            continue

        existing = conn.execute(
            "SELECT 1 FROM trend_alerts WHERE keyword = ? AND alert_date = ? AND severity = 'accelerating'",
            (keyword, date_str),
        ).fetchone()
        if existing:
            continue

        growth_rate = (weekly_avgs[-1] - weekly_avgs[0]) / max(weekly_avgs[0], 0.1)
        overall_avg = statistics.mean(counts)

        alert = TrendAlert(
            keyword=keyword,
            alert_date=date_str,
            z_score=round(growth_rate, 2),
            severity="accelerating",
            moving_avg_7d=round(weekly_avgs[-1], 2),
            moving_avg_30d=round(overall_avg, 2),
            std_dev_30d=round(statistics.stdev(counts) if len(counts) > 1 else 0.0, 2),
            today_count=counts[-1],
        )
        alerts.append(alert)

    logger.info("Long-term acceleration: %d keywords accelerating for %s", len(alerts), date_str)
    return alerts


def _store_alerts(conn, alerts: list[TrendAlert]) -> None:
    for alert in alerts:
        conn.execute(
            """INSERT INTO trend_alerts
               (keyword, alert_date, z_score, severity, moving_avg_7d, moving_avg_30d,
                std_dev_30d, today_count, llm_interpretation)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(keyword, alert_date)
               DO UPDATE SET z_score=excluded.z_score, severity=excluded.severity,
                             moving_avg_7d=excluded.moving_avg_7d, moving_avg_30d=excluded.moving_avg_30d,
                             std_dev_30d=excluded.std_dev_30d, today_count=excluded.today_count""",
            (
                alert.keyword,
                alert.alert_date,
                alert.z_score,
                alert.severity,
                alert.moving_avg_7d,
                alert.moving_avg_30d,
                alert.std_dev_30d,
                alert.today_count,
                alert.llm_interpretation,
            ),
        )


def _interpret_alerts(
    alerts: list[TrendAlert], conn=None, *, run_id: str | int, persist: bool = True
) -> dict[str, str]:
    if conn is None:
        conn = get_connection()

    alert_lines = []
    for a in alerts:
        sample_titles = conn.execute(
            """SELECT r.title FROM raw_items r
               JOIN keyword_mentions km ON json_each.value = r.id
               JOIN json_each(km.sample_item_ids) ON 1=1
               WHERE km.keyword = ? AND km.mention_date = ?
               LIMIT 3""",
            (a.keyword, a.alert_date),
        ).fetchall()
        titles = [r["title"] for r in sample_titles]
        alert_lines.append(
            f"- '{a.keyword}': z-score {a.z_score} ({a.severity}), "
            f"오늘 {a.today_count}회 vs 30일 평균 {a.moving_avg_30d}회. "
            f"관련 항목: {titles}"
        )

    prompt = f"""아래는 오늘 감지된 키워드 언급 급증 알림입니다.
각 키워드에 대해 투자 관점에서 이 급증이 왜 발생했고 무엇을 의미하는지 한국어 2문장으로 해석하세요.

{chr(10).join(alert_lines)}

JSON 객체로 반환: {{"interpretations": [{{"keyword": "...", "interpretation": "한국어 해석 2문장"}}]}}
유효한 JSON만 반환하세요."""

    interpretation_entry = {
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "enum": [alert.keyword for alert in alerts]},
            "interpretation": {"type": "string", "minLength": 1},
        },
        "required": ["keyword", "interpretation"],
        "additionalProperties": False,
    }
    llm.require_no_business_transaction(conn)
    result = llm.get_boundary().complete(
        instructions="Interpret trend alerts accurately and concisely.",
        input_text=prompt,
        max_output_tokens=1500,
        model=MODEL,
        workload="trend_interpretation",
        run_id=run_id,
        output_schema=llm.strict_object_schema(
            "trend_interpretations",
            {
                "interpretations": {
                    "type": "array", "minItems": len(alerts), "maxItems": len(alerts),
                    "items": interpretation_entry,
                }
            },
        ),
    )
    interpretations = result.parsed["interpretations"]
    interp_map = {entry["keyword"]: entry["interpretation"] for entry in interpretations}
    if set(interp_map) != {alert.keyword for alert in alerts} or len(interp_map) != len(interpretations):
        raise llm.LLMParseError("trend interpretations must cover each keyword exactly once")
    if persist:
        for alert in alerts:
            alert.llm_interpretation = interp_map[alert.keyword]
            conn.execute(
                "UPDATE trend_alerts SET llm_interpretation = ? WHERE keyword = ? AND alert_date = ?",
                (alert.llm_interpretation, alert.keyword, alert.alert_date),
            )
    return interp_map
