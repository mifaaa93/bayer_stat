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


def buyer_list(conn) -> list[dict]:
    """Explicit alias used by global-scope analytics tools."""
    return buyers(conn)


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
        f"AND {q('stats_buyer')}=%s"
        if cols["stats_buyer"] and buyer_id != "*"
        else ""
    )
    cost_buyer_filter = (
        f"AND {q('creo_buyer')}=%s" if buyer_id != "*" else ""
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
            WHERE {q("creo_date")} >= %s AND {q("creo_date")} < %s
              {cost_buyer_filter}
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
        parameters = [first, end_exclusive]
        if buyer_id != "*":
            parameters.append(buyer_id)
        if creative:
            parameters.append(creative)
        parameters.extend([first, end_exclusive])
        if creative:
            parameters.append(creative)
        if cols["stats_buyer"] and buyer_id != "*":
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
                FROM `creos` WHERE {date_col} >= %s AND {date_col} < %s
                {"AND " + buyer_col + "=%s" if buyer_id != "*" else ""}
                GROUP BY DATE({date_col}) ORDER BY stat_date""",
            (
                (first, last + timedelta(days=1), buyer_id)
                if buyer_id != "*"
                else (first, last + timedelta(days=1))
            ),
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


def _columns_for(conn, table: str) -> set[str]:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
            (os.environ["MYSQL_DATABASE"], table),
        )
        columns = {row["COLUMN_NAME"] for row in cursor.fetchall()}
    if not columns:
        raise ValueError(f"Таблица MySQL {table} не найдена или недоступна")
    return columns


def source_statistics(
    conn, buyer_id: str, first: date, last: date, source: str | None = None
) -> dict:
    """Return source-tagged spend and safely attributable funnel metrics.

    Events table has no id_blog in the current schema. Events are attributed
    to a source only when a creative/date maps to exactly one source in creos.
    Ambiguous event rows are returned separately instead of being duplicated.
    """
    cols = discover(conn)
    blogger_cols = _columns_for(conn, "bloggers")
    blog_id = quoted(pick(blogger_cols, ("id",), "bloggers"))
    blog_type = quoted(pick(blogger_cols, ("traf_type",), "bloggers"))
    blog_name = quoted(
        pick(blogger_cols, ("blogger_name", "blogger"), "bloggers")
    )
    q = lambda key: quoted(cols[key])
    from datetime import timedelta

    end = last + timedelta(days=1)
    source_filter = "AND b.traf_type=%s" if source else ""
    buyer_filter = "" if buyer_id == "*" else f"AND c.{q('creo_buyer')}=%s"
    source_query = f"""
        SELECT DATE(c.{q("creo_date")}) AS stat_date,
               c.{q("creo_name")} AS creative_name,
               c.id_blog AS source_id,
               COALESCE(b.{blog_type}, 'unknown') AS source_type,
               COALESCE(b.{blog_name}, c.id_blog) AS source_name,
               SUM(CAST(NULLIF(TRIM(c.{q("creo_spend")}), '') AS DECIMAL(20,8))) AS spend,
               SUM(c.{q("creo_spend")} IS NULL OR TRIM(c.{q("creo_spend")})='') AS missing_spend_rows
        FROM creos c
        LEFT JOIN bloggers b ON b.{blog_id}=c.id_blog
        WHERE 1=1 {buyer_filter}
          AND c.{q("creo_date")} >= %s AND c.{q("creo_date")} < %s
          {source_filter}
        GROUP BY DATE(c.{q("creo_date")}), c.{q("creo_name")},
                 c.id_blog, b.{blog_type}, b.{blog_name}
        ORDER BY stat_date, source_type, creative_name
    """
    events_query = f"""
        SELECT DATE({q("stats_date")}) AS stat_date,
               {q("stats_name")} AS creative_name,
               SUM(COALESCE({q("starts")},0)) AS starts,
               SUM(COALESCE({q("subs")},0)) AS subs,
               SUM(COALESCE({q("regs")},0)) AS regs,
               SUM(COALESCE({q("ftd")},0)) AS ftd
        FROM buyer_stats_today_start_sub
        WHERE {q("stats_date")} >= %s AND {q("stats_date")} < %s
        GROUP BY DATE({q("stats_date")}), {q("stats_name")}
    """
    with conn.cursor() as cursor:
        source_params = ([first, end] if buyer_id == "*" else [buyer_id, first, end])
        if source:
            source_params.append(source)
        cursor.execute(source_query, source_params)
        source_rows = cursor.fetchall()
        # Events have no buyer/source ID. For each creative/date, determine
        # whether the same name is also used by another buyer or source.
        cursor.execute(
            f"""SELECT DATE({q("creo_date")}) AS stat_date,
                       {q("creo_name")} AS creative_name,
                       COUNT(DISTINCT {q("creo_buyer")}) AS buyer_count,
                       COUNT(DISTINCT id_blog) AS source_count
                FROM creos
                WHERE {q("creo_date")} >= %s AND {q("creo_date")} < %s
                GROUP BY DATE({q("creo_date")}), {q("creo_name")}""",
            (first, end),
        )
        ownership = {
            (str(row["stat_date"]), row["creative_name"]):
            (int(row["buyer_count"]), int(row["source_count"]))
            for row in cursor.fetchall()
        }
        cursor.execute(events_query, (first, end))
        event_rows = {
            (str(row["stat_date"]), row["creative_name"]): row
            for row in cursor.fetchall()
        }

    source_keys: dict[tuple, set[str]] = {}
    for row in source_rows:
        key = (str(row["stat_date"]), row["creative_name"])
        source_keys.setdefault(key, set()).add(str(row["source_id"]))

    rows = []
    unattributed = []
    ambiguous_keys: set[tuple[str, str]] = set()
    for row in source_rows:
        key = (str(row["stat_date"]), row["creative_name"])
        event = event_rows.get(key)
        buyers_count, sources_count = ownership.get(key, (0, 0))
        exact = (
            len(source_keys[key]) == 1
            and buyers_count == 1
            and sources_count == 1
        )
        item = {
            "stat_date": key[0],
            "creative_name": row["creative_name"],
            "source_id": str(row["source_id"]),
            "source_type": str(row["source_type"]),
            "source_name": str(row["source_name"]),
            "spend": _number(row["spend"]),
            "spend_missing": bool(row["missing_spend_rows"]),
            "attribution": "exact" if exact else "ambiguous",
        }
        for metric in ("starts", "subs", "regs", "ftd"):
            item[metric] = _number(event[metric]) if exact and event else None
        rows.append(item)
        if event and not exact and key not in ambiguous_keys:
            ambiguous_keys.add(key)
            unattributed.append({
                "stat_date": key[0],
                "creative_name": row["creative_name"],
                **{metric: _number(event[metric]) for metric in
                   ("starts", "subs", "regs", "ftd")},
                "reason": "creative/date используются несколькими источниками или байерами",
            })
    return {
        "rows": rows,
        "unattributed_events": unattributed,
        "source_types": sorted({row["source_type"] for row in rows}),
        "source_mapping": "exact for unique creative/date; ambiguous events are not duplicated",
    }


def country_statistics(
    conn, buyer_id: str, first: date, last: date,
    source: str | None = None, country: str | None = None,
    creative: str | None = None,
) -> list[dict]:
    """Country registrations/FTD joined to the bound buyer and source.

    The country table has no buyer/source ID, so ownership is resolved through
    normalized date + creative name in creos. Ambiguous ownership is marked.
    """
    cols = discover(conn)
    blogger_cols = _columns_for(conn, "bloggers")
    blog_id = quoted(pick(blogger_cols, ("id",), "bloggers"))
    blog_type = quoted(pick(blogger_cols, ("traf_type",), "bloggers"))
    blog_name = quoted(pick(blogger_cols, ("blogger_name", "blogger"), "bloggers"))
    cdate, cname, cbuyer = (quoted(cols[key]) for key in
                           ("creo_date", "creo_name", "creo_buyer"))
    from datetime import timedelta
    end = last + timedelta(days=1)
    source_filter = "AND owners.source_type=%s" if source else ""
    buyer_filter = "" if buyer_id == "*" else f"AND c.{cbuyer}=%s"
    country_filter = "AND cs.country=%s" if country else ""
    creative_filter = "AND LOWER(cs.creative_name)=LOWER(%s)" if creative else ""
    # Existing imported test records have two date representations for one
    # country/creative/day. MAX deduplicates that legacy pair; future rows
    # should have unique keys as specified by the owner.
    query = f"""
        SELECT cs.stat_date, cs.creative_name, cs.country,
               cs.regs, cs.ftd,
               owners.source_type, owners.source_name,
               ownership.owner_count, ownership.source_count
        FROM (
            SELECT LEFT(date,10) AS stat_date, creo_name AS creative_name,
                   country, MAX(COALESCE(count_reg,0)) AS regs,
                   MAX(COALESCE(count_ftd,0)) AS ftd
            FROM buyer_stats_today_start_sub_country
            WHERE LEFT(date,10) >= %s AND LEFT(date,10) < %s
            GROUP BY LEFT(date,10), creo_name, country
        ) cs
        JOIN (
            SELECT LEFT(c.{cdate},10) AS stat_date,
                   c.{cname} AS creative_name,
                   c.{cbuyer} AS buyer_id, c.id_blog,
                   COALESCE(b.{blog_type},'unknown') AS source_type,
                   COALESCE(b.{blog_name},c.id_blog) AS source_name
            FROM creos c
            LEFT JOIN bloggers b ON b.{blog_id}=c.id_blog
            WHERE 1=1 {buyer_filter}
              AND LEFT(c.{cdate},10)>=%s AND LEFT(c.{cdate},10)<%s
            GROUP BY LEFT(c.{cdate},10),c.{cname},c.{cbuyer},
                     c.id_blog,b.{blog_type},b.{blog_name}
        ) owners
          ON owners.stat_date=cs.stat_date
         AND owners.creative_name=cs.creative_name
        JOIN (
            SELECT LEFT({cdate},10) AS stat_date,
                   {cname} AS creative_name,
                   COUNT(DISTINCT {cbuyer}) AS owner_count,
                   COUNT(DISTINCT id_blog) AS source_count
            FROM creos
            WHERE LEFT({cdate},10)>=%s AND LEFT({cdate},10)<%s
            GROUP BY LEFT({cdate},10),{cname}
        ) ownership
          ON ownership.stat_date=cs.stat_date
         AND ownership.creative_name=cs.creative_name
        WHERE 1=1 {source_filter} {country_filter} {creative_filter}
        ORDER BY cs.stat_date, cs.regs DESC, cs.country
    """
    params = [first.isoformat(), end.isoformat()]
    if buyer_id != "*":
        params.append(buyer_id)
    params.extend([
        first.isoformat(), end.isoformat(),
        first.isoformat(), end.isoformat(),
    ])
    if source:
        params.append(source)
    if country:
        params.append(country)
    if creative:
        params.append(creative)
    with conn.cursor() as cursor:
        cursor.execute(query, params)
        result: list[dict] = []
        seen_ambiguous: set[tuple[str, str, str]] = set()
        for row in cursor.fetchall():
            exact = int(row["owner_count"]) == 1 and int(row["source_count"]) == 1
            key = (str(row["stat_date"]), row["creative_name"], row["country"])
            if not exact and key in seen_ambiguous:
                continue
            if not exact:
                seen_ambiguous.add(key)
            result.append({
                "stat_date": str(row["stat_date"]),
                "creative_name": row["creative_name"],
                "country": row["country"],
                "regs": int(row["regs"] or 0) if exact else None,
                "ftd": int(row["ftd"] or 0) if exact else None,
                "source_type": str(row["source_type"]) if exact else None,
                "source_name": str(row["source_name"]) if exact else None,
                "attribution": "exact" if exact else "ambiguous",
            })
        return result
