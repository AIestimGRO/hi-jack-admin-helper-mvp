"""Centralized JACKSIDE copy: result ranges and shared UI strings."""

from __future__ import annotations

from typing import Any


RESULT_COPY_BY_RANGE: tuple[dict[str, Any], ...] = (
    {
        "code": "0_3",
        "min_correct": 0,
        "max_correct": 3,
        "title": "Тёплый стол",
        "message": "Сегодня раздача сложилась иначе. JACKCOIN за участие уже на балансе — завтра новый шанс.",
    },
    {
        "code": "4_6",
        "min_correct": 4,
        "max_correct": 6,
        "title": "Достойный результат",
        "message": "Хороший ход. Есть запас роста — следите за серией и возвращайтесь завтра.",
    },
    {
        "code": "7_8",
        "min_correct": 7,
        "max_correct": 8,
        "title": "Сильная раздача",
        "message": "Почти у финального стола. Ещё чуть точнее — и вы среди претендентов.",
    },
    {
        "code": "9",
        "min_correct": 9,
        "max_correct": 9,
        "title": "Почти идеально",
        "message": "Девять из десяти. Один промах от максимума — отличный день за JACKSIDE.",
    },
    {
        "code": "10",
        "min_correct": 10,
        "max_correct": 10,
        "title": "Идеальная десятка",
        "message": "10/10. Вы собрали максимум основной части. Ждём финальный стол!",
        "message_no_final": "10/10. Вы собрали максимум основной части. JACKCOIN уже на балансе.",
    },
)

DEFAULT_RULES_VERSION = "1.1"
DEFAULT_RULES_TITLE = "Правила JACKSIDE 4:14"
DEFAULT_RULES_CONTENT = """\
JACKSIDE — один общий стол на весь клуб Hi, Jack.

Каждый выпуск:
• 10 вопросов основной части;
• один общий таймер 4 минуты 14 секунд начинается для всего клуба одновременно;
• войти можно и после старта, но дополнительное время не даётся: если прошло 2 минуты, на ответы остаётся 2 минуты 14 секунд;
• зачётное время считается от общего старта выпуска, а не от момента входа игрока;
• одна попытка без возврата к уже сохранённым ответам;
• в зачёт попадает только полностью завершённая до общего дедлайна основная часть;
• в финал проходят до 10 лучших по правильным ответам, затем по зачётному времени;
• до последнего вопроса финала ошибка или отсутствие ответа выбивает игрока;
• победитель — тот, кто первым правильно ответил на последний вопрос финального стола;
• если финальный стол состоит из одного вопроса, побеждает первый правильный ответ на этот вопрос;
• если на последнем вопросе правильного ответа нет — победителя нет и главный приз не выдаётся.

JACKCOIN начисляются только за полностью завершённую основную часть.
Главный приз выпуска указывается в карточке дня.

Время сервера и момент приёма ответа сервером являются источником истины для таймеров, зачётного времени и определения победителя.
"""


def result_copy_for_score(
    correct_count: int, *, final_eligible: bool = True
) -> dict[str, str]:
    score = max(0, int(correct_count))
    for item in RESULT_COPY_BY_RANGE:
        if item["min_correct"] <= score <= item["max_correct"]:
            message = item["message"]
            if not final_eligible and item.get("message_no_final"):
                message = item["message_no_final"]
            elif not final_eligible and item["code"] in {"7_8", "9", "10"}:
                message = item["message"].replace(
                    "Ждём финальный стол!",
                    "JACKCOIN уже на балансе.",
                ).replace(
                    "Почти у финального стола. Ещё чуть точнее — и вы среди претендентов.",
                    "Хороший результат основной части. В отбор финала этот заход не вошёл.",
                )
            return {"code": item["code"], "title": item["title"], "message": message}
    return {
        "code": "0_3",
        "title": RESULT_COPY_BY_RANGE[0]["title"],
        "message": RESULT_COPY_BY_RANGE[0]["message"],
    }


def prize_headline(
    *,
    prize_type: str,
    jackcoin_amount: int = 0,
    card_title: str | None = None,
) -> str:
    kind = str(prize_type or "none")
    if kind == "jackcoin" and int(jackcoin_amount or 0) > 0:
        return f"Победитель получит {int(jackcoin_amount)} JACKCOIN"
    if kind == "reward_card" and card_title:
        return f"Победитель получит карту «{card_title}»"
    if kind == "reward_card":
        return "Победитель получит карту из THE VAULT"
    return "Главный приз выпуска уточняется"
