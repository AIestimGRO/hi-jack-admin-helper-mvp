"""Centralized JACKSIDE copy: result ranges and shared UI strings."""

from __future__ import annotations

from typing import Any


RESULT_COPY_BY_RANGE: tuple[dict[str, Any], ...] = (
    {
        "code": "0_3",
        "min_correct": 0,
        "max_correct": 3,
        "title": "Тёплый стол",
        "message": "Основная часть завершена. JACKCOIN за ответы уже на балансе.",
    },
    {
        "code": "4_6",
        "min_correct": 4,
        "max_correct": 6,
        "title": "Достойный результат",
        "message": "Основная часть завершена. JACKCOIN за ответы уже на балансе.",
    },
    {
        "code": "7_8",
        "min_correct": 7,
        "max_correct": 8,
        "title": "Сильная раздача",
        "message": "Основная часть завершена. JACKCOIN за ответы уже на балансе.",
    },
    {
        "code": "9",
        "min_correct": 9,
        "max_correct": 9,
        "title": "Почти идеально",
        "message": "Девять из десяти. Основная часть завершена, JACKCOIN уже на балансе.",
    },
    {
        "code": "10",
        "min_correct": 10,
        "max_correct": 10,
        "title": "Идеальная десятка",
        "message": "10/10. Максимум основной части, JACKCOIN уже на балансе.",
        "message_no_final": "10/10. Максимум основной части, JACKCOIN уже на балансе.",
    },
)

DEFAULT_RULES_VERSION = "1.2"
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
• в финальный стол проходят все участники, которые ответили на все 10 вопросов основной части до общего дедлайна 4:14;
• количество правильных ответов и скорость прохождения основной части на допуск в финал не влияют;
• после закрытия основной части идёт 1 минута ожидания, затем начинается финальный стол;
• финальный стол проводится даже если в него прошёл только один игрок — автоматической победы нет;
• до последнего вопроса финала ошибка или отсутствие ответа выбивает игрока;
• единственный финалист также обязан пройти финальные вопросы и правильно ответить на последний вопрос, чтобы победить;
• победитель — тот, кто первым правильно ответил на последний вопрос финального стола;
• если финальный стол состоит из одного вопроса, побеждает первый правильный ответ на этот вопрос;
• если на последнем вопросе правильного ответа нет — победителя нет и главный приз не выдаётся.

JACKCOIN начисляются только за полностью завершённую основную часть.
Главный приз выпуска указывается в карточке дня.

Время сервера и момент приёма ответа сервером являются источником истины для таймеров и определения победителя.
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
            if final_eligible:
                message = (
                    f"{message} Вы в финальном столе: на допуск в финал "
                    "не влияют ни количество правильных ответов, ни скорость."
                )
            else:
                message = (
                    f"{message} Финальный стол доступен только тем, кто успел "
                    "ответить на все 10 вопросов до общего дедлайна 4:14."
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
