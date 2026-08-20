from pathlib import Path

from app.jackside_critical_hotfix import rewrite_jackside_member_html


ROOT = Path(__file__).resolve().parents[1]


def test_member_account_hotfix_is_injected_only_into_member_shell() -> None:
    source = '<html><body><div data-account-tab="home">JACKSIDE</div></body></html>'
    rewritten = rewrite_jackside_member_html(source)
    assert '/static/js/jackside-member-critical-hotfix.js' in rewritten
    assert rewrite_jackside_member_html('<html><body>admin</body></html>') == '<html><body>admin</body></html>'


def test_member_actions_use_lobby_and_participate_labels_and_keep_rules() -> None:
    source = (ROOT / 'app/static/js/jackside-member-critical-hotfix.js').read_text(encoding='utf-8')
    assert "'Участвовать'" in source
    assert "'Лобби'" in source
    assert "'Полные правила'" in source
    assert "href = '/jackside/rules'" in source
    assert "current.startsWith('продолж')" in source


def test_intro_urgency_counts_down_to_main_round_end() -> None:
    source = (ROOT / 'app/static/js/jackside-critical-hotfix.js').read_text(encoding='utf-8')
    assert 'Торопитесь, квиз уже начался' in source
    assert 'До конца квиза осталось' in source
    assert 'app.dataset.activeUntil' in source
    assert "['welcome', 'daily-prize', 'daily-jackcoin']" in source
    assert 'jacksideUrgencyBlink' in source


def test_final_outcome_returns_to_jackside_and_opens_rating() -> None:
    source = (ROOT / 'app/static/js/jackside-critical-hotfix.js').read_text(encoding='utf-8')
    assert "existing.href = '/account'" in source
    assert "existing.textContent = 'Вернуться в JACKSIDE'" in source
    assert "rating.href = '/account?tab=rating'" in source
    assert "rating.textContent = 'Открыть рейтинг'" in source
