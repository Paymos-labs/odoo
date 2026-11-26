# Paymos Odoo 稳定币支付插件

[English](README.md) | **简体中文** | [Русский](README.ru.md) | [Español](README.es.md) | [Deutsch](README.de.md) | [Türkçe](README.tr.md)

在 Odoo 20.0 网站中使用 Paymos 托管收银台接收加密货币与稳定币支付（USDT/USDC 收款）。本插件依托 Odoo 支付交易体系与店铺财务账簿配置完成订单确认及退款入账。

本插件免费开源，采用 LGPL-3.0-only 许可协议。使用托管支付服务需要拥有 Paymos 账号与项目。详见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。

[![观看视频：12 款官方 CMS 稳定币支付插件 — 一键连接（Paymos）](https://i.ytimg.com/vi/VMLUGVumYkg/maxresdefault.jpg)](https://www.youtube.com/watch?v=VMLUGVumYkg)
> **视频演示（1:12，提供 6 种语言字幕）：** [在 YouTube 观看](https://www.youtube.com/watch?v=VMLUGVumYkg)

## 环境要求

- 自托管部署的 Odoo 20.0，已安装 `payment`、`account_payment` 和 `website_sale` 模块。
- Odoo 所使用的 Python 环境已安装 `cryptography`。经验证的运行环境为 Python 3.12。若 Python 版本低于 3.13，请使用与 `payment_paymos/` 同级的 `requirements.txt` 安装锁定版本的 `typing-extensions` wheel 包。
- 启用 HTTPS 的网站前台，以及具备对应目标环境访问权限的 Paymos 项目。
- 支付服务商所属公司下已配置银行日记账（bank journal），且包含对应的 Paymos 入账支付方式行（inbound payment method line）。

以下步骤适用于自托管的 Odoo 20.0 部署。Odoo.sh 可安装分支根目录 `requirements.txt` 中声明的 Python 依赖，其托管兼容性有待实际环境验证；Odoo Online 不支持第三方 Python 插件。参考：[Odoo 20 本地部署要求](https://www.odoo.com/documentation/20.0/administration/on_premise/source.html)、[Odoo 20 Odoo.sh 依赖指南](https://www.odoo.com/documentation/20.0/administration/odoo_sh/create_module.html#use-an-external-python-library)、[Odoo Apps 常见问题](https://apps.odoo.com/apps/faq)。

## 安装与一键连接

1. 将安装包 ZIP 解压到 Odoo 的 `addons_path` 目录中。该目录下应包含 `payment_paymos/__manifest__.py` 与 `requirements.txt`。
2. 在 Odoo 实际运行的同一 Python 环境中安装依赖。如需离线安装，请将 `typing_extensions-4.15.0-py3-none-any.whl` 放入本地 wheel 目录并执行以下命令，pip 会校验文件哈希。[锁定版本 wheel](https://pypi.org/project/typing-extensions/4.15.0/#files)

   ```sh
   python -m pip install --no-index --find-links /path/to/wheels --require-hashes -r requirements.txt
   ```

3. 使用 `cryptography.fernet.Fernet.generate_key()` 生成 Fernet 密钥，并将其设为 Odoo 服务端密钥配置中的 `PAYMOS_ODOO_ENVELOPE_KEY`，随后带上该配置重启 Odoo 服务。请妥善将该密钥存入受保护的备份中，已存储的连接凭据依赖它解密。
4. 使用 Odoo 系统管理员账号刷新应用列表并安装 **Paymos Payments**。打开 **Accounting → Configuration → Payment Providers**，选择店铺所属公司的 **Paymos**，点击 **Connect Paymos** 并在 Paymos 授权页确认请求。
5. 选择贵司的银行日记账并核对 Paymos 入账支付方式行。先在 Sandbox 模式下启用并发布该支付方式以测试结账流程。
6. 当网站前台与财务配置全部就绪后，点击 **Select Live**。切换运行环境会自动停用并取消发布该支付方式；核验 Live 连接正常后，再次启用并发布即可。

## 结账流程与数据边界

当管理员通过 **Connect Paymos** 打开授权页时，Paymos 授权页面只会收到店铺的规范 HTTPS URL 和一次性授权码。

创建账单时，Odoo 会发送 Paymos 项目 ID、订单金额、币种以及不透明的订单参考号。当 `sale.order.order_line` 中包含 1 至 50 条有效商品行（排除 `display_type`、`is_delivery` 和 `is_downpayment`）时，Odoo 还会一并传递可选的 `products[]` 数组（包含 `title`、正整数 `quantity`、精确十进制 `unit_amount` 以及可选纯文本 `description`）。如果订单有 0 条或超过 50 条商品行、存在小数数量或任意无效商品行，Odoo 会按全有或全无规则回退为使用原生订单总额的仅金额（amount-only）账单。规范化后的 `products_json` 快照会在首次请求前保存在 `paymos.attempt` 上；重试时会原样重放原始商品值，对较旧快照仅省略已废弃的 `image_url` 键而不改写库内数据。在首次请求发出之前，任何其他商品变动都会使已准备的支付尝试失效。Odoo 不会在该请求中包含任何买家身份信息；买家在 Paymos 托管收银台填写支付信息。Odoo 在确认关联销售订单之前，会校验签名通知并向 Paymos 复核账单状态；定时对账任务也会持续排查遗漏的通知。

[Paymos 文档](https://paymos.io/zh-hans/docs)、[Paymos 隐私政策](https://paymos.io/zh-hans/privacy)

## 退款

请使用具备该笔付款所属公司访问权限的 Odoo 系统管理员账号操作。

- **全额退款**：在 **Accounting** 中打开原始已过账的客户收款记录，选择 **Refund** 并按订单币种退还全额原款。Paymos 会计算关联账单对应的代币退付金额并原路退回原付款方；校验完成后，Odoo 会针对原收款记录入账该笔退款。[Odoo 20 退款工作流](https://www.odoo.com/documentation/20.0/applications/finance/payment_providers.html#refunds)
- **超额退款**：启用开发者模式，在 **Accounting → Configuration → Payment Transactions** 中打开原交易记录，选择 **Refund Paymos overpayment**。Paymos 确认退回后，Odoo 会在订单上添加备注，同时保留已结算的本金记录。

若某笔退款失败或达到校验次数上限，请在 Paymos 控制台核查后再采取后续操作。

## 从源码构建

可通过本地 SDK 源码树（`--sdk-source`，默认发布流程）或离线发布 wheel（`--sdk-wheel`）打包任意带版本标记的稳定版 Paymos Python SDK `1.x`（`>= 1.6.0`）。插件直接通过内置 SDK 的 `client.request("POST", "/v1/invoices", payload)` 通道发送可选的 `products[]` 载荷，无需修改 SDK 源码。构建脚本仅依赖本地输入即可校验 SDK 版本、必需运行时文件、许可证和 SHA-256 校验和；可选的镜像目录将收到与 ZIP 完全一致的文件集。[SDK 源码](https://github.com/Paymos-labs/python-sdk)、[PyPI 上的 SDK 发布](https://pypi.org/project/paymos-sdk/)

```sh
python scripts/build-odoo-plugin.py build --sdk-source /path/to/python-sdk --output /path/to/paymos-odoo-v1.1.0.zip --mirror-dir /path/to/empty-mirror
python scripts/build-odoo-plugin.py verify --archive /path/to/paymos-odoo-v1.1.0.zip --sdk-source /path/to/python-sdk
```

如需从离线发布 wheel 而非源码树构建：

```sh
python scripts/build-odoo-plugin.py build --sdk-wheel /path/to/paymos_sdk-1.7.1-py3-none-any.whl --output /path/to/paymos-odoo-v1.1.0.zip --mirror-dir /path/to/empty-mirror
python scripts/build-odoo-plugin.py verify --archive /path/to/paymos-odoo-v1.1.0.zip
```

基于发布提交构建时，请向 `build` 和 `verify` 传入相同的完整 `--source-commit`（以及可选的 `--sdk-version` / `--sdk-source-commit`）。`bundle-manifest.json` 会记录 Odoo 版本、标签、源码与文件哈希以及内置 SDK 元数据，命令执行结束时会输出 ZIP 的 SHA-256 值。
