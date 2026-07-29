from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_quiz_hosts_proxy_uploaded_media_before_catch_all() -> None:
    configs = (
        ROOT / "deploy" / "quiz.hijackpoker.ru.nginx",
        ROOT / "deploy" / "v2-test" / "quiz-v2.hijackpoker.ru.nginx",
    )

    for config in configs:
        text = config.read_text(encoding="utf-8")
        media_location = text.index("location /quiz-media/")
        catch_all = text.rindex("location /")

        assert "proxy_pass http://127.0.0.1:" in text[media_location:catch_all]
        assert media_location < catch_all


def test_quiz_hosts_proxy_remembered_identity_actions() -> None:
    configs = (
        ROOT / "deploy" / "quiz.hijackpoker.ru.nginx",
        ROOT / "deploy" / "v2-test" / "quiz-v2.hijackpoker.ru.nginx",
    )

    for config in configs:
        text = config.read_text(encoding="utf-8")
        catch_all = text.rindex("location /")
        for path in ("/api/quiz/identity/confirm", "/api/quiz/identity/forget"):
            location = text.index(f"location = {path}")
            assert "proxy_pass http://127.0.0.1:" in text[location:catch_all]
            assert location < catch_all


def test_quiz_hosts_proxy_member_portal_before_catch_all() -> None:
    configs = (
        ROOT / "deploy" / "quiz.hijackpoker.ru.nginx",
        ROOT / "deploy" / "v2-test" / "quiz-v2.hijackpoker.ru.nginx",
    )

    for config in configs:
        text = config.read_text(encoding="utf-8")
        catch_all = text.rindex("location /")
        account_page = text.index("location = /account")
        account_prefix = text.index("location ^~ /account/")
        assert "proxy_pass http://127.0.0.1:" in text[account_page:account_prefix]
        assert "proxy_pass http://127.0.0.1:" in text[account_prefix:catch_all]
        assert account_page < account_prefix < catch_all
