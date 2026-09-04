# c-cctoima · 河北四大运营商个人资费自动采集与 IMA 同步

自动采集 **中国移动 / 中国联通 / 中国电信 / 中国广电** 四家运营商
**河北省 → 个人用户 → 全部公开资费**，经完整性校验与标准化后，
与上次成功版本比较，**有变化才**增量同步到 IMA 知识库「河北运营商资费」。

```
采集（页面真实交互选河北+个人）
  → 完整性校验（integrity-report.json，FAIL 即阻断）
  → 标准化（normalized.json，统一 schema）
  → 与上次成功版本 diff（tariff_id + content_hash）
  → 有变化才更新 IMA（NO_CHANGE 不重复上传）
```

**核心原则**：宁可采集失败，也不用不完整数据覆盖旧数据或更新 IMA。

## 当前运行状态（2026-09-05）

| 运营商 | 采集数 | 完整性 | IMA 文件 |
| --- | --- | --- | --- |
| 中国移动 | 2210 | PASS（5 分类全达标） | 5 个分类文件 |
| 中国联通 | 1679 | PASS（21 分类组合全对齐） | 4 个分类文件 |
| 中国电信 | 877 | PASS（5 分类） | 3 个分类文件 |
| 中国广电 | 131 | PASS（12 类型组合） | 3 个分类文件 |

知识库「河北运营商资费」共 15 个现行分类文件（另有 4 个历史时间戳版本）。

## 快速开始

```bash
pip install -r requirements.txt
python -m playwright install chromium --with-deps

# 单运营商采集（含校验/标准化/diff）
python -m collector collect --operator cmcc

# DRY_RUN（采集与校验照常，不更新 IMA、不提升 latest）
python -m collector sync --dry-run

# 手动触发完整流水线（GitHub Actions）
# Actions → Tariff Scrape & IMA Sync → Run workflow
```

## 四家采集技术方案

| 运营商 | 入口 | 河北选择（页面交互） | 官方接口（实测发现） | 采集方式 |
| --- | --- | --- | --- | --- |
| 移动 | `h.app.coc.10086.cn/.../tariffZonePers.html` | `.prov-entry` 省份入口 → 河北省；个人资费专区；河北资费页签 | `getTariffListInfo` 等（**isWX 加密信封**） | 浏览器滚动懒加载 + `JSON.parse` 钩子捕获明文（CMCC-HE 生产方案） |
| 联通 | `img.client.10010.com/zifeizhuanquwt` | 级联选择器 河北 → 石家庄（本省资费 tab） | `queryTariffNew/{indexData,threeLevelName,operateData}` | UI 发现分类树 → 页面内批量调用 |
| 电信 | `www.189.cn/tariffZone/` | 城市页签 → 河北（前端省份表 provCode=609906） | `/bss/tariffZone/{newTarifZone12List,newTarifZone3Title}.do` | 瑞数 WAF 自动过 → 页面内 XHR 批量（令牌由站点钩子注入） |
| 广电 | `m.10099.com.cn/expensesNotice` | 省份入口 → 河北（HB00）；类型树选「公众」=个人 | `contact-web/api/{qryAreaList,queryTariffCondition,queryTariffAllByCond}` | UI 发现类型树 → 页面内 fetch 批量 |

**分类发现原则**：所有资费分类（移动资费类型下拉、联通 levelList、电信 lable1/lable2、
广电 type1/type2/type3）均**运行时从页面/接口动态发现**，页面新增分类自动覆盖，
本仓库不含任何硬编码分类列表。

**接口验证原则**：所有接口参数（provinceId=018、provCode=609906、applicableArea=HB00、
tariffAttributes=2 等）均通过 2026-09-05 真实页面交互捕获验证，运行时再次校验
（reportNo 前缀、province 字段、applicablePeople 等证据锚点）。

## 数据结构

```
data/
  cmcc/ cucc/ ctcc/ cbn/
    raw/collect.json          # 原始采集（运营商原始字段 + 交互证据 + API 日志）
    normalized.json           # 本次标准化结果（统一 schema）
    latest.json               # 上次【成功】版本（完整性 FAIL 时绝不覆盖）
    integrity-report.json     # 完整性校验报告
    diff.json                 # 与上次成功版本的 added/removed/modified/unchanged
    history/                  # 历史成功版本归档（gzip，保留 30 份）
  ima-state.json              # IMA 同步状态（分类文件 hash / media_id）
```

统一字段：`operator, province, audience, category, subcategory, tariff_id, name,
price, traffic, voice, sms, validity, eligibility, description, details,
source_url, source_api, collected_at, content_hash`

- `tariff_id`：运营商方案编号（如 `CMCC-26HE201171` / `CUCC-26HE300043` / `CTCC-26BJ100040` / `CBN-26JT500002`），稳定且可 diff
- `content_hash`：业务字段 SHA-256（不含采集时间），同一资费内容变化即改变

## 完整性校验（最高优先级）

每家运营商独立校验，任一 FAIL → 整体 FAIL → **禁止更新 latest、禁止更新 IMA、保留上次成功版本**：

1. **河北选择正确**：页面交互证据 + 接口参数证据 + 记录字段证据（province=311 / reportNo 前缀 HE 等）
2. **个人范围正确**：移动=个人资费专区（tariffZonePers+type1=1）、广电=公众类型树、联通/电信=网厅个人资费专区
3. **全部分类已发现**：动态枚举数量 > 0 且全部遍历
4. **官方 total 一致**：移动=page.total（bean 数）、联通=threeLevelName 数量、电信/广电=接口全量返回数
5. **详情采集完整**：name/price/details 字段覆盖率 ≥ 95%
6. **无明显重复**：tariff_id 唯一 + 重名率检查
7. **无严重 API/页面错误**
8. **无异常数量下降**：相对上次成功版本下降 > 30% 且 > 20 条即可疑；
   若官方 total 校验一致则判定为官方正常下线（豁免），否则 FAIL

失败证据（raw 数据、integrity-report、采集日志）通过 Artifact 保留 90 天。

## IMA 增量同步

- 知识库：**河北运营商资费**（官方 OpenAPI `openapi/wiki/v1`，协议来自
  [ima-skills-1.1.9](https://app-dl.ima.qq.com/skills/ima-skills-1.1.9.zip) 实测）
- 结构：`河北移动_套餐.md`、`河北联通_宽带.md` …（文件名编码「运营商→分类」层级，
  内容含分类下全部资费的标准字段与完整详情表）
- **只有 added/modified/removed > 0 才更新**；NO_CHANGE 不重复上传
- 上传流程（官方 GATE）：`check_repeated_names` → `create_media` → COS 上传 → `add_knowledge`
- 官方接口不支持"替换/删除"→ 更新=上传新版本文件（同名加时间戳后缀保留历史），
  下架资费在新版本内容中不再出现
- 分类级内容 hash（`data/ima-state.json`）驱动精确增量：仅重传有变化的分类文件
- 删除检测建立在本次完整性 PASS 前提上（FAIL 的运营商整体跳过 IMA 同步）

## GitHub Actions

`.github/workflows/scrape.yml`：

- **触发**：每日 UTC 20:00（北京 04:00）+ 手动 `workflow_dispatch`（可选运营商、DRY_RUN）
- **Matrix 并发**：`fail-fast: false`，四家互不影响
- **WARP 出口**：warp-cli TUN → docker socks5 → 直连兜底（参考 CMCC-HE 三级方案）
- **Retry**：CLI 内部 2 次尝试 + 外层 1 次重跑（冷却 60s）
- **Artifact**：`data-<op>-<run>`（90 天）、采集日志、运行摘要
- **汇总任务**：合并 Artifact → 完整性闸门 → promote latest → IMA 同步 → 提交 data →
  输出摘要（运营商/状态/数量/新增/修改/删除/IMA 状态/失败原因）与总体状态
  `SUCCESS / PARTIAL_SUCCESS / FAILED`

单测 CI（`.github/workflows/ci.yml`）基于 fixtures/mock，无网络依赖。

### Secrets 配置

| Secret | 用途 |
| --- | --- |
| `IMA_CLIENT_ID` | IMA OpenAPI Client ID（ima.qq.com/agent-interface 获取） |
| `IMA_API_KEY` | IMA OpenAPI API Key |

`GITHUB_TOKEN` 由 Actions 自动提供（用于提交 data）。
**凭证只经 Secrets 注入环境变量，不进入代码/日志/Artifact。**

## 设计决策与已知问题

1. **采集范围 = 省份视图**：移动=河北资费（分省）页签、联通=本省资费、电信=河北资费页签、
   广电=河北区域。各站点的"全网/集团资费"视图不在范围内（与页面"选择河北"交互语义一致，
   同 CMCC-HE 生产口径）。
2. **联通城市选择**：本省资费视图需选到城市，采用石家庄（省会）；资费按省打标
   （reportNo HE 前缀），城市选择不影响省级资费集合。
3. **移动采集耗时**：加密通道无法直接批量调接口，须浏览器懒加载滚动
   （page.total=方案数口径：套餐 418/414、营销活动 1035/1032），整轮约 40~60 分钟属预期；
   翻页在途缺口由自愈补滚（最多 3 轮冷却重滚）闭环。
4. **IMA 无删除/替换/建文件夹接口**（官方 1.1.9 实测）→ 版本化文件 + 文件名编码层级；
   旧版本文件保留在知识库中（官方"保留两者"模型）。当前知识库中「河北联通_套餐_20260904175435.md」
   等 4 个时间戳文件是 NO_CHANGE 误传 bug 修复前的历史版本（内容与现行版本一致），属官方模型下的正常存档。
5. **电信瑞数 WAF**：桌面 UA 在海外数据中心 IP 上会进入挑战循环（412→400），
   实测**移动 UA 走独立通道免挑战**（直连 200）→ 采集器使用移动 UA；
   沙箱/本地高频访问也会被拉黑（IP 信誉），属预期防护。
6. **广电两段式交互**：省份页签需点击两次（一次切视图、二次开 Vant 弹层级联），
   系前端 439 chunk 逆向验证的官方交互逻辑。
7. **同名不同编号**：移动官方数据存在同名独立方案（如"60元档分期包"×5 个不同
   方案编号），非采集重复；权威去重判据为 tariff_id（方案编号）。
