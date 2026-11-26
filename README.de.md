# Paymos Payments für Odoo

[English](README.md) | [简体中文](README.zh-CN.md) | [Русский](README.ru.md) | [Español](README.es.md) | **Deutsch** | [Türkçe](README.tr.md)

Akzeptieren Sie Kryptowährungszahlungen auf einer Odoo-20.0-Website über den gehosteten Paymos-Checkout. Das Add-on nutzt Odoo-Zahlungstransaktionen und die Buchhaltungskonfiguration Ihres Shops, um Bestellungen zu bestätigen und Rückerstattungen zu verbuchen.

Das Add-on ist kostenlos und unter LGPL-3.0-only lizenziert. Für die Nutzung des gehosteten Zahlungsdienstes sind ein Paymos-Konto und ein Projekt erforderlich. Siehe [LICENSE](LICENSE) und [NOTICE](NOTICE).

[![Video ansehen: 12 offizielle CMS-Krypto-Plugins — 1-Klick-Verbindung (Paymos)](https://i.ytimg.com/vi/VMLUGVumYkg/maxresdefault.jpg)](https://www.youtube.com/watch?v=VMLUGVumYkg)
> **Video-Rundgang (1:12, Untertitel in 6 Sprachen):** [Auf YouTube ansehen](https://www.youtube.com/watch?v=VMLUGVumYkg)

## Voraussetzungen

- Eine selbst gehostete Odoo-20.0-Installation mit `payment`, `account_payment` und `website_sale`.
- Die Python-Umgebung von Odoo mit `cryptography`. Die verifizierte Laufzeitumgebung ist Python 3.12. Installieren Sie für Python-Versionen unter 3.13 das gepinnte `typing-extensions`-Wheel mithilfe der `requirements.txt` neben `payment_paymos/`.
- Ein HTTPS-Shop und ein Paymos-Projekt mit Zugriff auf die Umgebung, die Sie verbinden.
- Ein Bankjournal im Unternehmen des Zahlungsanbieters mit zugehöriger eingehender Paymos-Zahlungsmethodenzeile.

Die folgenden Schritte gelten für eine selbst gehostete Odoo-20.0-Installation. Odoo.sh kann Python-Abhängigkeiten installieren, die in der `requirements.txt` im Stammverzeichnis des Branches aufgeführt sind; die Kompatibilität mit Odoo.sh steht noch unter Vorbehalt eines gehosteten Tests. Odoo Online schließt Python-Add-ons von Drittanbietern aus. [Odoo 20 On-Premises-Voraussetzungen](https://www.odoo.com/documentation/20.0/administration/on_premise/source.html), [Odoo 20 Odoo.sh-Abhängigkeitsleitfaden](https://www.odoo.com/documentation/20.0/administration/odoo_sh/create_module.html#use-an-external-python-library), [Odoo Apps FAQ](https://apps.odoo.com/apps/faq)

## Installieren und verbinden

1. Entpacken Sie das ZIP-Archiv in ein Verzeichnis innerhalb des Odoo-`addons_path`. Dieses Verzeichnis sollte `payment_paymos/__manifest__.py` und `requirements.txt` enthalten.
2. Installieren Sie die Abhängigkeiten in derselben Python-Umgebung, die Odoo verwendet. Für eine Offline-Installation legen Sie die Datei `typing_extensions-4.15.0-py3-none-any.whl` in Ihr Wheel-Verzeichnis und führen den folgenden Befehl aus. Der Datei-Hash wird durch pip geprüft. [Gepinntes Wheel](https://pypi.org/project/typing-extensions/4.15.0/#files)

   ```sh
   python -m pip install --no-index --find-links /path/to/wheels --require-hashes -r requirements.txt
   ```

3. Erzeugen Sie einen Fernet-Schlüssel mit `cryptography.fernet.Fernet.generate_key()` und setzen Sie ihn als `PAYMOS_ODOO_ENVELOPE_KEY` in der geschützten Serverkonfiguration von Odoo. Starten Sie den Odoo-Dienst mit dieser Einstellung neu. Bewahren Sie den Schlüssel zusammen mit Ihren geschützten Backups auf, da gespeicherte Verbindungsdaten von ihm abhängen.
4. Aktualisieren Sie als Odoo-Systemadministrator die App-Liste und installieren Sie **Paymos Payments**. Öffnen Sie **Accounting → Configuration → Payment Providers**, wählen Sie **Paymos** für das Unternehmen des Shops, klicken Sie auf **Connect Paymos** und bestätigen Sie die Anfrage in Paymos.
5. Wählen Sie das Bankjournal Ihres Unternehmens aus und prüfen Sie die zugehörige eingehende Paymos-Zahlungsmethodenzeile. Aktivieren und veröffentlichen Sie den Anbieter in Sandbox, um den Checkout zu testen.
6. Sobald Ihr Shop und Ihre Buchhaltung bereit sind, wählen Sie **Select Live**. Ein Umgebungswechsel deaktiviert den Anbieter und hebt die Veröffentlichung auf. Prüfen Sie die Live-Verbindung und aktivieren und veröffentlichen Sie den Anbieter anschließend erneut.

## Checkout und Daten

Die Paymos-Freigabeseite erhält die kanonische HTTPS-URL des Shops und einen einmaligen Freigabecode, wenn ein Administrator sie über **Connect Paymos** öffnet.

Zum Erstellen einer Rechnung sendet Odoo die Paymos-Projekt-ID, den Bestellbetrag, die Währung und eine opake Bestellreferenz. Bei Bestellungen mit 1 bis 50 gültigen Produktzeilen in `sale.order.order_line` (ausgenommen `display_type`, `is_delivery` und `is_downpayment`) übermittelt Odoo zusätzlich ein optionales `products[]`-Array (`title`, positive Ganzzahl `quantity`, exakter Dezimalwert `unit_amount` sowie optionaler Klartext `description`). Enthält eine Bestellung 0 oder mehr als 50 Produktzeilen, eine gebrochene Menge oder eine ungültige Zeile, fällt Odoo nach dem Alles-oder-nichts-Prinzip auf eine reine Betragsrechnung mit dem nativen Gesamtbetrag zurück. Der normalisierte `products_json`-Snapshot wird vor der ersten Anfrage in `paymos.attempt` gespeichert. Wiederholungen senden die ursprünglichen Produktwerte erneut und lassen bei älteren Snapshots lediglich den ausgemusterten Schlüssel `image_url` weg, ohne gespeicherte Daten umzuschreiben. Vor der ersten Anfrage machen alle sonstigen Produktänderungen den vorbereiteten Versuch ungültig. Odoo übermittelt in dieser Anfrage keine Identitätsdaten des Käufers. Der Käufer gibt Zahlungsdetails auf der gehosteten Paymos-Seite ein. Odoo prüft signierte Benachrichtigungen und verifiziert die Rechnung bei Paymos, bevor der zugehörige Auftrag bestätigt wird. Ein geplanter Abgleichsjob prüft auf verpasste Benachrichtigungen.

[Paymos-Dokumentation](https://paymos.io/de/docs), [Paymos-Datenschutzerklärung](https://paymos.io/de/privacy)

## Rückerstattungen

Verwenden Sie ein Odoo-Systemadministratorkonto mit Zugriff auf das Unternehmen der Zahlung.

- Für eine vollständige Rückerstattung öffnen Sie die ursprüngliche gebuchte Kundenzahlung unter **Accounting** und wählen **Refund** über den vollen ursprünglichen Betrag in der Bestellwährung. Paymos berechnet die Token-Auszahlung für die verknüpfte Rechnung und sendet sie an den ursprünglichen Zahler zurück. Nach der Prüfung verbucht Odoo die Rückerstattung gegen die ursprüngliche Zahlung. [Odoo 20 Rückerstattungs-Workflow](https://www.odoo.com/documentation/20.0/applications/finance/payment_providers.html#refunds)
- Um eine Überzahlung zurückzugeben, aktivieren Sie den Entwicklermodus und öffnen die ursprüngliche Transaktion unter **Accounting → Configuration → Payment Transactions**. Wählen Sie **Refund Paymos overpayment**. Nachdem Paymos die Rückzahlung bestätigt hat, fügt Odoo dem Auftrag eine Notiz hinzu und behält den beglichenen Hauptbetrag bei.

Prüfen Sie fehlgeschlagene Rückerstattungen oder Vorgänge, die ihr Prüfungslimit erreicht haben, zunächst in Paymos, bevor Sie weitere Schritte unternehmen.

## Aus dem Quellcode bauen

Bündeln Sie jedes markierte stabile Paymos-Python-SDK-Release `1.x` (`>= 1.6.0`) aus einem lokalen SDK-Quellbaum (`--sdk-source`, Standard-Release-Ablauf) oder aus einem Offline-Release-Wheel (`--sdk-wheel`). Das Add-on sendet optionale `products[]`-Payloads über den Transport `client.request("POST", "/v1/invoices", payload)` des gebündelten SDK, ohne den SDK-Quellcode zu verändern. Der Builder prüft SDK-Version, erforderliche Laufzeitdateien, Lizenz und SHA-256-Prüfsummen ausschließlich anhand lokaler Eingaben. Das optionale Mirror-Verzeichnis erhält exakt die Dateien aus dem ZIP. [SDK-Quellcode](https://github.com/Paymos-labs/python-sdk), [SDK-Releases auf PyPI](https://pypi.org/project/paymos-sdk/)

```sh
python scripts/build-odoo-plugin.py build --sdk-source /path/to/python-sdk --output /path/to/paymos-odoo-v1.1.0.zip --mirror-dir /path/to/empty-mirror
python scripts/build-odoo-plugin.py verify --archive /path/to/paymos-odoo-v1.1.0.zip --sdk-source /path/to/python-sdk
```

Um statt aus einem Quellbaum aus einem Offline-Release-Wheel zu bauen:

```sh
python scripts/build-odoo-plugin.py build --sdk-wheel /path/to/paymos_sdk-1.7.1-py3-none-any.whl --output /path/to/paymos-odoo-v1.1.0.zip --mirror-dir /path/to/empty-mirror
python scripts/build-odoo-plugin.py verify --archive /path/to/paymos-odoo-v1.1.0.zip
```

Übergeben Sie beim Bauen aus einem Release-Commit denselben vollständigen `--source-commit` (sowie optional `--sdk-version` / `--sdk-source-commit`) an `build` und `verify`. Die Datei `bundle-manifest.json` hält Odoo-Version, Tag, Quell- und Datei-Hashes sowie Metadaten des gebündelten SDK fest. Der Befehl gibt die SHA-256-Prüfsumme des ZIP-Archivs aus.
