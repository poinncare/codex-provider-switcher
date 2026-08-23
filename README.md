# Codex Provider Switcher

Небольшой macOS-свитчер для ChatGPT/Codex, который позволяет выбрать подписку Codex или подключённого внешнего провайдера из одного графического меню.

Проект не является продуктом OpenAI и не содержит авторизационных данных.

## Возможности

- обнаруживает аккаунты Codex через Orca и локальные данные Codex;
- переключает аккаунт без повторной авторизации;
- обнаруживает провайдеров OpenCode из opencode.json/opencode.jsonc;
- показывает OpenRouter и использует только модели с нулевой ценой при получении списка;
- не требует выбора модели до запуска — используется последняя выбранная модель;
- перезапускает только настольный ChatGPT/Codex-сервер, не трогая терминальные сессии;
- хранит ключ OpenRouter в macOS Keychain, а не в файле проекта.

## Требования

- macOS;
- установленный ChatGPT/Codex;
- Orca — для обнаружения и переключения нескольких аккаунтов Codex;
- OpenCode с OpenAI-compatible gateway /v1/models — только для OpenCode-провайдеров;
- Python 3, AppleScript и стандартные macOS-команды (osacompile, security, ditto).

## Установка

~~~sh
git clone https://github.com/<owner>/codex-provider-switcher.git
cd codex-provider-switcher
./scripts/install.sh
~~~

После установки приложение появится в ~/Applications/Codex Provider Switcher.app.

Для OpenRouter ключ можно сохранить в Keychain:

~~~sh
./scripts/configure-openrouter.sh
~~~

Обычная работа выполняется через приложение: открыть свитчер → выбрать Codex или найденного провайдера. При выборе Codex появится список обнаруженных email аккаунтов.

При выборе аккаунта свитчер меняет только активную OAuth-сессию в штатном `~/.codex/auth.json` и запускает ChatGPT без `CODEX_HOME` и Orca-окружения. Поэтому проекты, история и настройки остаются теми же, что при обычном запуске ChatGPT/Codex. Данные Orca и привязки его панелей при этом не изменяются, а Orca не запускается повторно.

## Как выполняется обнаружение

1. Аккаунты Codex берутся из команды Orca account list --json, затем из Orca orca-data.json. Если CLI Orca недоступен, используется локальная Codex-сессия как резервный источник.
2. OpenCode-конфигурация ищется в OPENCODE_CONFIG, OPENCODE_CONFIG_DIR, ~/.config/opencode и ~/Library/Application Support/opencode.
3. OpenCode-провайдеры и модели читаются из секции provider; URL берётся из options.baseURL.
4. OpenRouter всегда доступен в меню, а его ключ ищется в Keychain по сервису codex-provider-switcher.openrouter. Для совместимости также проверяется старое имя сервиса com.openai.codex.openrouter.

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

Готовое приложение будет в dist/. dist/ не включается в Git, потому что AppleScript внутри .app содержит локальный путь к backend текущей установки; его генерирует сборочный скрипт.

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
