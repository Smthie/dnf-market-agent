# -*- coding: utf-8 -*-
"""从分享工程价格表(items.csv)生成 SQL:
  1) item_ref_price.sql     -> frida.item_ref_price 表(回收 per-item 标准价数据源)
  2) restock_price_from_ref.sql -> restock_list.system_price 批量替换为 csv 价(未命中保持原价)

用法: python gen_ref_prices.py <items.csv 路径>
"""
import csv
import io
import sys

def main():
    src = sys.argv[1] if len(sys.argv) > 1 else r"D:/gitea/拍卖行分享/root/auction/items.csv"
    rows, seen = [], set()
    for r in csv.DictReader(open(src, encoding="utf-8-sig")):
        iid = int(r["item_id"])
        if iid in seen:  # csv 源偶发重复 item_id(如 27653), 保留首次出现
            continue
        seen.add(iid)
        rows.append((iid, r["name"], r["rarity"], int(r["price"])))
    rows.sort()

    # 1) item_ref_price.sql
    out = io.StringIO()
    out.write(u"-- 参考价格表(来自分享工程 items.csv, %d 条). 导入: mysql --default-character-set=utf8 -u<user> -p frida < item_ref_price.sql\n" % len(rows))
    out.write(u"-- price = 分享工程 P_std 基准价: 消耗品直接用; 装备作强化公式的 shop/P_base.\n")
    out.write(u"SET NAMES utf8;\n")
    out.write(u"CREATE TABLE IF NOT EXISTS `item_ref_price` (\n")
    out.write(u"  `item_id` INT UNSIGNED NOT NULL,\n")
    out.write(u"  `price` INT NOT NULL,\n")
    out.write(u"  PRIMARY KEY (`item_id`)\n")
    out.write(u") ENGINE=InnoDB DEFAULT CHARSET=utf8;\n")
    out.write(u"DELETE FROM `item_ref_price`;\n")
    for i in range(0, len(rows), 200):
        chunk = rows[i:i + 200]
        out.write(u"INSERT INTO `item_ref_price` (`item_id`,`price`) VALUES\n")
        out.write(u",\n".join(u"(%d,%d)" % (item_id, price) for item_id, _, _, price in chunk))
        out.write(u";\n")
    open("item_ref_price.sql", "w", encoding="utf-8").write(out.getvalue())

    # 2) restock 补货价替换(JOIN 匹配才改, 未命中保持原价)
    out = io.StringIO()
    out.write(u"-- 补货价批量替换为参考价: 命中 item_ref_price 的 restock_list.system_price = price;\n")
    out.write(u"-- 未命中(csv 没有的 item)保持本工程原价. 可重复执行(幂等).\n")
    out.write(u"SET NAMES utf8;\n")
    out.write(u"UPDATE `frida`.`restock_list` r JOIN `frida`.`item_ref_price` p ON r.item_id = p.item_id\n")
    out.write(u"SET r.system_price = p.price;\n")
    open("restock_price_from_ref.sql", "w", encoding="utf-8").write(out.getvalue())

    print("item_ref_price.sql: %d 条" % len(rows))
    print("restock_price_from_ref.sql: 生成")

if __name__ == "__main__":
    main()
