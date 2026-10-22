---
name: shopxz-japan-launch
description: 为已有 ShopXZ/Fecify 日本 B2C 实体商品店规划、配置和验收上线，覆盖店铺基础、支付、交易、国家与物流设置，以及应用、日语邮件、优惠券、强化召回、装修和迁移路径。适用于日本新站快速上线、对照标准补缺及中断续办；单项运营不默认重建全站。
metadata:
  version: "1.4.0"
---

# ShopXZ 日本站快速上线

适用于已有后台的日本 B2C 实体商品站。先识别本轮范围：规划、只读补缺检查、指定模块设置，或整站上线。默认复用已有正确结果，更新本轮授权的差异。

## 依赖与范围

- 实际站点操作依赖当前环境的 `fecify-site-manager-v6`；先读其 `SKILL.md`，按 `api-doc.js → api-call.js` 执行。本包不保存Token、不切换站点、不复制接口定义。
- 由管理技能绑定用户明确提供的目标站并执行preflight，核对 `shop_id`、`shop_type=10`、`public_url`。参考站只读，其品牌、ID、账号与经营承诺不能作为目标站默认值。
- 只收到凭据时仅绑定；同一消息包含业务任务则继续处理该任务。切换目标不延续另一店铺未获授权的写入。
- Python工具自动化：基础信息、交易字段、国家范围、选定的高级/TASK/验证码参数、已核对账号的支付方式启用与排序，以及日本结账、浏览记录、悬浮加购、邮件、券与召回。物流/税费生成具体区域、规则、请求与回读方案，再走管理技能门禁执行；支付账号录入、SMTP、页面装修与发布仍按对应工作流完成。
- 制作、安装或测试本技能不授权修改当前绑定店铺。普通建站不自动启动全站GEO、付费广告、批量营销发送或GMC申诉。

## 开始

读取 [站点资料](references/site-profile.md)。已有事实直接复用；只有缺失且影响结果的经营资料才集中询问，其他工作继续。

```text
python scripts/launch.py profile --preset jp-core --out <外部工作目录>/profile.json
python scripts/launch.py profile --preset jp-apparel --out <外部工作目录>/profile.json
```

`jp-core`是日本基础配置；`jp-apparel`加入399日元运费、满5000免运、3–7工作日发货的服饰示例。这不是日本通用要求，商家采用后才作为该站规则。营销默认未选中；采用组合后设置 `modules.marketing=true`。

## 流程

1. **检查目标**：采集店铺能力、已有配置与关联。应用标识存在不代表已设置或前台生效。
2. **具体方案**：分别列出新建、更新、复用、冲突、待资料；列明优惠次数/期限/叠加、发送节奏、页面与装修范围。已有授权不逐步重复询问。
3. **店铺基础配置**：按 [后台九项设置](references/store-settings.md) 完成基础、高级、TASK、验证码、支付、交易、税费、国家和物流检查。先应用可执行差异，再核对实际语言/币种与交易规则；不能仅填profile就认为设置已生效。
4. **按依赖执行**：独立模块可先完成。营销先准备并核对券、模板、活动及消息，再切换开关。基础语言/币种调整后重新inspect/plan，营销仍以实际站点状态为准。
5. **保存后回读**：核对实际字段、引用、状态和内容；HTTP200或业务200不能代替验收。
6. **前台与经营验证**：按 [验收](references/acceptance.md) 区分配置、渲染、加购、实际结账、邮件投递与外部账户证据。

```text
python scripts/launch.py inspect --profile <profile.json> --workdir <run>
python scripts/launch.py plan --profile <已核对profile.json> --snapshot <最新snapshot.json> --out <plan.json>
python scripts/launch.py apply --plan <plan.json> --workdir <run>
python scripts/launch.py resume --plan <plan.json> --workdir <run>
python scripts/launch.py verify --plan <plan.json> --workdir <run>
```

首次未知shop_id时，inspect会输出resolved-profile.json。Agent核对返回的名称/域名与用户指定目标一致后，使用该文件再做一次inspect，取最新快照用于plan；无需重复向商家索要数字ID授权。已有正确shop_id时直接使用原profile。

先看 `--help`。可用 `--manager-skill <管理技能目录>`、`--node <Node可执行文件>` 指定依赖；不要写作者电脑路径。运行产物放技能目录外。

`plan`离线；`inspect/verify`只读；`apply/resume`写入用户已授权的计划范围。实际确认挑战和权限错误按管理技能处理，不自制确认、不关闭校验。

## 按任务读取

| 工作 | 引用 |
|---|---|
| 资料、预设、范围与续跑 | [site-profile.md](references/site-profile.md) |
| 后台、日本市场、应用选择 | [settings-and-apps.md](references/settings-and-apps.md) |
| 截图中的店铺九项设置及物流/税费执行方案 | [store-settings.md](references/store-settings.md) |
| 日语邮件、优惠券、强化召回（四步节奏、支付中提醒、接管现有活动） | [email-and-recall.md](references/email-and-recall.md) |
| 商品、政策、装修与迁移 | [storefront.md](references/storefront.md) |
| 实测、发布与交付报告 | [acceptance.md](references/acceptance.md) |

## 运行约束

- 全量接口先读取完整前值，保留未修改字段；提交前检测商家是否另行编辑。
- 用当前站ID建立映射。券码、名称相同不代表业务条件相同；按规则、内容、引用核对。
- 冻结计划不跨店使用。记录操作指纹、开始/完成/未知状态。写入超时先回读，唯一精确匹配才继续；无匹配或多匹配停止该分支核对，不能盲重建。
- 新召回关联完整且验证成功前保留现有机制；切换成功后关闭默认召回与type9模板。只优化邮件不自动关召回。
- 主题、云存储、支付、广告和SMTP凭据不随包迁移；预览用姓名和订单不能进入生产模板。
- 日文由宿主AI编写/审核；代码可交给用户选择的工作器。真正翻译任务遵守管理技能的宿主AI来源门禁，不调用外部翻译服务。
- 图片生成优先宿主原生工具，商品展示用真实商品素材核验；缺能力记录未完成，不声称已生成/验收。

## 包自检

```text
python scripts/launch.py validate-assets
```

这仅验证包与模板。上线结论还需要该站回读及适用浏览器/业务证据。
