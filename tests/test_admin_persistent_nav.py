from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_admin_uses_only_one_persistent_navigation_shell() -> None:
    template = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    css = (ROOT / "app/static/css/admin-persistent-nav.css").read_text(encoding="utf-8")
    admin_js = (ROOT / "app/static/js/product-shell-admin.js").read_text(encoding="utf-8")

    assert "admin-persistent-nav.css" in template
    assert 'class="admin-menu-panel admin-persistent-nav"' in template
    assert "/master?tab=campaigns" in template
    assert "/master?tab=analytics" in template
    assert "/master?tab=engagement" in template
    assert "/master?tab=preferences" in template
    assert "/staff-access" in template
    assert "/master?tab=audit" in template

    assert ".admin-persistent-nav[hidden]" in css
    assert ".admin-app-body .master-tabs" in css
    assert "display: none !important" in css
    assert "right: 16px !important" in css

    assert "master-admin-organized.js" not in admin_js
    assert "master-admin-organized.css" not in admin_js


def test_profile_experience_is_loaded_only_on_profile_tab() -> None:
    script = (ROOT / "app/static/js/member-profile-refresh.js").read_text(encoding="utf-8")

    assert "if (tab === 'profile')" in script
    assert "profile-experience-v2.js?v=3" in script
    assert "member-achievement-carousel.css?v=4" in script