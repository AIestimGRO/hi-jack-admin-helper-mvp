from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_admin_base_uses_persistent_right_navigation() -> None:
    template = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    css = (ROOT / "app/static/css/admin-persistent-nav.css").read_text(encoding="utf-8")

    assert "admin-persistent-nav.css" in template
    assert "class=\"admin-menu-panel admin-persistent-nav\"" in template
    assert "/master/jackside-issues" in template
    assert "/master/hijack-rating" in template
    assert "/master/engagement-icons" in template

    assert ".admin-persistent-nav[hidden]" in css
    assert "display: flex !important" in css
    assert "right: 16px !important" in css
    assert "margin-right: calc(var(--admin-rail-width)" in css
