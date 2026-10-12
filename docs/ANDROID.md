# Android-приложение p2p-inventory

Своя сборка официального приложения InvenTree (исходники: https://github.com/inventree/inventree-app).
Отличия от оригинала:

* название **p2p-inventory**, иконка прежняя;
* свой идентификатор `online.p2ptd_inventory.app`, поэтому приложение ставится рядом с официальным InvenTree и не конфликтует с ним;
* отключена отправка отчётов об ошибках в Sentry разработчиков InvenTree.

Сборка идёт в GitHub Actions (`.github/workflows/android-app.yml`), ставить Android SDK не нужно.

## Один раз: ключ подписи

Все версии APK должны подписываться одним и тем же ключом, иначе телефон не даст обновить приложение поверх старого. Ключ создаётся один раз.

1. Создайте ключ (нужна Java; если её нет, можно через Docker, см. ниже):
   ```
   keytool -genkeypair -v -keystore p2ptd-release.jks -alias p2ptd -keyalg RSA -keysize 2048 -validity 10000
   ```
   Без Java:
   ```
   docker run --rm -it -v "$PWD":/w -w /w eclipse-temurin:17 keytool -genkeypair -v -keystore p2ptd-release.jks -alias p2ptd -keyalg RSA -keysize 2048 -validity 10000
   ```
2. Переведите ключ в base64:
   * macOS: `base64 -i p2ptd-release.jks | pbcopy` (результат сразу в буфере обмена)
   * Linux: `base64 -w0 p2ptd-release.jks`
   * Windows (PowerShell): `[Convert]::ToBase64String([IO.File]::ReadAllBytes("p2ptd-release.jks")) | Set-Clipboard`
3. В репозитории на GitHub: Settings → Secrets and variables → Actions → New repository secret. Добавьте четыре секрета:

   | Секрет | Значение |
   |---|---|
   | `ANDROID_KEYSTORE_BASE64` | base64 из шага 2 |
   | `ANDROID_KEYSTORE_PASSWORD` | пароль keystore |
   | `ANDROID_KEY_ALIAS` | `p2ptd` |
   | `ANDROID_KEY_PASSWORD` | пароль ключа (keytool мог предложить тот же, что у keystore) |

4. Файл `p2ptd-release.jks` и пароли сохраните в надёжном месте (менеджер паролей). В репозиторий файл не кладите. Если ключ потеряется, новую сборку нельзя будет поставить поверх старой: на всех телефонах придётся удалить приложение и поставить заново.

## Сборка новой версии

1. Actions → **Android app** → Run workflow.
2. Укажите версию InvenTree app (тег из https://github.com/inventree/inventree-app/releases), например `0.25.2`.
3. Через 10–15 минут APK появится в Releases этого репозитория: `p2p-inventory-<версия>.apk`.

Если сборка упала на шаге «Apply p2p-inventory branding», значит в новой версии InvenTree app поменялись файлы, которые мы правим. Тогда нужно обновить замены в workflow.

## Установка на телефон

1. Скачайте APK из Releases на телефон (нужен вход в GitHub с доступом к репозиторию).
2. Откройте файл и разрешите установку из неизвестных источников для браузера или файлового менеджера.
3. В приложении добавьте сервер `https://p2ptd-inventory.online` и войдите своим логином.
