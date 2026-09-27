from datetime import date
from unittest.mock import Mock, patch

import pytest

import mysql_stats
from ai_analysis import (
    CLARIFY_QUESTION, Analyst, TOOL_PROGRESS, TOOLS, parse_date, summarize,
)


class Cursor:
    def __init__(self, columns):
        self.columns = columns
        self.last = None
        self.params = None

    def execute(self, sql, params=()):
        self.last = sql
        self.params = params

    def fetchall(self):
        if "information_schema" in self.last:
            return [{"COLUMN_NAME": name} for name in self.columns[self.params[1]]]
        return [
            {"stat_date": date(2026, 9, 26), "creative_name": "Creative",
             "spend": 3, "missing_spend_rows": 0,
             "starts": 4, "subs": 2, "regs": 1, "ftd": 0}
        ]

    def fetchone(self):
        return {"buyer_id": 5, "buyer_name": "Buyer", "buyer_status": "Работает"}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class Conn:
    def __init__(self, columns):
        self.cursor_obj = Cursor(columns)

    def cursor(self):
        return self.cursor_obj


COLS = {
    "creos": {"date", "creo_name", "budget", "id_traf"},
    "buyer_stats_today_start_sub": {
        "date", "creo_name", "count_start", "count_sub", "count_reg", "count_ftd"
    },
    "traffers": {"id", "name", "traffer_status"},
}


def test_statistics_uses_bound_buyer_and_daily_dates(monkeypatch):
    monkeypatch.setenv("MYSQL_DATABASE", "test")
    conn = Conn(COLS)
    rows = mysql_stats.statistics(conn, "5", date(2026, 9, 25), date(2026, 9, 26))
    assert rows[0]["stat_date"] == "2026-09-26"
    assert conn.cursor_obj.params == [
        "5", date(2026, 9, 25), date(2026, 9, 27),
        date(2026, 9, 25), date(2026, 9, 27)
    ]
    assert "GROUP BY" in conn.cursor_obj.last
    assert "JOIN" in conn.cursor_obj.last


def test_missing_mysql_column_fails_loudly(monkeypatch):
    monkeypatch.setenv("MYSQL_DATABASE", "test")
    cols = {k: set(v) for k, v in COLS.items()}
    cols["traffers"].remove("name")
    with pytest.raises(ValueError, match="traffers"):
        mysql_stats.discover(Conn(cols))


def test_optional_stats_buyer_filter(monkeypatch):
    monkeypatch.setenv("MYSQL_DATABASE", "test")
    cols = {k: set(v) for k, v in COLS.items()}
    cols["buyer_stats_today_start_sub"].add("id_traf")
    conn = Conn(cols)
    mysql_stats.statistics(conn, "5", date(2026, 9, 26), date(2026, 9, 26))
    assert "`id_traf`=%s" in conn.cursor_obj.last
    assert conn.cursor_obj.params[-1] == "5"


def test_creative_search_is_parameterized_in_both_tables(monkeypatch):
    monkeypatch.setenv("MYSQL_DATABASE", "test")
    conn = Conn(COLS)
    mysql_stats.statistics(
        conn, "5", date(2026, 9, 26), date(2026, 9, 26),
        creative="ABC_%", exact=False,
    )
    sql, params = conn.cursor_obj.last, conn.cursor_obj.params
    assert sql.count("LOCATE(LOWER(%s)") == 2
    assert params[3] == params[6] == "ABC_%"
    assert "ABC_%" not in sql
    mysql_stats.statistics(
        conn, "5", date(2026, 9, 26), date(2026, 9, 26),
        creative="ABC_%", exact=True,
    )
    assert conn.cursor_obj.last.count("=LOWER(%s)") == 2


def test_tool_ignores_model_supplied_buyer(monkeypatch):
    class Context:
        def __enter__(self):
            return Conn(COLS)

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(mysql_stats, "connection", lambda: Context())
    monkeypatch.setattr(mysql_stats, "buyer", lambda conn, buyer_id: {"id": buyer_id, "name": "Buyer"})
    called = []
    monkeypatch.setattr(mysql_stats, "statistics", lambda conn, buyer_id, first, last, **kwargs:
                        called.append(buyer_id) or [{
                            "stat_date": "2026-09-26", "creative_name": "Creative",
                            "spend": 3, "starts": 4, "subs": 2, "regs": 1, "ftd": 0,
                        }])
    analyst = Analyst("5", "Buyer", "test", "test", "https://example.com/v1")
    result = analyst.call_tool(
        "get_statistics", {"date_from": "2026-09-26", "buyer_id": "999"}
    )
    assert called == ["5"]
    assert result["totals"]["starts"] == 4


def test_date_and_rates():
    today = date(2026, 9, 26)
    assert parse_date("вчера", today) == date(2026, 9, 25)
    assert summarize([{"spend": 12, "starts": 6, "subs": 3, "regs": 1, "ftd": 0}])["cost_starts"] == 2


def test_missing_spend_is_not_zero():
    result = summarize([
        {"spend": None, "spend_missing": True, "starts": 3, "subs": 1, "regs": 0, "ftd": 0}
    ])
    assert result["spend"] is None
    assert result["spend_incomplete"]
    assert result["cost_starts"] is None


def test_all_ai_tools_belong_to_bound_buyer(monkeypatch):
    class Context:
        def __enter__(self):
            return object()

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(mysql_stats, "connection", lambda: Context())
    monkeypatch.setattr(mysql_stats, "buyer", lambda conn, buyer_id: {
        "id": buyer_id, "name": "Bound buyer",
    })
    queries = []
    rows = [
        {"stat_date": "2026-09-25", "creative_name": "CreativeA", "spend": 10,
         "spend_missing": False, "starts": 10, "subs": 4, "regs": 2, "ftd": 1},
        {"stat_date": "2026-09-26", "creative_name": "CreativeA", "spend": None,
         "spend_missing": True, "starts": 20, "subs": 5, "regs": 3, "ftd": 1},
        {"stat_date": "2026-09-26", "creative_name": "CreativeB", "spend": 2,
         "spend_missing": False, "starts": 2, "subs": 1, "regs": 0, "ftd": 0},
    ]

    def stats(conn, buyer_id, first, last, creative=None, exact=False):
        queries.append((buyer_id, creative, exact))
        return [
            row for row in rows
            if str(first) <= row["stat_date"] <= str(last)
            and (not creative or (
                row["creative_name"].casefold() == creative.casefold() if exact
                else creative.casefold() in row["creative_name"].casefold()
            ))
        ]

    monkeypatch.setattr(mysql_stats, "statistics", stats)
    monkeypatch.setattr(mysql_stats, "availability", lambda conn, buyer_id, first, last: [
        {"date": "2026-09-26", "creative_count": 2, "creative_rows": 2,
         "missing_spend_rows": 1}
    ])
    model = Analyst("5", "Bound buyer", "key", "model", "https://example.test/v1")
    base = {"buyer_id": "999", "date_from": "2026-09-26"}
    overview = model.call_tool("get_overview", {**base, "by_day": True})
    assert overview["has_data"] and overview["totals"]["spend"] is None
    assert overview["totals"]["conversion_starts_to_subs"] == 0.2727
    assert len(overview["days"]) == 1
    assert model.call_tool("get_funnel", base)["totals"]["regs"] == 3
    creative = model.call_tool(
        "get_creative",
        {**base, "creative": "CreativeA", "by_day": True},
    )
    assert creative["creative_name"] == "CreativeA"
    assert creative["days"][0]["starts"] == 20
    assert queries[-1] == ("5", "CreativeA", True)
    fuzzy = model.call_tool(
        "get_creative", {**base, "creative": "Creative", "match_mode": "contains"}
    )
    assert fuzzy["requires_disambiguation"]
    listing = model.call_tool("list_creatives", {
        **base, "search": "Creative", "sort_by": "starts", "limit": 1, "offset": 1
    })
    assert listing["matching_rows"] == 2
    assert listing["rows"][0]["creative_name"] == "CreativeB"
    assert model.call_tool("get_data_availability", base)["dates_count"] == 1
    comparison = model.call_tool("compare_periods", {
        "buyer_id": "999", "first_from": "2026-09-25",
        "first_to": "2026-09-25", "second_from": "2026-09-26",
        "second_to": "2026-09-26",
    })
    assert comparison["changes_second_vs_first"]["starts"]["difference"] == 12
    creatives = model.call_tool("compare_creatives", {
        **base, "creative_a": "CreativeA", "creative_b": "CreativeB",
    })
    assert creatives["has_data"] and len(creatives["creatives"]) == 2
    anomalies = model.call_tool("find_anomalies", {
        "buyer_id": "999", "first_from": "2026-09-25",
        "first_to": "2026-09-25", "second_from": "2026-09-26",
        "second_to": "2026-09-26", "metric": "starts",
    })
    assert anomalies["changes"][0]["percent_change"] == 100.0
    assert all(buyer_id == "5" for buyer_id, _, _ in queries)


def test_tool_schemas_are_unique_and_do_not_expose_buyer_selection():
    names = [item["function"]["name"] for item in TOOLS]
    assert len(names) == len(set(names)) == 9
    assert {"get_creative", "compare_periods", "get_funnel",
            "get_data_availability", "find_anomalies"} <= set(names)
    for tool in TOOLS:
        parameters = tool["function"]["parameters"]
        assert "buyer_id" not in parameters["properties"]
        assert parameters["additionalProperties"] is False


def test_recent_group_conversation_is_included_without_unbounded_growth():
    model = Analyst("5", "Buyer", "key", "model", "https://example.test/v1")
    history = [{"role": "user", "content": f"Alice: сообщение {n}"} for n in range(40)]
    messages = model._messages("кит, уточни период", history)
    assert len(messages) == 32  # system + last 30 group messages + current question
    assert messages[1]["content"] == "Alice: сообщение 10"
    assert messages[-1]["content"] == "кит, уточни период"


@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [
        ("get_statistics", "Собираю данные"),
        ("get_overview", "Собираю сводку"),
        ("get_funnel", "Считаю показатели"),
        ("get_creative", "Изучаю креатив"),
        ("list_creatives", "Изучаю креативы"),
        ("compare_periods", "Сравниваю периоды"),
        ("compare_creatives", "Сравниваю креативы"),
        ("find_anomalies", "Проверяю изменения"),
        ("get_data_availability", "Проверяю доступные данные"),
    ],
)
def test_tool_statuses_are_user_friendly(tool_name, expected):
    analyst = Analyst("5", "Buyer", "key", "model", "https://example.test/v1")
    first = Mock()
    first.json.return_value = {
        "choices": [{"message": {
            "content": None,
            "tool_calls": [{"id": "call-1", "function": {
                "name": tool_name, "arguments": "{}"
            }}],
        }}]
    }
    second = Mock()
    second.json.return_value = {
        "choices": [{"message": {"content": "Готово", "tool_calls": []}}]
    }
    statuses = []
    with patch.object(analyst, "_request", side_effect=[first, second]):
        with patch.object(analyst, "call_tool", return_value={"has_data": True}):
            _, has_data, tools_used, direct = analyst._run_tools(
                analyst._messages("данные за сегодня"), statuses.append
            )
    assert has_data and tools_used
    assert direct == "Готово"
    assert expected in statuses
    assert all("MySQL" not in status and "tool" not in status.casefold()
               for status in statuses)


def test_tool_round_limit_returns_collected_data_without_raising():
    analyst = Analyst("5", "Buyer", "key", "model", "https://example.test/v1")
    keep_calling = Mock()
    keep_calling.json.return_value = {
        "choices": [{"message": {
            "content": None,
            "tool_calls": [{"id": "call-1", "function": {
                "name": "get_overview",
                "arguments": '{"date_from": "сегодня"}',
            }}],
        }}]
    }
    with patch.object(analyst, "_request", return_value=keep_calling) as request:
        with patch.object(analyst, "call_tool", return_value={"has_data": True}):
            messages, has_data, tools_used, direct = analyst._run_tools(
                analyst._messages("данные")
            )
    assert has_data and tools_used
    assert direct == ""
    assert request.call_count == 20
    assert sum(1 for item in messages if item.get("role") == "tool") == 20


def test_vague_question_without_tools_asks_to_clarify():
    analyst = Analyst("5", "Buyer", "key", "model", "https://example.test/v1")
    response = Mock()
    response.json.return_value = {
        "choices": [{"message": {
            "content": "Уточните, пожалуйста, период и метрики.",
            "tool_calls": [],
        }}]
    }
    with patch.object(analyst, "_request", return_value=response):
        answer = analyst.answer_stream("тест")
    assert "период" in answer.casefold()
    assert "Не удалось получить данные" not in answer


def test_vague_question_empty_model_reply_uses_fallback_clarify():
    analyst = Analyst("5", "Buyer", "key", "model", "https://example.test/v1")
    response = Mock()
    response.json.return_value = {
        "choices": [{"message": {"content": None, "tool_calls": []}}]
    }
    with patch.object(analyst, "_request", return_value=response):
        assert analyst.answer_stream("тест") == CLARIFY_QUESTION


def test_reasoning_effort_is_sent_in_payload():
    analyst = Analyst(
        "5", "Buyer", "key", "model", "https://example.test/v1",
        reasoning_effort="medium",
    )
    response = Mock()
    response.status_code = 200
    with patch("ai_analysis.requests.post", return_value=response) as post:
        analyst._request([{"role": "user", "content": "hi"}], tools=False)
    payload = post.call_args.kwargs["json"]
    assert payload["reasoning_effort"] == "medium"
