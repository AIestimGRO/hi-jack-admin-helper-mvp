from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_jackside_return_links_are_native_and_prestyled() -> None:
    html = (ROOT / "app/templates/quiz.html").read_text(encoding="utf-8")

    return_text = "\u0412\u0435\u0440\u043d\u0443\u0442\u044c\u0441\u044f \u0432 JACKSIDE"
    old_app_text = "\u0412\u0435\u0440\u043d\u0443\u0442\u044c\u0441\u044f \u0432 \u043f\u0440\u0438\u043b\u043e\u0436\u0435\u043d\u0438\u0435"
    old_rating_text = "\u0414\u043d\u0435\u0432\u043d\u043e\u0439 \u0440\u0435\u0439\u0442\u0438\u043d\u0433"

    assert html.count(f">{return_text}</a>") == 3
    assert old_app_text not in html
    assert old_rating_text not in html
    assert html.index(".jackside-return-link {") < html.index("</head>")
    assert ".jackside-return-link[hidden] { display: none; }" in html
    assert (
        'class="quiz-secondary quiz-account-return jackside-return-link" href="/account"'
        in html
    )
    assert (
        'class="quiz-secondary jackside-day-rating jackside-return-link" '
        'href="/account" hidden'
        in html
    )
