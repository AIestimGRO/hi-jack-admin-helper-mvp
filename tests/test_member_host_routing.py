from app.member_host_routing import member_host_redirect_target


def test_club_hosts_send_public_entry_points_to_member_login() -> None:
    for host in ("club-v2.hijackpoker.ru", "club.hijackpoker.ru"):
        assert member_host_redirect_target(host, "/") == "/account/login"
        assert member_host_redirect_target(host, "/login") == "/account/login"
        assert member_host_redirect_target(host, "/account/login") is None
        assert member_host_redirect_target(host, "/account") is None


def test_club_host_sends_admin_pages_to_quiz_v2() -> None:
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


def test_quiz_v2_sends_member_pages_to_club_v2() -> None:
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


def test_quiz_v2_keeps_admin_and_quiz_entry_points() -> None:
    assert member_host_redirect_target("quiz-v2.hijackpoker.ru", "/") is None
    assert member_host_redirect_target("quiz-v2.hijackpoker.ru", "/login") is None
    assert member_host_redirect_target("quiz-v2.hijackpoker.ru", "/master") is None
    assert member_host_redirect_target("quiz-v2.hijackpoker.ru", "/quiz") is None
