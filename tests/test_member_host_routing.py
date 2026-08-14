from app.member_host_routing import member_host_redirect_target


def test_club_hosts_send_public_entry_points_to_member_login() -> None:
    for host in ("club-v2.hijackpoker.ru", "club.hijackpoker.ru"):
        assert member_host_redirect_target(host, "/") == "/account/login"
        assert member_host_redirect_target(host, "/login") == "/account/login"


def test_admin_and_member_routes_are_not_rewritten_on_other_hosts() -> None:
    assert member_host_redirect_target("quiz-v2.hijackpoker.ru", "/") is None
    assert member_host_redirect_target("quiz-v2.hijackpoker.ru", "/login") is None
    assert member_host_redirect_target("club-v2.hijackpoker.ru", "/account/login") is None
    assert member_host_redirect_target("club-v2.hijackpoker.ru", "/account") is None
