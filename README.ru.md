# Paymos Payments для Odoo

[English](README.md) | [简体中文](README.zh-CN.md) | **Русский** | [Español](README.es.md) | [Deutsch](README.de.md) | [Türkçe](README.tr.md)

Принимайте криптовалютные платежи на сайте Odoo 20.0 через страницу оплаты Paymos. Модуль работает с платёжными транзакциями Odoo и бухгалтерскими настройками магазина, чтобы подтверждать заказы и проводить возвраты.

Модуль бесплатный и распространяется под лицензией LGPL-3.0-only. Для работы с платёжным сервисом нужны аккаунт и проект Paymos. См. [LICENSE](LICENSE) и [NOTICE](NOTICE).

[![Смотреть: 12 официальных криптоплагинов для CMS — подключение в один клик (Paymos)](https://i.ytimg.com/vi/VMLUGVumYkg/maxresdefault.jpg)](https://www.youtube.com/watch?v=VMLUGVumYkg)
> **Видеообзор (1:12, субтитры на 6 языках):** [Смотреть на YouTube](https://www.youtube.com/watch?v=VMLUGVumYkg)

## Требования

- Самостоятельно развёрнутый Odoo 20.0 с модулями `payment`, `account_payment` и `website_sale`.
- Окружение Python для Odoo с библиотекой `cryptography`. Проверенная среда — Python 3.12. Для версий Python ниже 3.13 установите зафиксированный wheel-пакет `typing-extensions` по файлу `requirements.txt` рядом с каталогом `payment_paymos/`.
- Витрина по HTTPS и проект Paymos с доступом к подключаемой среде.
- Банковский журнал в компании провайдера с настроенной строкой входящего способа оплаты Paymos.

Инструкция ниже относится к самостоятельно размещённому Odoo 20.0. Odoo.sh умеет ставить Python-зависимости из корневого `requirements.txt` ветки, но совместимость с Odoo.sh ещё ждёт отдельной проверки. Odoo Online не допускает сторонние Python-модули. [Требования Odoo 20 для on-premises](https://www.odoo.com/documentation/20.0/administration/on_premise/source.html), [Руководство по зависимостям Odoo 20 в Odoo.sh](https://www.odoo.com/documentation/20.0/administration/odoo_sh/create_module.html#use-an-external-python-library), [FAQ по Odoo Apps](https://apps.odoo.com/apps/faq)

## Установка и подключение

1. Распакуйте ZIP-архив в каталог из `addons_path` вашего Odoo. Внутри должны лежать `payment_paymos/__manifest__.py` и `requirements.txt`.
2. Установите зависимости в то же Python-окружение, под которым работает Odoo. Для офлайн-установки положите файл `typing_extensions-4.15.0-py3-none-any.whl` в каталог с wheel-пакетами и выполните команду ниже — pip проверит контрольную сумму файла. [Зафиксированный wheel](https://pypi.org/project/typing-extensions/4.15.0/#files)

   ```sh
   python -m pip install --no-index --find-links /path/to/wheels --require-hashes -r requirements.txt
   ```

3. Сгенерируйте ключ Fernet через `cryptography.fernet.Fernet.generate_key()` и задайте его в переменной `PAYMOS_ODOO_ENVELOPE_KEY` в защищённой конфигурации сервера Odoo. Перезапустите службу Odoo с этой настройкой. Храните ключ в защищённой резервной копии: без него сохранённые учётные данные не расшифруются.
4. Под системным администратором Odoo обновите список приложений и установите **Paymos Payments**. Откройте **Accounting → Configuration → Payment Providers**, выберите **Paymos** для компании магазина, нажмите **Connect Paymos** и подтвердите запрос в Paymos.
5. Укажите банковский журнал вашей компании и проверьте в нём строку входящего способа оплаты Paymos. Активируйте и опубликуйте провайдер в режиме Sandbox, чтобы проверить оформление заказа.
6. Когда витрина и бухгалтерский учёт готовы, нажмите **Select Live**. Смена среды автоматически деактивирует провайдер и снимает его с публикации. Проверьте подключение в Live, а затем снова активируйте и опубликуйте провайдер.

## Оформление заказа и передаваемые данные

Когда администратор нажимает **Connect Paymos**, на страницу подтверждения Paymos уходят только канонический HTTPS-адрес магазина и одноразовый код подтверждения.

При создании счёта Odoo отправляет ID проекта Paymos, сумму заказа, валюту и внутреннюю ссылку на заказ. Если в `sale.order.order_line` от 1 до 50 корректных товарных строк (за вычетом `display_type`, `is_delivery` и `is_downpayment`), Odoo дополнительно передаёт опциональный массив `products[]` (`title`, целое положительное `quantity`, точное десятичное `unit_amount` и опциональное текстовое `description`). Если в заказе 0 или больше 50 товарных строк, дробное количество либо любая некорректная строка, Odoo по правилу «всё или ничего» переключается на счёт только с итоговой суммой заказа. Нормализованный снимок `products_json` записывается в `paymos.attempt` до первого запроса. Повторные попытки отправляют исходные значения товаров, убирая из старых снимков лишь устаревший ключ `image_url` без перезаписи базы. До первого запроса любые другие правки товаров аннулируют подготовленную попытку. Персональные данные покупателя в запрос не попадают — реквизиты оплаты покупатель вводит на странице Paymos. Перед подтверждением связанного заказа Odoo проверяет подписанные уведомления и сверяет счёт в API Paymos, а плановая задача сверки подхватывает пропущенные уведомления.

[Документация Paymos](https://paymos.io/ru/docs), [Политика конфиденциальности Paymos](https://paymos.io/ru/privacy)

## Возвраты

Для работы с возвратами нужен системный администратор Odoo с доступом к компании платежа.

- Полный возврат: откройте исходный проведённый платёж клиента в разделе **Accounting** и выберите **Refund** на полную исходную сумму в валюте заказа. Paymos рассчитает выплату в токенах по связанному счёту и отправит её исходному плательщику. После проверки Odoo проведёт возврат по исходному платежу. [Порядок возвратов в Odoo 20](https://www.odoo.com/documentation/20.0/applications/finance/payment_providers.html#refunds)
- Возврат переплаты: включите режим разработчика и откройте исходную транзакцию в **Accounting → Configuration → Payment Transactions**, затем нажмите **Refund Paymos overpayment**. После подтверждения от Paymos Odoo добавит заметку в заказ, не меняя зачтённую основную сумму.

Если возврат завершился ошибкой или упёрся в лимит проверок, разберите операцию в кабинете Paymos перед следующими шагами.

## Сборка из исходников

В пакет можно встроить любой стабильный релиз Paymos Python SDK `1.x` (`>= 1.6.0`) из локального дерева исходников (`--sdk-source`, стандартный сценарий релиза) или из офлайн-wheel (`--sdk-wheel`). Модуль передаёт опциональный массив `products[]` через встроенный транспорт `client.request("POST", "/v1/invoices", payload)` без правки исходников SDK. Сборщик проверяет версию SDK, обязательные файлы среды выполнения, лицензию и контрольные суммы SHA-256 только по локальным файлам. В опциональный каталог зеркала (`--mirror-dir`) попадает точная копия содержимого ZIP-архива. [Исходники SDK](https://github.com/Paymos-labs/python-sdk), [Релизы SDK на PyPI](https://pypi.org/project/paymos-sdk/)

```sh
python scripts/build-odoo-plugin.py build --sdk-source /path/to/python-sdk --output /path/to/paymos-odoo-v1.1.0.zip --mirror-dir /path/to/empty-mirror
python scripts/build-odoo-plugin.py verify --archive /path/to/paymos-odoo-v1.1.0.zip --sdk-source /path/to/python-sdk
```

Чтобы собрать архив из офлайн-wheel вместо дерева исходников:

```sh
python scripts/build-odoo-plugin.py build --sdk-wheel /path/to/paymos_sdk-1.7.1-py3-none-any.whl --output /path/to/paymos-odoo-v1.1.0.zip --mirror-dir /path/to/empty-mirror
python scripts/build-odoo-plugin.py verify --archive /path/to/paymos-odoo-v1.1.0.zip
```

При сборке из релизного коммита передавайте в `build` и `verify` одинаковый полный `--source-commit` (и при необходимости `--sdk-version` / `--sdk-source-commit`). В `bundle-manifest.json` записываются версия Odoo, тег, хеши исходников и файлов, а также метаданные встроенного SDK. Команда выводит SHA-256 готового ZIP-архива.
