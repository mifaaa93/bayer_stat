"""Read-only MySQL statistics. No advertising metrics are stored in SQLite."""

from __future__ import annotations

import contextvars
import os
import re
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

import pymysql
import settings  # loads the ignored .env before reading MYSQL_* variables


TABLES = ("buyer_stats_today_start_sub", "creos", "traffers")
IDENT = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
ACTIVE_FUNNEL = contextvars.ContextVar("bayer_mysql_funnel", default="new")

# Picker and AI may use only these new-funnel traffer ids, plus «все байеры».
# Pavel in the old funnel is the whole Farm cabinet (leadb traffer id 18).
SELECTABLE_IDS = ("1", "5")
OLD_FUNNEL = {
    "1": {"buyer_id": "18", "cabinet": "Farm"},
}


def allowed_funnels(buyer_id: str) -> tuple[str, ...]:
    if buyer_id in OLD_FUNNEL:
        return ("new", "old")
    return ("new",)


def funnel_hint(buyer_id: str) -> str:
    if buyer_id in OLD_FUNNEL:
        return "Доступны новая и старая воронки."
    return "Доступна только новая воронка."


def database_name(funnel: str = "new") -> str:
    if funnel == "old":
        name = os.getenv("MYSQL_DATABASE_OLD", "").strip()
        if not name:
            raise RuntimeError("Не настроена старая база: MYSQL_DATABASE_OLD")
        return name
    if funnel != "new":
        raise RuntimeError("Неизвестная воронка")
    if not os.getenv("MYSQL_DATABASE"):
        raise RuntimeError("Не настроена новая база: MYSQL_DATABASE")
    return os.environ["MYSQL_DATABASE"]


def schema_name(conn) -> str:
    return getattr(conn, "schema_name", None) or database_name(ACTIVE_FUNNEL.get())


@contextmanager
def use_funnel(funnel: str):
    token = ACTIVE_FUNNEL.set(funnel)
    try:
        yield
    finally:
        ACTIVE_FUNNEL.reset(token)


def configured() -> bool:
    return all(os.getenv(key) for key in
               ("MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE"))


@contextmanager
def connection():
    missing = [k for k in ("MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD")
               if not os.getenv(k)]
    if missing:
        raise RuntimeError("Не настроен MySQL: " + ", ".join(missing))
    database = database_name(ACTIVE_FUNNEL.get())
    conn = pymysql.connect(
        host=os.environ["MYSQL_HOST"],
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASSWORD"],
        database=database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=30,
        autocommit=True,
    )
    conn.schema_name = database
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
                (schema_name(conn), table),
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
        "creo_chats": "chats" if "chats" in c else None,
        "creo_subs": "subs" if "subs" in c else None,
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
    """Buyers the picker and the model may select. Not the whole traffers table."""
    by_id = {row["id"]: row for row in buyers(conn)}
    selected = []
    for buyer_id in SELECTABLE_IDS:
        row = by_id.get(buyer_id)
        if not row:
            continue
        row = dict(row)
        row["funnels"] = list(allowed_funnels(buyer_id))
        link = OLD_FUNNEL.get(buyer_id)
        if link:
            row["old_buyer_id"] = link["buyer_id"]
            row["old_cabinet"] = link["cabinet"]
        selected.append(row)
    return selected


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


def traffer_report(conn, buyer_name: str, first: date, last: date) -> dict | None:
    """Buyer-level new-funnel totals from traffers_stat. Absent in the old database."""
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='traffers_stat'",
            (schema_name(conn),),
        )
        columns = {row["COLUMN_NAME"] for row in cursor.fetchall()}
    needed = {"date", "traffer_name", "count_start", "count_sub",
               "count_chat", "count_reg", "count_ftd"}
    if not needed <= columns:
        return None
    from datetime import timedelta
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT DATE(`date`) AS stat_date,
                   SUM(`count_start`) AS starts,
                   SUM(`count_sub`) AS subs,
                   SUM(`count_chat`) AS chats,
                   SUM(`count_reg`) AS regs,
                   SUM(`count_ftd`) AS ftd
            FROM `traffers_stat`
            WHERE `traffer_name`=%s AND `date`>=%s AND `date`<%s
            GROUP BY DATE(`date`)
            ORDER BY stat_date
            """,
            (buyer_name, first, last + timedelta(days=1)),
        )
        days = [
            {
                "date": str(row["stat_date"]),
                "starts": _number(row["starts"]) or 0,
                "subs": _number(row["subs"]) or 0,
                "chats": _number(row["chats"]) or 0,
                "regs": _number(row["regs"]) or 0,
                "ftd": _number(row["ftd"]) or 0,
            }
            for row in cursor.fetchall()
        ]
    totals = {
        metric: round(sum(day[metric] for day in days), 4)
        for metric in ("starts", "subs", "chats", "regs", "ftd")
    }
    return {
        "source": "traffers_stat",
        "level": "buyer",
        "buyer_name": buyer_name,
        "days_count": len(days),
        "first_date": days[0]["date"] if days else None,
        "last_date": days[-1]["date"] if days else None,
        "totals": totals,
        "days": days[-31:],
        "truncated": len(days) > 31,
        "note": (
            "Отчёт трафера новой воронки: итог байера за день, без креатива. "
            "Для Facebook-байера это источник стартов, регистраций и FTD. "
            "Не складывай эти числа с событиями каналов и с chats/creo_subs карточек. "
            "Если days_count меньше запрошенного периода, за пропущенные дни отчёта нет."
        ),
    }


def _number(value):
    if value is None:
        return None
    return float(value) if isinstance(value, Decimal) else value


# Creative names that are Keitaro macros, not a real creative.
# They are not joined to spend and are not shown as a creative.
PLACEHOLDER_CREATIVES = ("{tracker.campaign_name}", "{{campaign.name}}")


def _platform_code(value: str | None) -> str | None:
    if value is None:
        return None
    key = str(value).strip().casefold()
    return {
        "фб": "ФБ", "fb": "ФБ", "facebook": "ФБ",
        "тг": "ТГ", "tg": "ТГ", "telegram": "ТГ",
        "ггл": "ГГЛ", "google": "ГГЛ", "гугл": "ГГЛ",
    }.get(key)


def events_ready(conn) -> bool:
    """True when this schema has the buyer's platform and creative-level traffers_stat."""
    traffer_cols = _columns_for(conn, "traffers")
    if "platform_name" not in traffer_cols:
        return False
    try:
        stat_cols = _columns_for(conn, "traffers_stat")
    except ValueError:
        return False
    needed = {"date", "traffer_name", "creo_name", "count_start",
              "count_sub", "count_chat", "count_reg", "count_ftd"}
    return needed <= stat_cols


def _buyer_platform(conn, buyer_id: str) -> dict | None:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT `platform_name` AS platform_name, `traffer_name` AS buyer_name "
            "FROM `traffers` WHERE `id`=%s LIMIT 1",
            (buyer_id,),
        )
        row = cursor.fetchone()
    if not row:
        return None
    return {
        "platform": _platform_code(row.get("platform_name")),
        "name": str(row.get("buyer_name") or ""),
    }


def _creative_clause(column: str, creative: str | None, exact: bool,
                     name_token: str | None) -> tuple[str, list]:
    sql = ""
    params: list = []
    if creative:
        if exact:
            sql += f" AND LOWER({column})=LOWER(%s)"
        else:
            sql += f" AND LOCATE(LOWER(%s), LOWER({column}))>0"
        params.append(creative)
    if name_token:
        sql += f" AND LOCATE(LOWER(%s), LOWER({column}))>0"
        params.append(name_token)
    return sql, params


def _spend_rows(conn, buyer_id: str, first: date, last: date,
                creative: str | None, exact: bool, name_token: str | None,
                platform: str | None) -> list[dict]:
    """One spend value per buyer, day and creative. Repeated creos rows are not summed."""
    from datetime import timedelta

    cols = discover(conn)
    q = lambda key: quoted(cols[key])
    clause, clause_params = _creative_clause(
        f"c.{q('creo_name')}", creative, exact, name_token,
    )
    platform_sql = "AND t.`platform_name`=%s" if platform else ""
    buyer_sql = "" if buyer_id == "*" else f"AND c.{q('creo_buyer')}=%s"
    join_traffer = "JOIN `traffers` t ON t.`id`=c." + q("creo_buyer")
    query = f"""
        SELECT DATE(c.{q("creo_date")}) AS stat_date,
               c.{q("creo_name")} AS creative_name,
               COUNT(*) AS row_count,
               COUNT(DISTINCT NULLIF(TRIM(c.{q("creo_spend")}), '')) AS budget_variants,
               MAX(CAST(NULLIF(TRIM(c.{q("creo_spend")}), '') AS DECIMAL(20,8))) AS spend,
               SUM(c.{q("creo_spend")} IS NULL OR TRIM(c.{q("creo_spend")})='') AS missing_spend_rows
               {", t.`platform_name` AS platform_name" if platform else ""}
        FROM `creos` c
        {join_traffer if platform else ""}
        WHERE c.{q("creo_date")} >= %s AND c.{q("creo_date")} < %s
          {buyer_sql}
          {platform_sql}
          {clause}
        GROUP BY DATE(c.{q("creo_date")}), c.{q("creo_name")}
                 {", t.`platform_name`" if platform else ""}
    """
    params: list = [first, last + timedelta(days=1)]
    if buyer_id != "*":
        params.append(buyer_id)
    if platform:
        params.append(platform)
    params.extend(clause_params)
    with conn.cursor() as cursor:
        cursor.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            variants = int(row.get("budget_variants") or 0)
            conflict = variants > 1
            # An empty cell is a real zero: spend can stop while starts still arrive.
            if conflict:
                spend = None
            elif variants == 0:
                spend = 0
            else:
                spend = _number(row.get("spend"))
            rows.append({
                "stat_date": str(row["stat_date"]),
                "creative_name": row["creative_name"],
                "platform": _platform_code(row.get("platform_name")) if platform else None,
                "spend": spend,
                "spend_missing": conflict,
                "spend_conflict": conflict,
                "spend_duplicate": int(row.get("row_count") or 0) > 1,
            })
        return rows


def _event_rows(conn, buyer_id: str, first: date, last: date,
                creative: str | None, exact: bool, name_token: str | None,
                platform: str) -> list[dict]:
    """Facebook events come from traffers_stat. Telegram events come from the channel table."""
    from datetime import timedelta

    end = last + timedelta(days=1)
    buyer_sql = "" if buyer_id == "*" else "AND t.`id`=%s"
    if platform == "ФБ":
        clause, clause_params = _creative_clause("s.`creo_name`", creative, exact, name_token)
        query = f"""
            SELECT DATE(s.`date`) AS stat_date, s.`creo_name` AS creative_name,
                   SUM(COALESCE(s.`count_start`,0)) AS starts,
                   SUM(COALESCE(s.`count_sub`,0)) AS subs,
                   SUM(COALESCE(s.`count_chat`,0)) AS chats,
                   SUM(COALESCE(s.`count_reg`,0)) AS regs,
                   SUM(COALESCE(s.`count_ftd`,0)) AS ftd
            FROM `traffers_stat` s
            JOIN `traffers` t ON t.`traffer_name`=s.`traffer_name`
            WHERE t.`platform_name`=%s
              AND s.`date`>=%s AND s.`date`<%s
              AND s.`creo_name` NOT IN (%s, %s)
              {buyer_sql}
              {clause}
            GROUP BY DATE(s.`date`), s.`creo_name`
        """
        params: list = [platform, first, end, *PLACEHOLDER_CREATIVES]
    else:
        clause, clause_params = _creative_clause("e.`creo_name`", creative, exact, name_token)
        query = f"""
            SELECT DATE(e.`date`) AS stat_date, e.`creo_name` AS creative_name,
                   SUM(COALESCE(e.`count_start`,0)) AS starts,
                   SUM(COALESCE(e.`count_sub`,0)) AS subs,
                   SUM(COALESCE(e.`count_reg`,0)) AS regs,
                   SUM(COALESCE(e.`count_ftd`,0)) AS ftd
            FROM `buyer_stats_today_start_sub` e
            JOIN (
                SELECT DATE(c.`date`) AS stat_date, c.`creo_name` AS creative_name, c.`id_traf`
                FROM `creos` c
                JOIN `traffers` t ON t.`id`=c.`id_traf`
                WHERE t.`platform_name`=%s
                  AND c.`date`>=%s AND c.`date`<%s
                  {buyer_sql}
                GROUP BY DATE(c.`date`), c.`creo_name`, c.`id_traf`
            ) c ON c.stat_date=DATE(e.`date`) AND c.creative_name=e.`creo_name`
            WHERE e.`date`>=%s AND e.`date`<%s
              AND e.`creo_name` NOT IN (%s, %s)
              {clause}
            GROUP BY DATE(e.`date`), e.`creo_name`
        """
        params = [platform, first, end]
        if buyer_id != "*":
            params.append(buyer_id)
        params.extend([first, end, *PLACEHOLDER_CREATIVES])
    if platform == "ФБ" and buyer_id != "*":
        params.append(buyer_id)
    params.extend(clause_params)
    with conn.cursor() as cursor:
        cursor.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            item = {
                "stat_date": str(row["stat_date"]),
                "creative_name": row["creative_name"],
                "starts": _number(row["starts"]) or 0,
                "subs": _number(row["subs"]) or 0,
                "regs": _number(row["regs"]) or 0,
                "ftd": _number(row["ftd"]) or 0,
            }
            if platform == "ФБ":
                item["chats"] = _number(row.get("chats")) or 0
            rows.append(item)
        return rows


def _merge_spend_and_events(spend_rows: list[dict], event_rows: list[dict],
                            platform: str | None, events_source: str) -> list[dict]:
    spend_map = {(row["stat_date"], row["creative_name"]): row for row in spend_rows}
    event_map = {(row["stat_date"], row["creative_name"]): row for row in event_rows}
    merged = []
    for key in sorted(set(spend_map) | set(event_map)):
        spend = spend_map.get(key)
        event = event_map.get(key)
        item = {
            "stat_date": key[0],
            "creative_name": key[1],
            "platform": (spend or {}).get("platform") or platform,
            "events_source": events_source,
            "spend": spend["spend"] if spend else (0 if event else None),
            "spend_missing": spend["spend_missing"] if spend else False,
            "spend_conflict": spend.get("spend_conflict", False) if spend else False,
            "spend_duplicate": spend["spend_duplicate"] if spend else False,
            "starts": event["starts"] if event else None,
            "subs": event["subs"] if event else None,
            "regs": event["regs"] if event else None,
            "ftd": event["ftd"] if event else None,
        }
        if event and "chats" in event:
            item["chats"] = event["chats"]
        merged.append(item)
    return merged


def statistics(conn, buyer_id: str, first: date, last: date,
               creative: str | None = None, exact: bool = False,
               name_token: str | None = None) -> list[dict]:
    """Join spend from creos to the event table of the buyer's platform.

    Facebook events are traffers_stat. Telegram events are
    buyer_stats_today_start_sub. Both joins use the calendar day and the
    exact creative name. Repeated creos rows contribute one spend value.
    A schema without platform_name returns spend only.
    """
    if not events_ready(conn):
        spend_rows = _spend_rows(
            conn, buyer_id, first, last, creative, exact, name_token, platform=None,
        )
        return _merge_spend_and_events(spend_rows, [], None, "not_configured")

    if buyer_id == "*":
        rows = []
        for platform, source in (("ФБ", "traffers_stat"), ("ТГ", "buyer_stats_today_start_sub")):
            spend_rows = _spend_rows(
                conn, buyer_id, first, last, creative, exact, name_token, platform,
            )
            event_rows = _event_rows(
                conn, buyer_id, first, last, creative, exact, name_token, platform,
            )
            rows.extend(_merge_spend_and_events(spend_rows, event_rows, platform, source))
        return rows

    buyer = _buyer_platform(conn, buyer_id)
    if not buyer or not buyer["platform"]:
        return []
    platform = buyer["platform"]
    if platform not in ("ФБ", "ТГ"):
        spend_rows = _spend_rows(
            conn, buyer_id, first, last, creative, exact, name_token, platform,
        )
        return _merge_spend_and_events(
            spend_rows, [], platform, "postbacks_not_configured",
        )
    source = "traffers_stat" if platform == "ФБ" else "buyer_stats_today_start_sub"
    spend_rows = _spend_rows(
        conn, buyer_id, first, last, creative, exact, name_token, platform,
    )
    event_rows = _event_rows(
        conn, buyer_id, first, last, creative, exact, name_token, platform,
    )
    return _merge_spend_and_events(spend_rows, event_rows, platform, source)


def availability(conn, buyer_id: str, first: date, last: date,
                 name_token: str | None = None) -> list[dict]:
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
                {"AND LOCATE(LOWER(%s), LOWER(" + quoted(cols["creo_name"]) + "))>0"
                 if name_token else ""}
                GROUP BY DATE({date_col}) ORDER BY stat_date""",
            (
                (first, last + timedelta(days=1), buyer_id)
                if buyer_id != "*"
                else (first, last + timedelta(days=1))
            ) + ((name_token,) if name_token else ()),
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
            (schema_name(conn), table),
        )
        columns = {row["COLUMN_NAME"] for row in cursor.fetchall()}
    if not columns:
        raise ValueError(f"Таблица MySQL {table} не найдена или недоступна")
    return columns


def _source_from_joined(
    conn, buyer_id: str, first: date, last: date, source: str | None,
    name_token: str | None,
) -> dict:
    """Facebook and Telegram rows already joined by statistics()."""
    rows = statistics(
        conn, buyer_id, first, last, name_token=name_token,
    )
    wanted = _platform_code(source) if source else None
    if source and not wanted:
        needle = source.casefold()
        rows = [
            row for row in rows
            if needle in str(row.get("platform") or "").casefold()
        ]
    elif wanted:
        rows = [row for row in rows if row.get("platform") == wanted]
    shaped = []
    unattributed = []
    for row in rows:
        matched = row.get("starts") is not None and row.get("spend") is not None
        item = {
            "stat_date": row["stat_date"],
            "creative_name": row["creative_name"],
            "source_id": row.get("platform") or "",
            "source_type": row.get("platform") or "unknown",
            "source_name": row.get("platform") or "unknown",
            "spend": row.get("spend"),
            "spend_missing": bool(row.get("spend_missing")),
            "attribution": "exact" if matched else "unmatched",
            "starts": row.get("starts"),
            "subs": row.get("subs"),
            "regs": row.get("regs"),
            "ftd": row.get("ftd"),
        }
        shaped.append(item)
        if row.get("starts") is not None and row.get("spend") is None:
            unattributed.append({
                "stat_date": row["stat_date"],
                "creative_name": row["creative_name"],
                "starts": row.get("starts"),
                "subs": row.get("subs"),
                "regs": row.get("regs"),
                "ftd": row.get("ftd"),
                "reason": "нет затрат с тем же именем креатива и датой",
            })
    return {
        "rows": shaped,
        "unattributed_events": unattributed,
        "source_types": sorted({row["source_type"] for row in shaped}),
        "source_mapping": (
            "ФБ: события из traffers_stat. ТГ: события из "
            "buyer_stats_today_start_sub. Затраты только из creos, "
            "склейка по календарному дню и точному имени креатива."
        ),
    }


def source_statistics(
    conn, buyer_id: str, first: date, last: date, source: str | None = None,
    name_token: str | None = None,
) -> dict:
    """Return source-tagged spend and safely attributable funnel metrics.

    When platform_name exists, Facebook and Telegram use their own event
    tables. Otherwise only spend is returned: the shared channel table is
    not treated as the buyer's events.
    """
    if events_ready(conn):
        return _source_from_joined(
            conn, buyer_id, first, last, source, name_token,
        )
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
    token_filter = (
        f"AND LOCATE(LOWER(%s), LOWER(c.{q('creo_name')}))>0" if name_token else ""
    )
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
          {token_filter}
          {source_filter}
        GROUP BY DATE(c.{q("creo_date")}), c.{q("creo_name")},
                 c.id_blog, b.{blog_type}, b.{blog_name}
        ORDER BY stat_date, source_type, creative_name
    """
    with conn.cursor() as cursor:
        source_params = ([first, end] if buyer_id == "*" else [buyer_id, first, end])
        if name_token:
            source_params.append(name_token)
        if source:
            source_params.append(source)
        cursor.execute(source_query, source_params)
        source_rows = cursor.fetchall()
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
        # Channel events are not this buyer's events until platform_name exists.
        event_rows = {}

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
        "source_mapping": (
            "Схема без platform_name: в ответе только затраты. "
            "События Facebook и Telegram появятся после настройки тех же таблиц."
        ),
    }


def country_statistics(
    conn, buyer_id: str, first: date, last: date,
    source: str | None = None, country: str | None = None,
    creative: str | None = None, name_token: str | None = None,
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
    token_filter = (
        "AND LOCATE(LOWER(%s), LOWER(cs.creative_name))>0" if name_token else ""
    )
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
        WHERE 1=1 {source_filter} {country_filter} {creative_filter} {token_filter}
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
    if name_token:
        params.append(name_token)
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
