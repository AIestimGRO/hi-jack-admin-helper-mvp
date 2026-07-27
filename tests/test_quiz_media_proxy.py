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
