from datetime import date
from unittest.mock import Mock, patch

import pytest

import mysql_stats
from ai_analysis import (
    CLARIFY_QUESTION, Analyst, TOOL_PROGRESS, TOOLS, parse_date, report_checks,
    spend_gaps, summarize,
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
    assert rows[0]["events_source"] == "not_configured"
    assert rows[0]["starts"] is None
    assert conn.cursor_obj.params == [
        date(2026, 9, 25), date(2026, 9, 27), "5",
    ]
    assert "GROUP BY" in conn.cursor_obj.last
    assert "buyer_stats_today_start_sub" not in conn.cursor_obj.last
    assert "SUM(CAST" not in conn.cursor_obj.last


def test_missing_mysql_column_fails_loudly(monkeypatch):
    monkeypatch.setenv("MYSQL_DATABASE", "test")
    cols = {k: set(v) for k, v in COLS.items()}
    cols["traffers"].remove("name")
    with pytest.raises(ValueError, match="traffers"):
        mysql_stats.discover(Conn(cols))


def test_optional_stats_buyer_filter(monkeypatch):
    monkeypatch.setenv("MYSQL_DATABASE", "test")
    conn = Conn(COLS)
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
    assert sql.count("LOCATE(LOWER(%s)") == 1
    assert params[3] == "ABC_%"
    assert "ABC_%" not in sql
    mysql_stats.statistics(
        conn, "5", date(2026, 9, 26), date(2026, 9, 26),
        creative="ABC_%", exact=True,
    )
    assert conn.cursor_obj.last.count("=LOWER(%s)") == 1


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


def test_known_spend_stays_visible_when_other_rows_are_empty():
    rows = [
        {"stat_date": "2026-09-30", "creative_name": "Filled", "spend": 10,
         "spend_missing": False, "starts": 4, "subs": 1, "regs": 0, "ftd": 0},
        {"stat_date": "2026-10-01", "creative_name": "Empty", "spend": 0,
         "spend_missing": False, "starts": 2, "subs": 1, "regs": 0, "ftd": 0},
        {"stat_date": "2026-09-30", "creative_name": "NoEvent", "spend": 3,
         "spend_missing": False, "spend_duplicate": True, "starts": None,
         "subs": None, "regs": None, "ftd": None},
    ]
    result = summarize(rows)
    assert result["spend"] == 13
    assert not result["spend_incomplete"]
    assert not result["events_incomplete"]
    assert result["cost_starts"] == round(13 / 6, 4)
    gaps = spend_gaps(rows)
    assert gaps["known_spend"] == 13
    assert gaps["matched_same_day_spend"] == 10
    assert gaps["spend_without_same_day_event"] == 3
    assert gaps["empty_or_conflicting_budget_rows"] == 0
    assert gaps["duplicate_day_rows"] == 1
    assert gaps["creatives_with_no_filled_spend"] == 0
    assert gaps["largest_spend_without_same_day_event"][0]["creative_name"] == "NoEvent"
    checks = report_checks(rows)
    assert checks["events_without_budget"]["rows"] == 1
    assert checks["events_without_budget"]["starts"] == 2
    assert checks["spend_without_same_day_event"]["spend"] == 3
    assert checks["duplicate_rows"]["rows"] == 1
    assert checks["duplicate_rows"]["counted_as"] == "one_row"
    assert checks["duplicate_rows"]["examples"] == [{
        "stat_date": "2026-09-30",
        "creative_name": "NoEvent",
        "spend_kept": 3,
        "conflict": False,
    }]
    assert checks["empty_budget_without_events"] == 0
    assert checks["conflicting_budgets"]["rows"] == 0


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
        {"stat_date": "2026-09-26", "creative_name": "CreativeA", "spend": 0,
         "spend_missing": False, "starts": 20, "subs": 5, "regs": 3, "ftd": 1},
        {"stat_date": "2026-09-26", "creative_name": "CreativeB", "spend": 2,
         "spend_missing": False, "starts": 2, "subs": 1, "regs": 0, "ftd": 0},
    ]

    def stats(conn, buyer_id, first, last, creative=None, exact=False, **kwargs):
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
    monkeypatch.setattr(mysql_stats, "availability", lambda conn, buyer_id, first, last, **kwargs: [
        {"date": "2026-09-26", "creative_count": 2, "creative_rows": 2,
         "missing_spend_rows": 1}
    ])
    model = Analyst("5", "Bound buyer", "key", "model", "https://example.test/v1")
    base = {"buyer_id": "999", "date_from": "2026-09-26"}
    overview = model.call_tool("get_overview", {**base, "by_day": True})
    assert overview["has_data"] and overview["totals"]["spend"] == 2
    assert not overview["totals"]["spend_incomplete"]
    assert overview["totals"]["cost_starts"] is None
    assert overview["totals"]["cost_by_day"] is False
    assert "неделе или месяцу" in overview["note"]
    assert overview["report_checks"]["events_without_budget"]["starts"] == 20
    assert overview["totals"]["conversion_starts_to_subs"] == 0.2727
    assert len(overview["days"]) == 1
    assert overview["days"][0]["totals"]["spend"] == 2
    assert overview["days"][0]["totals"]["cost_starts"] is None
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


def test_tool_schemas_are_unique_and_scope_buyer_selection():
    names = [item["function"]["name"] for item in TOOLS]
    assert len(names) == len(set(names)) == 16
    assert {"get_creative", "compare_periods", "get_funnel",
            "get_data_availability", "find_anomalies",
            "list_available_buyers", "get_buyer_statistics",
            "compare_buyers"} <= set(names)
    legacy_tools = {
        "get_statistics", "get_overview", "get_funnel", "get_creative",
        "list_creatives", "compare_periods", "compare_creatives",
        "get_data_availability", "get_source_statistics", "compare_sources",
        "get_country_statistics", "compare_countries", "find_anomalies",
    }
    for tool in TOOLS:
        parameters = tool["function"]["parameters"]
        if tool["function"]["name"] in legacy_tools:
            assert "buyer_id" not in parameters["properties"]
        assert parameters["additionalProperties"] is False
    assert "buyer_id" in next(
        item["function"]["parameters"]["properties"]
        for item in TOOLS
        if item["function"]["name"] == "get_buyer_statistics"
    )


def test_global_scope_can_select_one_buyer(monkeypatch):
    class Context:
        def __enter__(self):
            return object()

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(mysql_stats, "connection", lambda: Context())
    monkeypatch.setattr(mysql_stats, "buyer", lambda conn, buyer_id: (
        {"id": str(buyer_id), "name": "Анастасия", "status": "Работает"}
        if str(buyer_id) == "5" else None
    ))
    monkeypatch.setattr(mysql_stats, "statistics", lambda *args, **kwargs: [
        {"stat_date": "2026-09-29", "creative_name": "A", "spend": 10,
         "spend_missing": False, "starts": 20, "subs": 5, "regs": 2, "ftd": 1},
    ])
    model = Analyst("*", "Все байеры", "key", "model", "https://example.test/v1")
    result = model.call_tool("get_buyer_statistics", {
        "date_from": "2026-09-29", "buyer_id": "5",
    })
    assert result["selected_buyer"]["name"] == "Анастасия"
    assert result["totals"]["starts"] == 20


def test_global_scope_prompt_contains_full_buyer_list(monkeypatch):
    class Context:
        def __enter__(self):
            return object()

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(mysql_stats, "connection", lambda: Context())
    monkeypatch.setattr(mysql_stats, "buyer_list", lambda conn: [
        {"id": "5", "name": "Анастасия", "status": "Работает"},
        {"id": "6", "name": "Иван", "status": "Пауза"},
    ])
    model = Analyst("*", "Все байеры", "key", "model", "https://example.test/v1")
    messages = model._messages("статистика Анастасии")
    assert '"id": "5"' in messages[1]["content"]
    assert '"id": "6"' in messages[1]["content"]
    assert messages[-1]["content"] == "статистика Анастасии"
    assert "buyer_name" not in next(
        item["function"]["parameters"]["properties"]
        for item in model._tools()
        if item["function"]["name"] == "get_buyer_statistics"
    )
    assert all(
        item["function"]["name"] not in {"list_available_buyers",
                                          "get_buyer_statistics", "compare_buyers"}
        for item in Analyst("5", "Buyer", "key", "model",
                            "https://example.test/v1")._tools()
    )


def test_global_question_can_use_model_selected_id_from_full_list(monkeypatch):
    class Context:
        def __enter__(self):
            return object()

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(mysql_stats, "connection", lambda: Context())
    monkeypatch.setattr(mysql_stats, "buyer_list", lambda conn: [
        {"id": "5", "name": "NEW_Anastacia", "status": "Работает"},
        {"id": "6", "name": "NEW_Dima", "status": "Пауза"},
    ])
    monkeypatch.setattr(mysql_stats, "buyer", lambda conn, buyer_id: (
        {"id": "5", "name": "NEW_Anastacia", "status": "Работает"}
        if buyer_id == "5" else None
    ))
    called = []
    def stats(conn, buyer_id, first, last, **kwargs):
        called.append(buyer_id)
        return [{
            "stat_date": "2026-09-29", "creative_name": "A", "spend": 10,
            "spend_missing": False, "starts": 12, "subs": 3, "regs": 1, "ftd": 0,
        }]
    monkeypatch.setattr(mysql_stats, "statistics", stats)
    analyst = Analyst("*", "Все байеры", "key", "model", "https://example.test/v1")
    first = Mock()
    first.json.return_value = {
        "choices": [{"message": {
            "content": None, "tool_calls": [{
                "id": "call-1", "function": {
                    "name": "get_buyer_statistics",
                    "arguments": '{"date_from":"2026-09-29","buyer_id":"5"}',
                },
            }],
        }}],
    }
    final = Mock()
    final.json.return_value = {
        "choices": [{"message": {"content": "Данные Анастасии", "tool_calls": []}}],
    }
    with patch.object(analyst, "_request", side_effect=[first, final]) as request:
        messages, has_data, used, direct = analyst._run_tools(
            analyst._messages("Покажи статистику Анастасии сегодня")
        )
    assert "NEW_Anastacia" in request.call_args_list[0].args[0][1]["content"]
    assert has_data and used and direct == "Данные Анастасии"
    assert called == ["5"]
    assert '"buyer": "NEW_Anastacia"' in messages[-1]["content"]


def test_global_buyer_name_argument_does_not_silently_return_all_buyers():
    model = Analyst("*", "Все байеры", "key", "model", "https://example.test/v1")
    result = model.call_tool("get_overview", {
        "date_from": "2026-09-29", "buyer_name": "Анастасия",
    })
    assert "error" in result
    assert "has_data" not in result


def test_global_scope_rejects_unknown_buyer_id(monkeypatch):
    class Context:
        def __enter__(self):
            return object()

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(mysql_stats, "connection", lambda: Context())
    monkeypatch.setattr(mysql_stats, "buyer", lambda conn, buyer_id: None)
    model = Analyst("*", "Все байеры", "key", "model", "https://example.test/v1")
    result = model.call_tool("get_buyer_statistics", {
        "date_from": "2026-09-29", "buyer_id": "999",
    })
    assert "error" in result


def test_global_scope_can_compare_buyers(monkeypatch):
    class Context:
        def __enter__(self):
            return object()

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(mysql_stats, "connection", lambda: Context())
    names = {"1": "NEW_Pavel", "5": "NEW_Anastacia"}
    monkeypatch.setattr(mysql_stats, "buyer", lambda conn, buyer_id: {
        "id": str(buyer_id), "name": names[str(buyer_id)], "status": "Работает",
    })
    monkeypatch.setattr(mysql_stats, "statistics", lambda conn, buyer_id, first, last,
                        **kwargs: [{
                            "stat_date": "2026-09-29", "creative_name": str(buyer_id),
                            "spend": 10, "spend_missing": False,
                            "starts": int(buyer_id), "subs": 1, "regs": 1, "ftd": 0,
                        }])
    model = Analyst("*", "Все байеры", "key", "model", "https://example.test/v1")
    result = model.call_tool("compare_buyers", {
        "date_from": "2026-09-29", "buyer_ids": ["1", "5"],
    })
    assert result["has_data"]
    assert [item["buyer"]["id"] for item in result["buyers"]] == ["1", "5"]


def test_single_scope_cannot_select_or_compare_other_buyers(monkeypatch):
    model = Analyst("5", "Анастасия", "key", "model", "https://example.test/v1")
    selected = model.call_tool("get_buyer_statistics", {
        "date_from": "2026-09-29", "buyer_id": "6",
    })
    compared = model.call_tool("compare_buyers", {
        "date_from": "2026-09-29", "buyer_ids": ["5", "6"],
    })
    assert "только привязанный" in selected["error"]
    assert "только привязанный" in compared["error"]


def test_tool_request_enables_parallel_calls():
    model = Analyst("5", "Buyer", "key", "model", "https://example.test/v1")
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
    with patch("ai_analysis.requests.post", return_value=response) as request:
        model._request([{"role": "user", "content": "test"}], tools=True)
    payload = request.call_args.kwargs["json"]
    assert payload["parallel_tool_calls"] is True


def test_service_tier_is_forwarded_when_configured(monkeypatch):
    model = Analyst("5", "Buyer", "key", "model", "https://example.test/v1")
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
    monkeypatch.setattr("ai_analysis.SERVICE_TIER", "fast")
    with patch("ai_analysis.requests.post", return_value=response) as request:
        model._request([{"role": "user", "content": "test"}])
    assert request.call_args.kwargs["json"]["service_tier"] == "fast"


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


def test_final_answer_is_non_streaming():
    analyst = Analyst("5", "Buyer", "key", "model", "https://example.test/v1")
    response = Mock()
    response.json.return_value = {
        "choices": [{"message": {"content": "готово"}}]
    }
    with patch.object(analyst, "_request", return_value=response) as request:
        assert analyst._final([{"role": "user", "content": "test"}]) == "готово"
    request.assert_called_once_with([{"role": "user", "content": "test"}], stream=False)


def test_tool_batch_callback_runs_after_all_results():
    analyst = Analyst("5", "Buyer", "key", "model", "https://example.test/v1")
    first = Mock()
    first.json.return_value = {
        "choices": [{"message": {
            "content": None,
            "tool_calls": [
                {"id": "a", "function": {"name": "get_overview", "arguments": "{}"}},
                {"id": "b", "function": {"name": "get_funnel", "arguments": "{}"}},
            ],
        }}]
    }
    second = Mock()
    second.json.return_value = {
        "choices": [{"message": {"content": "готово", "tool_calls": []}}]
    }
    calls = []
    with patch.object(analyst, "_request", side_effect=[first, second]):
        with patch.object(analyst, "call_tool", return_value={"has_data": True}):
            analyst._run_tools(
                analyst._messages("данные"),
                after_tool_batch=lambda: calls.append("after_batch"),
            )
    assert calls == ["after_batch"]


def test_country_sql_is_read_only_parameterized_and_deduplicates_old_dates(monkeypatch):
    monkeypatch.setenv("MYSQL_DATABASE", "test")

    class CountryCursor(Cursor):
        def __init__(self, columns):
            super().__init__(columns)
            self.calls = []

        def execute(self, sql, params=()):
            super().execute(sql, params)
            self.calls.append((sql, params))

        def fetchall(self):
            if "information_schema" in self.last:
                if self.params[1] == "bloggers":
                    return [{"COLUMN_NAME": value} for value in
                            ("id", "traf_type", "blogger_name")]
                return super().fetchall()
            return [
                {"stat_date": date(2026, 9, 28), "creative_name": "Crypto",
                 "country": "SA", "regs": 3, "ftd": 1,
                 "source_type": "ТГ", "source_name": "рами",
                 "owner_count": 1, "source_count": 1},
                {"stat_date": date(2026, 9, 28), "creative_name": "Shared",
                 "country": "IQ", "regs": 2, "ftd": 0,
                 "source_type": "фб", "source_name": "рами",
                 "owner_count": 2, "source_count": 2},
                {"stat_date": date(2026, 9, 28), "creative_name": "Shared",
                 "country": "IQ", "regs": 2, "ftd": 0,
                 "source_type": "ТГ", "source_name": "рами",
                 "owner_count": 2, "source_count": 2},
            ]

    class CountryConn(Conn):
        def __init__(self, columns):
            self.cursor_obj = CountryCursor(columns)

    conn = CountryConn(COLS)
    rows = mysql_stats.country_statistics(
        conn, "5", date(2026, 9, 27), date(2026, 9, 28),
        country="SA", creative="Crypto", source="ТГ",
    )
    assert len(rows) == 2
    assert rows[0]["country"] == "SA"
    assert rows[0]["regs"] == 3 and rows[0]["ftd"] == 1
    assert rows[1]["attribution"] == "ambiguous"
    assert rows[1]["regs"] is None
    sql, params = conn.cursor_obj.calls[-1]
    assert "MAX(COALESCE(count_reg,0))" in sql
    assert "GROUP BY LEFT(date,10), creo_name, country" in sql
    assert "SELECT" in sql.upper()
    assert "INSERT" not in sql.upper()
    assert params == ["2026-09-27", "2026-09-29", "5",
                      "2026-09-27", "2026-09-29",
                      "2026-09-27", "2026-09-29", "ТГ", "SA", "Crypto"]
    assert "Crypto" not in sql and "'ТГ'" not in sql


def test_country_tools_respect_bound_buyer_and_total_before_limit(monkeypatch):
    class Context:
        def __enter__(self):
            return object()

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(mysql_stats, "connection", lambda: Context())
    monkeypatch.setattr(
        mysql_stats, "buyer",
        lambda conn, buyer_id: {"id": buyer_id, "name": "Buyer"},
    )
    captured = []
    def rows(conn, buyer_id, first, last, source=None, country=None, creative=None,
             name_token=None):
        captured.append((buyer_id, source, country, creative))
        if first == date(2026, 9, 27):
            return [
                {"stat_date": "2026-09-27", "country": "IQ",
                 "creative_name": "Old", "source_type": "ТГ",
                 "source_name": "рами", "regs": 4, "ftd": 1,
                 "attribution": "exact"},
            ]
        return [
            {"stat_date": "2026-09-28", "country": "SA",
             "creative_name": "New", "source_type": "ТГ",
             "source_name": "рами", "regs": 3, "ftd": 1,
             "attribution": "exact"},
            {"stat_date": "2026-09-28", "country": "IQ",
             "creative_name": "Old", "source_type": "ТГ",
             "source_name": "рами", "regs": 2, "ftd": 0,
             "attribution": "exact"},
        ]
    monkeypatch.setattr(mysql_stats, "country_statistics", rows)
    analyst = Analyst("5", "Buyer", "key", "model", "https://example.test/v1")
    result = analyst.call_tool(
        "get_country_statistics",
        {"date_from": "2026-09-28", "buyer_id": "other",
         "source": "ТГ", "group_by": "country", "limit": 1},
    )
    assert result["has_data"]
    assert result["totals"]["regs"] == 5
    assert result["totals"]["ftd"] == 1
    assert len(result["rows"]) == 1 and result["truncated"]
    comparison = analyst.call_tool(
        "compare_countries",
        {"first_from": "2026-09-27", "first_to": "2026-09-27",
         "second_from": "2026-09-28", "second_to": "2026-09-28",
         "buyer_id": "other"},
    )
    assert comparison["has_data"]
    assert {row["country"] for row in comparison["changes"]} == {"SA", "IQ"}
    assert all(buyer_id == "5" for buyer_id, *_ in captured)


def test_traffer_report_is_buyer_level_and_absent_without_table():
    class Cursor:
        def __init__(self):
            self.sql = ""
            self.params = ()

        def execute(self, sql, params=()):
            self.sql = sql
            self.params = params

        def fetchall(self):
            if "information_schema" in self.sql:
                return [{"COLUMN_NAME": name} for name in (
                    "date", "traffer_name", "count_start", "count_sub",
                    "count_chat", "count_reg", "count_ftd",
                )]
            return [{
                "stat_date": date(2026, 9, 29),
                "starts": 38, "subs": 15, "chats": 7, "regs": 3, "ftd": 2,
            }]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    conn = type("Conn", (), {"schema_name": "lea_partners_db", "cursor": lambda self: Cursor()})()
    report = mysql_stats.traffer_report(conn, "NEW_Pavel", date(2026, 9, 29), date(2026, 9, 29))
    assert report["totals"]["starts"] == 38
    assert report["level"] == "buyer"
    assert report["first_date"] == "2026-09-29"

    class EmptyCursor(Cursor):
        def fetchall(self):
            return []

    empty = type("Conn", (), {"schema_name": "leadb", "cursor": lambda self: EmptyCursor()})()
    assert mysql_stats.traffer_report(empty, "Farm", date(2026, 9, 29), date(2026, 9, 29)) is None


def test_overview_does_not_replace_starts_with_a_second_report(monkeypatch):
    class Context:
        def __enter__(self):
            return object()

        def __exit__(self, *_):
            return False

    monkeypatch.setenv("MYSQL_DATABASE", "lea_partners_db")
    monkeypatch.setattr(mysql_stats, "connection", lambda: Context())
    monkeypatch.setattr(mysql_stats, "buyer", lambda conn, buyer_id: {
        "id": "1", "name": "NEW_Pavel", "status": "Работает",
    })
    monkeypatch.setattr(mysql_stats, "statistics", lambda *args, **kwargs: [{
        "stat_date": "2026-09-29", "creative_name": "Ad", "spend": 10,
        "spend_missing": False, "starts": 38, "subs": 15, "regs": 3, "ftd": 2,
        "events_source": "traffers_stat", "platform": "ФБ",
    }])
    result = Analyst("1", "NEW_Pavel", "key", "model", "https://example.test/v1").call_tool(
        "get_overview", {"date_from": "2026-09-29", "funnel": "new"},
    )
    assert "traffer_report" not in result
    assert result["totals"]["starts"] == 38


def test_name_token_filters_both_sides(monkeypatch):
    monkeypatch.setenv("MYSQL_DATABASE", "test")
    conn = Conn(COLS)
    mysql_stats.statistics(
        conn, "18", date(2026, 9, 26), date(2026, 9, 26), name_token="Pavel",
    )
    assert conn.cursor_obj.last.count("LOCATE(LOWER(%s)") == 1
    assert conn.cursor_obj.params.count("Pavel") == 1
    assert "Pavel" not in conn.cursor_obj.last


def test_pavel_reads_both_funnels_and_anastacia_only_new(monkeypatch):
    class Context:
        def __enter__(self):
            return object()

        def __exit__(self, *_):
            return False

    monkeypatch.setenv("MYSQL_DATABASE", "lea_partners_db")
    monkeypatch.setenv("MYSQL_DATABASE_OLD", "leadb")
    monkeypatch.setattr(mysql_stats, "connection", lambda: Context())
    monkeypatch.setattr(mysql_stats, "buyer", lambda conn, buyer_id: {
        "id": str(buyer_id),
        "name": "Farm" if str(buyer_id) == "18" else "NEW_Pavel",
        "status": "Работает",
    })
    seen = []

    def stats(conn, buyer_id, first, last, **kwargs):
        seen.append((str(buyer_id), kwargs.get("name_token"), mysql_stats.ACTIVE_FUNNEL.get()))
        return [{
            "stat_date": "2026-09-29", "creative_name": "A", "spend": 1,
            "spend_missing": False, "starts": 2, "subs": 1, "regs": 0, "ftd": 0,
        }]

    monkeypatch.setattr(mysql_stats, "statistics", stats)
    both = Analyst("1", "NEW_Pavel", "key", "model", "https://example.test/v1").call_tool(
        "get_overview", {"date_from": "2026-09-29"},
    )
    assert both["funnel"] == "both" and both["has_data"]
    assert ("1", None, "new") in seen
    assert ("18", None, "old") in seen
    assert "creative_name_contains" not in both["old_funnel"]
    assert both["old_funnel"]["database"] == "leadb"
    assert both["old_funnel"]["cabinet_id"] == "18"
    refused = Analyst(
        "5", "NEW_Anastacia", "key", "model", "https://example.test/v1",
    ).call_tool("get_overview", {"date_from": "2026-09-29", "funnel": "old"})
    assert "только новая" in refused["error"]
    unknown = Analyst("*", "Все байеры", "key", "model", "https://example.test/v1").call_tool(
        "get_buyer_statistics", {"date_from": "2026-09-29", "buyer_id": "6"},
    )
    assert "недоступен" in unknown["error"]


READY = {
    "creos": {"date", "creo_name", "budget", "id_traf"},
    "buyer_stats_today_start_sub": {
        "date", "creo_name", "count_start", "count_sub", "count_reg", "count_ftd",
    },
    "traffers": {"id", "traffer_name", "traffer_status", "platform_name"},
    "traffers_stat": {
        "date", "traffer_name", "creo_name", "count_start", "count_sub",
        "count_chat", "count_reg", "count_ftd",
    },
}


class ReadyCursor:
    def __init__(self, platform):
        self.platform = platform
        self.calls = []
        self.last = ""
        self.params = ()

    def execute(self, sql, params=()):
        self.last = sql
        self.params = params
        self.calls.append(sql)

    def fetchall(self):
        if "information_schema" in self.last:
            return [{"COLUMN_NAME": name} for name in READY[self.params[1]]]
        if "FROM `traffers_stat`" in self.last or "FROM `buyer_stats_today_start_sub`" in self.last:
            return [{
                "stat_date": date(2026, 10, 5), "creative_name": "Ad",
                "starts": 4, "subs": 1, "chats": 2, "regs": 1, "ftd": 0,
            }]
        return [{
            "stat_date": date(2026, 10, 5), "creative_name": "Ad",
            "row_count": 4, "budget_variants": 1, "spend": 9,
            "missing_spend_rows": 0, "platform_name": self.platform,
        }]

    def fetchone(self):
        return {"platform_name": self.platform, "buyer_name": "Buyer"}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_empty_budget_is_zero_and_a_conflict_stays_unknown(monkeypatch):
    monkeypatch.setenv("MYSQL_DATABASE", "lea_partners_db")

    class Cursor(ReadyCursor):
        def fetchall(self):
            if "information_schema" in self.last:
                return [{"COLUMN_NAME": name} for name in READY[self.params[1]]]
            if "FROM `buyer_stats_today_start_sub`" in self.last:
                return [{
                    "stat_date": date(2026, 10, 5), "creative_name": "Late",
                    "starts": 3, "subs": 1, "regs": 0, "ftd": 0,
                }]
            return [
                {
                    "stat_date": date(2026, 10, 5), "creative_name": "Late",
                    "row_count": 1, "budget_variants": 0, "spend": None,
                    "missing_spend_rows": 1, "platform_name": "ТГ",
                },
                {
                    "stat_date": date(2026, 10, 5), "creative_name": "Split",
                    "row_count": 2, "budget_variants": 2, "spend": 8,
                    "missing_spend_rows": 0, "platform_name": "ТГ",
                },
            ]

    conn = type("Conn", (), {"schema_name": "lea_partners_db", "cursor": lambda self: Cursor("ТГ")})()
    rows = {row["creative_name"]: row for row in mysql_stats.statistics(
        conn, "5", date(2026, 10, 5), date(2026, 10, 5)
    )}
    assert rows["Late"]["spend"] == 0
    assert rows["Late"]["spend_missing"] is False
    assert rows["Late"]["starts"] == 3
    assert rows["Split"]["spend"] is None
    assert rows["Split"]["spend_conflict"] is True


def test_facebook_events_come_from_traffers_stat_and_spend_is_not_summed(monkeypatch):
    monkeypatch.setenv("MYSQL_DATABASE", "lea_partners_db")
    cursor = ReadyCursor("ФБ")
    conn = type("Conn", (), {"schema_name": "lea_partners_db", "cursor": lambda self: cursor})()
    rows = mysql_stats.statistics(conn, "1", date(2026, 10, 5), date(2026, 10, 5))
    assert rows[0]["events_source"] == "traffers_stat"
    assert rows[0]["starts"] == 4
    assert rows[0]["chats"] == 2
    assert rows[0]["spend"] == 9
    assert rows[0]["spend_duplicate"]
    joined = "\n".join(cursor.calls)
    assert "FROM `traffers_stat`" in joined
    assert "t.`traffer_name`=s.`traffer_name`" in joined
    assert "Pavel" not in cursor.params
    assert "buyer_stats_today_start_sub" not in joined
    assert "MAX(CAST" in joined
    assert "{tracker.campaign_name}" in cursor.params
    assert "{{campaign.name}}" in cursor.params


def test_old_farm_events_use_pavel_name(monkeypatch):
    monkeypatch.setenv("MYSQL_DATABASE", "leadb")
    cursor = ReadyCursor("ФБ")
    conn = type("Conn", (), {"schema_name": "leadb", "cursor": lambda self: cursor})()
    rows = mysql_stats.statistics(conn, "18", date(2026, 10, 5), date(2026, 10, 7))
    assert rows[0]["events_source"] == "traffers_stat"
    joined = "\n".join(cursor.calls)
    assert "s.`traffer_name`=%s" in joined
    assert "t.`traffer_name`=s.`traffer_name`" not in joined
    assert cursor.params.count("Pavel") == 1
    assert cursor.params.count("18") == 1


def test_telegram_events_come_from_channel_table(monkeypatch):
    monkeypatch.setenv("MYSQL_DATABASE", "lea_partners_db")
    cursor = ReadyCursor("ТГ")
    conn = type("Conn", (), {"schema_name": "lea_partners_db", "cursor": lambda self: cursor})()
    rows = mysql_stats.statistics(conn, "5", date(2026, 10, 5), date(2026, 10, 5))
    assert rows[0]["events_source"] == "buyer_stats_today_start_sub"
    assert rows[0]["platform"] == "ТГ"
    assert "chats" not in rows[0]
    joined = "\n".join(cursor.calls)
    assert "FROM `buyer_stats_today_start_sub`" in joined
    assert "FROM `traffers_stat`" not in joined
