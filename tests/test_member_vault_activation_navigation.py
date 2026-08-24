from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_member_vault_activation_returns_to_exact_card() -> None:
    html = (ROOT / "app/templates/member_vault_fast.html").read_text(encoding="utf-8")

    assert 'id="card-{{ reward.id }}"' in html
    assert (
        'action="/account/rewards/{{ reward.id }}/activate#card-{{ reward.id }}"'
        in html
    )


def test_activation_keeps_existing_post_route_and_csrf() -> None:
    html = (ROOT / "app/templates/member_vault_fast.html").read_text(encoding="utf-8")

    assert 'method="post"' in html
    assert 'name="csrf_token" value="{{ csrf_token }}"' in html
    assert '/account/rewards/{{ reward.id }}/qr.png' in html
