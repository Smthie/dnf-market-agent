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

## 🚀 快速上手（只想用它，不用看原理）

**日常你只需碰三样**：

| 想做什么 | 动哪里 | 改完怎么办 |
|---|---|---|
| 改回收价 / 开关 | `config.json` 的 `markets.<市场>.recycle` | 下一轮自动生效 |
| 改补货品项 / 补货价 | `restock_list` 表（拍卖行） | 改价后跑一次 `python market_agent.py reset` 全量重建 |
| 看有没有正常跑 | `market_agent.log` | 搜 `回收 N 件` / `补货 N 件` / `探测异常` |

**部署就 5 步**（细节见下文「部署」）：
1. 把 `market_agent.py`、`config.json`、`vendor/pymysql` 放到服务器。
2. 改 `config.json`：数据库账号密码、两个服务的停启命令。
3. `python market_agent.py init` 建表。
4. 导入 `item_catalog.sql`（回收判价用）。
5. `python market_agent.py once` 试跑一轮 → 确认正常后挂 cron 每整点跑。

---

## 运行环境与依赖

- **Python**：**2.7** 或 **3.9+**（同一份脚本双兼容）；依赖仅标准库 + **vendored pymysql**（纯 Python，拷目录即用，版本对应见部署第 1 步）。
- **平台**：仅 **Linux/Unix**——用了 `fcntl` 文件锁与 `os.setsid`，Windows 不可运行。
- **数据库**：MySQL（开发实测于 5.0）。

---

## 部署（照着手册做，每步都有验证）

> 前提：服务器是 Linux，MySQL 已装好，游戏服务端（含拍卖行 / 金币寄售两个服务）已能正常运行。
> 以下以部署目录 `/data/auction` 为例，所有 `<...>` 按你的实际环境替换。

### 第 0 步：动手前先确认 3 件事

| 确认项 | 怎么确认 |
| --- | --- |
| Python 版本 | `python --version`（2.7 或 3.9+ 都可以） |
| 两个服务怎么停 / 启 | 手动停启一次拍卖行服务，记下命令，第 3 步填进 config |
| MySQL 连接方式 | 本机连库用 socket 还是 host/port；知道各库名（frida、拍卖行、charac） |

### 第 1 步：放脚本 + vendor pymysql

```bash
mkdir -p /data/auction/vendor
cp market_agent.py config.json /data/auction/
# 下载对应 Python 版本的 PyMySQL 源码（py2.7→0.10.x，py3.9+→1.x），解压后只拷包目录:
cp -r /path/to/PyMySQL-x.y.z/pymysql  /data/auction/vendor/pymysql
```

**验证**：

```bash
cd /data/auction
python -c "import sys; sys.path.insert(0,'vendor'); import pymysql; print(pymysql.__version__)"
```

能打印版本号 = OK；报 `ModuleNotFoundError` = vendor 路径不对，检查 `vendor/pymysql/__init__.py` 是否存在。

### 第 2 步：建最小权限 MySQL 账号（推荐，别用 root）

```sql
CREATE USER 'auction_bot'@'localhost' IDENTIFIED BY '换成你的密码';
GRANT SELECT,INSERT,UPDATE,DELETE,CREATE ON frida.* TO 'auction_bot'@'localhost';                          -- 脚本自己的库: 建表/读写
GRANT SELECT ON taiwan_cain.*            TO 'auction_bot'@'localhost';                                     -- charac 库: 只读, 判系统/玩家
GRANT SELECT,DELETE ON taiwan_cain_2nd.* TO 'auction_bot'@'localhost';                                    -- game 库: 宠物回收要删 creature_items
GRANT SELECT,INSERT,UPDATE,DELETE ON taiwan_cain_auction_gold.* TO 'auction_bot'@'localhost';              -- 拍卖行挂单库
GRANT SELECT,INSERT,UPDATE,DELETE ON taiwan_cain_auction_cera.* TO 'auction_bot'@'localhost';              -- 金币寄售挂单库
FLUSH PRIVILEGES;
```

> 库名按你服的实际情况替换。game 库只读会报权限，但宠物删除有 try/except 兜底（只告警不阻断），仍建议给 DELETE。

### 第 3 步：改 config.json（必改 4 处）

打开 `config.json`，只改这 4 处，其余保持默认：

| 位置 | 改成什么 |
| --- | --- |
| `mysql.user` / `mysql.password` | 第 2 步建的账号 |
| `mysql.unix_socket` 或 `mysql.host/port` | 本机实际连接方式（unix_socket 优先，另一组可留作注释） |
| `vendor_path` / `log_path` | `/data/auction/vendor` / `/data/auction/market_agent.log` |
| `markets.auction.stop_cmd` / `start_cmd`、`markets.cera.stop_cmd` / `start_cmd` | 第 0 步记下的真实停启命令 |

> 回收价/补货价不是部署项（那是上线后调优）；`system_owner.id_base=90000001` 一般不动（仅当你服有玩家 charac_no 大于它时才改大）。

**验证**（config 是合法 JSON）：

```bash
python -c "import json; json.load(open('config.json')); print('config ok')"
```

### 第 4 步：初始化表

```bash
cd /data/auction
python market_agent.py init
```

**验证**：

```bash
mysql -uauction_bot -p frida -e "SHOW TABLES;"
```

应看到 `pending_mail` / `restock_list` / `cera_consign_list` 三张表。
卡住：报权限错误 → 回第 2 步补 GRANT；报连接失败 → 回第 3 步检查连接配置。

### 第 5 步：导入物品目录 + 参考价表（必做）

三个文件连导（都自带建表/清空，可重复执行）：

```bash
mysql --default-character-set=utf8 -uauction_bot -p frida < item_catalog.sql           # 物品属性目录(回收判型/稀有度)
mysql --default-character-set=utf8 -uauction_bot -p frida < item_ref_price.sql          # 参考价表(回收概率判价数据源)
mysql --default-character-set=utf8 -uauction_bot -p frida < restock_price_from_ref.sql  # 补货价批量替换为参考价(未命中保持原价)
```

> `item_ref_price.sql` / `restock_price_from_ref.sql` 由 `gen_ref_prices.py` 从分享工程价格表（items.csv）生成；换价格表后重跑生成再导。

**验证**：

```bash
mysql -uauction_bot -p frida -e "SELECT COUNT(*) FROM item_catalog; SELECT COUNT(*) FROM item_ref_price;"
```

应返回 `26543`（目录条数）和 `5097`（参考价条数，随价格表版本变）。

### 第 6 步：配置补货列表

两个选择：

- **直接导入现成全量列表**（省事；物品按清风 1031 版本整理，换版本务必自己重挑）：

  ```bash
  mysql -uauction_bot -p frida < restock_list_generated.sql
  mysql -uauction_bot -p frida -e "SELECT COUNT(*) FROM restock_list;"   # 应返回 6521
  ```

- **自己按版本挑**：参考 `restock_list_example.sql`（字段含义）和 `item_tradable_catalog.js`（按分类折叠的可交易清单），逐条 INSERT。

### 第 7 步：合并 frida 消费者 + 冒烟测试

本仓库根的 `frida.js` 已合并好消费者模块（在 `init_db()` 末尾加了 `auction_module_init();`），直接覆盖到游戏服实际加载的 frida.js 路径，让游戏重新加载。

**冒烟测试**（验证发信链路通）：

```sql
INSERT INTO frida.pending_mail (auction_id,charac_no,title,text,gold,status,created_at)
VALUES (NULL, <你的角色charac_no>, '拍卖行', '测试金币邮件', 12345, 0, NOW());
```

等一个轮询周期（默认几秒），然后：

```sql
SELECT id, status, claimed_by FROM frida.pending_mail ORDER BY id DESC LIMIT 3;
```

你的角色应收到 12345 金币（在线弹窗 / 离线上线可见），该行 `status` 应变 `1`。
卡住：status 一直是 0 → frida.js 没被加载或 `auction_module_init()` 没执行；邮件乱码 → （my.cnf `[client]` 加 `default-character-set=latin1`）。

### 第 8 步：试跑一轮 → 上定时器

```bash
cd /data/auction
python market_agent.py once
sleep 5 && tail -30 market_agent.log
```

首轮预期日志：每个市场 `[1/4] 关闭 → [2/4] 回收 → [3/4] 补货 → [4/4] 开启`，服务进程结束并确实重启。之后轮次大多显示 `无待回收/补货, 跳过(不停启服务)` 或 `[在线] 回收(不停服...)`。

确认无误后挂 cron（每整点跑一次）：

```cron
0 * * * *  cd /data/auction && python market_agent.py once >> market_agent.log 2>&1
```

> `frida.js` 会在整点前一分钟广播“即将重启”，与整点 cron 对齐。**别用无参模式**（常驻 daemon，会持锁挡住 cron）。

### 命令一览

| 命令                      | 作用                                                                                                                                                                           |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `market_agent.py init`    | 建表（首次）。不加锁。                                                                                                                                                         |
| `market_agent.py once`    | 跑一轮即退出（cron 用这个）。                                                                                                                                                  |
| `market_agent.py reset`   | 清空各市场系统挂单后**全量重补**（改了 config / 补货列表的价/量后用；普通 `once` 只补缺口、存量不更新）。`reset` 会让服务重启加载全部挂单、期间打不开，只手动用、别放进 cron。 |
| `market_agent.py`（无参） | 常驻循环 daemon（不推荐，会持锁）。                                                                                                                                            |

---

### 排错速查

| 现象（日志关键字） | 原因 | 处理 |
| --- | --- | --- |
| `回收探测异常` / `补货探测异常` + `1146` | 表没建 / 没导入 | 回第 4 / 5 步 |
| `1142` 或 `permission denied` | 账号权限不足 | 回第 2 步补 GRANT |
| `命令失败`（停/启） | stop/start 命令写错 | 回第 3 步，先手动验证命令本身能停能启 |
| 邮件乱码 | 连接字符集二次转码 | my.cnf `[client]` 加 `default-character-set=latin1`（别放 `[mysqld]`） |
| 每轮都停启（无探测异常） | 回收限价太宽，总有玩家单命中 | 调 `recycle` 限价，或先 `recycle.online=false` 观察 |
| 补货后拍卖行崩 / 起不来 | 补货池混进不在 iteminfo 的物品 / add_info 非法 | 对照 iteminfo 白名单，删非法条目后 `reset` |
| 玩家买系统物品后维修即消失 | 普通装备 add_info 填了 0 | 应填 999999998 |

---

## config.json 速览

| 块                         | 说明                                                                                                                                                                          |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `mysql`                    | 库连接：`unix_socket` 或 `host/port`、`user/password`、`charset`、`frida_db`。                                                                                                |
| `interval`                 | 常驻循环的轮间隔秒（cron 模式不用）。                                                                                                                                         |
| `mail_encoding`            | 邮件文本编码，固定 `utf-8`（配合 `my.cnf [client]=latin1`；**切勿改 gbk**）。                                                                                       |
| `markets.<名>`             | 每个市场：`enabled`、`label`、`db`、`stop_cmd`/`start_cmd`、`restock`（mode/list/卖家心态 seller_mind + 饱和度 + 价格下限）、`recycle`（mode/规则/费率/押金/`pricing` 参考价公式）。                                                   |
| `system_owner`             | `id_base`（系统/玩家边界）、`rotate_every`（每 owner 件数上限，别 ≥15）、`special`（零桩块：`addinfo_base`/`tokens`/`creature_tokens`/`drop_tokens`/`charac_db`/`game_db`）。 |
| `vendor_path` / `log_path` | pymysql 包目录 / 日志文件路径，**按你的实际部署目录修改**（示例值为 `/data/auction/...`）；省略这两项则用默认 = 脚本同目录的 `vendor/` 与 `market_agent.log`。                |

回收规则（`markets.<名>.recycle`）是**双轨**：

- **主判定（参考分享工程）**：JOIN `item_ref_price` 取 per-item 基准价算 `P_std`（材料/消耗品=基准价；白蓝装 `A(n)×基准价+B(n)`；紫粉装 `基准价+BaseCost(n)×(1+√(基准价/scale))`；`upgrade<10` 视为白板=基准价），再按 `R=玩家单价/P_std` **概率回收**：`R≤0.5` 必收、`0.5–0.9` 线性降、`0.9–1.1` 收 50%、`1.1–1.5` 再降、`>1.5` 不收——只收便宜到离谱的单，不吃满仓。
- **回退（csv 没价的物品）**：`item_ref_price` 未导入 / 物品无参考价 → 旧规则必收上限，优先级 `item_overrides` > `special_by_kind_type` > 按稀有度（`equipment_by_rarity` / `material_by_rarity`）。

系数在 `recycle.pricing`（`scale` / `white_blue_coeff` / `purple_pink_base_cost`，来自分享工程 config.yaml）。

---

## ⚠️ 移植须知（不同版本必读）

- **物品列表仅供参考，禁止完全套用**：`item_catalog.sql`、`item_tradable_catalog.js`、`restock_list_generated.sql` 里的物品列表是按 **清风 1031 版本**、还原所有旧装备 / 真紫装备整理而来，**仅供参考**。不同游戏版本的物品并不一样，必须**按你自己的版本重新整理**，切勿照搬。
  - 整理时务必保证**四处 `iteminfo.dat` 内容一致**：① 服务端 pvf 内的 `iteminfo.dat`、② 客户端 pvf 内的 `iteminfo.dat`、③ `/home/neople/auction` 内的 `iteminfo.dat`、④ `/home/neople/point` 内的 `iteminfo.dat`。四者不一致会导致上架/加载异常。
- **`frida.js` 因版本而异**：不同版本游戏服的 `frida.js` 并不一致，本仓库这份是在特定版本上合并的。其中做市消费者模块的**关键逻辑位于第 2414–2688 行**——移植到你的版本时只参考这一段、对照你自己的 `frida.js` 自行修改接入，**不要整文件覆盖**。

---

## 文件清单

| 件                         | 说明                                                                                               |
| -------------------------- | -------------------------------------------------------------------------------------------------- |
| `market_agent.py`          | 多市场编排脚本（py2.7 / py3.9+ 双兼容，pymysql；拍卖行 + 金币寄售；特殊补货零桩）。                |
| `config.json`              | 配置（库连接 / `markets` 多市场 / `system_owner.special` 零桩块）。仓库内密码用 `CHANGE_ME` 占位。 |
| `frida.js`                 | 已合并消费者模块的 frida 脚本（金币邮件 + 代币券物品邮件 + 多频道防双发），部署这份。              |
| `restock_list_example.sql` | 拍卖行补货列表字段说明 + 材料/装备填写实例。                                                       |
| `restock_list_generated.sql` | 全量补货列表（`DELETE` 全表 + 重灌，可覆盖）。                                                   |
| `item_catalog.sql`         | 物品静态属性目录（回收判型/判稀有度依赖，可重复导入）。                                            |
| `item_ref_price.sql`       | 参考价表（5097 条，回收概率判价数据源；由 `gen_ref_prices.py` 生成）。                              |
| `restock_price_from_ref.sql` | 补货价批量替换为参考价（命中才改，未命中保持原价；由 `gen_ref_prices.py` 生成）。                |
| `gen_ref_prices.py`        | 从分享工程价格表（items.csv）生成上面两个 SQL。                                                     |
| `item_tradable_catalog.js` | 按分类折叠的可交易物品清单（挑补货白名单用）。                                                     |
| `vendor/pymysql`           | 纯 Python 驱动（py2.7→0.10.x / py3.9+→1.x）。                                                      |
| `LICENSE`                  | GNU General Public License v3.0 全文。                                                             |

**库表**（都在 frida 库）：`restock_list` 补货清单（材料/稀有/神器/消耗品/称号/宠物/装备/装扮）、`item_catalog` 物品属性目录（回收判型/稀有度）、`item_ref_price` 参考价表（回收 P_std 基准）、`cera_consign_list` 金币寄售补货+回收配置、`pending_mail` 邮件队列（幂等键 `(market, auction_id, occ_time)`）。

**发信链路**：`market_agent` **先入队后删单**（幂等：崩在任意一步都不重复打款也不吞单）→ `frida.js` 轮询 `pending_mail`、调游戏原生发信函数投递。

---

## 安全提醒

- 给脚本建**最小权限**库账号，别在 `config.json` 里放 root。
- 若 MySQL 监听公网端口，务必绑 `127.0.0.1` 或用防火墙挡掉，并收紧 root 的 host / 改强密码。
- 部署/手工改库前，先停定时器再动库——否则它可能用旧代码重建状态、冲掉你的手工操作。

---

## License

GNU General Public License v3.0 or later（GPL-3.0-or-later）。详见 [LICENSE](LICENSE)。
