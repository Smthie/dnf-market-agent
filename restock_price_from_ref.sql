-- 补货价批量替换为参考价: 命中 item_ref_price 的 restock_list.system_price = price;
-- 未命中(csv 没有的 item)保持本工程原价. 可重复执行(幂等).
SET NAMES utf8;
UPDATE `frida`.`restock_list` r JOIN `frida`.`item_ref_price` p ON r.item_id = p.item_id
SET r.system_price = p.price;
