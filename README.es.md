# Paymos Payments para Odoo

[English](README.md) | [简体中文](README.zh-CN.md) | [Русский](README.ru.md) | **Español** | [Deutsch](README.de.md) | [Türkçe](README.tr.md)

Acepte pagos en criptomonedas en un sitio web Odoo 20.0 mediante el checkout alojado de Paymos. El complemento utiliza las transacciones de pago de Odoo y la configuración contable de la tienda para confirmar pedidos y registrar reembolsos.

El complemento es gratuito y tiene licencia LGPL-3.0-only. El uso del servicio de pagos alojado requiere una cuenta y un proyecto de Paymos. Consulte [LICENSE](LICENSE) y [NOTICE](NOTICE).

[![Ver vídeo: 12 plugins oficiales de pagos cripto para CMS — Conexión en 1 clic (Paymos)](https://i.ytimg.com/vi/VMLUGVumYkg/maxresdefault.jpg)](https://www.youtube.com/watch?v=VMLUGVumYkg)
> **Recorrido en vídeo (1:12, subtítulos en 6 idiomas):** [Ver en YouTube](https://www.youtube.com/watch?v=VMLUGVumYkg)

## Requisitos

- Una instalación autohospedada de Odoo 20.0 con `payment`, `account_payment` y `website_sale`.
- Entorno de Python de Odoo con `cryptography`. El entorno de ejecución verificado es Python 3.12. Para versiones de Python inferiores a 3.13, instale el wheel fijado de `typing-extensions` mediante el archivo `requirements.txt` situado junto a `payment_paymos/`.
- Una tienda HTTPS y un proyecto de Paymos con acceso al entorno que vaya a conectar.
- Un diario bancario en la empresa del proveedor con su línea de método de pago entrante de Paymos.

Los pasos siguientes cubren una instalación autohospedada de Odoo 20.0. Odoo.sh puede instalar dependencias de Python declaradas en el archivo `requirements.txt` raíz de la rama; la compatibilidad con Odoo.sh está pendiente de verificación alojada. Odoo Online excluye complementos de Python de terceros. [Requisitos on-premises de Odoo 20](https://www.odoo.com/documentation/20.0/administration/on_premise/source.html), [Guía de dependencias de Odoo 20 en Odoo.sh](https://www.odoo.com/documentation/20.0/administration/odoo_sh/create_module.html#use-an-external-python-library), [Preguntas frecuentes de Odoo Apps](https://apps.odoo.com/apps/faq)

## Instalar y conectar

1. Extraiga el archivo ZIP en un directorio incluido en el `addons_path` de Odoo. Ese directorio debe contener `payment_paymos/__manifest__.py` y `requirements.txt`.
2. Instale las dependencias en el mismo entorno Python que utiliza Odoo. Para una instalación sin conexión, coloque el archivo `typing_extensions-4.15.0-py3-none-any.whl` en su directorio de wheels y ejecute el siguiente comando. pip verifica el hash del archivo. [Wheel fijado](https://pypi.org/project/typing-extensions/4.15.0/#files)

   ```sh
   python -m pip install --no-index --find-links /path/to/wheels --require-hashes -r requirements.txt
   ```

3. Genere una clave Fernet con `cryptography.fernet.Fernet.generate_key()` y configúrela como `PAYMOS_ODOO_ENVELOPE_KEY` en la configuración secreta del servidor de Odoo. Reinicie el servicio de Odoo con ese ajuste. Guarde la clave junto con sus copias de seguridad protegidas, porque las credenciales de conexión almacenadas dependen de ella.
4. Como administrador del sistema en Odoo, actualice la lista de aplicaciones e instale **Paymos Payments**. Abra **Accounting → Configuration → Payment Providers**, seleccione **Paymos** para la empresa de la tienda, elija **Connect Paymos** y apruebe la solicitud en Paymos.
5. Seleccione el diario bancario de su empresa y verifique su línea de método de pago entrante de Paymos. Active y publique el proveedor en Sandbox para probar el checkout.
6. Cuando su tienda y su configuración contable estén listas, elija **Select Live**. Cambiar el entorno desactiva y despublica el proveedor. Verifique la conexión Live y vuelva a activar y publicar el proveedor.

## Checkout y datos

La página de aprobación de Paymos recibe la URL HTTPS canónica de la tienda y un código de aprobación de un solo uso cuando un administrador la abre mediante **Connect Paymos**.

Para crear una factura, Odoo envía el ID del proyecto de Paymos, el importe del pedido, la divisa y una referencia opaca del pedido. En pedidos con entre 1 y 50 líneas de producto válidas en `sale.order.order_line` (excluyendo `display_type`, `is_delivery` e `is_downpayment`), Odoo también envía un array opcional `products[]` (`title`, entero positivo `quantity`, decimal exacto `unit_amount` y texto plano opcional `description`). Si un pedido tiene 0 o más de 50 líneas de producto, una cantidad fraccionaria o cualquier línea no válida, Odoo recurre con criterio todo o nada a una factura solo de importe con el total nativo del pedido. La instantánea normalizada `products_json` se guarda en `paymos.attempt` antes de la primera solicitud. Los reintentos reenvían sus valores originales de producto, omitiendo únicamente la clave retirada `image_url` de instantáneas antiguas sin sobrescribir los datos almacenados. Antes de la primera solicitud, cualquier otro cambio en los productos invalida el intento preparado. Odoo mantiene la identidad del comprador fuera de esa solicitud. El comprador introduce los datos de pago en la página alojada de Paymos. Odoo verifica las notificaciones firmadas y consulta la factura en Paymos antes de confirmar la venta vinculada. Una tarea programada de conciliación detecta posibles notificaciones perdidas.

[Documentación de Paymos](https://paymos.io/es/docs), [Política de privacidad de Paymos](https://paymos.io/es/privacy)

## Reembolsos

Utilice una cuenta de administrador del sistema de Odoo con acceso a la empresa del pago.

- Para un reembolso completo, abra el pago de cliente contabilizado original en **Accounting** y seleccione **Refund** por el importe íntegro original en la divisa del pedido. Paymos calcula el pago en tokens para la factura vinculada y lo envía al pagador original. Odoo registra el reembolso contra el pago original tras la verificación. [Flujo de reembolsos en Odoo 20](https://www.odoo.com/documentation/20.0/applications/finance/payment_providers.html#refunds)
- Para devolver un sobrepago, active el modo desarrollador y abra la transacción original en **Accounting → Configuration → Payment Transactions**. Elija **Refund Paymos overpayment**. Una vez que Paymos confirme la devolución, Odoo añade una nota al pedido y conserva el importe principal liquidado.

Revise en Paymos cualquier reembolso fallido o cualquier operación que alcance su límite de verificación antes de realizar nuevas acciones.

## Compilar desde el código fuente

Empaquete cualquier versión estable etiquetada del SDK de Python de Paymos `1.x` (`>= 1.6.0`) desde un árbol de código fuente local del SDK (`--sdk-source`, el flujo de publicación predeterminado) o desde un wheel de lanzamiento sin conexión (`--sdk-wheel`). El complemento envía los payloads opcionales `products[]` a través del transporte `client.request("POST", "/v1/invoices", payload)` del SDK incluido sin modificar el código fuente del SDK. El script de compilación verifica la versión del SDK, los archivos de ejecución requeridos, la licencia y las sumas de comprobación SHA-256 utilizando únicamente entradas locales. El directorio espejo opcional recibe exactamente los mismos archivos que el ZIP. [Código fuente del SDK](https://github.com/Paymos-labs/python-sdk), [Versiones del SDK en PyPI](https://pypi.org/project/paymos-sdk/)

```sh
python scripts/build-odoo-plugin.py build --sdk-source /path/to/python-sdk --output /path/to/paymos-odoo-v1.1.0.zip --mirror-dir /path/to/empty-mirror
python scripts/build-odoo-plugin.py verify --archive /path/to/paymos-odoo-v1.1.0.zip --sdk-source /path/to/python-sdk
```

Para compilar a partir de un wheel de lanzamiento sin conexión en lugar de un árbol de código fuente:

```sh
python scripts/build-odoo-plugin.py build --sdk-wheel /path/to/paymos_sdk-1.7.1-py3-none-any.whl --output /path/to/paymos-odoo-v1.1.0.zip --mirror-dir /path/to/empty-mirror
python scripts/build-odoo-plugin.py verify --archive /path/to/paymos-odoo-v1.1.0.zip
```

Pase el mismo `--source-commit` completo (y opcionalmente `--sdk-version` / `--sdk-source-commit`) tanto a `build` como a `verify` al compilar desde un commit de lanzamiento. El archivo `bundle-manifest.json` registra la versión de Odoo, la etiqueta, los hashes del código fuente y de los archivos, y los metadatos del SDK incluido. El comando imprime el SHA-256 del archivo ZIP.
