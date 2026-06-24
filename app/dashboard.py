"""
Read-only Job History dashboard (FastAPI + Jinja2).

Run separately from the poller:
  uvicorn app.dashboard:app --host 127.0.0.1 --port 8080

Uses the same SQLite file as ``Settings.sqlite_path`` (``SQLITE_PATH`` in ``.env``).
The live schema is the ``jobs`` table in ``app.storage.sqlite`` (not ``seen_jobs``).
If a ``notified INTEGER`` column is added later, alert filters and charts use it;
otherwise alert counts stay 0 and the UI shows an explanatory note.
"""

from __future__ import annotations

import logging
import math
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, AsyncGenerator

import aiosqlite
from fastapi import Depends, FastAPI, Query, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.config import Settings, load_file_config

log = logging.getLogger(__name__)

JOBS_TABLE = "jobs"
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class _DashboardState:
    db_path: str = ""
    jobs_columns: frozenset[str] = frozenset()


_state = _DashboardState()


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = Settings()
    _state.db_path = settings.sqlite_path
    try:
        async with aiosqlite.connect(_state.db_path) as conn:
            cur = await conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                (JOBS_TABLE,),
            )
            if not await cur.fetchone():
                _state.jobs_columns = frozenset()
                log.warning("SQLite has no %s table yet — run the bot once to create it.", JOBS_TABLE)
            else:
                cur = await conn.execute(f"PRAGMA table_info({JOBS_TABLE})")
                rows = await cur.fetchall()
                _state.jobs_columns = frozenset(str(r[1]) for r in rows)
    except Exception as e:
        log.error("Dashboard startup DB check failed: %s", e)
        _state.jobs_columns = frozenset()
    yield


app = FastAPI(title="Job History Dashboard", lifespan=_lifespan)


def _has_jobs_table() -> bool:
    return bool(_state.jobs_columns)


def _has_notified_column() -> bool:
    return "notified" in _state.jobs_columns


def _project_timezone() -> str:
    try:
        s = Settings()
        return load_file_config(s.config_path).project.timezone
    except Exception:
        return "Europe/London"


def _local_day_start_utc_iso() -> str:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(_project_timezone())
    now_local = datetime.now(tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(UTC).isoformat()


async def _get_conn() -> AsyncGenerator[aiosqlite.Connection, None]:
    async with aiosqlite.connect(_state.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn


DbConn = Annotated[aiosqlite.Connection, Depends(_get_conn)]


class SummaryJson(BaseModel):
    jobs_today: int
    jobs_week: int
    alerts_today: int
    active_subscribers: int
    last_poll_minutes_ago: int | None


def _normalize_trend_days(raw: int) -> int:
    if raw <= 7:
        return 7
    if raw <= 14:
        return 14
    return 30


def _utc_day_axis(num_days: int) -> tuple[list[str], str]:
    """
    Calendar UTC day labels (oldest first) and ISO cutoff for ``first_seen_utc >= cutoff``.
    Uses substring YYYY-MM-DD bucketing so ISO8601 from Python always groups correctly.
    """
    today = datetime.now(UTC).date()
    start = today - timedelta(days=num_days - 1)
    labels = [(start + timedelta(days=i)).isoformat() for i in range(num_days)]
    cutoff_dt = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
    return labels, cutoff_dt.isoformat()


async def build_stats_payload(conn: aiosqlite.Connection, trend_days: int) -> dict[str, Any]:
    """Chart + insight data for /stats and GET /api/stats."""
    trend_days = _normalize_trend_days(trend_days)
    empty = {
        "trend_days": trend_days,
        "labels_days": [],
        "counts_days": [],
        "loc_labels": [],
        "loc_counts": [],
        "alert_pct": [],
        "type_labels": [],
        "type_counts": [],
        "insights": {
            "jobs_in_window": 0,
            "distinct_locations": 0,
            "busiest_day": None,
            "busiest_day_count": 0,
            "quietest_day_nonzero": None,
            "quietest_day_count": 0,
            "top_location": None,
            "top_location_count": 0,
            "source_split": [],
            "avg_jobs_per_day": 0.0,
            "pct_jobsatamazon": None,
        },
    }
    if not _has_jobs_table():
        return empty

    labels_days, cutoff_iso = _utc_day_axis(trend_days)

    cur = await conn.execute(
        f"""
        SELECT substr(first_seen_utc, 1, 10) AS d, COUNT(*) AS c
        FROM {JOBS_TABLE}
        WHERE first_seen_utc >= ?
          AND length(first_seen_utc) >= 10
        GROUP BY d
        ORDER BY d
        """,
        (cutoff_iso,),
    )
    by_day: dict[str, int] = {}
    for row in await cur.fetchall():
        by_day[str(row["d"])] = int(row["c"])
    counts_days = [by_day.get(d, 0) for d in labels_days]

    pct_by_day: dict[str, float] = {}
    if _has_notified_column():
        cur = await conn.execute(
            f"""
            SELECT substr(first_seen_utc, 1, 10) AS d,
                   SUM(CASE WHEN notified = 1 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS pct
            FROM {JOBS_TABLE}
            WHERE first_seen_utc >= ?
              AND length(first_seen_utc) >= 10
            GROUP BY d
            ORDER BY d
            """,
            (cutoff_iso,),
        )
        for row in await cur.fetchall():
            pct_by_day[str(row["d"])] = round(float(row["pct"]) * 100.0, 2)
    alert_pct = [pct_by_day.get(d, 0.0) for d in labels_days]

    loc_labels: list[str] = []
    loc_counts: list[int] = []
    cur = await conn.execute(
        f"""
        SELECT IFNULL(location, '') AS loc, COUNT(*) AS cnt
        FROM {JOBS_TABLE}
        WHERE first_seen_utc >= ?
        GROUP BY loc
        ORDER BY cnt DESC
        LIMIT 10
        """,
        (cutoff_iso,),
    )
    for row in await cur.fetchall():
        loc = str(row["loc"]).strip() or "(blank)"
        loc_labels.append(loc)
        loc_counts.append(int(row["cnt"]))

    type_labels: list[str] = []
    type_counts: list[int] = []
    cur = await conn.execute(
        f"""
        SELECT IFNULL(source, '') AS src, COUNT(*) AS cnt
        FROM {JOBS_TABLE}
        WHERE first_seen_utc >= ?
        GROUP BY src
        ORDER BY cnt DESC
        """,
        (cutoff_iso,),
    )
    for row in await cur.fetchall():
        lab = str(row["src"]).strip() or "(blank)"
        type_labels.append(lab)
        type_counts.append(int(row["cnt"]))

    jobs_in_window = sum(counts_days)
    cur = await conn.execute(
        f"SELECT COUNT(DISTINCT IFNULL(location, '')) FROM {JOBS_TABLE} WHERE first_seen_utc >= ?",
        (cutoff_iso,),
    )
    distinct_locations = int((await cur.fetchone())[0])

    busiest_idx = 0
    busiest_count = 0
    if counts_days and jobs_in_window > 0:
        busiest_idx = max(range(len(counts_days)), key=lambda i: counts_days[i])
        busiest_count = counts_days[busiest_idx]
    busiest_day = labels_days[busiest_idx] if (labels_days and jobs_in_window > 0) else None

    nonzero = [(labels_days[i], counts_days[i]) for i in range(len(counts_days)) if counts_days[i] > 0]
    if nonzero:
        qd, qc = min(nonzero, key=lambda x: x[1])
        quietest_day_nonzero, quietest_day_count = qd, qc
    else:
        quietest_day_nonzero, quietest_day_count = None, 0

    top_location = loc_labels[0] if loc_labels else None
    top_location_count = loc_counts[0] if loc_counts else 0

    source_split: list[dict[str, Any]] = []
    if jobs_in_window > 0:
        for lab, cnt in zip(type_labels, type_counts):
            source_split.append(
                {
                    "label": lab,
                    "count": cnt,
                    "pct": round(100.0 * cnt / jobs_in_window, 1),
                }
            )
    ja_pct: float | None = None
    for s in source_split:
        if "jobsatamazon" in s["label"].lower():
            ja_pct = s["pct"]
            break

    avg_jobs_per_day = round(jobs_in_window / trend_days, 2) if trend_days else 0.0

    return {
        "trend_days": trend_days,
        "labels_days": labels_days,
        "counts_days": counts_days,
        "loc_labels": loc_labels,
        "loc_counts": loc_counts,
        "alert_pct": alert_pct,
        "type_labels": type_labels,
        "type_counts": type_counts,
        "insights": {
            "jobs_in_window": jobs_in_window,
            "distinct_locations": distinct_locations,
            "busiest_day": busiest_day,
            "busiest_day_count": busiest_count,
            "quietest_day_nonzero": quietest_day_nonzero,
            "quietest_day_count": quietest_day_count,
            "top_location": top_location,
            "top_location_count": top_location_count,
            "source_split": source_split,
            "avg_jobs_per_day": avg_jobs_per_day,
            "pct_jobsatamazon": ja_pct,
        },
    }


class StatsJson(BaseModel):
    trend_days: int
    labels_days: list[str]
    counts_days: list[int]
    loc_labels: list[str]
    loc_counts: list[int]
    alert_pct: list[float]
    type_labels: list[str]
    type_counts: list[int]
    insights: dict[str, Any]


async def _compute_summary(conn: aiosqlite.Connection) -> dict[str, Any]:
    if not _has_jobs_table():
        n_sub = 0
        try:
            cur = await conn.execute("SELECT COUNT(*) FROM subscribers")
            row = await cur.fetchone()
            n_sub = int(row[0]) if row else 0
        except Exception:
            pass
        return {
            "jobs_today": 0,
            "jobs_week": 0,
            "alerts_today": 0,
            "active_subscribers": n_sub,
            "last_poll_minutes_ago": None,
        }

    day_start = _local_day_start_utc_iso()
    week_cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()

    cur = await conn.execute(
        f"SELECT COUNT(*) FROM {JOBS_TABLE} WHERE first_seen_utc >= ?",
        (day_start,),
    )
    jobs_today = int((await cur.fetchone())[0])

    cur = await conn.execute(
        f"SELECT COUNT(*) FROM {JOBS_TABLE} WHERE first_seen_utc >= ?",
        (week_cutoff,),
    )
    jobs_week = int((await cur.fetchone())[0])

    if _has_notified_column():
        cur = await conn.execute(
            f"SELECT COUNT(*) FROM {JOBS_TABLE} WHERE notified = 1 AND first_seen_utc >= ?",
            (day_start,),
        )
        alerts_today = int((await cur.fetchone())[0])
    else:
        alerts_today = 0

    active_subscribers = 0
    try:
        cur = await conn.execute("SELECT COUNT(*) FROM subscribers")
        active_subscribers = int((await cur.fetchone())[0])
    except Exception:
        pass

    last_poll_minutes_ago: int | None = None
    cur = await conn.execute(f"SELECT MAX(last_seen_utc) FROM {JOBS_TABLE}")
    row = await cur.fetchone()
    last_raw = row[0] if row else None
    if last_raw:
        try:
            last_dt = datetime.fromisoformat(str(last_raw).replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=UTC)
            delta = datetime.now(UTC) - last_dt.astimezone(UTC)
            last_poll_minutes_ago = max(0, int(delta.total_seconds() // 60))
        except Exception:
            last_poll_minutes_ago = None

    return {
        "jobs_today": jobs_today,
        "jobs_week": jobs_week,
        "alerts_today": alerts_today,
        "active_subscribers": active_subscribers,
        "last_poll_minutes_ago": last_poll_minutes_ago,
    }


def _pay_cell(row: aiosqlite.Row) -> str:
    pt = row["pay_text"]
    if pt is not None and str(pt).strip():
        return str(pt).strip()
    raw = row["pay_gbp_per_hour"]
    if raw is not None:
        try:
            return f"£{float(raw):.2f}/hr"
        except (TypeError, ValueError):
            pass
    return "—"


def _notified_value(row: aiosqlite.Row) -> int | None:
    if not _has_notified_column():
        return None
    try:
        return int(row["notified"])
    except (KeyError, TypeError, ValueError):
        return None


@app.get("/api/summary")
async def api_summary(conn: DbConn) -> SummaryJson:
    data = await _compute_summary(conn)
    return SummaryJson.model_validate(data)


@app.get("/")
async def index(request: Request, conn: DbConn) -> Any:
    summary = await _compute_summary(conn)
    recent: list[dict[str, Any]] = []
    if _has_jobs_table():
        sel = (
            "key, title, location, pay_text, pay_gbp_per_hour, source, "
            "source_url, first_seen_utc"
        )
        if _has_notified_column():
            sel += ", notified"
        cur = await conn.execute(
            f"SELECT {sel} FROM {JOBS_TABLE} ORDER BY first_seen_utc DESC LIMIT 50",
        )
        for row in await cur.fetchall():
            recent.append(
                {
                    "title": row["title"] or "—",
                    "location": row["location"] or "—",
                    "pay": _pay_cell(row),
                    "type": row["source"] or "—",
                    "first_seen": row["first_seen_utc"],
                    "source_url": row["source_url"] or "#",
                    "notified": _notified_value(row),
                }
            )

    last_m = summary["last_poll_minutes_ago"]
    if last_m is None:
        last_poll_label = "No job rows yet (never polled into this DB)"
    elif last_m == 0:
        last_poll_label = "Last poll: just now"
    elif last_m == 1:
        last_poll_label = "Last poll: 1 minute ago"
    else:
        last_poll_label = f"Last poll: {last_m} minutes ago"

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "nav_active": "home",
            "summary": summary,
            "recent": recent,
            "last_poll_label": last_poll_label,
            "track_alerts": _has_notified_column(),
            "timezone_note": _project_timezone(),
        },
    )


def _parse_days(raw: str) -> int | None:
    if raw.lower() == "all":
        return None
    try:
        n = int(raw)
    except ValueError:
        return 7
    if n in (7, 14, 30):
        return n
    return 7


@app.get("/jobs")
async def jobs_page(
    request: Request,
    conn: DbConn,
    location: str = Query("", description="LIKE filter on location"),
    title: str = Query("", description="LIKE filter on title"),
    source: str = Query("", description="Exact source filter (use __blank__ for empty)"),
    alerted: str = Query("all"),
    days: str = Query("7"),
    page: int = Query(1, ge=1),
) -> Any:
    if not _has_jobs_table():
        return templates.TemplateResponse(
            request,
            "jobs.html",
            {
                "nav_active": "jobs",
                "rows": [],
                "total": 0,
                "page": 1,
                "total_pages": 0,
                "location": location,
                "title": title,
                "source": source,
                "alerted": alerted,
                "days": days,
                "track_alerts": _has_notified_column(),
            },
        )

    where: list[str] = ["1=1"]
    params: list[Any] = []

    tq = title.strip()
    if tq:
        where.append("IFNULL(title,'') LIKE ?")
        params.append(f"%{tq}%")
    lq = location.strip()
    if lq == "__blank__":
        where.append("(location IS NULL OR TRIM(IFNULL(location,'')) = '')")
    elif lq:
        where.append("IFNULL(location,'') LIKE ?")
        params.append(f"%{lq}%")

    sq = source.strip()
    if sq == "__blank__":
        where.append("(source IS NULL OR TRIM(IFNULL(source,'')) = '')")
    elif sq:
        where.append("IFNULL(source,'') = ?")
        params.append(sq)

    if _has_notified_column():
        if alerted == "yes":
            where.append("notified = 1")
        elif alerted == "no":
            where.append("COALESCE(notified, 0) = 0")

    days_n = _parse_days(days)
    if days_n is not None:
        cutoff = (datetime.now(UTC) - timedelta(days=days_n)).isoformat()
        where.append("first_seen_utc >= ?")
        params.append(cutoff)

    where_sql = " AND ".join(where)

    cur = await conn.execute(
        f"SELECT COUNT(*) FROM {JOBS_TABLE} WHERE {where_sql}",
        params,
    )
    total = int((await cur.fetchone())[0])

    per_page = 25
    total_pages = max(1, math.ceil(total / per_page)) if total else 1
    page = min(page, total_pages)
    offset = (page - 1) * per_page

    sel = (
        "key, title, location, pay_text, pay_gbp_per_hour, source, "
        "source_url, first_seen_utc"
    )
    if _has_notified_column():
        sel += ", notified"
    cur = await conn.execute(
        f"SELECT {sel} FROM {JOBS_TABLE} WHERE {where_sql} "
        f"ORDER BY first_seen_utc DESC LIMIT ? OFFSET ?",
        [*params, per_page, offset],
    )
    rows: list[dict[str, Any]] = []
    for row in await cur.fetchall():
        rows.append(
            {
                "title": row["title"] or "—",
                "location": row["location"] or "—",
                "pay": _pay_cell(row),
                "type": row["source"] or "—",
                "first_seen": row["first_seen_utc"],
                "source_url": row["source_url"] or "#",
                "notified": _notified_value(row),
            }
        )

    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "nav_active": "jobs",
            "rows": rows,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "location": location,
            "title": title,
            "source": source,
            "alerted": alerted,
            "days": days,
            "track_alerts": _has_notified_column(),
        },
    )


@app.get("/api/stats")
async def api_stats(conn: DbConn, days: int = Query(14, ge=7, le=90)) -> StatsJson:
    payload = await build_stats_payload(conn, days)
    return StatsJson.model_validate(payload)


@app.get("/stats")
async def stats_page(
    request: Request,
    conn: DbConn,
    days: int = Query(14, ge=7, le=90),
) -> Any:
    trend_days = _normalize_trend_days(days)
    payload = await build_stats_payload(conn, trend_days)
    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "nav_active": "stats",
            "stats_payload": payload,
            "track_alerts": _has_notified_column(),
        },
    )
