# DNF · 拍卖行 & 金币寄售 自动做市

给 DNF（地下城与勇士）做的**拍卖行 + 金币寄售两个市场的"自动做市"系统**。一个编排脚本（`market_agent.py`）+ 一段注入游戏服的发信脚本（`frida.js`），让两个市场**永远有货可买**、并**自动收购玩家的低价单/寄售单**。

干两件事：

1. **补货（restock）**：定时往两个市场挂"系统卖单"，按补货列表的品类/数量/定价维持市场常备库存。
2. **回收（recycle）**：玩家挂的低价单/寄售单被系统按规则自动收购，并给玩家发**金币邮件 / 代币券物品邮件**（退押金、扣手续费）。

每轮（默认每小时）对每个启用的市场执行：**先只读探测**——仅回收活时**在线回收（不停服）**；有补货缺口才 `停服务 → 回收 → 补货 → 启服务`。无活则整轮跳过，不打扰玩家。

| 市场             | 商品 ⇄ 货币     | 补货                               | 回收                                                                         |
| ---------------- | --------------- | ---------------------------------- | ---------------------------------------------------------------------------- |
| `auction` 拍卖行 | 物品 ⇄ 金币     | `restock_list` 列表，随机定价      | JOIN `item_catalog` + 规则定限价，发金币邮件（手续费 5% / 押金 10000 必退）  |
| `cera` 金币寄售  | 金币包 ⇄ 代币券 | `cera_consign_list` 列表，固定标价 | `instant_price ≤ recycle_price` 即收，发代币券物品邮件（手续费 2% / 无押金） |

> 两个市场完全独立：不同库、不同服务进程、不同货币。改任一市场的列表/规则只需改 `config.json` 或对应表，下一轮自动生效，互不影响。关闭某市场设 `config.json` 的 `markets.<名>.enabled=false`。

---

## 运行环境与依赖

- **Python**：**2.7** 或 **3.9+**（同一份脚本双兼容，已实测 `py_compile` / `pyflakes` 通过）。
  - 脚本源码语言级兼容 py2.6–2.7 与 py3.3+；含依赖的实际下界由 pymysql 决定（见下）。
- **依赖**：仅标准库 + **vendored pymysql**（纯 Python，无需编译/联网，拷目录即用）。
  - py2.7 → pymysql **0.10.x**（最后支持 Python 2 的版本线）。
  - py3.9+ → pymysql **1.x**。
  - 放到脚本旁的 `vendor/` 目录（`config.json` 的 `vendor_path`），`import pymysql` 自动从这里加载。
- **平台**：仅 **Linux/Unix**——用了 `fcntl` 文件锁与 `os.setsid`，Windows 不可运行。
- **数据库**：MySQL（开发实测于 5.0；脚本对 MySQL 5.0 的两个特性有依赖，见[坑点 ⑧](#-charac_no-自增污染--零桩根治)与[性能](#补货性能auction_main-只有主键)）。
- **形态**：编排脚本常驻在能连库的环境内执行；`frida.js` 注入游戏服进程实际投递邮件。

---

## 架构与组成

| 件                        | 角色                                                                                                      |
| ------------------------- | --------------------------------------------------------------------------------------------------------- |
| `market_agent.py`         | 编排脚本。按 `config.json` 对每个市场跑「停 → 回收 → 补货 → 启」；把邮件**入队**到 `frida.pending_mail`。 |
| `frida.js`                | 注入游戏服进程的脚本。**消费 `pending_mail` 队列发邮件**（在线即时发 / 离线落库），含多频道防双发。       |
| `config.json`             | 库连接 + 两市场（停启命令 / 补货 / 回收规则）+ `system_owner.special` 零桩特殊补货块。                    |
| `restock_list`（表）      | 拍卖行补货清单（材料 / 稀有 / 神器 / 消耗品 / 称号 / 宠物 / 装备 / 装扮）。                               |
| `item_catalog`（表）      | 物品静态属性目录（回收判型/判稀有度、补货判 kind/token 依赖）。                                           |
| `cera_consign_list`（表） | 金币寄售的补货 + 回收配置（12 种金币包）。                                                                |
| `pending_mail`（表）      | 邮件队列。`market` 列区分市场，`item_id=0` 金币 / 非 0 物品；幂等键 `(market, auction_id, occ_time)`。    |

发信链路：`market_agent` **先入队后删单**（幂等：`INSERT IGNORE` + 崩在任意一步都不重复打款也不吞单）→ `frida.js` 轮询队列、调游戏原生发信函数投递。

---

## 核心机制与经济口径

### 系统 / 玩家的区分

玩家手动上架与系统补货**两者 `owner_type` 同为 1**，无法靠它区分。改用 **`owner_id` 边界**：

- `owner_id < id_base`（默认 `90000001`）= 玩家挂单（`owner_id` 即其 `charac_no`），是回收对象；
- `owner_id >= id_base` = 系统挂单，用于补货计数/续期，**绝不会被自己回收**。

**唯一约束**：`system_owner.id_base` 必须大于你服任何真实玩家的 `charac_no`。判据另以 **`m_id` 兜底**（`_sys_owner_where` / `_player_owner_where`：系统 = `owner≥id_base` 且 `charac_info` 无该号真玩家 `m_id<>0` 行），覆盖 reset/回收/续期/库存/沉金邮件清理，防历史高位污染号被误判（见[坑点 ⑧](#-charac_no-自增污染--零桩根治)）。

### 经济口径（实测）

- 成交价 = **单价 × 数量**（`auction_main` 装备 `add_info`=品级、材料 `add_info`=堆叠数）。
- 押金固定 **10000 必退**（玩家上架时已被游戏扣，直接删挂单不触发自动退款，须如数退还 → 一进一出不产生套利）。
- 手续费：拍卖行 **5%**、金币寄售 **2%**。回收价 = 成交价 − 手续费 + 退押金。
- 金币寄售货币 = **代币券**；玩家寄售标价 ≤ `recycle_price` 即回收，到手 = 标价 × (1 − 2%)，无押金。

### 无活跳过 / 在线回收 / 并发锁

- **无活跳过**：非 reset 轮先只读探测 `_has_recycle_work` / `_has_restock_work`：无待回收且无补货缺口就**不停启服务**（每小时大多没活，空跑停服会让玩家卡几秒）。
- **在线回收（不停服）**：仅有待回收、无补货缺口时，若 `markets.<名>.recycle.online=true` 则直接在线 DELETE 回收（发信经 frida 队列），**不停服**——只有真有补货缺口才停服。前提是游戏服能容忍在线 DELETE（玩家端刷新列表即消失、点购买无异常），上线前先按下方冒烟测试实测；出现幽灵单/购买异常就把该市场 `recycle.online` 设 `false` 回退为停服窗口内回收。
- **并发单例锁**：`once`/`reset`/常驻循环启动即抢 `flock`，拿不到就退出，防上一轮卡死时下一轮叠加成双实例互相踩踏。

### 补货性能（auction_main 只有主键）

`auction_main` 是游戏建的表，实测**只有 `auction_id` 主键、无其它索引**，按 `item_id`/`owner_id` 过滤都是全表扫。**不要给这张游戏热表加索引**（容器重启/游戏更新会重置表结构，且拖慢游戏自身高频写入）。补货侧靠三项优化把每轮对该表的查询压成**常数次**：一次 `GROUP BY owner_id,item_id` 取全量库存分布、批量多值 `INSERT`（每 500 行一条）、owner 装箱游标化。停机窗口与补货列表规模基本解耦（亚秒级；首次冷启动灌几万行也是个位数秒）。

---

## 关键实测定论 / 坑点

> 每条都是踩过坑、用数据库实证的结论，改回收逻辑/发信函数/特殊补货前务必先看。

### ① 装备的 `add_info` 是品级，不是数量

装备的 `auction_main.add_info` 存的是**品级**（实测可达 999999998），不是堆叠数；只有 `kind=stackable`（材料）的 `add_info` 才是真实堆叠数。

- **回收侧**：装备 `count` 必须强制 = 1，否则 `单价 × add_info` 溢出 INT（截成 21.47 亿天价金币）。代码已对装备置 1，并加 20 亿单笔硬上限兜底（超限跳过 + 告警）。
- **补货侧**：系统补货上架普通装备（有耐久的武器/防具等）时 `add_info` 填 **999999998**（最上级品级）。**不要填 0**——白板 0 能正常加载、正常购买，但玩家买到手后一经维修装备即消失（实测）；填非法品级 → 拍卖行启动加载该挂单时 `RegistItem() 失败 → 服务进程退出 → 整服起不来`（与挂单数量/内存无关，是程序主动退出）。称号/装扮/红蓝绿装备/宠物不用此值（无耐久不涉维修，且特殊补货对 `add_info` 另有唯一性要求，见坑点 ⑦）。

### ② `letter_id` 必须用 AUTO_INCREMENT，禁止手动 MAX+1

`letter.letter_id` 是自增主键（`postal` 主键是 `postal_id`，`letter_id` 只是外键列）。手动 `MAX+1` 会与游戏原生发信、以及"邮件被领取/删除后 MySQL 5.0 重启把自增重算成 `MAX+1`"撞车 → 同一 `letter_id` 出现两行 `postal` 被游戏**合并成一封信**（代币券邮件混入天价金币）。正解：`INSERT letter` 不指定 `letter_id`，紧接 `LAST_INSERT_ID()` 回写 `postal.letter_id`。

### ③ `postal.gold` 列语义随物品类型变

- 金币邮件：`gold` = 金币、`item_id = 0`。
- 代币券（货币物品，`unlimit_flag=1`）：`gold` = **数量**、`add_info = 0`，靠 `item_id` 区分。在线发代币券走 `ReqDBSendNewSystemMail` 时数量传**第 3 参（gold 位）**，`inven` 只装道具标记 `item_id`、不写 count（曾用 `inven.count` 承载，在线收到数量为 0）。

### ④ 邮件硬约束：正文 ≤ 156 字节，UTF-8，MailDate 0 非无限

- 自定义邮件正文有约 **156 字节**上限，超过则正文整段空白（发件人正常）。脚本文案已压在上限内、超长自动回退单行。
- 中文必须 **UTF-8**：文本编码成 UTF-8 原始字节、经 `UNHEX` 写入 VARBINARY 列，不被连接字符集二次编码。
- `ReqDBSendNewSystemMail` 第 7 参（保留天数）传 0 **并非无限制**（实测），沿用 30 天。

### ⑤ 中文乱码与连接字符集

本服客户端按 **UTF-8** 字节渲染中文，MySQL 默认连接字符集会在读写 `letter`/`postal` 时二次转码、破坏原始字节。要让游戏拿到原始 UTF-8 字节，连接必须**字节透明**：在 MySQL 配置（`my.cnf`）的 `[client]` 段加 `default-character-set=latin1`（单字节透明，所有客户端读写不再二次转码）。**不要**在 `[mysqld]` 下加，MySQL 5.x 启动会报错。脚本侧已把文本以 UTF-8 字节 `UNHEX` 写入 VARBINARY 列、离线发信用 `CONVERT(... USING latin1)` 复刻游戏原生形态，无需改。

> 验证：`SELECT HEX(text) FROM frida.pending_mail ORDER BY id DESC LIMIT 1;` 中文"您"开头应为 `E682A8`（UTF-8）；`C4FA` = GBK（乱码）。

### ⑥ "可交易" ≠ "能上架"：iteminfo 白名单

拍卖行启动时从 `iteminfo.dat` 加载物品定义，**只有 iteminfo 收录的物品才能上架**。补货池只要混进一款不在 iteminfo 的，拍卖行加载其挂单时 `RegistItem` 失败、整服退出（同坑点 ①）。换游戏版本后重新挑补货物品时，记得对照 iteminfo 白名单过滤 `restock_list`。

### ⑦ 特殊补货 = 零桩（称号 / 装扮 / 红蓝绿装备 / 宠物）

这些 `type_token`（`title name` / `creature` / `artifact red|blue|green`）曾被误判"必须建 `charac_info` 桩角色"，实测**全部攻克为零桩**——和普通装备一样挂系统 **90M 假 owner**（`≥id_base`、不在 `charac_info`），**不再建任何桩角色**。加载的真实约束只有两条：

- **add_info**：非宠物（称号/装扮/装备）的 `add_info` 必须**全局唯一**（填 0 重复会崩 `RegistItem`）→ 从高位段 `addinfo_base`（默认 2.1 亿，> 玩家观测上限）`MAX+1` 唯一递增；宠物的 `add_info` = `creature_items.ui_id`（天然唯一）。
- **宠物建实例 + owner 匹配**：宠物须在 `creature_items` 建行（`it_id`=蛋 item_id、`stomach=100`），且挂单 `owner_id` 必须 == 该实例的 `charac_no`（两者同设那个 90M 假号）。`ui_id` 是 AUTO_INCREMENT，**绝不手填**（游戏服在线并发孵宠会撞主键，同 `letter_id` 教训）→ 插入不带 `ui_id`、用 `LAST_INSERT_ID` 当 `add_info`。

owner 密度与普通系统同源，按 `rotate_every`（默认 10）/owner 轮换（每 owner 过密会崩 `RegistItem`，上限类型相关且偏低，10 远低于上限）。回收照常含这几类（`count` 恒 1）。由 `market_agent.restock_special`（`system_owner.special` 块）实现。

### ⑧ charac_no 自增污染 → 零桩根治

（历史经验，零桩已从源头消除此问题，全新部署不涉及。）`charac_info.charac_no` 是 AUTO_INCREMENT，且 **MySQL 5.0 的 InnoDB 不持久化自增计数器、每次 mysqld 重启用 `MAX(charac_no)+1` 重算**。早期建桩方案显式插入高位桩号（如 9900xxxx）把自增顶到高位后，之后新建的真玩家也落高位、与桩交错；只要高位桩行存在，`ALTER AUTO_INCREMENT` 压低也会被下次重启的 `MAX+1` 弹回——`owner_id≥id_base=系统` 阈值会把这些高位真玩家误判成系统。**零桩根治**：不建任何桩 → `MAX(charac_no)` 永远是真玩家 → 自增稳定在玩家低位段；系统全用 90M 假号（不在 `charac_info`）。

> ⚠️ **避坑提示**：若你确实需要直改 `charac_info.charac_no`（如把已污染服的真玩家回迁低位），**必须同步修复 `charac_view.info`**——它是登录大厅"角色选择列表"的缓存，`info` 列是 **zlib 压缩**（MySQL `COMPRESS` 格式）的 blob，内部以 **4 字节小端**存着每个角色的 `charac_no`。直改 `charac_info` 不更新它 → 登录拿到旧号 → `DB_LoadInventory fetch ERROR charac_no=旧号` 无法登录。因为是压缩的，明文/HEX 全库扫描**扫不到**（极隐蔽）。修法：`UPDATE charac_view SET info=COMPRESS(REPLACE(UNCOMPRESS(info), UNHEX(旧号小端hex), UNHEX(新号小端hex))) WHERE m_id=受影响账号;`（读库即生效，无需重启）。零桩新部署不迁号，不会踩此坑。

### ⑨ 多频道：在线投递 + 防双发

多频道时（每个游戏服进程 = 一个频道）**各频道都注入一份 frida、同抢一个 `pending_mail` 队列**，而"在线判断/在线发信/通知"只能作用于**本频道**玩家 → 在线投递天然绑定玩家所在频道。解法 = **两阶段 + 原子认领**：

- 阶段 1（各频道）：只处理"本频道在线"的玩家 → 原子认领后在线发信 + 通知。
- 阶段 2（离线兜底）：`status=0` 且 `created_at` 超过宽限期（须 > 轮询间隔，确保各频道都轮询过、在线频道先认领）= 真离线 → 任一频道认领后离线落库。
- 原子认领：`UPDATE status=1,claimed_by=频道 WHERE id AND status=0`（InnoDB 行锁串行）+ 回读确认 → 杜绝双发；发信失败退回 `status=0` 重试不吞单。`enqueue_mail` 须填 `created_at=NOW()`。

### ⑩ 系统卖家沉金邮件清理

玩家买走系统物品时，游戏服把成交金币**发邮件给系统卖家**（`owner_id≥id_base`），这些卖家永不读取 → `letter`/`postal` 随成交量只增不减膨胀。`_purge_system_seller_mail` 在有实际成交/补货的轮顺手清（纯沉金、删之无经济影响、绝不碰真玩家 `<id_base`；空轮不做以免无谓全表扫）。可用 `system_owner.purge_seller_mail=false` 关闭。

---

## 部署

> 以下为通用流程，不绑定具体容器/调度器/路径。把 `<服务端环境>`、`<部署目录>`、`<frida.js 加载路径>`、库名/服务进程名按你的服务端替换。

### 1. 放置脚本 + vendor pymysql

把 `market_agent.py`、`config.json` 放到部署目录（脚本与 `vendor/` 建议放在重启不丢失的持久化目录）。下载对应 Python 版本的 pymysql 源码，取出纯 Python 包目录放进 `vendor/`：

```bash
# py2.7 → PyMySQL 0.10.x；py3.9+ → PyMySQL 1.x。解压后只需要包目录:
cp -r PyMySQL-x.y.z/pymysql  <部署目录>/vendor/pymysql
python -c "import sys; sys.path.insert(0,'<部署目录>/vendor'); import pymysql; print(pymysql.__version__)"
```

### 2. 配置 `config.json`

填库连接（`mysql`：`unix_socket` 或 `host/port`、最小权限账号 `user/password`、`charset`）、两个市场的停启命令/补货/回收规则、`system_owner`（`id_base`、`special` 零桩块）。建议建一个最小权限库账号而非用 root。

### 3. 初始化表

```bash
python market_agent.py init
```

建好 `pending_mail` / `restock_list` / `cera_consign_list`（并播种 12 种金币包默认值）。

### 4. 导入物品目录（回收依赖，必做一次）

`item_catalog` 是物品静态属性（类型/稀有度/等级）在库里的只读镜像，回收逻辑靠它判定归类：

```bash
mysql --default-character-set=utf8 -u<user> -p <frida库> < item_catalog.sql
```

可重复导入（先清后灌）。只有游戏数据变更才需重新生成，**调回收价不需碰它**。

### 5. 配置补货列表

往 `restock_list` 填要常备补货的物品（字段含义、材料/装备两类换算见 `restock_list_example.sql`），或直接导入现成的全量列表 `restock_list_generated.sql`（开头 `DELETE` + 重灌，可覆盖）。挑编号可参考 `item_tradable_catalog.js`。

### 6. 合并 frida 消费者 + 冒烟测试

项目根的 `frida.js` 已把消费者模块合并好（追加模块 + 在 `init_db()` 末尾加一行 `auction_module_init();`）。放到游戏服实际加载的 frida.js 路径，让游戏重新加载。冒烟测试：

```sql
INSERT INTO frida.pending_mail (auction_id,charac_no,title,text,gold,status,created_at)
VALUES (NULL, <你的角色charac_no>, '拍卖行', '测试金币邮件', 12345, 0, NOW());
```

一个轮询周期内该行 `status` 应变 1，角色收到 12345 金币（在线弹窗 / 离线上线可见）。

### 7. 先手动跑一轮，再上定时器

```bash
python market_agent.py once     # 跑一轮即退出，观察日志四步正常、服务进程结束后确实重启
```

确认无误后，用**外部定时器**（cron 或任意调度器，独立于游戏进程生命周期、最抗重启）每整点跑一次 `once`：

```cron
0 * * * *  cd <部署目录> && python market_agent.py once >> market_agent.log 2>&1
```

> `frida.js` 会在每个整点前一分钟广播"即将重启"，与整点 cron 对齐。**别用无参模式**（那是常驻循环 daemon，会持锁挡住 cron）。

### 命令一览

| 命令                      | 作用                                                                                                                                                                           |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `market_agent.py init`    | 建表（首次）。不加锁。                                                                                                                                                         |
| `market_agent.py once`    | 跑一轮即退出（cron 用这个）。                                                                                                                                                  |
| `market_agent.py reset`   | 清空各市场系统挂单后**全量重补**（改了 config / 补货列表的价/量后用；普通 `once` 只补缺口、存量不更新）。`reset` 会让服务重启加载全部挂单、期间打不开，只手动用、别放进 cron。 |
| `market_agent.py`（无参） | 常驻循环 daemon（不推荐，会持锁）。                                                                                                                                            |

---

## config.json 速览

| 块                         | 说明                                                                                                                                                                          |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `mysql`                    | 库连接：`unix_socket` 或 `host/port`、`user/password`、`charset`、`frida_db`。                                                                                                |
| `interval`                 | 常驻循环的轮间隔秒（cron 模式不用）。                                                                                                                                         |
| `mail_encoding`            | 邮件文本编码，固定 `utf-8`（配合 `my.cnf [client]=latin1`，见坑点 ⑤；**切勿改 gbk**）。                                                                                       |
| `markets.<名>`             | 每个市场：`enabled`、`label`、`db`、`stop_cmd`/`start_cmd`、`restock`（mode/list/定价）、`recycle`（mode/规则/费率/押金）。                                                   |
| `system_owner`             | `id_base`（系统/玩家边界）、`rotate_every`（每 owner 件数上限，别 ≥15）、`special`（零桩块：`addinfo_base`/`tokens`/`creature_tokens`/`drop_tokens`/`charac_db`/`game_db`）。 |
| `vendor_path` / `log_path` | pymysql 包目录 / 日志文件路径，**按你的实际部署目录修改**（示例值为 `/data/auction/...`）；省略这两项则用默认 = 脚本同目录的 `vendor/` 与 `market_agent.log`。                |

回收规则（`markets.<名>.recycle`）语义是**单价上限**：玩家挂单单价 ≤ 上限即按挂单价回收，没命中任何规则 = 不回收。优先级 `item_overrides`（按物品编号）> `special_by_kind_type` > 按稀有度（`equipment_by_rarity` / `material_by_rarity`）。

---

## ⚠️ 移植须知（不同版本必读）

- **物品列表仅供参考，禁止完全套用**：`item_catalog.sql`、`item_tradable_catalog.js`、`restock_list_generated.sql` 里的物品列表是按 **清风 1031 版本**、还原所有旧装备 / 真紫装备整理而来，**仅供参考**。不同游戏版本的物品并不一样，必须**按你自己的版本重新整理**，切勿照搬。
  - 整理时务必保证**四处 `iteminfo.dat` 内容一致**：① 服务端 pvf 内的 `iteminfo.dat`、② 客户端 pvf 内的 `iteminfo.dat`、③ `/home/neople/auction` 内的 `iteminfo.dat`、④ `/home/neople/point` 内的 `iteminfo.dat`。四者不一致会导致上架/加载异常（参见上文坑点 ⑥ iteminfo 白名单）。
- **`frida.js` 因版本而异**：不同版本游戏服的 `frida.js` 并不一致，本仓库这份是在特定版本上合并的。其中做市消费者模块的**关键逻辑位于第 2414–2688 行**——移植到你的版本时只参考这一段、对照你自己的 `frida.js` 自行修改接入，**不要整文件覆盖**。

---

## 文件清单

| 文件                         | 说明                                                                                               |
| ---------------------------- | -------------------------------------------------------------------------------------------------- |
| `market_agent.py`            | 多市场编排脚本（py2.7 / py3.9+ 双兼容，pymysql；拍卖行 + 金币寄售；特殊补货零桩）。                |
| `config.json`                | 配置（库连接 / `markets` 多市场 / `system_owner.special` 零桩块）。仓库内密码用 `CHANGE_ME` 占位。 |
| `frida.js`                   | 已合并消费者模块的 frida 脚本（金币邮件 + 代币券物品邮件 + 多频道防双发），部署这份。              |
| `restock_list_example.sql`   | 拍卖行补货列表字段说明 + 材料/装备填写实例。                                                       |
| `restock_list_generated.sql` | 全量补货列表（`DELETE` 全表 + 重灌，可覆盖）。                                                     |
| `item_catalog.sql`           | 物品静态属性目录（回收判型/判稀有度依赖，可重复导入）。                                            |
| `item_tradable_catalog.js`   | 按分类折叠的可交易物品清单（挑补货白名单用）。                                                     |
| `vendor/pymysql`             | 纯 Python 驱动（py2.7→0.10.x / py3.9+→1.x）。                                                      |
| `LICENSE`                    | GNU General Public License v3.0 全文。                                                             |

---

## 安全提醒

- 给脚本建**最小权限**库账号，别在 `config.json` 里放 root。
- 若 MySQL 监听公网端口，务必绑 `127.0.0.1` 或用防火墙挡掉，并收紧 root 的 host / 改强密码。
- 部署/手工改库前，先停定时器再动库——否则它可能用旧代码重建状态、冲掉你的手工操作。

---

## License

GNU General Public License v3.0 or later（GPL-3.0-or-later）。详见 [LICENSE](LICENSE)。
