# Параллельное тестовое развёртывание v2.0

Эта схема не меняет действующий каталог `/opt/hi-jack-admin-helper`, службу
`hi-jack-admin-helper`, порт 8090, рабочую базу или текущие Nginx-конфиги.

Тестовая версия использует:

- каталог `/opt/hi-jack-admin-helper-v2`;
- службу `hi-jack-admin-helper-v2`;
- порт `127.0.0.1:8091`;
- отдельную копию базы;
- `club-v2.hijackpoker.ru` и `quiz-v2.hijackpoker.ru`.

## 1. DNS

Создайте две A-записи на тот же IPv4, который использует текущий
`quiz.hijackpoker.ru`:

- `club-v2.hijackpoker.ru`;
- `quiz-v2.hijackpoker.ru`.

Рекомендуемый TTL на время проверки — 300 секунд. Если DNS обслуживает
Cloudflare, на время выпуска сертификатов используйте режим DNS only.

## 2. Проверка действующей версии

```bash
sudo systemctl is-active hi-jack-admin-helper
curl --fail http://127.0.0.1:8090/health
sudo ss -ltnp | grep ':8091'
```

Последняя команда до запуска v2 не должна показывать занятого порта 8091.

## 3. Получение тестовой ветки

```bash
sudo mkdir -p /opt/hi-jack-admin-helper-v2
sudo chown www-data:www-data /opt/hi-jack-admin-helper-v2
REPO_URL="$(sudo -u www-data git -C /opt/hi-jack-admin-helper remote get-url origin)"
sudo -u www-data git clone --branch agent/v2-responsive-identity --single-branch "$REPO_URL" /opt/hi-jack-admin-helper-v2
cd /opt/hi-jack-admin-helper-v2
sudo -u www-data python3 -m venv .venv
sudo -u www-data .venv/bin/pip install -r requirements.txt
```

## 4. Изолированная копия данных

SQLite копируется штатным online backup API, поэтому рабочую службу
останавливать не нужно.

```bash
sudo install -d -o www-data -g www-data /opt/hi-jack-admin-helper-v2/data
sudo -u www-data /opt/hi-jack-admin-helper/.venv/bin/python -c "import sqlite3; s=sqlite3.connect('/opt/hi-jack-admin-helper/data/club_tools.sqlite3'); d=sqlite3.connect('/opt/hi-jack-admin-helper-v2/data/club_tools.sqlite3'); s.backup(d); d.close(); s.close()"
sudo cp /opt/hi-jack-admin-helper/.env /opt/hi-jack-admin-helper-v2/.env
sudo sed -i 's/^HJC_PORT=.*/HJC_PORT=8091/' /opt/hi-jack-admin-helper-v2/.env
sudo sed -i 's|^HJC_PUBLIC_BASE_URL=.*|HJC_PUBLIC_BASE_URL=https://club-v2.hijackpoker.ru|' /opt/hi-jack-admin-helper-v2/.env
sudo sed -i 's|^HJC_QUIZ_PUBLIC_BASE_URL=.*|HJC_QUIZ_PUBLIC_BASE_URL=https://quiz-v2.hijackpoker.ru|' /opt/hi-jack-admin-helper-v2/.env
sudo chown -R www-data:www-data /opt/hi-jack-admin-helper-v2/data
sudo chown root:www-data /opt/hi-jack-admin-helper-v2/.env
sudo chmod 640 /opt/hi-jack-admin-helper-v2/.env
```

Если в рабочей версии есть нужные загруженные файлы, скопируйте их отдельно:

```bash
sudo install -d -o www-data -g www-data /opt/hi-jack-admin-helper-v2/data/uploads
sudo cp -a /opt/hi-jack-admin-helper/data/uploads/. /opt/hi-jack-admin-helper-v2/data/uploads/
sudo chown -R www-data:www-data /opt/hi-jack-admin-helper-v2/data/uploads
```

## 5. Отдельная systemd-служба

```bash
sudo cp deploy/v2-test/hi-jack-admin-helper-v2.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hi-jack-admin-helper-v2
sudo systemctl status hi-jack-admin-helper-v2 --no-pager
curl --fail http://127.0.0.1:8091/health
curl --fail http://127.0.0.1:8090/health
```

Обе health-проверки должны завершиться успешно.

## 6. Nginx и HTTPS

Сначала используется HTTP-конфиг только для ACME challenge:

```bash
sudo mkdir -p /var/www/certbot/.well-known/acme-challenge
sudo cp deploy/v2-test/hijack-v2-bootstrap.nginx /etc/nginx/sites-available/hijack-v2-bootstrap
sudo ln -s /etc/nginx/sites-available/hijack-v2-bootstrap /etc/nginx/sites-enabled/hijack-v2-bootstrap
sudo nginx -t
sudo systemctl reload nginx
sudo certbot certonly --webroot -w /var/www/certbot -d club-v2.hijackpoker.ru
sudo certbot certonly --webroot -w /var/www/certbot -d quiz-v2.hijackpoker.ru
```

После успешного выпуска обоих сертификатов:

```bash
sudo cp deploy/v2-test/club-v2.hijackpoker.ru.nginx /etc/nginx/sites-available/club-v2.hijackpoker.ru
sudo cp deploy/v2-test/quiz-v2.hijackpoker.ru.nginx /etc/nginx/sites-available/quiz-v2.hijackpoker.ru
sudo ln -s /etc/nginx/sites-available/club-v2.hijackpoker.ru /etc/nginx/sites-enabled/club-v2.hijackpoker.ru
sudo ln -s /etc/nginx/sites-available/quiz-v2.hijackpoker.ru /etc/nginx/sites-enabled/quiz-v2.hijackpoker.ru
sudo unlink /etc/nginx/sites-enabled/hijack-v2-bootstrap
sudo nginx -t
sudo systemctl reload nginx
```

## 7. Отдельные ежедневные копии

```bash
sudo mkdir -p /opt/hi-jack-club-tools-v2-backups
sudo chmod 700 /opt/hi-jack-club-tools-v2-backups
sudo cp deploy/v2-test/hi-jack-admin-helper-v2-backup.service /etc/systemd/system/
sudo cp deploy/v2-test/hi-jack-admin-helper-v2-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hi-jack-admin-helper-v2-backup.timer
sudo systemctl start hi-jack-admin-helper-v2-backup.service
sudo systemctl list-timers hi-jack-admin-helper-v2-backup.timer --no-pager
```

## 8. Проверка

```bash
curl -I https://club-v2.hijackpoker.ru/login
curl -I https://quiz-v2.hijackpoker.ru/
curl --fail 'https://quiz-v2.hijackpoker.ru/api/quiz/questions?campaign=default'
curl --fail http://127.0.0.1:8090/health
```

Telegram Login потребует добавить в BotFather тестовые разрешённые URL:

- `https://quiz-v2.hijackpoker.ru`;
- `https://quiz-v2.hijackpoker.ru/quiz/telegram/callback`.

Рабочие Telegram URL при этом не удаляются.

## Отключение только тестовой версии

```bash
sudo systemctl disable --now hi-jack-admin-helper-v2
sudo systemctl disable --now hi-jack-admin-helper-v2-backup.timer
sudo unlink /etc/nginx/sites-enabled/club-v2.hijackpoker.ru
sudo unlink /etc/nginx/sites-enabled/quiz-v2.hijackpoker.ru
sudo nginx -t
sudo systemctl reload nginx
curl --fail http://127.0.0.1:8090/health
```

Каталог и тестовая база после отключения намеренно сохраняются для диагностики.
