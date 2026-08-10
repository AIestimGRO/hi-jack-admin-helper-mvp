(() => {
  if (window.location.pathname !== '/master') return;

  const tabs = document.querySelector('.master-tabs');
  if (!tabs || tabs.dataset.organized === '1') return;
  tabs.dataset.organized = '1';

  const panelNodes = Array.from(document.querySelectorAll('[data-master-panel]'));
  if (!panelNodes.length) return;

  const buttons = new Map(
    Array.from(tabs.querySelectorAll('[data-master-tab]')).map((button) => [button.dataset.masterTab, button]),
  );

  const labels = {
    preferences: 'Преференции',
    admins: 'Администраторы',
    campaigns: 'Квизы и JACKSIDE',
    analytics: 'Аналитика JACKSIDE',
    engagement: 'Звания и рефералы',
    audit: 'Журнал действий',
  };
  Object.entries(labels).forEach(([key, label]) => {
    const button = buttons.get(key);
    if (button) button.textContent = label;
  });

  const intro = document.createElement('div');
  intro.className = 'master-nav-intro';
  intro.innerHTML = '<strong>Мастер-админ</strong><small>Выберите рабочий раздел. Настройки сгруппированы по задачам.</small>';

  const groups = [
    {
      title: 'Игра',
      tabs: ['campaigns', 'analytics'],
      links: [
        ['/master/jackside-issues', 'Выпуски JACKSIDE', ''],
        ['/master/hijack-rating', 'Рейтинг HI, JACK!', 'is-gold'],
      ],
    },
    {
      title: 'Игроки',
      tabs: ['engagement'],
      links: [
        ['/master/engagement-icons', 'Иконки коллекции', ''],
      ],
    },
    {
      title: 'Награды',
      tabs: ['preferences'],
      links: [
        ['/admin/vault', 'Hi, Store', 'is-gold'],
      ],
    },
    {
      title: 'Система',
      tabs: ['admins', 'audit'],
      links: [],
    },
  ];

  tabs.textContent = '';
  tabs.append(intro);

  groups.forEach((definition) => {
    const group = document.createElement('div');
    group.className = 'master-nav-group';
    const title = document.createElement('span');
    title.className = 'master-nav-group-title';
    title.textContent = definition.title;
    group.append(title);

    definition.tabs.forEach((key) => {
      const button = buttons.get(key);
      if (button) group.append(button);
    });

    definition.links.forEach(([href, label, className]) => {
      const link = document.createElement('a');
      link.href = href;
      link.className = `master-nav-link ${className}`.trim();
      link.textContent = label;
      group.append(link);
    });
    tabs.append(group);
  });

  // Keep future/unclassified tabs visible instead of accidentally hiding features.
  const used = new Set(groups.flatMap((group) => group.tabs));
  const otherButtons = Array.from(buttons.entries()).filter(([key]) => !used.has(key));
  if (otherButtons.length) {
    const other = document.createElement('div');
    other.className = 'master-nav-group';
    const title = document.createElement('span');
    title.className = 'master-nav-group-title';
    title.textContent = 'Другое';
    other.append(title);
    otherButtons.forEach(([, button]) => other.append(button));
    tabs.append(other);
  }

  const shell = document.createElement('div');
  shell.className = 'master-organized-shell';
  const content = document.createElement('main');
  content.className = 'master-organized-content';

  tabs.insertAdjacentElement('beforebegin', shell);
  shell.append(tabs, content);
  panelNodes.forEach((panel) => content.append(panel));

  const active = tabs.querySelector('[data-master-tab].active');
  active?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
})();
