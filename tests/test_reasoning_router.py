import pytest

from ai_analysis import select_reasoning_effort


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Статистика Анастасии за сегодня", "low"),
        ("Сравни сегодня и вчера по источникам", "medium"),
        ("Статистика за сегодня и вчера", "medium"),
        ("Почему просели старты и что делать с бюджетом?", "high"),
        ("Сравни всех баеров, группы, источники и найди точки роста", "high"),
    ],
)
def test_adaptive_reasoning_routes_by_question_complexity(question, expected):
    assert select_reasoning_effort(question, "auto") == expected


def test_explicit_reasoning_setting_wins_over_router():
    assert select_reasoning_effort(
        "Почему просели старты и что делать?", "low"
    ) == "low"


def test_unknown_setting_falls_back_to_adaptive_router():
    assert select_reasoning_effort("Статистика за сегодня", "unsupported") == "low"
