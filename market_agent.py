#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
多市场自动做市主控脚本(market_agent.py) —— Python 2.7 / 3.9+ 双兼容(配 pymysql 0.10.x / 1.x)
================================================================================
对每个启用的市场, 每 interval 秒一轮:  停服务 -> 回收玩家低价单 -> 系统补货 -> 启服务
当前两个市场(config.json 的 markets):
  - auction(拍卖行): 物品⇄金币. 库 gold, 服务 df_auction_r. 补货按 restock_list,
    回收 JOIN item_catalog + 规则定价, 发金币邮件(手续费5%/押金10000必退).
  - cera(金币寄售): 金币包⇄代币券. 库 cera, 服务 df_point_r. 补货按 cera_consign_list,
    回收按 instant_price<=阈值, 发代币券物品邮件(item 2681762, 手续费2%/无押金).

发信走 frida.pending_mail(市场用 market 列区分, item_id=0 金币 / 非0 物品),
由游戏进程内的 frida.js 消费者实际投递. 幂等键 (market, auction_id, occ_time):
INSERT IGNORE + "先入队后删单", 崩在任意一步都不重复打款也不吞单.

安全要点(沿用拍卖行实测加固):
  1. 玩家/系统挂单 owner_type 同为 1, 用 owner_id 边界区分(< id_base = 玩家).
  2. stop 命令失败即中止本市场本轮(绝不在服务进程仍存活时改库).
  3. run_once_market 用 try/finally 保证服务一定重新开启.
  4. 系统补货 owner_id 按现有占用装箱(规避单 owner 上架数上限, 跨轮不超限).
依赖: 纯 Python 版 pymysql(vendor 进 /data, 见 README).
用法: python market_agent.py [init|once]   (无参数=定时循环)
"""
from __future__ import print_function
import os
import sys
import json
import time
import random
import logging
import binascii
import subprocess
import fcntl

# ---- 1. 载入 vendor 的 pymysql ----
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_config():
    path = os.path.join(SCRIPT_DIR, "config.json")
    with open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


CFG = _load_config()
VENDOR = CFG.get("vendor_path") or os.path.join(SCRIPT_DIR, "vendor")
if VENDOR and os.path.isdir(VENDOR) and VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)

try:
    import pymysql
except ImportError:
    sys.stderr.write(
        "[FATAL] 未找到 pymysql. 请把纯 Python 版 pymysql 放到 %s (见 README 步骤一).\n" % VENDOR
    )
    raise

MYSQL = CFG["mysql"]
SYS = CFG["system_owner"]
MARKETS = CFG["markets"]
INTERVAL = int(CFG.get("interval", 3600))
LOG_PATH = CFG.get("log_path") or os.path.join(SCRIPT_DIR, "market_agent.log")
MAIL_ENCODING = CFG.get("mail_encoding", "utf-8")

SYS_OWNER_TYPE = int(SYS.get("owner_type", 1))
ID_BASE = int(SYS.get("id_base", 90000001))               # owner_id 边界; ★必须 > 任何真实玩家 charac_no★
NEXON_BASE = int(SYS.get("nexon_base", 18000000))
ROTATE_EVERY = int(SYS.get("rotate_every", 10))           # 每多少件轮换一次 owner_id

FRIDA_DB = MYSQL["frida_db"]

# ---- 特殊类(称号/装扮/红蓝绿装备/宠物)补货配置 ----
# 全挂普通系统 90M 假 owner 零桩补货(加载硬条件/历史误诊详见下方 SP_TOKENS 处 ★零桩★ 注释).
SPECIAL = SYS.get("special", {})
SP_ENABLED = bool(SPECIAL.get("enabled", False))
SP_CHARAC_DB = SPECIAL.get("charac_db", "taiwan_cain")      # charac_info 所在库(系统/玩家判据用)
SP_GAME_DB = SPECIAL.get("game_db", "taiwan_cain_2nd")      # creature_items 所在库(宠物实例)
SP_ADDINFO_BASE = int(SPECIAL.get("addinfo_base", 2100000000))  # 非宠物特殊 add_info 高位段起点(>玩家观测上限, <int上限21.47亿)
# ★零桩★: 称号/装扮/红蓝绿装备/宠物全挂普通系统 90M 假 owner(同普通补货, 不再建 charac_info 桩角色).
#   实测加载: 这些类型 owner 无须真实存在于 charac_info; 但 add_info 有讲究——
#   装扮/装备(artifact red/blue/green)的 add_info 必须【全局唯一】(=0 重复会崩RegistItem), 称号宽容;
#   故非宠物统一走高位段唯一递增. 宠物(creature)须 auction.owner_id==creature_items.charac_no(同为该90M假号), add_info=ui_id.
#   (注: artifact red 曾被误判"需桩", 实为早期测试克隆了称号的非法列值; 用 restock_special 同一列方案[seal=1/endur=0/高位add_info]即正常零桩.)
SP_TOKENS = set(SPECIAL.get("tokens",
    ["title name", "creature", "artifact red", "artifact blue", "artifact green"]))
SP_CREATURE_TOKENS = set(SPECIAL.get("creature_tokens", ["creature"]))
SP_DROP_TOKENS = set(SPECIAL.get("drop_tokens", []))   # 完全不补货的 type_token(catalog/special 都跳过); 实测各特殊类型均零桩可上架 -> 默认空, 仅作禁补开关备用

# 系统卖家成交收款邮件清理: 玩家买系统物品时游戏服把金币发邮件给系统卖家(owner_id>=ID_BASE),
# 系统虚拟卖家永不读取, letter/postal 只增不减 -> 有活的轮顺手清(纯沉金邮件, 删之无经济影响).
PURGE_SELLER_MAIL = bool(SYS.get("purge_seller_mail", True))
SELLER_MAIL_DB = SYS.get("seller_mail_db", SP_GAME_DB)        # letter/postal 所在库(默认同 game_db)


# ---- 系统/玩家判据(m_id 区分, 兜底历史污染) ----
# 零桩后系统 owner 全是 90M 假号(不在 charac_info), 真玩家在低位(<ID_BASE), 正常 owner>=ID_BASE 即系统.
# 但历史上建桩曾把 charac_info 自增顶高、令个别真玩家 charac_no 落到 >=ID_BASE 高位(回迁前可能残留);
# 故判据仍以 m_id 兜底: 系统 = owner>=ID_BASE 且 charac_info 无"该 charac_no 的真玩家(m_id<>0)"行;
#   玩家 = owner<ID_BASE 或 是真玩家(m_id<>0). 相关子查询(PK 点查), SELECT/DELETE/UPDATE 通用.
def _sys_owner_where(col):
    return ("%s >= %d AND NOT EXISTS (SELECT 1 FROM %s.charac_info _ci "
            "WHERE _ci.charac_no=%s AND _ci.m_id<>0)") % (col, ID_BASE, SP_CHARAC_DB, col)


def _player_owner_where(col):
    return ("(%s < %d OR EXISTS (SELECT 1 FROM %s.charac_info _ci "
            "WHERE _ci.charac_no=%s AND _ci.m_id<>0))") % (col, ID_BASE, SP_CHARAC_DB, col)

# ---- 2. 日志 ----
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("market")
try:
    _fh = logging.FileHandler(LOG_PATH)
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(_fh)
except Exception as e:
    log.warning("无法写日志文件 %s: %s", LOG_PATH, e)


def _u(s):
    if isinstance(s, bytes):
        return s.decode("utf-8", "replace")
    return s


# ---- 3. 数据库连接 ----
def get_conn(db_name):
    kw = dict(user=MYSQL["user"], password=MYSQL["password"],
              database=db_name, charset=MYSQL.get("charset", "utf8"), autocommit=False)
    sock = MYSQL.get("unix_socket")
    if sock:
        kw["unix_socket"] = sock
    else:
        kw["host"] = MYSQL.get("host", "127.0.0.1")
        kw["port"] = int(MYSQL.get("port", 3306))
    return pymysql.connect(**kw)


# ---- 4. 命令执行(停/启服务进程) ----
def run_local(cmd, detach=False):
    if not cmd:
        return None
    if detach:
        subprocess.Popen(cmd, shell=True, stdout=open(os.devnull, "w"),
                         stderr=subprocess.STDOUT, stdin=open(os.devnull, "r"),
                         close_fds=True, preexec_fn=os.setsid)
        return None
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate()
    if p.returncode != 0:
        log.error("命令失败(code=%d): %s", p.returncode, cmd)
        if out:
            log.error("stdout: %s", _u(out).strip())
        if err:
            log.error("stderr: %s", _u(err).strip())
        raise RuntimeError("命令执行失败: %s" % cmd)
    return out


def stop_market(m):
    if m.get("stop_cmd"):
        log.info(u"[1/4] 关闭 %s 服务...", m["label"])
        run_local(m["stop_cmd"])               # 失败 raise -> 本市场本轮中止改库(关键安全点)
    else:
        log.info(u"[1/4] 跳过关闭(未配置 stop_cmd)")


def start_market(m):
    if m.get("start_cmd"):
        log.info(u"[4/4] 开启 %s 服务...", m["label"])
        run_local(m["start_cmd"], detach=True)
    else:
        log.info(u"[4/4] 跳过开启(未配置 start_cmd)")


# ---- 5. 邮件入队(两市场共用; item_id=0 金币邮件, 非0 物品邮件如代币券) ----
def enqueue_mail(cur_f, conn_frida, market, auction_id, occ_time, charac_no, title, text, gold, item_id=0):
    # 客户端要原始 UTF-8 字节: 用 UNHEX(纯 ASCII 十六进制, 与连接字符集无关)写入 VARBINARY 列.
    title_hex = binascii.hexlify(title.encode(MAIL_ENCODING, "replace")).decode("ascii")
    text_hex = binascii.hexlify(text.encode(MAIL_ENCODING, "replace")).decode("ascii")
    # created_at=NOW(): frida 多频道离线兜底靠它判定"超过宽限期、各频道都没在线认领" -> 必须填
    cur_f.execute(
        "INSERT IGNORE INTO pending_mail (market, auction_id, occ_time, charac_no, title, text, gold, item_id, status, created_at) "
        "VALUES (%s, %s, %s, %s, UNHEX(%s), UNHEX(%s), %s, %s, 0, NOW())",
        (market, auction_id, occ_time, charac_no, title_hex, text_hex, gold, item_id),
    )
    conn_frida.commit()


# ---- 6. 回收: 拍卖行(catalog_rules, 发金币邮件) ----
def _recycle_price_limit(rc, item_id, kind, token, rarity):
    """(fallback) 物品回收单价上限; None=不回收. 优先级: 单品覆盖 > kind:token 特例 > 装备/材料按稀有度."""
    ov = rc.get("item_overrides") or {}
    if str(item_id) in ov:
        return int(ov[str(item_id)])
    sp = rc.get("special_by_kind_type") or {}
    key = "%s:%s" % (kind, token)
    if key in sp:
        return int(sp[key])
    if rarity is None:
        return None
    if kind == "equipment":
        mm = rc.get("equipment_by_rarity") or {}
        return int(mm[str(rarity)]) if str(rarity) in mm else None
    if token in (rc.get("material_tokens") or []):
        mm = rc.get("material_by_rarity") or {}
        return int(mm[str(rarity)]) if str(rarity) in mm else None
    return None


def _pstd_price(pc, kind, rarity, ref_price, upgrade):
    """分享工程 P_std 标准价(参考): 材料/消耗品=价格表单价; 白蓝装=A(n)*shop+B(n); 紫粉装=P_base+BaseCost(n)*(1+sqrt(P_base/scale)).
    ref_price 充当 shop_price / P_base. upgrade<10 视为白板(直接用 ref_price); n>18 取 18 档. 返回 None = 不走公式(史诗/无系数)."""
    if ref_price is None:
        return None
    if kind != "equipment":
        return int(ref_price)                     # 材料/消耗品: 不参与强化
    n = min(int(upgrade or 0), 18)
    if n < 10:
        return int(ref_price)                     # 白板/低强化: 基础价
    if rarity in (0, 1):                          # 白/蓝装
        coeff = (pc.get("white_blue_coeff") or {}).get(str(n))
        if not coeff:
            return None
        return int(coeff["A"] * ref_price + coeff["B"])
    if rarity in (2, 3):                          # 紫/粉装
        cost = (pc.get("purple_pink_base_cost") or {}).get(str(n))
        if cost is None:
            return None
        scale = float(pc.get("scale", 150000))
        return int(ref_price + cost * (1.0 + (ref_price / scale) ** 0.5))
    return None                                  # 史诗等: 不走公式, 走 fallback


def _recycle_prob(r):
    """分享工程回收概率: R=玩家单价/P_std. <=0.5 必收; 0.5-0.9 线性降; 0.9-1.1 50%; 1.1-1.5 再降; >1.5 不收."""
    if r <= 0.5:
        return 1.0
    if r < 0.9:
        return 1.0 - 0.7 * (r - 0.5) / 0.4
    if r <= 1.1:
        return 0.5
    if r <= 1.5:
        return 0.5 * (1.0 - (r - 1.1) / 0.4)
    return 0.0


def recycle_catalog_rules(m, conn_mk, conn_frida):
    rc = m["recycle"]
    pc = rc.get("pricing") or {}
    fee_rate = float(rc.get("fee_rate", 0.05))
    deposit = int(rc.get("deposit", 10000))
    cur_a = conn_mk.cursor()
    cur_f = conn_frida.cursor()
    # 玩家挂单 JOIN item_catalog 取类型/稀有度 + item_ref_price 取参考价(P_std 基准); unit_price=0 为纯竞拍单不收.
    # item_ref_price 不存在/为空(未导入 csv 参考价) -> 降级为无参考价版本, 全部走旧规则(fallback), 回收不中断.
    has_ref = True
    try:
        cur_a.execute("SELECT COUNT(*) FROM " + FRIDA_DB + ".item_ref_price")
        if cur_a.fetchone()[0] == 0:
            has_ref = False
    except Exception:
        has_ref = False
    # 无 csv 价(item_ref_price 未命中)的物品: 回退 restock_list.system_price(= 补货基准), 与补货基准价保持一致
    join_ref = (" LEFT JOIN " + FRIDA_DB + ".item_ref_price rp ON a.item_id = rp.item_id "
                " LEFT JOIN " + FRIDA_DB + ".restock_list rs ON a.item_id = rs.item_id ") if has_ref else ""
    sel = ("a.auction_id, a.owner_id, a.occ_time, a.add_info, "
           "a.unit_price, a.item_id, a.upgrade, c.cname, c.kind, c.type_token, c.rarity"
           + (", rp.price, rs.system_price" if has_ref else ""))
    try:
        cur_a.execute(
            "SELECT " + sel + " FROM auction_main a "
            "INNER JOIN " + FRIDA_DB + ".item_catalog c ON a.item_id = c.item_id "
            + join_ref +
            "WHERE " + _player_owner_where("a.owner_id") + " AND a.unit_price > 0"
        )
    except Exception as e:
        code = e.args[0] if getattr(e, "args", None) else 0
        if code == 1146:
            log.error(u"  frida.item_catalog 不存在, 跳过回收 —— 请先导入 item_catalog.sql(见 README)")
            return
        raise
    rows = cur_a.fetchall()
    processed = 0
    cre_del = []            # 被回收的宠物 ui_id(=add_info); 末尾批量销毁 creature_items 实例
    SANE_MAX = 2000000000   # 单笔回收金额硬上限(20亿, 低于 INT 列上限); 任何口径错误致天价 -> 跳过不发
    for row in rows:
        auction_id, owner_id, occ_time, add_info, unit_price, item_id, upgrade = row[:7]
        cname, kind, token, rarity = row[7:11]
        ref_price = row[11] if has_ref else None
        if ref_price is None and has_ref:
            ref_price = row[12]        # 无 csv 价 -> 回退补货基准(restock_list.system_price), 与补货一致
        # 参考价存在且公式适用 -> 分享工程概率回收; 否则 fallback 旧规则(稀有度上限必收)
        limit = None
        if has_ref and ref_price is not None:
            pstd = _pstd_price(pc, kind, rarity, ref_price, upgrade)
            if pstd is not None:
                rr = int(unit_price) / float(pstd)
                prob = _recycle_prob(rr)
                if random.random() >= prob:
                    continue                    # 概率没中: 不回收(模拟玩家行为, 不吃满仓)
            else:
                limit = _recycle_price_limit(rc, item_id, kind, token, rarity)
        else:
            limit = _recycle_price_limit(rc, item_id, kind, token, rarity)
        if limit is not None and int(unit_price) > limit:
            continue
        # 数量: 仅可堆叠物品(材料)的 add_info 才是堆叠数; 装备的 add_info 是内部值(实测 23207/4006/十亿级),
        # 绝不可当数量, 否则 sell_price 溢出天价. 装备不可堆叠 -> count 恒为 1.
        count = 1 if kind == "equipment" else (int(add_info) if add_info and int(add_info) > 0 else 1)
        sell_price = int(unit_price) * count       # 成交价 = 单价 × 数量
        fee = int(sell_price * fee_rate)
        gold = sell_price - fee + deposit          # 退还玩家上架已付押金, 与正常成交一致
        if gold < 0:
            gold = 0
        if gold > SANE_MAX:                        # 兜底: 绝不因口径错误白送天价金币
            log.warning(u"  跳过异常回收 auction_id=%s item=%s kind=%s count=%s unit=%s gold=%s(超上限)",
                        auction_id, item_id, kind, count, unit_price, gold)
            continue
        name = _u(cname) if cname else (u"item_%d" % item_id)
        name = name[:16]
        text = (
            u"您上架的[%s] %d个已被系统回收\n"
            u" 成交价: + %d\n"
            u" 押金: + %d\n"
            u" 手续费: - %d\n"
            u"共获得 %d 金币"
        ) % (name, count, sell_price, deposit, fee, gold)
        if len(text.encode(MAIL_ENCODING, "replace")) > 156:
            text = u"您上架的 %s %d个已被系统回收，成交价 %d，共获得 %d 金币。" % (name, count, sell_price, gold)
        # 先入队后删单(崩在两步间下轮挂单仍在, 不重复给钱也不吞物品)
        enqueue_mail(cur_f, conn_frida, "auction", auction_id, occ_time, owner_id, u"拍卖行", text, gold, 0)
        cur_a.execute("DELETE FROM auction_main WHERE auction_id = %s", (auction_id,))
        conn_mk.commit()
        # 回收宠物: 系统买走 -> 宠物消失, 连带销毁其 creature_items 实例(add_info=ui_id; 玩家已得金币)
        if SP_ENABLED and token in SP_CREATURE_TOKENS and add_info and int(add_info) > 0:
            cre_del.append(int(add_info))
        processed += 1
        log.info(u"  回收 auction_id=%s owner=%s gold=%s", auction_id, owner_id, gold)
    if cre_del:
        try:
            conn_game = get_conn(SP_GAME_DB)
            cg = conn_game.cursor()
            cg.execute("DELETE FROM creature_items WHERE ui_id IN (%s)" % ",".join(["%s"] * len(cre_del)),
                       cre_del)
            conn_game.commit()
            conn_game.close()
            log.info(u"  回收销毁宠物实例 %d 条", len(cre_del))
        except Exception as e:
            log.warning(u"  回收宠物实例删除失败(下次 reset 清孤儿): %s", e)
    log.info(u"  扫描玩家挂单 %d 条, 回收 %d 件", len(rows), processed)


# ---- 6b. 回收: 金币寄售(consign_threshold, 发代币券物品邮件) ----
def recycle_consign_threshold(m, conn_mk, conn_frida):
    rc = m["recycle"]
    fee_rate = float(rc.get("fee_rate", 0.02))
    token_item = int(rc.get("mail_item_id", 2681762))
    list_tbl = m["restock"]["list"]            # cera_consign_list(补货/回收共表)
    cur_a = conn_mk.cursor()
    cur_f = conn_frida.cursor()
    # 玩家寄售单 JOIN 配置表拉全部(扫描数=全部玩家寄售单), 价格判断放 Python —— 与拍卖行回收口径一致.
    try:
        cur_a.execute(
            "SELECT a.auction_id, a.owner_id, a.occ_time, a.instant_price, a.item_id, l.gold_label, l.recycle_price "
            "FROM auction_main a "
            "INNER JOIN " + FRIDA_DB + "." + list_tbl + " l ON a.item_id = l.item_id "
            "WHERE " + _player_owner_where("a.owner_id") + " AND l.enabled = 1 AND a.instant_price > 0"
        )
    except Exception as e:
        code = e.args[0] if getattr(e, "args", None) else 0
        if code == 1146:
            log.error(u"  frida.%s 不存在, 跳过回收 —— 请先 init 建表(见 README)", list_tbl)
            return
        raise
    rows = cur_a.fetchall()
    processed = 0
    for auction_id, owner_id, occ_time, instant_price, item_id, label, recycle_price in rows:
        if int(instant_price) > int(recycle_price):    # 标价高于回收价不收(扫描计数仍含此单, 口径同拍卖行)
            continue
        price = int(instant_price)
        fee = int(price * fee_rate)
        token = price - fee                      # 代币券数量; 无押金(实测 200标价→196)
        if token < 0:
            token = 0
        name = _u(label) if label else (u"金币包%d" % item_id)
        name = name[:16]
        text = (
            u"您寄售的[%s]已被系统回收\n"
            u" 成交价: + %d 代币券\n"
            u" 手续费: - %d 代币券\n"
            u"共获得 %d 代币券"
        ) % (name, price, fee, token)
        if len(text.encode(MAIL_ENCODING, "replace")) > 156:
            text = u"您寄售的 %s 已被系统回收，共获得 %d 代币券。" % (name, token)
        # 队列 gold 列承载代币券"数量"(item_id!=0); 代币券是货币物品, frida 落 postal.gold(数量), add_info=0
        enqueue_mail(cur_f, conn_frida, "cera", auction_id, occ_time, owner_id, u"金币寄售", text, token, token_item)
        cur_a.execute("DELETE FROM auction_main WHERE auction_id = %s", (auction_id,))
        conn_mk.commit()
        processed += 1
        log.info(u"  回收 auction_id=%s owner=%s 代币券=%s", auction_id, owner_id, token)
    log.info(u"  扫描玩家寄售 %d 条, 回收 %d 件", len(rows), processed)


def do_recycle(m, conn_mk, conn_frida):
    rc = m.get("recycle") or {}
    if not rc.get("enabled", True):
        log.info(u"[2/4] %s 回收未启用, 跳过", m["label"])
        return
    log.info(u"[2/4] 回收 %s 玩家低价单...", m["label"])
    mode = rc.get("mode")
    if mode == "catalog_rules":
        recycle_catalog_rules(m, conn_mk, conn_frida)
    elif mode == "consign_threshold":
        recycle_consign_threshold(m, conn_mk, conn_frida)
    else:
        log.warning(u"  未知回收 mode=%s, 跳过", mode)


# ---- 7. 补货公共件: 续期 / 库存装箱 / 批量插单 ----
def renew_near_expiry(conn_mk):
    cur_a = conn_mk.cursor()
    threshold = int(time.time()) + 2 * 3600
    cur_a.execute(
        "UPDATE auction_main SET occ_time = NOW(), expire_time = %s "
        "WHERE " + _sys_owner_where("owner_id") + " AND expire_time < %s",
        (int(time.time()) + 24 * 3600, threshold),
    )
    if cur_a.rowcount:
        log.info(u"  续期 %d 件即将过期的系统挂单", cur_a.rowcount)
    conn_mk.commit()


def _collect_occ_have(cur_a):
    """一次 GROUP BY 同时取: owner_id 占用(装箱用) + item_id 现有系统库存(算缺口用).
    auction_main 仅 auction_id 主键, 故压成 1 次全表扫描(替代逐物品 COUNT)."""
    occ, have = {}, {}
    cur_a.execute(
        "SELECT owner_id, item_id, COUNT(*) FROM auction_main "
        "WHERE " + _sys_owner_where("owner_id") + " GROUP BY owner_id, item_id"
    )
    for _oid, _iid, _cnt in cur_a.fetchall():
        c = int(_cnt)
        occ[int(_oid)] = occ.get(int(_oid), 0) + c
        have[int(_iid)] = have.get(int(_iid), 0) + c
    return occ, have


_INSERT_COLS = (
    "INSERT INTO auction_main ("
    " auction_id, occ_time, expire_time, owner_id, owner_name, owner_type, owner_nexon_id,"
    " buyer_id, buyer_name, price, instant_price, seal_flag, item_id, add_info, upgrade,"
    " amplify_option, amplify_value, seal_cnt, endurance, extend_info, black_point, unit_price,"
    " random_option, roi_high_key, roi_low_key, seperate_upgrade, item_guid"
    ") VALUES "
)
# 13 个 %s 顺序: auction_id, expire_time, owner_id, owner_name, owner_type, owner_nexon,
#                instant_price, seal_flag, item_id, add_info, upgrade, endurance, unit_price
_ROW_TMPL = (
    "(%s, NOW(), %s, %s, %s, %s, %s,"
    " -1, '', -1, %s, %s, %s, %s, %s,"
    " 0, 0, 0, %s, 0, 0, %s,"
    " UNHEX(REPEAT('00',14)), 0, 0, 0, UNHEX(REPEAT('00',10)))"
)
_BATCH = 500


def _insert_listings(cur_a, rows):
    """批量插 auction_main; rows = list of 13-tuple(顺序同 _ROW_TMPL)."""
    for i in range(0, len(rows), _BATCH):
        chunk = rows[i:i + _BATCH]
        sql = _INSERT_COLS + ",".join([_ROW_TMPL] * len(chunk))
        flat = []
        for r in chunk:
            flat.extend(r)
        cur_a.execute(sql, flat)


# ---- 7a-special. 补货: 特殊类(称号/装扮/红蓝绿装备/宠物) ----
# ★ui_id 不手填: creature_items.ui_id 是 AUTO_INCREMENT, 游戏服(df_game_r 补货时仍在线)会并发给玩家
#   孵化的宠物自增分配 ui_id. 手动 MAX+1 会与之竞态撞主键(同 letter_id 教训). 故插入不带 ui_id, 用
#   LAST_INSERT_ID 取回自增值当挂单 add_info. 单条 INSERT 语句内自增连续(MySQL5.0 持表级 AUTO-INC 锁).
_CRE_COLS = (
    "INSERT INTO creature_items"
    " (charac_no,slot,it_id,reg_date,name,stomach,exp,endurance,creature_type,"
    " no_charge,stat,item_lock_key,ipg_agency_no,expire_date,delete_date) VALUES "
)
# charac_no, it_id 两个 %s; 其余取新宠物缺省(满饥饿100/经验0/无耐久/不收费/不过期)
_CRE_TMPL = "(%s,0,%s,NOW(),'',100,0,0,0,0,0,0,'','9999-12-31 23:59:59','9999-12-31 23:59:59')"


def _insert_creature_items_autoinc(cur_g, rows):
    """rows = list of (charac_no, it_id), 按自增建实例行; 返回各行被分配的 ui_id(顺序对应输入)."""
    uiids = []
    for i in range(0, len(rows), _BATCH):
        chunk = rows[i:i + _BATCH]
        sql = _CRE_COLS + ",".join([_CRE_TMPL] * len(chunk))
        flat = []
        for (cn, it_id) in chunk:
            flat.extend((cn, it_id))
        cur_g.execute(sql, flat)
        first = cur_g.lastrowid                            # 本条多行 INSERT 的首个自增值, 余者连续
        uiids.extend(first + j for j in range(len(chunk)))
    return uiids


def restock_special(m, conn_mk):
    """补货特殊类(称号/装扮/红蓝绿装备/宠物): 全部挂普通系统 90M 假 owner, 零桩.
    称号/装扮/蓝绿装备 add_info 取高位段唯一递增(=0 重复会崩 RegistItem); 宠物建 creature_items 实例并令其
    charac_no=该 owner(加载要求 owner==charac_no), 用自增 ui_id 当 add_info.
    (DROP_TOKENS 默认空=无类型排除; 若某 type_token 确实零桩无法上架, 加入其 token 即跳过补货.)"""
    if not SP_ENABLED or not SP_TOKENS:
        return
    owner_name = m.get("owner_name", u"拍卖行")
    cur_a = conn_mk.cursor()
    conn_frida = get_conn(FRIDA_DB)
    cur_f = conn_frida.cursor()
    ph = ",".join(["%s"] * len(SP_TOKENS))
    cur_f.execute(
        "SELECT r.item_id, r.system_price, r.quantity, c.type_token "
        "FROM restock_list r JOIN item_catalog c ON r.item_id = c.item_id "
        "WHERE c.type_token IN (" + ph + ")", tuple(SP_TOKENS))
    items = cur_f.fetchall()
    conn_frida.close()
    if not items:
        return

    # 缺口 + 系统 90M owner 占用(一次全表扫描): have 按 item 算缺口, occ 按 owner 做轮换装箱
    occ, have = _collect_occ_have(cur_a)
    plan = [(int(iid), int(sp), max(0, int(q) - have.get(int(iid), 0)), tok) for iid, sp, q, tok in items]
    total_need = sum(p[2] for p in plan)
    if total_need <= 0:
        log.info(u"  [特殊] 无缺口, 跳过")
        return

    # owner: 普通系统 90M 假号轮换(与普通补货同源, 每 owner 不超 ROTATE_EVERY 件; 实测过密会崩加载)
    _cur_oid = [ID_BASE]
    def pick_owner():
        oid = _cur_oid[0]
        while occ.get(oid, 0) >= ROTATE_EVERY:
            oid += 1
        _cur_oid[0] = oid
        occ[oid] = occ.get(oid, 0) + 1
        return oid

    conn_game = get_conn(SP_GAME_DB)
    cur_g = conn_game.cursor()
    try:
        cur_a.execute("SELECT COALESCE(MAX(auction_id), 0) FROM auction_main")
        next_id = [cur_a.fetchone()[0] + 1]                # 列表做可变计数器(闭包内自增)
        expire_time = int(time.time()) + 24 * 3600

        # 按装箱顺序定 owner, 拆 宠物 / 非宠物(称号/装扮/蓝绿装备)两路
        pet_jobs, other_jobs = [], []           # (owner_id, item_id, price)
        for item_id, system_price, need, token in plan:
            is_creature = token in SP_CREATURE_TOKENS
            price = system_price if system_price > 0 else 1
            for _i in range(need):
                oid = pick_owner()
                (pet_jobs if is_creature else other_jobs).append((oid, item_id, price))

        # 宠物: 建 creature_items 实例(charac_no=该 90M owner, 满足 owner==charac_no), 自增 ui_id 当 add_info.
        # ★ui_id 不手填: 自增, 否则与游戏服并发孵化撞主键(同 letter_id 教训)★
        pet_uiids = []
        if pet_jobs:
            pet_uiids = _insert_creature_items_autoinc(cur_g, [(o, it) for o, it, _p in pet_jobs])
            conn_game.commit()

        # 非宠物特殊: add_info 取高位段 MAX+1 唯一递增(★装扮/artifact 的 add_info 必须全局唯一, =0 重复会崩★;
        # 段起点 SP_ADDINFO_BASE > 玩家观测上限, 撞玩家概率~1e-6). 只看系统(m_id排除真玩家)名下高位 add_info.
        cur_a.execute("SELECT COALESCE(MAX(add_info), %s) FROM auction_main "
                      "WHERE " + _sys_owner_where("owner_id") + " AND add_info >= %s",
                      (SP_ADDINFO_BASE - 1, SP_ADDINFO_BASE))
        next_band = [max(cur_a.fetchone()[0], SP_ADDINFO_BASE - 1) + 1]

        rows = []
        def add_row(oid, item_id, price, add_info):
            # 一口价=系统价(精确品级×系数), seal=1(封装), upgrade=0, endurance=0(无耐久)
            rows.append((next_id[0], expire_time, oid, owner_name[:20], SYS_OWNER_TYPE, str(NEXON_BASE + oid)[:25],
                         price, 1, item_id, add_info, 0, 0, price))
            next_id[0] += 1
        for i, (oid, item_id, price) in enumerate(pet_jobs):
            add_row(oid, item_id, price, pet_uiids[i])     # 宠物 add_info = creature_items.ui_id
        for oid, item_id, price in other_jobs:
            add_row(oid, item_id, price, next_band[0])     # 称号/装扮/蓝绿装备: 高位段唯一 add_info
            next_band[0] += 1
        _insert_listings(cur_a, rows)
        conn_mk.commit()
    finally:
        conn_game.close()                                  # 出错也关连接, 不留悬挂事务抢锁
    log.info(u"  [特殊] 补货 %d 件(非宠物 %d + 宠物 %d), 零桩全挂 90M",
             len(rows), len(other_jobs), len(pet_jobs))


def _pick_mind(bands):
    """参考工程卖家心态: 概率选档, 档内均匀浮动. bands 形如 [{name,prob,mult_min,mult_max}]; 无配置 -> 1.0(固定价)."""
    r = random.random()
    for b in bands or []:
        r -= float(b.get("prob", 0))
        if r < 0:
            return random.uniform(float(b["mult_min"]), float(b["mult_max"]))
    return 1.0


def _pick_enhance(bands):
    """参考工程强化等级分布: 概率选档位. bands 形如 [{lvl, p}]; 无配置/史诗 -> 0(白板)."""
    r = random.random()
    for b in bands or []:
        r -= float(b.get("p", 0))
        if r < 0:
            return int(b["lvl"])
    return 0


# ---- 7a. 补货: 拍卖行(catalog_list, 按 quantity/stack_size 随机定价) ----
def restock_catalog_list(m, conn_mk, conn_frida):
    rs = m["restock"]
    # 参考工程: 卖家心态 4 档概率浮动 + 市场饱和度降价 + 上架价硬下限
    sat = rs.get("saturation") or {}
    scarce_th = int(sat.get("scarce_threshold", 0))
    scarce_shift = float(sat.get("scarce_shift", 0))
    sat_th = int(sat.get("saturated_threshold", 20))
    sat_shift = float(sat.get("saturated_shift", 0))
    min_ratio = float(rs.get("min_price_ratio", 0.10))
    edist = rs.get("enhance_dist") or {}
    min_up = rs.get("min_upgrade") or {}
    eq_grade = int(rs.get("equip_grade", 999999998))
    pc = (m.get("recycle") or {}).get("pricing") or {}    # 强化公式系数(与回收共用)
    owner_name = m.get("owner_name", u"拍卖行")
    cur_a = conn_mk.cursor()
    cur_f = conn_frida.cursor()

    cur_f.execute(
        "SELECT r.item_id, r.system_price, r.quantity, r.stack_size, r.upgrade, r.endurance, r.seal_flag, "
        "c.kind, c.type_token, c.rarity "
        "FROM " + rs["list"] + " r LEFT JOIN item_catalog c ON r.item_id = c.item_id"
    )
    items = cur_f.fetchall()
    if not items:
        log.info(u"  补货列表为空, 跳过补货")
        return

    cur_a.execute("SELECT COALESCE(MAX(auction_id), 0) FROM auction_main")
    next_id = cur_a.fetchone()[0] + 1
    expire_time = int(time.time()) + 24 * 3600
    occ, have = _collect_occ_have(cur_a)

    _cur_oid = [ID_BASE]
    def pick_owner():
        oid = _cur_oid[0]
        while occ.get(oid, 0) >= ROTATE_EVERY:
            oid += 1
        _cur_oid[0] = oid
        return oid

    # 装备各字段按 item_catalog 属性自动判定(均为实测合法值; restock_list 的 upgrade/endurance/seal_flag 不再使用):
    #   add_info: 宠物(creature)=1(数量); 称号/红蓝绿装备=0; 普通装备=EQUIP_GRADE(最上级品级.
    #     ★白板0能加载能购买, 但玩家维修后装备消失; 非法品级 -> RegistItem 崩、整服起不来★).
    #   endurance: 称号/宠物/红蓝绿装备无耐久=0; 武器/防具等普通装备=35.
    #   seal_flag(封装): 稀有及以上(rarity>=2)=1; 否则 0.
    NO_ENDUR = ("creature", "artifact red", "artifact blue", "artifact green", "title name")  # 称号/宠物/红蓝绿装备无耐久
    EQUIP_GRADE = eq_grade if eq_grade else 999999998       # 装备品级 add_info(可配置, 默认最上级实测合法值)
    RARITY_ENH = {0: "white", 1: "blue", 2: "purple", 3: "pink"}  # rarity 数字 -> 强化分布键; 史诗(4+)无档 -> 白板
    rows, total = [], 0
    for item_id, system_price, quantity, stack_size, upgrade, endurance, seal_flag, kind, type_token, rarity in items:
        if (SP_ENABLED and type_token in SP_TOKENS) or type_token in SP_DROP_TOKENS:
            continue                                       # SP_TOKENS 走 restock_special; DROP_TOKENS 内 token 完全不补货(默认空, 留作禁补开关)
        is_equip = (kind == "equipment")
        eff_stack = 1 if is_equip else (stack_size or 1)
        target_records = (quantity + eff_stack - 1) // eff_stack
        current = have.get(int(item_id), 0)
        need = max(0, target_records - current)
        if is_equip:
            tok = type_token or ""
            eq_endur = 0 if tok in NO_ENDUR else 35
            eq_seal = 1 if (rarity is not None and int(rarity) >= 2) else 0
            eq_addinfo = 1 if tok == "creature" else (0 if tok in NO_ENDUR else EQUIP_GRADE)
        for i in range(need):
            pos = current + i
            if is_equip:
                add_info = eq_addinfo
                # 强化等级每件独立抽档(参考 enhance_dist): 白/蓝 11-18 强, 紫/粉 0-15 强, 史诗保持白板
                lvl = 0
                rk = RARITY_ENH.get(int(rarity)) if rarity is not None else None
                if rk:
                    lvl = _pick_enhance(edist.get(rk) or [])
                    mn = int(min_up.get(rk, 0) or 0)
                    if lvl and lvl < mn:
                        lvl = mn
                base = _pstd_price(pc, "equipment", rarity, system_price, lvl)
                if base is None:
                    base = system_price            # 史诗/无系数: 回退基准价
            else:
                add_info = eff_stack if pos < target_records - 1 else (quantity - (target_records - 1) * eff_stack)
            # 定价: 卖家心态每件独立选档(0.5~1.5), 稀缺(<scarce_th)提价 / 饱和(>=sat_th)降价, 硬下限=基准价*min_ratio
            mult = _pick_mind(rs.get("seller_mind"))
            if scarce_th > 0 and current < scarce_th:
                mult += scarce_shift
            elif current >= sat_th:
                mult += sat_shift
            base = base if is_equip else system_price
            unit_price = max(max(1, int(system_price * min_ratio)), int(base * mult))
            # 装备一口价=单价(add_info 是品级, 非堆叠); 材料一口价=单价×堆叠数
            instant_price = unit_price if is_equip else unit_price * add_info
            owner_id = pick_owner()
            nexon = NEXON_BASE + owner_id
            if is_equip:
                rows.append((
                    next_id, expire_time, owner_id, owner_name[:20], SYS_OWNER_TYPE, str(nexon)[:25],
                    instant_price, eq_seal, item_id, add_info, lvl, eq_endur, unit_price,
                ))
            else:                                              # 材料/消耗品: seal=0, upgrade=0, endurance=0
                rows.append((
                    next_id, expire_time, owner_id, owner_name[:20], SYS_OWNER_TYPE, str(nexon)[:25],
                    instant_price, 0, item_id, add_info, 0, 0, unit_price,
                ))
            occ[owner_id] = occ.get(owner_id, 0) + 1
            next_id += 1
            total += 1
    _insert_listings(cur_a, rows)
    conn_mk.commit()
    log.info(u"  补货 %d 件(系统卖家占用 %d 个 owner_id)", total, len([1 for _v in occ.values() if _v > 0]))


# ---- 7b. 补货: 金币寄售(fixed_list, 每种补足 restock_qty 条, 固定代币券标价, 不堆叠) ----
def restock_fixed_list(m, conn_mk, conn_frida):
    rs = m["restock"]
    owner_name = m.get("owner_name", u"金币寄售")
    cur_a = conn_mk.cursor()
    cur_f = conn_frida.cursor()

    cur_f.execute(
        "SELECT item_id, gold_label, restock_price, restock_qty FROM " + rs["list"] + " WHERE enabled = 1"
    )
    items = cur_f.fetchall()
    if not items:
        log.info(u"  补货列表为空, 跳过补货")
        return

    cur_a.execute("SELECT COALESCE(MAX(auction_id), 0) FROM auction_main")
    next_id = cur_a.fetchone()[0] + 1
    expire_time = int(time.time()) + 24 * 3600
    occ, have = _collect_occ_have(cur_a)

    _cur_oid = [ID_BASE]
    def pick_owner():
        oid = _cur_oid[0]
        while occ.get(oid, 0) >= ROTATE_EVERY:
            oid += 1
        _cur_oid[0] = oid
        return oid

    rows, total = [], 0
    for item_id, gold_label, restock_price, restock_qty in items:
        current = have.get(int(item_id), 0)
        need = max(0, int(restock_qty) - current)
        for _i in range(need):
            owner_id = pick_owner()
            nexon = NEXON_BASE + owner_id
            # 金币包寄售单: add_info=1(不堆叠), instant_price=代币券标价, unit_price=0, seal/upgrade/endurance=0
            rows.append((
                next_id, expire_time, owner_id, owner_name[:20], SYS_OWNER_TYPE, str(nexon)[:25],
                int(restock_price), 0, item_id, 1, 0, 0, 0,
            ))
            occ[owner_id] = occ.get(owner_id, 0) + 1
            next_id += 1
            total += 1
    _insert_listings(cur_a, rows)
    conn_mk.commit()
    log.info(u"  补货 %d 条金币包(系统卖家占用 %d 个 owner_id)", total, len([1 for _v in occ.values() if _v > 0]))


def do_restock(m, conn_mk, conn_frida):
    log.info(u"[3/4] %s 系统补货...", m["label"])
    renew_near_expiry(conn_mk)
    mode = m["restock"].get("mode")
    if mode == "catalog_list":
        restock_catalog_list(m, conn_mk, conn_frida)
        restock_special(m, conn_mk)                        # 特殊类(零桩90M owner/非宠物唯一add_info/宠物建实例)
    elif mode == "fixed_list":
        restock_fixed_list(m, conn_mk, conn_frida)
    else:
        log.warning(u"  未知补货 mode=%s, 跳过", mode)


# ---- 8. 单市场单轮 / 全市场单轮 ----
def _clear_system_listings(m, conn_mk):
    # reset 用: 清空本市场全部系统挂单(owner_id>=ID_BASE), 让随后补货全量重建.
    # 改了 config 参数/补货列表(价/量)后需要这步, 否则普通 once 只补缺口、存量挂单不会更新.
    cur = conn_mk.cursor()
    # ★只删系统挂单, 必须排除真玩家(m_id<>0)★ —— 污染玩家 charac_no 落在 >=ID_BASE 高位段, 若只按
    # owner_id>=ID_BASE 删会误删真玩家挂单. _sys_owner_where 用 m_id 排除真玩家.
    cur.execute("DELETE FROM auction_main WHERE " + _sys_owner_where("owner_id"))
    log.info(u"  [reset] 清空 %s 系统挂单 %d 条, 本轮全量重补", m["label"], cur.rowcount)
    conn_mk.commit()
    # 清空系统挂单后, 系统 90M 假号名下的 creature_items(未售出系统宠物实例)全部成孤儿 -> 一并删, 防累积.
    # 仅对做特殊补货的市场(catalog_list)执行, 避免 cera 轮误删刚由本市场 restock_special 建的实例.
    # 系统假号(>=ID_BASE 且非真玩家)在线玩家从不持有/操作 -> 不与游戏服抢锁; 已售出实例 charac_no 已转买家
    # (真玩家 m_id<>0)被 _sys_owner_where 排除不受影响. 失败也不阻断 reset(自增续号本就不撞, 残留下轮再清).
    if SP_ENABLED and m["restock"].get("mode") == "catalog_list":
        try:
            conn_game = get_conn(SP_GAME_DB)
            cg = conn_game.cursor()
            # 只删【系统 90M 假号】名下的宠物实例; 真玩家(m_id<>0, 含残留污染号)的宠物绝不能删
            cg.execute("DELETE FROM creature_items WHERE " + _sys_owner_where("charac_no"))
            n = cg.rowcount
            conn_game.commit()
            conn_game.close()
            log.info(u"  [reset] 清理孤儿系统宠物实例 %d 条", n)
        except Exception as e:
            log.warning(u"  [reset] 清理 creature_items 失败(不阻断, 下轮再清): %s", e)


def _has_recycle_work(m, conn_mk, conn_frida):
    """只读探测本市场是否有待回收玩家挂单(服务在线时跑, 不改库). 异常保守按有活.
    探测口径与实际 recycle 一致, 宁可误判(多停启/多扫一次)也不漏."""
    rc = m.get("recycle") or {}
    if not rc.get("enabled"):
        return False
    try:
        cur_a = conn_mk.cursor()
        if rc.get("mode") == "catalog_rules":
            cur_a.execute(
                "SELECT a.unit_price, a.item_id, c.kind, c.type_token, c.rarity "
                "FROM auction_main a INNER JOIN " + FRIDA_DB + ".item_catalog c ON a.item_id = c.item_id "
                "WHERE " + _player_owner_where("a.owner_id") + " AND a.unit_price > 0")
            for unit_price, item_id, kind, token, rarity in cur_a.fetchall():
                limit = _recycle_price_limit(rc, item_id, kind, token, rarity)
                if limit is not None and int(unit_price) <= limit:
                    return True
        elif rc.get("mode") == "consign_threshold":
            cur_a.execute(
                "SELECT a.instant_price, l.recycle_price FROM auction_main a "
                "INNER JOIN " + FRIDA_DB + "." + m["restock"]["list"] + " l ON a.item_id = l.item_id "
                "WHERE " + _player_owner_where("a.owner_id") + " AND l.enabled = 1 AND a.instant_price > 0")
            for instant_price, recycle_price in cur_a.fetchall():
                if int(instant_price) <= int(recycle_price):
                    return True
    except Exception as e:
        log.warning(u"  [%s] 回收探测异常(保守按有活处理): %s", m["label"], e)
        return True
    return False


def _has_restock_work(m, conn_mk, conn_frida):
    """只读探测本市场是否有补货缺口(服务在线时跑, 不改库). 异常保守按有活.
    have 只数系统挂单(m_id 排除污染真玩家; 含普通+特殊段, item_id 不重叠)."""
    try:
        cur_a = conn_mk.cursor()
        cur_f = conn_frida.cursor()
        cur_a.execute("SELECT item_id, COUNT(*) FROM auction_main WHERE " + _sys_owner_where("owner_id") + " GROUP BY item_id")
        have = dict(cur_a.fetchall())
        rmode = m["restock"].get("mode")
        if rmode == "catalog_list":
            cur_f.execute("SELECT r.item_id, r.quantity, r.stack_size, c.kind, c.type_token "
                          "FROM " + m["restock"]["list"] + " r LEFT JOIN item_catalog c ON r.item_id = c.item_id")
            for item_id, quantity, stack_size, kind, type_token in cur_f.fetchall():
                if type_token in SP_DROP_TOKENS:
                    continue                               # DROP_TOKENS 内 token 不补货, 不算缺口(否则永远判有活、永不跳过停启)
                eff = 1 if kind == "equipment" else (stack_size or 1)
                target_records = (int(quantity) + eff - 1) // eff
                if have.get(int(item_id), 0) < target_records:
                    return True
        elif rmode == "fixed_list":
            cur_f.execute("SELECT item_id, restock_qty FROM " + m["restock"]["list"] + " WHERE enabled = 1")
            for item_id, restock_qty in cur_f.fetchall():
                if have.get(int(item_id), 0) < int(restock_qty):
                    return True
    except Exception as e:
        log.warning(u"  [%s] 补货探测异常(保守按有活处理): %s", m["label"], e)
        return True
    return False


def _purge_system_seller_mail():
    """清理系统卖家成交收款邮件. 系统虚拟卖家永不读取, letter/postal 只增不减 -> 成交即沉金, 删之无经济影响.
    ★只删系统卖家(owner>=ID_BASE 且非真玩家 m_id<>0)的邮件, 绝不碰真玩家(含被污染到高位的)★. 失败不阻断."""
    if not PURGE_SELLER_MAIL:
        return
    try:
        conn = get_conn(SELLER_MAIL_DB)
        cur = conn.cursor()
        cur.execute("DELETE FROM postal WHERE " + _sys_owner_where("receive_charac_no"))
        np = cur.rowcount
        cur.execute("DELETE FROM letter WHERE " + _sys_owner_where("charac_no"))
        nl = cur.rowcount
        conn.commit()
        conn.close()
        if np or nl:
            log.info(u"  [清理] 系统卖家沉金邮件 postal %d / letter %d 条", np, nl)
    except Exception as e:
        log.warning(u"  [清理] 系统卖家邮件清理失败(不阻断): %s", e)


def run_once_market(name, m, reset=False):
    """返回 True=本轮进入了停启/改库流程, False=无活跳过."""
    log.info(u"===== 市场 [%s] 开始 =====", m["label"])
    conn_mk = get_conn(m["db"])
    conn_frida = get_conn(FRIDA_DB)
    try:
        # reset 强制全量; 普通轮先只读探测: 无活跳过不停启; 仅回收活则在线回收(不停服), 有补货缺口才停服
        if not reset:
            rec_work = _has_recycle_work(m, conn_mk, conn_frida)
            rst_work = _has_restock_work(m, conn_mk, conn_frida)
            if not rec_work and not rst_work:
                log.info(u"  [%s] 无待回收/补货, 跳过(不停启服务)", m["label"])
                return False
            # 仅回收活: 在线回收不停服(前提: 游戏服能容忍在线 DELETE, 上线前先实测;
            # recycle.online=false 则回收仍进停服窗口, 与补货顺带清残)
            if rec_work and not rst_work and (m.get("recycle") or {}).get("online", True):
                log.info(u"  [%s] 在线回收(不停服, 无补货缺口)...", m["label"])
                do_recycle(m, conn_mk, conn_frida)
                return True
        try:
            stop_market(m)
            if reset:
                _clear_system_listings(m, conn_mk)    # 先清空系统挂单, 再全量补
            do_recycle(m, conn_mk, conn_frida)
            do_restock(m, conn_mk, conn_frida)
        finally:
            start_market(m)                           # 一旦进入停启流程, 无论如何重启服务
    finally:
        conn_mk.close()
        conn_frida.close()
    log.info(u"===== 市场 [%s] 结束 =====", m["label"])
    return True


def run_once(reset=False):
    did_work = False
    for name, m in MARKETS.items():
        if not m.get("enabled", True):
            log.info(u"市场 [%s] 未启用, 跳过", m.get("label", name))
            continue
        try:
            if run_once_market(name, m, reset):
                did_work = True
        except Exception as e:
            log.exception(u"市场 [%s] 本轮异常: %s", m.get("label", name), e)
            did_work = True                               # 异常可能已改库, 保守清一次邮件
    # 有市场实际改了库(成交/补货发生)才清系统卖家沉金邮件, 空轮不做无谓全表扫
    if reset or did_work:
        _purge_system_seller_mail()


# ---- 9. 建表(首次) ----
def init_tables():
    conn = get_conn(FRIDA_DB) if _db_exists(FRIDA_DB) else _create_frida_db()
    c = conn.cursor()
    # pending_mail: 邮件队列, 直接建最优结构(测试服可重建, 不做兼容迁移; DROP 丢历史队列无碍).
    # market 区分市场, item_id=0 金币邮件/非0 物品邮件(代币券); 幂等键 (market, auction_id, occ_time).
    c.execute("DROP TABLE IF EXISTS pending_mail")
    c.execute(
        "CREATE TABLE pending_mail ("
        " id INT AUTO_INCREMENT PRIMARY KEY,"
        " market VARCHAR(16) NOT NULL DEFAULT 'auction',"
        " auction_id BIGINT DEFAULT NULL,"
        " occ_time DATETIME NULL,"
        " charac_no INT NOT NULL,"
        " title VARBINARY(192) NOT NULL,"
        " text VARBINARY(765) NOT NULL,"
        " gold INT NOT NULL DEFAULT 0,"
        " item_id INT NOT NULL DEFAULT 0,"
        " status TINYINT NOT NULL DEFAULT 0,"
        " created_at DATETIME NULL,"
        " claimed_by VARCHAR(40) DEFAULT NULL,"            # frida 多频道原子认领(哪个频道处理了)
        " UNIQUE KEY uniq_listing (market, auction_id, occ_time),"
        " KEY idx_status (status)"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8"
    )

    # 拍卖行补货列表(配置数据, CREATE IF NOT EXISTS 保留已填配置)
    c.execute(
        "CREATE TABLE IF NOT EXISTS restock_list ("
        " item_id INT UNSIGNED NOT NULL PRIMARY KEY,"
        " cname VARCHAR(64) DEFAULT NULL,"
        " system_price INT NOT NULL,"
        " quantity INT NOT NULL DEFAULT 1,"
        " stack_size INT NOT NULL DEFAULT 1,"
        " upgrade TINYINT UNSIGNED DEFAULT 0,"
        " endurance SMALLINT UNSIGNED DEFAULT 35,"
        " seal_flag TINYINT UNSIGNED DEFAULT 1"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8"
    )

    # 金币寄售配置表(补货+回收合一); 首次播种 12 种金币包默认值(线性 2 代币券/万金币)
    c.execute(
        "CREATE TABLE IF NOT EXISTS cera_consign_list ("
        " item_id INT UNSIGNED NOT NULL PRIMARY KEY,"
        " gold_label VARCHAR(32) DEFAULT NULL,"
        " restock_price INT NOT NULL,"
        " restock_qty INT NOT NULL DEFAULT 20,"
        " recycle_price INT NOT NULL,"
        " enabled TINYINT NOT NULL DEFAULT 1"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8"
    )
    _seed_cera_consign_list(c)

    # item_catalog 仅校验(由 gen_item_catalog.py 生成的 item_catalog.sql 导入, 含数据)
    try:
        c.execute("SELECT COUNT(*) FROM item_catalog")
        n = c.fetchone()[0]
        if n:
            log.info(u"  item_catalog 就绪(%d 行)", n)
        else:
            log.warning(u"  item_catalog 为空 —— 拍卖行回收不会命中, 请导入 item_catalog.sql")
    except Exception:
        log.warning(u"  item_catalog 不存在 —— 拍卖行回收将被跳过, 请导入 item_catalog.sql(见 README)")

    # item_ref_price 参考价表(由 gen_ref_prices.py 生成的 item_ref_price.sql 导入, 含数据): 回收概率判价数据源
    c.execute(
        "CREATE TABLE IF NOT EXISTS item_ref_price ("
        " item_id INT UNSIGNED NOT NULL PRIMARY KEY,"
        " price INT NOT NULL"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8"
    )
    try:
        c.execute("SELECT COUNT(*) FROM item_ref_price")
        n = c.fetchone()[0]
        if n:
            log.info(u"  item_ref_price 就绪(%d 行)", n)
        else:
            log.warning(u"  item_ref_price 为空 —— 回收概率判价将回退为本工程稀有度上限规则, 请导入 item_ref_price.sql")
    except Exception:
        log.warning(u"  item_ref_price 不存在 —— 回收概率判价将回退为本工程稀有度上限规则, 请导入 item_ref_price.sql")
    conn.commit()
    conn.close()
    log.info(u"frida 表已就绪(pending_mail / restock_list / cera_consign_list)")


def _seed_cera_consign_list(c):
    # 12 种金币包(2675336-2675347): 100万-1000万 + 2000万/3000万. 默认 2 代币券/万金币线性.
    wan = [(2675336, 100), (2675337, 200), (2675338, 300), (2675339, 400),
           (2675340, 500), (2675341, 600), (2675342, 700), (2675343, 800),
           (2675344, 900), (2675345, 1000), (2675346, 2000), (2675347, 3000)]
    seeded = 0
    for item_id, w in wan:
        price = w * 2                            # 2 代币券/万金币(100万=200, 与实测一致)
        try:
            c.execute(
                "INSERT IGNORE INTO cera_consign_list "
                "(item_id, gold_label, restock_price, restock_qty, recycle_price, enabled) "
                "VALUES (%s, %s, %s, 20, %s, 1)",
                (item_id, u"%d万金币" % w, price, price),
            )
            seeded += c.rowcount
        except Exception as e:
            log.error(u"  cera_consign_list 播种失败 item_id=%s: %s", item_id, e)
    if seeded:
        log.info(u"  cera_consign_list 播种 %d 行默认金币包(可改表调价)", seeded)


def _db_exists(name):
    conn = pymysql.connect(
        user=MYSQL["user"], password=MYSQL["password"], charset=MYSQL.get("charset", "utf8"),
        **({"unix_socket": MYSQL["unix_socket"]} if MYSQL.get("unix_socket")
           else {"host": MYSQL.get("host", "127.0.0.1"), "port": int(MYSQL.get("port", 3306))})
    )
    try:
        cur = conn.cursor()
        cur.execute("SHOW DATABASES LIKE %s", (name,))
        return cur.fetchone() is not None
    finally:
        conn.close()


def _create_frida_db():
    conn = pymysql.connect(
        user=MYSQL["user"], password=MYSQL["password"], charset=MYSQL.get("charset", "utf8"),
        **({"unix_socket": MYSQL["unix_socket"]} if MYSQL.get("unix_socket")
           else {"host": MYSQL.get("host", "127.0.0.1"), "port": int(MYSQL.get("port", 3306))})
    )
    conn.cursor().execute("CREATE DATABASE IF NOT EXISTS %s DEFAULT CHARSET utf8" % FRIDA_DB)
    conn.commit()
    conn.close()
    return get_conn(FRIDA_DB)


# ---- 10. 入口 ----
_LOCK_FH = None


def _acquire_singleton_lock():
    """flock 单例锁: 防 cron 上一轮卡死时下一轮叠加 -> 双实例停启/补货互踩. 拿不到锁直接退出.
    init 不加锁(只建表无害); once/reset/loop 加锁. 锁随进程退出自动释放."""
    global _LOCK_FH
    lock_path = CFG.get("lock_path") or os.path.join(SCRIPT_DIR, "market_agent.lock")
    try:
        _LOCK_FH = open(lock_path, "w")
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        log.warning(u"已有 market_agent 实例在运行(锁 %s 被占用), 本次退出", lock_path)
        return False
    return True


def main():
    argv = sys.argv[1:]
    cmd = next((a for a in argv if a in ("init", "reset", "once")), None)
    log.info(u"=== 多市场自动做市(market_agent) === 日志: %s", LOG_PATH)
    if cmd == "init":
        init_tables()
        return
    # 随机种子: config runtime.random_seed 或命令行 --seed N(优先), 固定后可复现测试
    seed = None
    rt = CFG.get("runtime") or {}
    if isinstance(rt, dict) and rt.get("random_seed") is not None:
        seed = int(rt["random_seed"])
    if "--seed" in argv:
        try:
            seed = int(argv[argv.index("--seed") + 1])
        except (ValueError, IndexError):
            pass
    if seed is not None:
        random.seed(seed)
        log.info(u"随机种子固定: %s", seed)
    if not _acquire_singleton_lock():                     # once/reset/loop 单例, 防并发
        return
    if cmd == "reset":
        log.info(u"=== reset: 清空各市场系统挂单后全量重补(改 config/补货列表后用) ===")
        run_once(reset=True)
        return
    if cmd == "once":
        run_once()
        return
    while True:
        try:
            run_once()
        except Exception as e:
            log.exception(u"本轮异常: %s", e)
        log.info(u"下一轮 %d 秒后...", INTERVAL)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
