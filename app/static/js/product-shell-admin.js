(() => {
  const HIJACK_CONDITIONS = [
    ['hijack_global_rating', 'HI, JACK! · глобальный рейтинг'],
    ['hijack_global_kills', 'HI, JACK! · глобальные киллы'],
    ['hijack_year_rating', 'HI, JACK! · рейтинг за год'],
    ['hijack_month_rating', 'HI, JACK! · рейтинг за месяц'],
    ['hijack_latest_rating', 'HI, JACK! · рейтинг последнего турнира'],
    ['hijack_year_kills', 'HI, JACK! · киллы за год'],
    ['hijack_month_kills', 'HI, JACK! · киллы за месяц'],
    ['hijack_latest_kills', 'HI, JACK! · киллы последнего турнира'],
    ['hijack_tournaments_played', 'HI, JACK! · сыграно турниров'],
    ['hijack_top3_finishes', 'HI, JACK! · финиши в топ-3'],
    ['hijack_wins', 'HI, JACK! · победы в турнирах'],
    ['hijack_best_rating', 'HI, JACK! · лучший рейтинг за один турнир'],
  ];

  function renameVaultCopy() {
    document.querySelectorAll('.admin-primary-nav a, .admin-menu-panel strong, .admin-bottom-nav small').forEach((node) => {
      const text = node.textContent.trim();
      if (text === 'THE VAULT' || text === 'Хранилище' || text === 'Vault') {
        node.textContent = text === 'Vault' ? 'Store' : 'Hi, Store';
      }
    });
    if (window.location.pathname.startsWith('/admin/vault')) {
      document.querySelectorAll('h1, h2, .eyebrow, .muted').forEach((node) => {
        const text = node.textContent.trim();
        if (text === 'THE VAULT' || text === 'Хранилище') node.textContent = 'Hi, Store';
      });
    }
  }

  function addEngagementTools() {
    const panel = document.querySelector('[data-master-panel="engagement"]');
    if (!panel || panel.querySelector('[data-engagement-tools]')) return;
    const tools = document.createElement('div');
    tools.dataset.engagementTools = '1';
    tools.style.display = 'flex';
    tools.style.flexWrap = 'wrap';
    tools.style.gap = '8px';
    tools.style.margin = '0 0 18px';
    tools.innerHTML = `
      <a class="button" href="/master/engagement-icons">Иконки званий и достижений</a>
      <a class="button" href="/master/hijack-rating">HI, JACK! рейтинг</a>
    `;
    const head = panel.querySelector('.section-head');
    if (head) head.insertAdjacentElement('afterend', tools);
    else panel.prepend(tools);
  }

  function configureConditionSelect(select) {
    if (select.dataset.hijackExtended === '1') return;
    select.dataset.hijackExtended = '1';
    const group = document.createElement('optgroup');
    group.label = 'HI, JACK!';
    HIJACK_CONDITIONS.forEach(([value, label]) => {
      if (select.querySelector(`option[value="${value}"]`)) return;
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      group.append(option);
    });
    select.append(group);

    const form = select.closest('form');
    if (!form) return;
    const period = form.querySelector('select[name="period_code"]');
    const updatePeriod = () => {
      const isHiJack = select.value.startsWith('hijack_');
      if (!period) return;
      period.disabled = isHiJack;
      if (isHiJack) period.value = 'all_time';
      period.closest('label')?.classList.toggle('is-hijack-period-disabled', isHiJack);
      if (isHiJack) {
        period.title = 'Период уже определён выбранным показателем HI, JACK!';
      } else {
        period.removeAttribute('title');
      }
    };
    select.addEventListener('change', updatePeriod);
    updatePeriod();

    form.addEventListener('submit', () => {
      if (!select.value.startsWith('hijack_')) return;
      if (period) period.disabled = false;
      if (form.action.includes('/api/master/jackside-titles/create')) {
        form.action = '/api/master/hijack-titles/create';
        return;
      }
      const match = form.action.match(/\/api\/master\/jackside-titles\/(\d+)\/update/);
      if (match) form.action = `/api/master/hijack-titles/${match[1]}/update`;
    });
  }

  function extendTitleConditions() {
    document.querySelectorAll('[data-master-panel="engagement"] select[name="condition_code"]').forEach(configureConditionSelect);
  }

  function loadRatingManager() {
    if (window.location.pathname !== '/master/hijack-rating') return;
    if (document.querySelector('script[data-hijack-rating-manager]')) return;
    const script = document.createElement('script');
    script.dataset.hijackRatingManager = '1';
    script.src = '/static/js/hijack-rating-admin-v2.js?v=1';
    document.body.append(script);
  }

  renameVaultCopy();
  addEngagementTools();
  extendTitleConditions();
  loadRatingManager();
})();
