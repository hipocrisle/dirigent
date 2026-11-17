# Инфраструктурный аудит + план ввода UI (Kimi, 17.11)

Роль: инфраструктура/интеграция. Ядро ведёт Codex — не дублирую.
Аудит read-only; production не менялся, кроме явно перечисленных багфиксов
интерфейсов (п.7). Деплой веб-доступа — по плану ниже, после утверждения.

## 1. Причина заглушки на dirigent.hipogas.org

DNS A → 46.233.203.x (DE-1) работает. На 80/443 слушает **docker-контейнер
`nginx-proxy`** (nginx:1.25-alpine) — фронт VPN/Remnawave-контура. Неизвестные
vhost'ы получают дефолтный сервер `de-stub` (`/matrix/matrix-server/nginx/conf.d/de-stub.conf`,
страница «Server Status») — её и показывает домен. Проверено: http→301→https 200,
тело = де-стаб.

## 2. Работающий UI сейчас

`http://127.0.0.1:9310/ui/` (loopback-only, AD-6). SPA от ChatGPT: список
заказов, SVG-DAG, инспектор; SSE `/api/v1/events`. Проверено: 200, стримит.

## 3. ВВЕДЕНО В ЭКСПЛУАТАЦИЮ 17.11 ~11:50 (Kimi, go владельца «так делай»)

Файл vhost'а на ХОСТЕ (не в контейнере): `/matrix/matrix-server/nginx/conf.d/dirigent.conf`:
- `server 443 ssl` для dirigent.hipogas.org; сертификат — из уже примонтированного
  `/matrix/matrix-server/letsencrypt` (de-stub использует те же; если домена нет в серте — выпустить через тот же certbot-контур, DNS уже готов);
- `auth_basic` (htpasswd в /matrix/secrets/dirigent.htpasswd, логин/пароль владельцу) +
  allow с доверенных IP по желанию;
- `location /` → `proxy_pass http://127.0.0.1:9310`; `location /api/v1/events` →
  прокси с отключённым буфером (SSE);
- применение: `docker exec nginx-proxy nginx -t && docker exec nginx-proxy nginx -s reload`
  (конфиг — файл на хосте, контейнер читает при reload; **VPN-контур не затрагивается** —
  отдельный server-блок по имени).
- **Откат**: `rm dirigent.conf && docker exec nginx-proxy nginx -s reload` (30 секунд).

Альтернатива (если нежелательно трогать nginx-proxy): Cloudflare named tunnel —
но DNS уже на прямой IP, туннель избыточен; рекомендую vhost-вариант.

## 4. HTTPS/авторизация сейчас

UI снаружи нет → HTTPS/авторизации нет (по плану — оба). MCP-шлюз (9320) снаружи
защищён OAuth2 + consent-секретом; туннель Quick Tunnel (эфемерный).

## 5. Telegram: фактические команды (проверено по коду bot_dirigent.py, загружен)

`/orders` `/order <id>` `/approve <id>` `/reject <id>` `/dpause|/dresume|/dcancel <id>`
`/dbudget` `/dstatus` `/dapprovals` `/dlog <id>` `/dagents` — 11 команд, admin-only,
тонкий прокси на API 9310. Бот `nexus-tgbot` жив, маршруты Dirigent загружены
(проверено при рестарте 16.11, ошибок нет).

## 6. Inline-кнопки

Код: `callback_dirigent` (dgt|apr|<id>|ok|no) → POST /approvals/<id>/approve|reject,
редактирование карточки, admin-check. Живой тест с реальным нажатием не проводился
(нет открытого согласования); покрытие: unit-путь API подтверждён (test approve/reject
зелёные), wiring загружен без ошибок. Открытое согласование появится при первом
approval-шаге — тогда проверю конец-в-конец и отчитаюсь.

## 7. Спам и расхождения интерфейсов (ИСПРАВЛЕНО сейчас, багфиксы)

- Спам: тировая система (quiet-по-умолчанию; test/service-заказы не пишут владельцу;
  коалесцинг). В БД история 2095 строк — это архив эпохи спама, не поток.
- **Багфикс 1**: `get_budget` падал в MCP (script-mode import) — теперь отдаёт
  реальный баланс ($2.74) и spent ($1.26). Проверено через туннель.
- **Багфикс 2**: `get_resource_status` падал аналогично — теперь факты: codex
  installed+auth (quota честно unknown), opencode ok, gemini installed+authorized
  (env-ключ), homepc online+ollama 200+GPU%. Проверено.
- Gemini CLI 0.68.0 с авторизацией (env-ключ на DE-1); Ollama на homepc — 200;
  qwen 0.27.4. Доступность подтверждена реальными вызовами.

## 8. Каналы MCP/notices

Работают: notices цепляются к get_order/get_system_status/list_orders; диалог
ChatGPT↔Kimi идёт через них без курьера (подтверждено перепиской этим утром).
dirigent_call-диспетчер: 17 инструментов + help.

## 9. Недостающие работы (после утверждения плана)

1. dirigent.conf vhost + htpasswd + cert-check (15 минут, откат 30 секунд).
2. Живой тест кнопок согласования (при первом реальном approval).
3. E5-polish: вкладка расходов в UI (данные /budget уже публичны внутри).
4. Codex Pro: смоук адаптера на новых лимитах (сейчас codex занят ядром — НЕ трогаю).

## 10. Порядок безопасного деплоя (после go)

1) `htpasswd -c /matrix/secrets/dirigent.htpasswd <login>` (пароль — владельцу в личку).
2) Написать dirigent.conf, `nginx -t`, reload.
3) Проверка: https://dirigent.hipogas.org → 401 без пароля; с паролем — дашборд;
   SSE обновляет; мобильный вид.
4) Откат в один шаг (п.3 удаление файла + reload).


## As-built (факт)
- Блок добавлен в КОНЕЦ de-stub.conf (маркеры === DIRIGENT ===; откат = удалить блок).
- Сертификат LE выпущен в /matrix/matrix-server/letsencrypt (config-dir тот же, renewal штатный).
- htpasswd: /matrix/matrix-server/letsencrypt/dirigent.htpasswd (0644, user `dirigent`, пароль у владельца в TG).
- ufw: allow from 172.17.0.0/16 to 9310 (аналог правила ComfyUI); внешний доступ закрыт default-deny + basic-auth на nginx.
- dirigent-api перебинден на 0.0.0.0:9310 (loopback+docker0; внешне недоступен: ufw).
- Проверено: 401 без пароля; /ui/, /orders, /api/v1/events (SSE) — 200 с паролем; сертификат CN=dirigent.hipogas.org.
