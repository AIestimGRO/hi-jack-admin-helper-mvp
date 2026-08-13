from app.prelaunch_profile_sharing import (
    PROFILE_VISIBILITY_DEFAULTS,
    _inject_profile_visibility_entry,
)


def test_profile_visibility_defaults_are_open() -> None:
    assert PROFILE_VISIBILITY_DEFAULTS
    assert all(PROFILE_VISIBILITY_DEFAULTS.values())
    assert set(PROFILE_VISIBILITY_DEFAULTS) == {
        "nickname",
        "avatar",
        "result",
        "place",
        "titles",
        "achievements",
        "game_stats",
        "game_history",
    }


def test_profile_visibility_link_is_injected_once() -> None:
    source = '<main><section class="member-card account-panel profile-panel">profile</section></main>'
    rendered = _inject_profile_visibility_entry(source)
    assert rendered.count("profile-visibility-entry") == 1
    assert "Видимость профиля" in rendered
    assert 'href="/account/profile-sharing"' in rendered
    assert _inject_profile_visibility_entry(rendered) == rendered
