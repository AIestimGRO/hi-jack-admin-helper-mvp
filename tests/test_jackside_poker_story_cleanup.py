from app.jackside_poker_story_cleanup import public_daily_questions_without_poker_story
from app.services.daily_414 import stage_for_question


def test_jackside_public_questions_hide_poker_story_but_keep_sections() -> None:
    questions = [
        {
            "id": f"q{index + 1}",
            "section": {"title": f"Block {index // 2}", "theme": f"theme-{index // 2}"},
        }
        for index in range(10)
    ]
    public = public_daily_questions_without_poker_story(questions)

    assert [item["id"] for item in public] == [f"q{index + 1}" for index in range(10)]
    assert all(item["game_stage"] == "" for item in public)
    assert all(item["river_reveal"] is False for item in public)
    assert [item["section"] for item in public] == [item["section"] for item in questions]


def test_internal_jackside_stage_grouping_remains_available() -> None:
    assert [stage_for_question(index) for index in range(10)] == [
        "preflop",
        "preflop",
        "flop",
        "flop",
        "flop",
        "turn",
        "turn",
        "turn",
        "river",
        "river",
    ]
