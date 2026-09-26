"""Read-only MySQL statistics. No advertising metrics are stored in SQLite."""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

import pymysql
import settings  # loads the ignored .env before reading MYSQL_* variables


TABLES = ("buyer_stats_today_start_sub", "creos", "traffers")
IDENT = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def configured() -> bool:
    return all(os.getenv(key) for key in
               ("MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE"))


@contextmanager
def connection():
    missing = [k for k in ("MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE")
               if not os.getenv(k)]
    if missing:
        raise RuntimeError("Не настроен MySQL: " + ", ".join(missing))
    conn = pymysql.connect(
        host=os.environ["MYSQL_HOST"],
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASSWORD"],
        database=os.environ["MYSQL_DATABASE"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=30,
        autocommit=True,
    )
    try:
        yield conn
    finally:
        conn.close()


def pick(columns: set[str], candidates: tuple[str, ...], table: str) -> str:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    raise ValueError(f"В {table} отсутствует колонка из: {', '.join(candidates)}")


def quoted(identifier: str) -> str:
    if not IDENT.fullmatch(identifier):
        raise ValueError("Недопустимое имя колонки")
    return f"`{identifier}`"


def discover(conn) -> dict:
    """Identify only the columns actually needed; never accept user SQL."""
    result = {}
    with conn.cursor() as cursor:
        for table in TABLES:
            cursor.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                (os.environ["MYSQL_DATABASE"], table),
            )
            columns = {row["COLUMN_NAME"] for row in cursor.fetchall()}
            if not columns:
                raise ValueError(f"Таблица MySQL {table} не найдена или недоступна")
            result[table] = columns
    s, c, t = (result[table] for table in TABLES)
    return {
        "creo_date": pick(c, ("date", "stat_date", "created_at"), "creos"),
        "creo_name": pick(c, ("creo_name", "creative_name"), "creos"),
        "creo_spend": pick(c, ("budget", "spend", "total_spend"), "creos"),
        "creo_buyer": pick(c, ("id_traf", "traffer_id", "buyer_id"), "creos"),
        "stats_date": pick(s, ("date", "stat_date"), "buyer_stats_today_start_sub"),
        "stats_name": pick(s, ("creo_name", "creative_name"), "buyer_stats_today_start_sub"),
        "stats_buyer": next(
            (name for name in ("id_traf", "traffer_id", "buyer_id") if name in s), None
        ),
        "starts": pick(s, ("count_start", "total_starts", "starts"), "buyer_stats_today_start_sub"),
        "subs": pick(s, ("count_sub", "total_subs", "subs"), "buyer_stats_today_start_sub"),
        "regs": pick(s, ("count_reg", "total_regs", "regs"), "buyer_stats_today_start_sub"),
        "ftd": pick(s, ("count_ftd", "total_ftd", "ftd"), "buyer_stats_today_start_sub"),
        "traffer_id": pick(t, ("id_traf", "id", "traffer_id", "buyer_id"), "traffers"),
        "traffer_name": pick(t, ("name", "traffer_name", "buyer_name", "title", "fio"), "traffers"),
        "traffer_status": pick(t, ("traffer_status", "status"), "traffers"),
    }


def buyers(conn) -> list[dict]:
    cols = discover(conn)
    id_col, name_col = (quoted(cols[k]) for k in ("traffer_id", "traffer_name"))
    status_col = quoted(cols["traffer_status"])
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT {id_col} AS buyer_id, {name_col} AS buyer_name, "
            f"{status_col} AS buyer_status "
            f"FROM `traffers` ORDER BY {name_col}"
        )
        return [{"id": str(row["buyer_id"]), "name": str(row["buyer_name"]),
                 "status": str(row["buyer_status"] or "не указан")}
                for row in cursor.fetchall()]


def buyer(conn, buyer_id: str) -> dict | None:
    cols = discover(conn)
    id_col, name_col = (quoted(cols[k]) for k in ("traffer_id", "traffer_name"))
    status_col = quoted(cols["traffer_status"])
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT {id_col} AS buyer_id, {name_col} AS buyer_name, "
            f"{status_col} AS buyer_status "
            f"FROM `traffers` WHERE {id_col}=%s LIMIT 1",
            (buyer_id,),
        )
        row = cursor.fetchone()
        return {"id": str(row["buyer_id"]), "name": str(row["buyer_name"]),
                "status": str(row["buyer_status"] or "не указан")} if row else None


def _number(value):
    if value is None:
        return None
    return float(value) if isinstance(value, Decimal) else value


def statistics(conn, buyer_id: str, first: date, last: date,
               creative: str | None = None, exact: bool = False) -> list[dict]:
    """Aggregate by buyer, calendar date and creative, as in the DataLens join.

    Stats are aggregated before joining to ad spend to avoid multiplication
    when either source has duplicate creative/date rows.
    """
    cols = discover(conn)
    q = lambda key: quoted(cols[key])
    events_buyer_filter = (
        f"AND {q('stats_buyer')}=%s" if cols["stats_buyer"] else ""
    )
    # Both predicates are parameterized. LOCATE treats %, _ and backslashes
    # as literal characters, unlike a LIKE expression.
    def creative_predicate(column: str) -> str:
        if not creative:
            return ""
        if exact:
            return f"AND LOWER({column})=LOWER(%s)"
        return f"AND LOCATE(LOWER(%s), LOWER({column}))>0"

    cost_creative_filter = creative_predicate(q("creo_name"))
    event_creative_filter = creative_predicate(q("stats_name"))
    query = f"""
        SELECT cost.day AS stat_date, cost.creative_name, cost.spend,
               cost.missing_spend_rows,
               COALESCE(events.starts,0) AS starts,
               COALESCE(events.subs,0) AS subs,
               COALESCE(events.regs,0) AS regs,
               COALESCE(events.ftd,0) AS ftd
        FROM (
            SELECT DATE({q("creo_date")}) AS day, {q("creo_name")} AS creative_name,
                   SUM(CAST(NULLIF(TRIM({q("creo_spend")}), '') AS DECIMAL(20,8))) AS spend,
                   SUM({q("creo_spend")} IS NULL OR TRIM({q("creo_spend")})='') AS missing_spend_rows
            FROM `creos`
            WHERE {q("creo_buyer")}=%s
              AND {q("creo_date")} >= %s AND {q("creo_date")} < %s
              {cost_creative_filter}
            GROUP BY DATE({q("creo_date")}), {q("creo_name")}
        ) AS cost
        LEFT JOIN (
            SELECT DATE({q("stats_date")}) AS day, {q("stats_name")} AS creative_name,
                   SUM(COALESCE({q("starts")},0)) AS starts,
                   SUM(COALESCE({q("subs")},0)) AS subs,
                   SUM(COALESCE({q("regs")},0)) AS regs,
                   SUM(COALESCE({q("ftd")},0)) AS ftd
            FROM `buyer_stats_today_start_sub`
            WHERE {q("stats_date")} >= %s AND {q("stats_date")} < %s
              {event_creative_filter}
              {events_buyer_filter}
            GROUP BY DATE({q("stats_date")}), {q("stats_name")}
        ) AS events
          ON events.day=cost.day AND events.creative_name=cost.creative_name
        ORDER BY cost.day, cost.creative_name
    """
    from datetime import timedelta
    end_exclusive = last + timedelta(days=1)
    with conn.cursor() as cursor:
        parameters = [buyer_id, first, end_exclusive, first, end_exclusive]
        if creative:
            parameters = [buyer_id, first, end_exclusive, creative,
                          first, end_exclusive, creative]
        if cols["stats_buyer"]:
            parameters.append(buyer_id)
        cursor.execute(query, parameters)
        return [
            {"stat_date": str(row["stat_date"]), "creative_name": row["creative_name"],
             "spend_missing": bool(row["missing_spend_rows"]),
             "spend": _number(row["spend"]),
             **{metric: _number(row[metric]) for metric in ("starts", "subs", "regs", "ftd")}}
            for row in cursor.fetchall()
        ]


def availability(conn, buyer_id: str, first: date, last: date) -> list[dict]:
    """Dates with buyer-specific creatives and count of missing spend values."""
    from datetime import timedelta

    cols = discover(conn)
    buyer_col = quoted(cols["creo_buyer"])
    date_col = quoted(cols["creo_date"])
    spend_col = quoted(cols["creo_spend"])
    with conn.cursor() as cursor:
        cursor.execute(
            f"""SELECT DATE({date_col}) AS stat_date,
                       COUNT(*) AS creative_rows,
                       COUNT(DISTINCT {quoted(cols["creo_name"])}) AS creative_count,
                       SUM({spend_col} IS NULL OR TRIM({spend_col})='') AS missing_spend_rows
                FROM `creos` WHERE {buyer_col}=%s AND {date_col} >= %s AND {date_col} < %s
                GROUP BY DATE({date_col}) ORDER BY stat_date""",
            (buyer_id, first, last + timedelta(days=1)),
        )
        return [
            {
                "date": str(row["stat_date"]),
                "creative_count": int(row["creative_count"]),
                "creative_rows": int(row["creative_rows"]),
                "missing_spend_rows": int(row["missing_spend_rows"] or 0),
            }
            for row in cursor.fetchall()
        ]
