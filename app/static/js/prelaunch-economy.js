(() => {
  const label = (type) => type === 'final' ? 'Финал — 0 JC за участие' : 'Обычный — 50 JC за участие';

  const render = (imports) => {
    if (!Array.isArray(imports) || !imports.length) return;
    const page = document.querySelector('.prelaunch-page');
    const audit = document.querySelector('.prelaunch-audit');
    const csrf = document.querySelector('input[name="csrf_token"]')?.value || '';
    if (!page || !csrf || document.querySelector('[data-hijack-economy]')) return;

    const section = document.createElement('section');
    section.className = 'card prelaunch-hijack-economy';
    section.dataset.hijackEconomy = '1';
    section.innerHTML = `
      <div class="section-head"><div><p class="eyebrow">HI JACK</p><h2>Типы последних турниров</h2></div><span class="muted">проверка начисления за участие</span></div>
      <p class="muted">По названию «Финал…» определяется автоматически. Здесь можно исправить тип вручную; система проведёт только корректирующую ledger-операцию, не удаляя историю.</p>
      <div class="prelaunch-hijack-list"></div>`;
    const list = section.querySelector('.prelaunch-hijack-list');

    imports.slice(0, 20).forEach((item) => {
      const row = document.createElement('form');
      row.method = 'post';
      row.action = `/api/master/economy/hijack/${item.id}/type`;
      row.className = 'prelaunch-hijack-row';
      const current = item.tournament_type === 'final' ? 'final' : 'regular';
      row.innerHTML = `
        <input type="hidden" name="csrf_token" value="${csrf.replaceAll('&', '&amp;').replaceAll('"', '&quot;')}">
        <div><strong>${String(item.tournament_name || 'HI JACK').replaceAll('&', '&amp;').replaceAll('<', '&lt;')}</strong><small>${String(item.tournament_date || '')}</small></div>
        <select name="tournament_type" aria-label="Тип турнира">
          <option value="regular" ${current === 'regular' ? 'selected' : ''}>Обычный</option>
          <option value="final" ${current === 'final' ? 'selected' : ''}>Финал</option>
        </select>
        <span>${label(current)}</span>
        <button type="submit">Сохранить</button>`;
      row.querySelector('select').addEventListener('change', (event) => {
        row.querySelector('span').textContent = label(event.target.value);
      });
      list.appendChild(row);
    });
    page.insertBefore(section, audit || null);
  };

  const run = async () => {
    if (location.pathname !== '/master/economy') return;
    try {
      const response = await fetch('/api/master/hijack-rating/manage', {
        credentials: 'same-origin', headers: { Accept: 'application/json' }
      });
      if (!response.ok) return;
      const payload = await response.json();
      render(payload.imports || []);
    } catch (_) {
      // Economy settings remain fully usable when HI JACK history is unavailable.
    }
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run, { once: true });
  else run();
})();
