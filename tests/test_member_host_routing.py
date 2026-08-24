from app.member_host_routing import (
    admin_root_redirect_target,
    member_host_redirect_target,
    member_reward_activation_redirect_target,
)


def test_club_hosts_send_public_entry_points_to_member_login() -> None:
    for host in ("club-v2.hijackpoker.ru", "club.hijackpoker.ru"):
        assert member_host_redirect_target(host, "/") == "/account/login"
        assert member_host_redirect_target(host, "/login") == "/account/login"
        assert member_host_redirect_target(host, "/account/login") is None
        assert member_host_redirect_target(host, "/account") is None


def test_staging_club_host_sends_admin_pages_to_staging_quiz() -> None:
    assert (
        member_host_redirect_target("club-v2.hijackpoker.ru", "/master")
        == "https://quiz-v2.hijackpoker.ru/master"
    )
    assert (
        member_host_redirect_target("club-v2.hijackpoker.ru", "/master/settings")
        == "https://quiz-v2.hijackpoker.ru/master/settings"
    )
    assert (
        member_host_redirect_target("club-v2.hijackpoker.ru", "/logout")
        == "https://quiz-v2.hijackpoker.ru/logout"
    )


def test_production_club_host_sends_admin_pages_to_production_quiz() -> None:
    assert (
        member_host_redirect_target("club.hijackpoker.ru", "/master")
        == "https://quiz.hijackpoker.ru/master"
    )
    assert (
        member_host_redirect_target("club.hijackpoker.ru", "/master/settings")
        == "https://quiz.hijackpoker.ru/master/settings"
    )
    assert (
        member_host_redirect_target("club.hijackpoker.ru", "/logout")
        == "https://quiz.hijackpoker.ru/logout"
    )


def test_staging_quiz_sends_member_pages_to_staging_club() -> None:
    assert (
        member_host_redirect_target("quiz-v2.hijackpoker.ru", "/account")
        == "https://club-v2.hijackpoker.ru/account"
    )
    assert (
        member_host_redirect_target("quiz-v2.hijackpoker.ru", "/account/login")
        == "https://club-v2.hijackpoker.ru/account/login"
    )
    assert (
        member_host_redirect_target("quiz-v2.hijackpoker.ru", "/players/p/example")
        == "https://club-v2.hijackpoker.ru/players/p/example"
    )
    assert (
        member_host_redirect_target("quiz-v2.hijackpoker.ru", "/legal/privacy")
        == "https://club-v2.hijackpoker.ru/legal/privacy"
    )


def test_production_quiz_sends_member_pages_to_production_club() -> None:
    assert (
        member_host_redirect_target("quiz.hijackpoker.ru", "/account")
        == "https://club.hijackpoker.ru/account"
    )
    assert (
        member_host_redirect_target("quiz.hijackpoker.ru", "/account/login")
        == "https://club.hijackpoker.ru/account/login"
    )
    assert (
        member_host_redirect_target("quiz.hijackpoker.ru", "/players/p/example")
        == "https://club.hijackpoker.ru/players/p/example"
    )
    assert (
        member_host_redirect_target("quiz.hijackpoker.ru", "/legal/privacy")
        == "https://club.hijackpoker.ru/legal/privacy"
    )


def test_admin_host_root_goes_to_login_or_workspace() -> None:
    for host in ("quiz-v2.hijackpoker.ru", "quiz.hijackpoker.ru"):
        assert admin_root_redirect_target(host, authenticated=False) == "/login"
        assert admin_root_redirect_target(host, authenticated=True) == "/master/clients"

    assert admin_root_redirect_target("club.hijackpoker.ru", authenticated=False) is None


def test_admin_hosts_keep_other_admin_and_quiz_entry_points() -> None:
    for host in ("quiz-v2.hijackpoker.ru", "quiz.hijackpoker.ru"):
        assert member_host_redirect_target(host, "/") is None
        assert member_host_redirect_target(host, "/login") is None
        assert member_host_redirect_target(host, "/master") is None
        assert member_host_redirect_target(host, "/quiz") is None


def test_reward_activation_redirect_returns_to_exact_card() -> None:
    location = (
        "/account?tab=vault&ok=%D0%9D%D0%B0%D0%B3%D1%80%D0%B0%D0%B4%D0%B0+"
        "%D0%B0%D0%BA%D1%82%D0%B8%D0%B2%D0%B8%D1%80%D0%BE%D0%B2%D0%B0%D0%BD%D0%B0"
    )

    assert member_reward_activation_redirect_target(
        "POST",
        "/account/rewards/417/activate",
        303,
        location,
    ) == f"{location}#card-417"


def test_reward_activation_redirect_does_not_touch_other_redirects() -> None:
    assert (
        member_reward_activation_redirect_target(
            "POST",
            "/account/rewards/417/purchase",
            303,
            "/account?tab=vault&ok=done",
        )
        is None
    )
    assert (
        member_reward_activation_redirect_target(
            "POST",
            "/account/rewards/417/activate",
            303,
            "/account?tab=rating&ok=done",
        )
        is None
    )
    assert (
        member_reward_activation_redirect_target(
            "GET",
            "/account/rewards/417/activate",
            303,
            "/account?tab=vault&ok=done",
        )
        is None
    )
