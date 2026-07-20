# Развёртывание на VPS

Приложение независимо от `hi-jack-timer`: отдельная папка, процесс, порт, systemd unit и поддомен.

## 1. DNS и каталог

Создайте A-записи `club.hijackpoker.ru` и `quiz.hijackpoker.ru` на IP VPS. Затем:

```bash
sudo mkdir -p /opt/hi-jack-admin-helper
sudo chown "$USER":www-data /opt/hi-jack-admin-helper
git clone <URL_НОВОГО_РЕПОЗИТОРИЯ> /opt/hi-jack-admin-helper
cd /opt/hi-jack-admin-helper
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Сгенерируйте секрет и отредактируйте `.env`:

```bash
openssl rand -hex 32
nano .env
```

Обязательно задайте `HJC_MASTER_LOGIN`, `HJC_ADMIN_PIN`, `HJC_SECRET_KEY`, `HJC_ADMIN_NAME`, `HJC_PUBLIC_BASE_URL` и `HJC_QUIZ_PUBLIC_BASE_URL`. `HJC_ADMIN_PIN` используется только для создания первого мастер-аккаунта. Затем:

```bash
sudo chown -R www-data:www-data /opt/hi-jack-admin-helper/data
sudo chmod 600 /opt/hi-jack-admin-helper/.env
```

## 2. Первый локальный запуск

```bash
set -a
source .env
set +a
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8090
```

В другом окне проверьте `curl http://127.0.0.1:8090/health`, затем остановите процесс.

## 3. systemd

```bash
sudo cp deploy/hi-jack-admin-helper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hi-jack-admin-helper
sudo systemctl status hi-jack-admin-helper
```

## 4. Nginx и HTTPS

Сначала включите временный HTTP-конфиг и получите два сертификата через webroot. Nginx и таймер останавливать не нужно:

```bash
sudo mkdir -p /var/www/certbot/.well-known/acme-challenge
sudo cp deploy/hijack-tools-bootstrap.nginx /etc/nginx/sites-available/hijack-tools-bootstrap
sudo ln -s /etc/nginx/sites-available/hijack-tools-bootstrap /etc/nginx/sites-enabled/hijack-tools-bootstrap
sudo nginx -t && sudo systemctl reload nginx
sudo certbot certonly --webroot -w /var/www/certbot -d club.hijackpoker.ru
sudo certbot certonly --webroot -w /var/www/certbot -d quiz.hijackpoker.ru
sudo cp deploy/club.hijackpoker.ru.nginx /etc/nginx/sites-available/club.hijackpoker.ru
sudo cp deploy/quiz.hijackpoker.ru.nginx /etc/nginx/sites-available/quiz.hijackpoker.ru
sudo ln -s /etc/nginx/sites-available/club.hijackpoker.ru /etc/nginx/sites-enabled/club.hijackpoker.ru
sudo ln -s /etc/nginx/sites-available/quiz.hijackpoker.ru /etc/nginx/sites-enabled/quiz.hijackpoker.ru
sudo rm /etc/nginx/sites-enabled/hijack-tools-bootstrap
sudo nginx -t
sudo systemctl reload nginx
```

Не меняйте конфиг и unit турнирного таймера. Admin Helper и Quiz используют общий upstream `127.0.0.1:8090`, поскольку работают с одной базой клиентов. На публичном поддомене Nginx разрешает только квиз, его API старта/ответов/завершения/отправки результата и статические файлы; админские маршруты там закрыты.

Проверка после запуска:

```bash
curl -I https://club.hijackpoker.ru/login
curl -I https://quiz.hijackpoker.ru/
curl https://quiz.hijackpoker.ru/api/quiz/questions?campaign=default
```

## 5. Ежедневный бэкап

```bash
sudo mkdir -p /opt/hi-jack-club-tools-backups
sudo chmod 700 /opt/hi-jack-club-tools-backups
sudo cp deploy/hi-jack-admin-helper-backup.service deploy/hi-jack-admin-helper-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hi-jack-admin-helper-backup.timer
sudo systemctl start hi-jack-admin-helper-backup.service
sudo systemctl list-timers hi-jack-admin-helper-backup.timer
```

## Обновление

```bash
cd /opt/hi-jack-admin-helper
sudo -u www-data git pull --ff-only
.venv/bin/pip install -r requirements.txt
sudo systemctl restart hi-jack-admin-helper
curl http://127.0.0.1:8090/health
```

После обновления существующего MVP входите с логином из `HJC_MASTER_LOGIN` (по умолчанию `master`) и прежним `HJC_ADMIN_PIN`. Существующая база клиентов и история операций мигрируются автоматически.

Если PIN мастер-администратора утерян, остановите сервис и сбросьте его локально:

```bash
sudo systemctl stop hi-jack-admin-helper
sudo -u www-data .venv/bin/python scripts/reset_admin_pin.py --db data/club_tools.sqlite3 --username master
sudo systemctl start hi-jack-admin-helper
```

## Диагностика и откат данных

```bash
journalctl -u hi-jack-admin-helper -n 100 --no-pager
sudo systemctl stop hi-jack-admin-helper
sudo cp /opt/hi-jack-club-tools-backups/<ДАТА>/club_tools.sqlite3 /opt/hi-jack-admin-helper/data/club_tools.sqlite3
sudo chown www-data:www-data /opt/hi-jack-admin-helper/data/club_tools.sqlite3
sudo systemctl start hi-jack-admin-helper
```
