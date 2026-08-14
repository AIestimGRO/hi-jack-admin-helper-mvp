from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_wallet_suffix_is_hidden_before_javascript_runs() -> None:
    cleanup = (ROOT / "app/static/css/member-brand-refinement.css").read_text(
        encoding="utf-8"
    )
    script = (ROOT / "app/static/js/member-final-polish.js").read_text(
        encoding="utf-8"
    )
    base = (ROOT / "app/templates/member_base.html").read_text(encoding="utf-8")

    selector = (
        '.member-app-page[data-account-tab="home"] '
        ".jc-wallet-balance small"
    )
    assert selector in cleanup
    assert "display: none !important;" in cleanup
    assert "querySelector('.jc-wallet-balance small')?.remove()" not in script

    # The CSS must stay in the document head as a normal render-blocking
    # stylesheet; otherwise a cold origin could paint the legacy suffix first.
    link_marker = "data-member-brand-refinement"
    assert link_marker in base
    assert base.index(link_marker) < base.index("</head>")
