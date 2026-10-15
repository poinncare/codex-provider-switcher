# Codex Launcher

Небольшой macOS-launcher для ChatGPT/Codex, который открывается как обычный Codex и позволяет выбрать подписку Codex или подключённого внешнего провайдера из одного графического меню.

Проект не является продуктом OpenAI и не содержит авторизационных данных.

## Возможности

- обнаруживает аккаунты Codex через Orca и локальные данные Codex;
- переключает аккаунт без повторной авторизации;
- обнаруживает провайдеров OpenCode из opencode.json/opencode.jsonc;
- показывает OpenRouter и использует только модели с нулевой ценой при получении списка;
- не подключает Copilot как отдельного провайдера; модели Copilot остаются доступны, если их отдаёт другой провайдер;
- не требует выбора модели: провайдер подключается напрямую, а модель выбирается уже в интерфейсе Codex;
- перезаписывает свои секции `[model_providers.*]` с `wire_api = "responses"`, поэтому старые названия и устаревшие блоки не накапливаются;
- перезапускает только настольный ChatGPT/Codex-сервер, не трогая терминальные сессии;
- хранит ключ OpenRouter в macOS Keychain, а не в файле проекта.

## Требования

- macOS;
- установленный ChatGPT/Codex;
- Orca — для обнаружения уже зарегистрированных аккаунтов Codex;
- OpenCode с OpenAI-compatible gateway /v1/models — только для OpenCode-провайдеров;
- Python 3, AppleScript и стандартные macOS-команды (osacompile, security, ditto).

## Установка

~~~sh
git clone https://github.com/<owner>/codex-provider-switcher.git
cd codex-provider-switcher
./scripts/install.sh
~~~

После установки приложение появится в `~/Applications/Codex.app`. Именно его нужно закрепить в Dock и запускать вместо отдельного свитчера.

Официальное приложение ChatGPT/Codex не изменяется и остаётся доступным для прямого запуска из `/Applications/ChatGPT.app`.

Для OpenRouter ключ можно сохранить в Keychain:

~~~sh
./scripts/configure-openrouter.sh
~~~

Обычная работа выполняется через `Codex.app`: открыть Codex → выбрать Codex или найденного провайдера. При выборе Codex появится список обнаруженных email аккаунтов и пункт добавления нового аккаунта.

При добавлении нового аккаунта launcher открывает Terminal и запускает обычный `codex login` в отдельном временном `CODEX_HOME`. После входа через браузер авторизация копируется в отдельное хранилище аккаунта и добавляется в локальные данные Orca. Текущая активная сессия и её привязка не изменяются. Для этого сценария не используется `codex login --device-auth`, поэтому разрешение Device Code Authorization в ChatGPT не требуется. Хранилище авторизации для временного входа принудительно выбирается как локальный файл, чтобы аккаунт можно было безопасно зарегистрировать и потом переключать.

При выборе аккаунта свитчер меняет только активную OAuth-сессию в штатном `~/.codex/auth.json` и запускает ChatGPT без `CODEX_HOME` и Orca-окружения. Поэтому проекты, история и настройки остаются теми же, что при обычном запуске ChatGPT/Codex. Данные Orca и привязки его панелей при этом не изменяются, а Orca не запускается повторно.

## Как выполняется обнаружение

1. Аккаунты Codex берутся из команды Orca account list --json, затем из Orca orca-data.json. Если CLI Orca недоступен, используется локальная Codex-сессия как резервный источник.
2. OpenCode-конфигурация ищется в OPENCODE_CONFIG, OPENCODE_CONFIG_DIR, ~/.config/opencode и ~/Library/Application Support/opencode.
3. OpenCode-провайдеры и модели читаются из секции provider; URL берётся из options.baseURL. Провайдер, который определяется как Copilot, пропускается и не становится отдельным пунктом меню.
4. При переключении на OpenCode-провайдера launcher проверяет доступность gateway, а затем перезаписывает секции `[model_providers.openrouter]` и `[model_providers.opencode-*]` в `~/.codex/config.toml`, всегда проставляя `wire_api = "responses"`. Секции, которыми владеет launcher, предварительно удаляются, поэтому переименования и удалённые провайдеры не остаются в конфиге.
5. Модель здесь не выбирается: launcher лишь подставляет рабочую стартовую модель, а окончательный выбор делается в интерфейсе Codex.
6. OpenRouter всегда доступен в меню, а его ключ ищется в Keychain по сервису codex-provider-switcher.openrouter. Для совместимости также проверяется старое имя сервиса com.openai.codex.openrouter.

Пути строятся от домашней директории текущего пользователя. В проекте нет привязки к конкретному имени пользователя, email, account ID или абсолютной папке разработчика.

## Разработка

Проверить синтаксис:

~~~sh
python3 -m py_compile src/codex_provider_switcher.py
python3 src/codex_provider_switcher.py list-providers
python3 src/codex_provider_switcher.py prepare-accounts
~~~

Собрать приложение:

~~~sh
./scripts/build-app.sh
~~~

Готовое приложение будет в dist/Codex.app. dist/ не включается в Git, потому что AppleScript внутри .app содержит локальный путь к backend текущей установки; его генерирует сборочный скрипт.

## Безопасность

Не добавляйте в Git:

- auth.json;
- ключи OpenRouter;
- конфигурации с токенами;
- содержимое ~/Library/Application Support/orca;
- личные email и account ID.

Перед публикацией проверьте:

~~~sh
git grep -n -i -E 'sk-or-|access_token|refresh_token|auth\\.json|/Users/|@gmail\\.com'
~~~

## Лицензия

MIT. См. LICENSE.
