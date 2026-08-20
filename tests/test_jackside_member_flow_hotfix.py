from pathlib import Path

from app.jackside_critical_hotfix import rewrite_jackside_member_html, rewrite_jackside_quiz_html


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
    css = (ROOT / 'app/static/css/jackside-final-recovery.css').read_text(encoding='utf-8')
    assert 'Торопитесь, квиз уже начался' in source
    assert 'До конца квиза осталось' in source
    assert 'mainRoundSeconds = 254' in source
    assert 'start + Math.max(1, Number(mainRoundSeconds || 254)) * 1000' in source
    assert "['welcome', 'daily-prize', 'daily-jackcoin']" in source
    assert 'jackside-intro-clock' in source
    assert 'urgency-mm' in source
    assert 'urgency-ss' in source
    assert '.jackside-intro-urgency' in css
    assert '.jackside-intro-clock' in css


def test_final_outcome_returns_to_jackside_and_opens_rating() -> None:
    source = (ROOT / 'app/static/js/jackside-critical-hotfix.js').read_text(encoding='utf-8')
    assert "existing.href = '/account'" in source
    assert "existing.textContent = 'Вернуться в JACKSIDE'" in source
    assert "rating.href = '/account?tab=rating'" in source
    assert "rating.textContent = 'Открыть рейтинг'" in source


def test_closed_final_uses_persisted_recovery_instead_of_404_reload() -> None:
    source = (ROOT / 'app/static/js/jackside-critical-hotfix.js').read_text(encoding='utf-8')
    recovery = (ROOT / 'app/jackside_final_recovery.py').read_text(encoding='utf-8')
    assert '/api/jackside/final-result' in source
    assert 'renderRecoveredFinalOutcome' in source
    assert 'response.status === 404' in source
    assert 'window.location.reload();\n            return;' not in source[source.find('response.status === 404'):source.find('response.status === 404') + 180]
    assert '@app.get("/api/jackside/final-result")' in recovery
    assert 'response.status_code == 404' in recovery
    assert 'Вернуться в JACKSIDE' in recovery
    assert 'Открыть рейтинг' in recovery


def test_lobby_copy_does_not_show_need_minimum_phrase() -> None:
    source = (ROOT / 'app/static/js/jackside-critical-hotfix.js').read_text(encoding='utf-8')
    assert 'cleanLobbyMinimumCopy' in source
    assert 'нужно минимум' in source
    assert "replace(/\\s*\\(нужно минимум\\s+\\d+\\)/gi, '')" in source


def test_jackside_brand_replaces_old_member_logo_and_pwa_links() -> None:
    source = (
        '<html><head><link rel="icon" href="/favicon.ico">'
        '<link rel="apple-touch-icon" href="/pwa/apple-touch-icon.png">'
        '<link rel="manifest" href="/manifest.webmanifest"></head>'
        '<body><div data-account-tab="home">JACKSIDE</div>'
        '<img src="/static/img/brand/hi-jack-mark.webp?v=old"></body></html>'
    )
    rewritten = rewrite_jackside_member_html(source)
    assert '/jackside-brand/logo.webp?v=old' in rewritten
    assert '/jackside-brand/favicon.png' in rewritten
    assert '/jackside-brand/apple-touch-icon.png' in rewritten
    assert '/jackside.webmanifest' in rewritten
    assert '/favicon.ico' not in rewritten
    assert '/pwa/apple-touch-icon.png' not in rewritten


def test_jackside_quiz_shell_gets_new_brand_and_browser_icons() -> None:
    source = (
        '<html><head></head><body><main id="quiz-app" '
        'data-campaign-type="daily_414" data-campaign-background="">'
        '<img src="/static/img/brand/hi-jack-mark.webp"></main></body></html>'
    )
    rewritten = rewrite_jackside_quiz_html(source)
    assert '/jackside-brand/logo.webp' in rewritten
    assert '/jackside-brand/favicon.png' in rewritten
    assert '/jackside.webmanifest' in rewritten
