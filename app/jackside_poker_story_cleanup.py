from __future__ import annotations

from typing import Any

from app.services import daily_414


_ORIGINAL_PUBLIC_DAILY_QUESTIONS = daily_414.public_daily_questions


def public_daily_questions_without_poker_story(
    questions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep JACKSIDE section styling but remove public poker-street flow signals."""
    result = _ORIGINAL_PUBLIC_DAILY_QUESTIONS(questions)
    for item in result:
        # game_stage is an internal grouping detail. An empty public value keeps
        # quiz.js from rendering PREFLOP/FLOP/TURN/RIVER while section/theme data
        # continues to drive the existing colored question blocks.
        item["game_stage"] = ""
        # The client only opens the legacy intermediate river screen when this
        # flag is true. Keep the DOM for compatibility, but never route a modern
        # JACKSIDE player through it.
        item["river_reveal"] = False
    return result


def apply_jackside_poker_story_cleanup() -> None:
    if getattr(daily_414, "_poker_story_cleanup_applied", False):
        return
    daily_414.public_daily_questions = public_daily_questions_without_poker_story
    daily_414._poker_story_cleanup_applied = True


__all__ = [
    "apply_jackside_poker_story_cleanup",
    "public_daily_questions_without_poker_story",
]
