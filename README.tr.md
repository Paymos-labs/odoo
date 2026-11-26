# Odoo için Paymos Payments

[English](README.md) | [简体中文](README.zh-CN.md) | [Русский](README.ru.md) | [Español](README.es.md) | [Deutsch](README.de.md) | **Türkçe**

Paymos barındırılan ödeme sayfasını kullanarak Odoo 20.0 web sitesinde kripto para ödemeleri kabul edin. Eklenti, siparişleri onaylamak ve iadeleri kaydetmek için Odoo ödeme işlemlerini ve mağazanın muhasebe kurulumunu kullanır.

Eklenti ücretsizdir ve LGPL-3.0-only lisansı altındadır. Barındırılan ödeme hizmetini kullanmak için bir Paymos hesabı ve projesi gerekir. Bkz. [LICENSE](LICENSE) ve [NOTICE](NOTICE).

[![İzleyin: 12 Resmi CMS Kripto Eklentisi — Tek Tıkla Bağlantı (Paymos)](https://i.ytimg.com/vi/VMLUGVumYkg/maxresdefault.jpg)](https://www.youtube.com/watch?v=VMLUGVumYkg)
> **Video anlatım (1:12, 6 dilde altyazı):** [YouTube'da izleyin](https://www.youtube.com/watch?v=VMLUGVumYkg)

## Gereksinimler

- `payment`, `account_payment` ve `website_sale` modüllerine sahip, kendi sunucunuzda barındırılan bir Odoo 20.0 kurulumu.
- `cryptography` paketi kurulu Odoo Python ortamı. Doğrulanan çalışma zamanı Python 3.12'dir. Python 3.13 altındaki sürümlerde, `payment_paymos/` yanındaki `requirements.txt` dosyasını kullanarak sabitlenmiş `typing-extensions` wheel paketini kurun.
- Bir HTTPS mağaza vitrini ve bağlanacağınız ortama erişimi olan bir Paymos projesi.
- Sağlayıcının şirketinde Paymos gelen ödeme yöntemi satırına sahip bir banka yevmiyesi (bank journal).

Aşağıdaki adımlar kendi sunucunuzda barındırılan bir Odoo 20.0 kurulumunu kapsar. Odoo.sh, dalın kök dizinindeki `requirements.txt` içinde listelenen Python bağımlılıklarını kurabilir; Odoo.sh uyumluluğu barındırılan ortam doğrulamasını beklemektedir. Odoo Online üçüncü taraf Python eklentilerini desteklemez. [Odoo 20 şirket içi gereksinimleri](https://www.odoo.com/documentation/20.0/administration/on_premise/source.html), [Odoo 20 Odoo.sh bağımlılık kılavuzu](https://www.odoo.com/documentation/20.0/administration/odoo_sh/create_module.html#use-an-external-python-library), [Odoo Apps SSS](https://apps.odoo.com/apps/faq)

## Kurulum ve bağlantı

1. ZIP paketini Odoo `addons_path` yolundaki bir dizine çıkarın. Bu dizin `payment_paymos/__manifest__.py` ve `requirements.txt` dosyalarını içermelidir.
2. Bağımlılıkları Odoo'nun kullandığı Python ortamına kurun. Çevrimdışı kurulum için `typing_extensions-4.15.0-py3-none-any.whl` dosyasını wheel dizininize koyun ve aşağıdaki komutu çalıştırın. Dosya karması pip tarafından doğrulanır. [Sabitlenmiş wheel](https://pypi.org/project/typing-extensions/4.15.0/#files)

   ```sh
   python -m pip install --no-index --find-links /path/to/wheels --require-hashes -r requirements.txt
   ```

3. `cryptography.fernet.Fernet.generate_key()` ile bir Fernet anahtarı oluşturun ve Odoo sunucusunun gizli yapılandırmasında `PAYMOS_ODOO_ENVELOPE_KEY` olarak tanımlayın. Odoo hizmetini bu ayarla yeniden başlatın. Kayıtlı bağlantı kimlik bilgileri bu anahtara bağlı olduğu için anahtarı korumalı yedeklerinizle birlikte saklayın.
4. Odoo sistem yöneticisi olarak Uygulamalar listesini güncelleyin ve **Paymos Payments** modülünü kurun. **Accounting → Configuration → Payment Providers** ekranını açın, mağazanın şirketi için **Paymos** sağlayıcısını seçin, **Connect Paymos** düğmesine tıklayın ve isteği Paymos üzerinde onaylayın.
5. Şirketinizin banka yevmiyesini seçin ve Paymos gelen ödeme yöntemi satırını doğrulayın. Ödeme akışını denemek için sağlayıcıyı Sandbox ortamında etkinleştirip yayımlayın.
6. Mağaza vitrininiz ve muhasebe kurulumunuz hazır olduğunda **Select Live** seçeneğini belirleyin. Ortam değişikliği sağlayıcıyı devre dışı bırakır ve yayından kaldırır. Live bağlantısını doğruladıktan sonra sağlayıcıyı yeniden etkinleştirip yayımlayın.

## Ödeme ve veriler

Bir yönetici **Connect Paymos** üzerinden onay sayfasını açtığında Paymos onay sayfası mağazanın kanonik HTTPS URL'sini ve tek kullanımlık bir onay kodunu alır.

Fatura oluşturmak için Odoo, Paymos proje kimliğini, sipariş tutarını, para birimini ve opak bir sipariş referansını gönderir. `sale.order.order_line` üzerinde (`display_type`, `is_delivery` ve `is_downpayment` hariç) 1 ile 50 arasında geçerli ürün satırı bulunan siparişlerde Odoo isteğe bağlı bir `products[]` dizisi (`title`, pozitif tam sayı `quantity`, tam ondalık `unit_amount` ve isteğe bağlı düz metin `description`) de iletir. Bir siparişte 0 veya 50'den fazla ürün satırı, kesirli miktar ya da geçersiz bir ürün satırı varsa, Odoo hep-ya-da-hiç kuralıyla yerel sipariş toplamını kullanan yalnızca tutar faturasına geri döner. Normalleştirilmiş `products_json` anlık görüntüsü ilk istekten önce `paymos.attempt` üzerinde saklanır. Yeniden denemeler özgün ürün değerlerini tekrar gönderir; eski anlık görüntülerdeki kullanımdan kaldırılmış `image_url` anahtarını yalnızca giden kopyadan çıkarır ve saklanan veriyi yeniden yazmaz. İlk istekten önce diğer tüm ürün değişiklikleri hazırlanan denemeyi geçersiz kılar. Odoo bu istekte alıcı kimlik bilgilerini paylaşmaz. Alıcı ödeme ayrıntılarını barındırılan Paymos sayfasında girer. Odoo bağlı satışı onaylamadan önce imzalı bildirimleri doğrular ve faturayı Paymos üzerinden kontrol eder. Zamanlanmış mutabakat görevi kaçırılan bildirimleri denetler.

[Paymos dokümantasyonu](https://paymos.io/tr/docs), [Paymos gizlilik politikası](https://paymos.io/tr/privacy)

## İadeler

Ödemenin ait olduğu şirkete erişimi olan bir Odoo sistem yöneticisi hesabı kullanın.

- Tam iade için **Accounting** altında kayda alınmış özgün müşteri ödemesini açın ve siparişin para birimindeki tam özgün tutar için **Refund** seçeneğini belirleyin. Paymos bağlı fatura için token ödemesini hesaplar ve özgün ödeyene geri gönderir. Odoo doğrulama sonrasında iadeyi özgün ödemeye karşı kaydeder. [Odoo 20 iade iş akışı](https://www.odoo.com/documentation/20.0/applications/finance/payment_providers.html#refunds)
- Fazla ödemeyi iade etmek için geliştirici modunu etkinleştirin ve **Accounting → Configuration → Payment Transactions** altında özgün işlemi açın. **Refund Paymos overpayment** seçeneğini seçin. Paymos iadeyi onayladıktan sonra Odoo siparişe bir not ekler ve tahsil edilen anaparayı korur.

Başarısız bir iadeyi veya doğrulama sınırına ulaşan bir işlemi, ek adım atmadan önce Paymos üzerinden inceleyin.

## Kaynaktan derleme

Yerel bir SDK kaynak ağacından (`--sdk-source`, varsayılan sürüm akışı) veya çevrimdışı bir sürüm wheel paketinden (`--sdk-wheel`) etiketlenmiş herhangi bir kararlı Paymos Python SDK `1.x` sürümünü (`>= 1.6.0`) paketleyin. Eklenti, isteğe bağlı `products[]` yüklerini SDK kaynağını değiştirmeden gömülü SDK'nın `client.request("POST", "/v1/invoices", payload)` aktarımı üzerinden gönderir. Derleyici yalnızca yerel girdileri kullanarak SDK sürümünü, gerekli çalışma zamanı dosyalarını, lisansı ve SHA-256 sağlama toplamlarını doğrular. İsteğe bağlı yansı (mirror) dizini ZIP içindeki dosyaların birebir kopyasını alır. [SDK kaynağı](https://github.com/Paymos-labs/python-sdk), [PyPI üzerindeki SDK sürümleri](https://pypi.org/project/paymos-sdk/)

```sh
python scripts/build-odoo-plugin.py build --sdk-source /path/to/python-sdk --output /path/to/paymos-odoo-v1.1.0.zip --mirror-dir /path/to/empty-mirror
python scripts/build-odoo-plugin.py verify --archive /path/to/paymos-odoo-v1.1.0.zip --sdk-source /path/to/python-sdk
```

Kaynak ağacı yerine çevrimdışı sürüm wheel paketinden derlemek için:

```sh
python scripts/build-odoo-plugin.py build --sdk-wheel /path/to/paymos_sdk-1.7.1-py3-none-any.whl --output /path/to/paymos-odoo-v1.1.0.zip --mirror-dir /path/to/empty-mirror
python scripts/build-odoo-plugin.py verify --archive /path/to/paymos-odoo-v1.1.0.zip
```

Bir sürüm commit'inden derlerken `build` ve `verify` komutlarına aynı tam `--source-commit` değerini (ve isteğe bağlı `--sdk-version` / `--sdk-source-commit` parametrelerini) verin. `bundle-manifest.json` dosyası Odoo sürümünü, etiketi, kaynak ve dosya karmalarını ve gömülü SDK meta verilerini kaydeder. Komut, ZIP dosyasının SHA-256 değerini yazdırır.
