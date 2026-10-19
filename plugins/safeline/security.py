"""Typed list rules, cloud subscriptions, CC history and verified configuration edits."""

import asyncio
import copy
import ipaddress
import math
import re

from plugins.safeline.application_pages import RULE_FIELDS, public_rules
from plugins.safeline.connection import SafeLineError
from plugins.safeline.editing import Pattern, fingerprint, validated
from plugins.safeline.service import now, policy, rows, safe, take, text


TARGETS = {
    "src_ip": ("來源 IP", "eq not_eq match cidr not_cidr in not_in geo_eq geo_not_eq"),
    "website": ("防護應用", "eq not_eq"), "uri": ("URL（含查詢參數）", "eq not_eq has not_has prefix re"),
    "uri_no_query": ("URL 路徑", "eq not_eq has not_has prefix re"), "host": ("域名", "eq not_eq match re has not_has"),
    "get_param": ("GET 參數", "eq not_eq has not_has re exist not_exist"),
    "post_param": ("POST 參數", "eq not_eq has not_has re exist not_exist"),
    "req_header": ("請求 Header", "eq not_eq has not_has re exist not_exist"),
    "req_body": ("請求 Body", "re has not_has"), "res_body": ("回應 Body", "re has not_has"),
    "req_raw_header": ("完整請求 Header", "re has not_has"), "res_raw_header": ("完整回應 Header", "re has not_has"),
    "method": ("請求方法", "eq not_eq"), "ja4": ("JA4 指紋", "ja4_in eq not_eq"),
}
OPERATORS = {"eq": "等於", "not_eq": "不等於", "match": "萬用字元匹配", "cidr": "屬於網段", "not_cidr": "不屬於網段",
             "in": "屬於 IP 組", "not_in": "不屬於 IP 組", "geo_eq": "屬於地理位置", "geo_not_eq": "不屬於地理位置",
             "has": "包含", "not_has": "不包含", "prefix": "前綴匹配", "re": "正則匹配", "exist": "存在", "not_exist": "不存在", "ja4_in": "屬於指紋庫"}


# ISO country and province identifiers used by the target console.
GEO = [{'id': 'CN-BJ', 'name': '北京'},
 {'id': 'CN-SH', 'name': '上海'},
 {'id': 'CN-GD', 'name': '广东'},
 {'id': 'CN-ZJ', 'name': '浙江'},
 {'id': 'CN-AH', 'name': '安徽'},
 {'id': 'CN-CQ', 'name': '重庆'},
 {'id': 'CN-FJ', 'name': '福建'},
 {'id': 'CN-GS', 'name': '甘肃'},
 {'id': 'CN-GZ', 'name': '贵州'},
 {'id': 'CN-HA', 'name': '河南'},
 {'id': 'CN-HB', 'name': '湖北'},
 {'id': 'CN-HE', 'name': '河北'},
 {'id': 'CN-HI', 'name': '海南'},
 {'id': 'CN-HL', 'name': '黑龙江'},
 {'id': 'CN-HN', 'name': '湖南'},
 {'id': 'CN-JL', 'name': '吉林'},
 {'id': 'CN-JS', 'name': '江苏'},
 {'id': 'CN-JX', 'name': '江西'},
 {'id': 'CN-LN', 'name': '辽宁'},
 {'id': 'CN-QH', 'name': '青海'},
 {'id': 'CN-SC', 'name': '四川'},
 {'id': 'CN-SD', 'name': '山东'},
 {'id': 'CN-SN', 'name': '陕西'},
 {'id': 'CN-SX', 'name': '山西'},
 {'id': 'CN-TJ', 'name': '天津'},
 {'id': 'CN-YN', 'name': '云南'},
 {'id': 'CN-GX', 'name': '广西'},
 {'id': 'CN-NM', 'name': '内蒙古'},
 {'id': 'CN-NX', 'name': '宁夏'},
 {'id': 'CN-XJ', 'name': '新疆'},
 {'id': 'CN-XZ', 'name': '西藏自治区'},
 {'id': 'AF', 'name': '阿富汗'},
 {'id': 'AM', 'name': '亚美尼亚'},
 {'id': 'AZ', 'name': '阿塞拜疆'},
 {'id': 'BH', 'name': '巴林'},
 {'id': 'BD', 'name': '孟加拉国'},
 {'id': 'BT', 'name': '不丹'},
 {'id': 'IO', 'name': '英属印度洋领地'},
 {'id': 'BN', 'name': '文莱'},
 {'id': 'KH', 'name': '柬埔寨'},
 {'id': 'CN', 'name': '中国'},
 {'id': 'IN', 'name': '印度'},
 {'id': 'ID', 'name': '印尼'},
 {'id': 'IR', 'name': '伊朗'},
 {'id': 'IQ', 'name': '伊拉克'},
 {'id': 'IL', 'name': '以色列'},
 {'id': 'JP', 'name': '日本'},
 {'id': 'JO', 'name': '约旦'},
 {'id': 'KZ', 'name': '哈萨克斯坦'},
 {'id': 'KP', 'name': '朝鲜'},
 {'id': 'KR', 'name': '韩国'},
 {'id': 'KW', 'name': '科威特'},
 {'id': 'KG', 'name': '吉尔吉斯斯坦'},
 {'id': 'LA', 'name': '老挝'},
 {'id': 'LB', 'name': '黎巴嫩'},
 {'id': 'MY', 'name': '马来西亚'},
 {'id': 'MV', 'name': '马尔代夫'},
 {'id': 'MN', 'name': '蒙古'},
 {'id': 'MM', 'name': '缅甸'},
 {'id': 'NP', 'name': '尼泊尔'},
 {'id': 'OM', 'name': '阿曼'},
 {'id': 'PK', 'name': '巴基斯坦'},
 {'id': 'PS', 'name': '巴勒斯坦'},
 {'id': 'PH', 'name': '菲律宾'},
 {'id': 'QA', 'name': '卡塔尔'},
 {'id': 'SA', 'name': '沙特阿拉伯'},
 {'id': 'SG', 'name': '新加坡'},
 {'id': 'LK', 'name': '斯里兰卡'},
 {'id': 'SY', 'name': '叙利亚'},
 {'id': 'TJ', 'name': '塔吉克斯坦'},
 {'id': 'TH', 'name': '泰国'},
 {'id': 'TL', 'name': '东帝汶'},
 {'id': 'TM', 'name': '土库曼斯坦'},
 {'id': 'AE', 'name': '阿联酋'},
 {'id': 'UZ', 'name': '乌兹别克斯坦'},
 {'id': 'VN', 'name': '越南'},
 {'id': 'YE', 'name': '也门'},
 {'id': 'AX', 'name': '奥兰'},
 {'id': 'AL', 'name': '阿尔巴尼亚'},
 {'id': 'AD', 'name': '安道尔'},
 {'id': 'AT', 'name': '奥地利'},
 {'id': 'BY', 'name': '白俄罗斯'},
 {'id': 'BE', 'name': '比利时'},
 {'id': 'BA', 'name': '波黑'},
 {'id': 'BG', 'name': '保加利亚'},
 {'id': 'HR', 'name': '克罗地亚'},
 {'id': 'CY', 'name': '塞浦路斯'},
 {'id': 'CZ', 'name': '捷克'},
 {'id': 'DK', 'name': '丹麦'},
 {'id': 'EE', 'name': '爱沙尼亚'},
 {'id': 'FO', 'name': '法罗群岛'},
 {'id': 'FI', 'name': '芬兰'},
 {'id': 'FR', 'name': '法国'},
 {'id': 'GE', 'name': '格鲁吉亚'},
 {'id': 'DE', 'name': '德国'},
 {'id': 'GI', 'name': '直布罗陀'},
 {'id': 'GR', 'name': '希腊'},
 {'id': 'GG', 'name': '根西'},
 {'id': 'VA', 'name': '梵蒂冈'},
 {'id': 'HU', 'name': '匈牙利'},
 {'id': 'IS', 'name': '冰岛'},
 {'id': 'IE', 'name': '爱尔兰'},
 {'id': 'IM', 'name': '马恩岛'},
 {'id': 'IT', 'name': '意大利'},
 {'id': 'JE', 'name': '泽西'},
 {'id': 'XK', 'name': '科索沃'},
 {'id': 'LV', 'name': '拉脱维亚'},
 {'id': 'LI', 'name': '列支敦士登'},
 {'id': 'LT', 'name': '立陶宛'},
 {'id': 'LU', 'name': '卢森堡'},
 {'id': 'MT', 'name': '马耳他'},
 {'id': 'MD', 'name': '摩尔多瓦'},
 {'id': 'MC', 'name': '摩纳哥'},
 {'id': 'ME', 'name': '黑山'},
 {'id': 'NL', 'name': '荷兰'},
 {'id': 'MK', 'name': '北马其顿'},
 {'id': 'NO', 'name': '挪威'},
 {'id': 'PL', 'name': '波兰'},
 {'id': 'PT', 'name': '葡萄牙'},
 {'id': 'RO', 'name': '罗马尼亚'},
 {'id': 'RU', 'name': '俄罗斯'},
 {'id': 'SM', 'name': '圣马力诺'},
 {'id': 'RS', 'name': '塞尔维亚'},
 {'id': 'SK', 'name': '斯洛伐克'},
 {'id': 'SI', 'name': '斯洛文尼亚'},
 {'id': 'ES', 'name': '西班牙'},
 {'id': 'SJ', 'name': '斯瓦尔巴和扬马延'},
 {'id': 'SE', 'name': '瑞典'},
 {'id': 'CH', 'name': '瑞士'},
 {'id': 'TR', 'name': '土耳其'},
 {'id': 'UA', 'name': '乌克兰'},
 {'id': 'GB', 'name': '英国'},
 {'id': 'DZ', 'name': '阿尔及利亚'},
 {'id': 'AO', 'name': '安哥拉'},
 {'id': 'BJ', 'name': '贝宁'},
 {'id': 'BW', 'name': '博茨瓦纳'},
 {'id': 'BV', 'name': '布韦岛'},
 {'id': 'BF', 'name': '布基纳法索'},
 {'id': 'BI', 'name': '布隆迪'},
 {'id': 'CV', 'name': '佛得角'},
 {'id': 'CM', 'name': '喀麦隆'},
 {'id': 'CF', 'name': '中非'},
 {'id': 'TD', 'name': '乍得'},
 {'id': 'KM', 'name': '科摩罗'},
 {'id': 'CG', 'name': '刚果共和国'},
 {'id': 'CD', 'name': '刚果民主共和国'},
 {'id': 'CI', 'name': '科特迪瓦'},
 {'id': 'DJ', 'name': '吉布提'},
 {'id': 'EG', 'name': '埃及'},
 {'id': 'GQ', 'name': '赤道几内亚'},
 {'id': 'ER', 'name': '厄立特里亚'},
 {'id': 'SZ', 'name': '斯威士兰'},
 {'id': 'ET', 'name': '埃塞俄比亚'},
 {'id': 'GA', 'name': '加蓬'},
 {'id': 'GM', 'name': '冈比亚'},
 {'id': 'GH', 'name': '加纳'},
 {'id': 'GN', 'name': '几内亚'},
 {'id': 'GW', 'name': '几内亚比绍'},
 {'id': 'KE', 'name': '肯尼亚'},
 {'id': 'LS', 'name': '莱索托'},
 {'id': 'LR', 'name': '利比里亚'},
 {'id': 'LY', 'name': '利比亚'},
 {'id': 'MG', 'name': '马达加斯加'},
 {'id': 'MW', 'name': '马拉维'},
 {'id': 'ML', 'name': '马里'},
 {'id': 'MR', 'name': '毛里塔尼亚'},
 {'id': 'MU', 'name': '毛里求斯'},
 {'id': 'YT', 'name': '马约特'},
 {'id': 'MA', 'name': '摩洛哥'},
 {'id': 'MZ', 'name': '莫桑比克'},
 {'id': 'NA', 'name': '纳米比亚'},
 {'id': 'NE', 'name': '尼日尔'},
 {'id': 'NG', 'name': '尼日利亚'},
 {'id': 'RE', 'name': '留尼汪'},
 {'id': 'RW', 'name': '卢旺达'},
 {'id': 'SH', 'name': '圣赫勒拿、阿森松和特里斯坦-达库尼亚'},
 {'id': 'ST', 'name': '圣多美和普林西比'},
 {'id': 'SN', 'name': '塞内加尔'},
 {'id': 'SC', 'name': '塞舌尔'},
 {'id': 'SL', 'name': '塞拉利昂'},
 {'id': 'SO', 'name': '索马里'},
 {'id': 'ZA', 'name': '南非'},
 {'id': 'SS', 'name': '南苏丹'},
 {'id': 'SD', 'name': '苏丹'},
 {'id': 'TZ', 'name': '坦桑尼亚'},
 {'id': 'TG', 'name': '多哥'},
 {'id': 'TN', 'name': '突尼斯'},
 {'id': 'UG', 'name': '乌干达'},
 {'id': 'EH', 'name': '西撒哈拉'},
 {'id': 'ZM', 'name': '赞比亚'},
 {'id': 'ZW', 'name': '津巴布韦'},
 {'id': 'AI', 'name': '安圭拉'},
 {'id': 'AG', 'name': '安提瓜和巴布达'},
 {'id': 'AW', 'name': '阿鲁巴'},
 {'id': 'BS', 'name': '巴哈马'},
 {'id': 'BB', 'name': '巴巴多斯'},
 {'id': 'BZ', 'name': '伯利兹'},
 {'id': 'BM', 'name': '百慕大'},
 {'id': 'BQ', 'name': '荷兰加勒比区'},
 {'id': 'CA', 'name': '加拿大'},
 {'id': 'KY', 'name': '开曼群岛'},
 {'id': 'CR', 'name': '哥斯达黎加'},
 {'id': 'CU', 'name': '古巴'},
 {'id': 'CW', 'name': '库拉索'},
 {'id': 'DM', 'name': '多米尼克'},
 {'id': 'DO', 'name': '多米尼加'},
 {'id': 'SV', 'name': '萨尔瓦多'},
 {'id': 'GL', 'name': '格陵兰'},
 {'id': 'GD', 'name': '格林纳达'},
 {'id': 'GP', 'name': '瓜德罗普'},
 {'id': 'GT', 'name': '危地马拉'},
 {'id': 'HT', 'name': '海地'},
 {'id': 'HN', 'name': '洪都拉斯'},
 {'id': 'JM', 'name': '牙买加'},
 {'id': 'MQ', 'name': '马提尼克'},
 {'id': 'MX', 'name': '墨西哥'},
 {'id': 'MS', 'name': '蒙特塞拉特'},
 {'id': 'NI', 'name': '尼加拉瓜'},
 {'id': 'PA', 'name': '巴拿马'},
 {'id': 'PR', 'name': '波多黎各'},
 {'id': 'BL', 'name': '圣巴泰勒米'},
 {'id': 'KN', 'name': '圣基茨和尼维斯'},
 {'id': 'LC', 'name': '圣卢西亚'},
 {'id': 'MF', 'name': '法属圣马丁'},
 {'id': 'PM', 'name': '圣皮埃尔和密克隆'},
 {'id': 'VC', 'name': '圣文森特和格林纳丁斯'},
 {'id': 'SX', 'name': '荷属圣马丁'},
 {'id': 'TT', 'name': '特立尼达和多巴哥'},
 {'id': 'TC', 'name': '特克斯和凯科斯群岛'},
 {'id': 'US', 'name': '美国'},
 {'id': 'VG', 'name': '英属维尔京群岛'},
 {'id': 'VI', 'name': '美属维尔京群岛'},
 {'id': 'AR', 'name': '阿根廷'},
 {'id': 'BO', 'name': '玻利维亚'},
 {'id': 'BR', 'name': '巴西'},
 {'id': 'CL', 'name': '智利'},
 {'id': 'CO', 'name': '哥伦比亚'},
 {'id': 'EC', 'name': '厄瓜多尔'},
 {'id': 'FK', 'name': '福克兰群岛'},
 {'id': 'GF', 'name': '法属圭亚那'},
 {'id': 'GY', 'name': '圭亚那'},
 {'id': 'PY', 'name': '巴拉圭'},
 {'id': 'PE', 'name': '秘鲁'},
 {'id': 'GS', 'name': '南乔治亚和南桑威奇群岛'},
 {'id': 'SR', 'name': '苏里南'},
 {'id': 'UY', 'name': '乌拉圭'},
 {'id': 'VE', 'name': '委内瑞拉'},
 {'id': 'AU', 'name': '澳大利亚'},
 {'id': 'FJ', 'name': '斐济'},
 {'id': 'TV', 'name': '图瓦卢'},
 {'id': 'GU', 'name': '关岛'},
 {'id': 'NZ', 'name': '新西兰'},
 {'id': 'WF', 'name': '瓦利斯和富图纳'},
 {'id': 'CX', 'name': '圣诞岛'},
 {'id': 'PN', 'name': '皮特凯恩群岛'},
 {'id': 'WS', 'name': '萨摩亚'},
 {'id': 'PW', 'name': '帕劳'},
 {'id': 'NF', 'name': '诺福克岛'},
 {'id': 'NC', 'name': '新喀里多尼亚'},
 {'id': 'TK', 'name': '托克劳'},
 {'id': 'CC', 'name': '科科斯（基林）群岛'},
 {'id': 'CK', 'name': '库克群岛'},
 {'id': 'FM', 'name': '密克罗尼西亚联邦'},
 {'id': 'KI', 'name': '基里巴斯'},
 {'id': 'TO', 'name': '汤加'},
 {'id': 'MP', 'name': '北马里亚纳群岛'},
 {'id': 'AS', 'name': '美属萨摩亚'},
 {'id': 'NR', 'name': '瑙鲁'},
 {'id': 'PF', 'name': '法属波利尼西亚'},
 {'id': 'VU', 'name': '瓦努阿图'},
 {'id': 'NU', 'name': '纽埃'},
 {'id': 'PG', 'name': '巴布亚新几内亚'},
 {'id': 'UM', 'name': '美国本土外小岛屿'},
 {'id': 'SB', 'name': '所罗门群岛'},
 {'id': 'MH', 'name': '马绍尔群岛'},
 {'id': 'HM', 'name': '赫德岛和麦克唐纳群岛'},
 {'id': 'TF', 'name': '法属南部和南极领地'},
 {'id': 'AQ', 'name': '南极洲'},
 {'id': 'CN-MO', 'name': '澳门'},
 {'id': 'CN-HK', 'name': '香港'},
 {'id': 'CN-TW', 'name': '台湾'}]


def require(value, code="invalid_safeline_security"):
    if not value:
        raise SafeLineError(code, 422)


def integer(value, low=0, high=9_007_199_254_740_991):
    require(type(value) is int and low <= value <= high)
    return value


def bounded(value, key=None):
    result = rows(value, key) if key else value
    if not isinstance(result, list) or len(result) > 10000 or any(not isinstance(r, dict) for r in result):
        raise SafeLineError("safeline_unexpected_response")
    return result


def stamp(value):
    return value if type(value) is int and 0 <= value <= 9_007_199_254_740_991 else None


def application_fields(row):
    names = row.get("site_server_names")
    return {"site_id": stamp(row.get("site_id")),
            "site_name": text(row.get("site_comment"), 1024) or text(row.get("site_title"), 1024) or "—",
            "domains": [text(n, 253) for n in names[:128] if isinstance(n, str)] if isinstance(names, list) else []}


def history_params(kind, params):
    result = {k: v for k, v in params.items() if k not in {"start", "end"}}
    for key in ("start", "end"):
        if key in params:
            target = ("begin" if key == "start" else "end") if kind == "rate" else "started_at_" + ("begin" if key == "start" else "end")
            result[target] = (params[key] + 999) // 1000 if kind == "rate" or key == "end" else params[key] // 1000
    return result


async def history(service, kind, params):
    client, revision = service.client()
    data = await client.get("/api/open/records/" + ("acl" if kind == "rate" else "waiting"), history_params(kind, params))
    values = bounded(data, "data"); total = data.get("total")
    if type(total) is not int or not 0 <= total <= 9_007_199_254_740_991 or len(values) > params["page_size"] or total < len(values):
        raise SafeLineError("safeline_unexpected_response")
    items = []
    for row in values:
        item = application_fields(row)
        if kind == "rate":
            item.update({k: text(row.get(k), 1024) for k in ("ip", "country", "province", "city", "reason", "result", "action", "created_at", "valid_before")})
            item.update({k: stamp(row.get(k)) for k in ("id", "status", "period", "count", "denied_count", "block_min")})
        else:
            item.update({k: stamp(row.get(k)) for k in ("rule_id", "max_concurrent", "total_waiting", "top_waiting", "avg_wait_sec", "dur_sec", "started_at", "ended_at")})
            rate = row.get("bounce_rate")
            item["bounce_rate"] = rate if type(rate) in {int, float} and math.isfinite(rate) and 0 <= rate <= 1 else None
        items.append(item)
    return safe({"items": items, "total": total, "connection_revision": revision, "updated_at": now()}, client.connection.token)


class SecurityEditor:
    def __init__(self, editor):
        self.editor, self.service, self.store = editor, editor.service, editor.store

    async def raw(self, client, area, section, params):
        if area == "cc" and section == "relieve":
            items, total = [], None
            for page in range(1, 51):
                data = await client.get("/api/open/records/acl", history_params("rate", {**params, "page": page, "page_size": 100}))
                batch = bounded(data, "data"); total = data.get("total")
                if type(total) is not int or total < 0 or len(batch) > 100:
                    raise SafeLineError("safeline_unexpected_response")
                items.extend(batch)
                if len(items) >= total or not batch: break
            if len(items) != total or any(type(r.get("id")) is not int or r["id"] <= 0 for r in items) or len({r["id"] for r in items}) != len(items):
                raise SafeLineError("safeline_edit_incomplete", 409)
            return {"ids": sorted(integer(r.get("id"), 1) for r in items if type(r.get("status")) is int and r["status"] > 0), "filters": params}
        edition = await client.get("/api/open/system/edition")
        level = edition.get("effective_edition", edition.get("version")) if isinstance(edition, dict) else None
        raw = {"edition": level if type(level) is int and level in {0, 1, 2} else None, "scope": params}
        if area == "cc":
            raw["rules"] = bounded(await client.get("/api/open/site/0/acl"))
            if (any(type(r.get("id")) is not int or r["id"] <= 0 for r in raw["rules"]) or
                    len({r["id"] for r in raw["rules"]}) != len(raw["rules"])):
                raise SafeLineError("safeline_unexpected_response")
            return raw
        values, total, limited = await self.service.policies(client)
        if limited or type(total) is not int or len(values) != total:
            raise SafeLineError("safeline_edit_incomplete", 409)
        if any(type(r.get("id")) is not int or r["id"] <= 0 for r in values) or len({r["id"] for r in values}) != len(values):
            raise SafeLineError("safeline_unexpected_response")
        raw["rules"] = values
        if section == "subscribe":
            raw["catalog"] = bounded(await client.get("/api/open/cloud/policies"))
        if section in {"create", "edit", "summary"}:
            async def optional(path):
                try: return await client.get(path)
                except SafeLineError: return None
            groups, sites, ja4 = await asyncio.gather(optional("/api/open/ipgroup"), optional("/api/open/site"), optional("/api/open/ja4"))
            raw["groups"] = [take(v, ("id", "comment")) for v in bounded(groups, "nodes")] if isinstance(groups, dict) and isinstance(groups.get("nodes"), list) else []
            raw["sites"] = [take(v, ("id", "comment", "title", "server_names")) for v in bounded(sites, "data")] if isinstance(sites, dict) and isinstance(sites.get("data"), list) else []
            raw["ja4"] = [take(ja4, ("id", "name"))] if isinstance(ja4, dict) and type(ja4.get("id")) is int and ja4["id"] > 0 else []
        if section in {"edit", "summary", "switch", "delete"}:
            value = await client.get("/api/open/policy/detail", {"id": params["id"]})
            if not isinstance(value, dict) or value.get("id") != params["id"]:
                raise SafeLineError("safeline_resource_not_found", 404)
            raw["rule"] = value
        return raw

    def public(self, raw, area, section, token):
        caps = {"edition": raw.get("edition"), "premium": raw.get("edition") in {1, 2}}
        if area == "cc":
            rules, locked = public_rules([take(r, RULE_FIELDS) for r in raw.get("rules", [])], token)
            return {"values": {"ids": raw["ids"]} if section == "relieve" else {"rules": rules},
                    "locked": [] if section == "relieve" else locked, "capabilities": caps}
        rules = [policy(row) for row in raw["rules"]]
        result = {"values": {}, "rules": rules, "locked": [], "capabilities": caps}
        if section == "subscribe":
            result["catalog"] = [take(row, ("id", "policy_name", "org_name", "black_rule", "white_rule", "compatible", "tags", "added", "updated_at")) for row in raw["catalog"]]
        if section in {"create", "edit", "summary"}:
            result["options"] = {"targets": [{"id": key, "name": label, "operators": [{"id": op, "name": OPERATORS[op]} for op in ops.split()]} for key, (label, ops) in TARGETS.items()],
                                 "groups": raw["groups"], "sites": raw["sites"], "ja4": raw["ja4"], "geo": GEO}
        if section in {"switch", "delete"}:
            original = raw["rule"]
            result["values"] = take(original, ("id", "action", "name", "is_enabled", "builtin", "cloud_id", "black_rule", "white_rule"))
            result["capabilities"].update(can_switch=original.get("action") in {0, 1, 999} and type(original.get("is_enabled")) is bool,
                can_delete=not original.get("builtin") and original.get("action") in {0, 1, 999})
        if section in {"edit", "summary"}:
            original = raw["rule"]
            result["values"] = take(original, ("id", "action", "name", "is_enabled", "log", "pattern"))
            try:
                pattern = original.get("pattern")
                require(isinstance(pattern, list) and bool(pattern))
                normalized = [[validated(Pattern, row) for row in group] for group in pattern if isinstance(group, list) and group]
                require(normalized == pattern and all(r["k"] in TARGETS and r["op"] in TARGETS[r["k"]][1].split()
                    and not any("\n" in v or "\r" in v for v in r["v"]) for group in normalized for r in group))
                editable_pattern = True
            except SafeLineError:
                editable_pattern = False
            if not editable_pattern or safe(result["values"], token) != result["values"] or policy(original)["pattern"] != pattern:
                result["values"].pop("pattern", None); result["locked"].append("pattern")
            if original.get("builtin") or original.get("cloud_id", 0) or original.get("action") not in {0, 1}:
                result["locked"] = list(result["values"])
            if section == "summary":
                result["values"].update(take(original, ("order", "builtin", "cloud_id", "negate", "created_at", "updated_at", "req_count", "pass_count", "black_rule", "white_rule")))
                result["preview"] = policy(original)["pattern"]
                custom = not original.get("builtin") and not original.get("cloud_id") and original.get("action") in {0, 1}
                result["capabilities"].update(can_edit=custom, can_edit_pattern=custom and "pattern" not in result["locked"],
                    can_switch=original.get("action") in {0, 1, 999} and type(original.get("is_enabled")) is bool,
                    can_delete=not original.get("builtin") and original.get("action") in {0, 1, 999})
        return safe(result, token)

    async def load(self, area, section, params):
        client, revision = self.service.client()
        if client.connection is None: raise SafeLineError("safeline_not_configured", 409)
        raw = await self.raw(client, area, section, params)
        return {**self.public(raw, area, section, client.connection.token), "fingerprint": self.resource_fingerprint(client, revision, raw),
                "connection_revision": revision, "updated_at": now()}

    def resource_fingerprint(self, client, revision, raw):
        # Live hit counters change independently of configuration while a dialog is open.
        stable = copy.deepcopy(raw)
        for rule in stable.get("rules", []) + ([stable["rule"]] if "rule" in stable else []):
            for key in ("req_count", "pass_count", "deny_count"):
                rule.pop(key, None)
        return self.editor.resource_fingerprint(client, revision, stable)

    def patterns(self, value, raw):
        require(isinstance(value, list) and 1 <= len(value) <= 64)
        originals = [r for group in raw.get("rule", {}).get("pattern") or [] if isinstance(group, list) for r in group]
        result = []
        for group in value:
            require(isinstance(group, list) and 1 <= len(group) <= 64)
            output = []
            for row in group:
                item = validated(Pattern, row)
                if item in originals: output.append(copy.deepcopy(item)); continue
                key, op, values = item["k"], item["op"], item["v"]
                require(key in TARGETS and op in TARGETS[key][1].split())
                if op in {"geo_eq", "geo_not_eq"} or key in {"res_body", "res_raw_header"}:
                    if raw["edition"] not in {1, 2}: raise SafeLineError("safeline_feature_unavailable", 409)
                require(all(not any(ord(c) < 32 or ord(c) == 127 for c in v) for v in values))
                if key in {"get_param", "post_param", "req_header"}:
                    require(bool(item["sub_k"]) and not any(ord(c) < 33 or ord(c) == 127 for c in item["sub_k"]))
                else: require(not item["sub_k"])
                require(not values if op in {"exist", "not_exist"} else bool(values) and all(values))
                if key == "src_ip" and op in {"eq", "not_eq", "cidr", "not_cidr"}:
                    try:
                        values = [str(ipaddress.ip_network(v, strict=False) if op in {"cidr", "not_cidr"} else ipaddress.ip_address(v)) for v in values]
                        require(not (op == "eq" and "0.0.0.0" in values))
                    except ValueError: raise SafeLineError("invalid_safeline_ips", 422) from None
                if key == "src_ip" and op == "match":
                    require(all(re.fullmatch(r"[0-9a-fA-F:.*?]{1,128}", v) for v in values))
                if key == "src_ip" and op in {"in", "not_in"} or key == "website" or op == "ja4_in":
                    known = {str(r["id"]) for r in raw["groups" if key == "src_ip" else "sites" if key == "website" else "ja4"]}
                    require(all(v in known for v in values))
                if key == "method": require(set(values) <= {"GET", "HEAD", "POST", "PUT", "DELETE", "CONNECT", "OPTIONS", "TRACE", "PATCH"})
                if op in {"geo_eq", "geo_not_eq"}: require(set(values) <= {row["id"] for row in GEO})
                item["v"] = values; output.append(item)
            require(not (any(r["k"] in {"res_body", "res_raw_header"} for r in output) and any(r["k"] in {"req_body", "post_param"} for r in output)))
            result.append(output)
        return result

    def plan(self, client, raw, area, section, params, changes):
        require(isinstance(changes, dict) and bool(changes))
        if area == "cc" and section == "relieve":
            require(set(changes) == {"ids"} and isinstance(changes["ids"], list) and 1 <= len(changes["ids"]) <= 5000)
            ids = [integer(v, 1) for v in changes["ids"]]; require(len(set(ids)) == len(ids) and set(ids) <= set(raw["ids"]))
            async def verify(_):
                updated = await self.raw(client, area, section, params)
                return not set(ids) & set(updated["ids"])
            return [("PUT", "/api/acl/relieve", {"id": ids})], verify
        if area == "cc":
            require(set(changes) == {"rules"} and isinstance(changes["rules"], list) and bool(changes["rules"]))
            originals = {r["id"]: r for r in raw["rules"]}; expected = {}; merged = copy.deepcopy(originals)
            for patch in changes["rules"]:
                require(isinstance(patch, dict) and set(patch) <= {"id", "enabled", "period", "count", "block_min", "action", "custom_status_codes"})
                key = integer(patch.get("id"), 1); require(key in originals and key not in expected and len(patch) > 1)
                if "enabled" in patch: require(type(patch["enabled"]) is bool)
                for name in ("period", "count", "block_min"):
                    if name in patch: integer(patch[name], 1, 1_000_000_000 if name == "count" else 86400)
                if "action" in patch:
                    require(type(patch["action"]) is str and patch["action"] in {"block", "challenge_v1", "challenge_v2"})
                    if patch["action"] == "challenge_v2" and originals[key].get("action") != "challenge_v2" and raw["edition"] not in {1, 2}:
                        raise SafeLineError("safeline_feature_unavailable", 409)
                if "custom_status_codes" in patch: require(isinstance(patch["custom_status_codes"], list) and len(patch["custom_status_codes"]) <= 64 and all(type(v) is str and re.fullmatch(r"[1-5][0-9]{2}", v) for v in patch["custom_status_codes"]))
                expected[key] = patch; merged[key].update(patch)
            async def verify(_):
                current = {r["id"]: r for r in bounded(await client.get("/api/open/site/0/acl"))}
                return set(current) == set(originals) and all(all(current[key].get(k) == v for k, v in patch.items()) for key, patch in expected.items())
            return [("PUT", "/api/open/site/0/acl", {"rules": list(merged.values())})], verify
        if section == "switch":
            original = raw["rule"]
            require(original.get("action") in {0, 1, 999} and type(original.get("is_enabled")) is bool)
            require(set(changes) == {"is_enabled"} and type(changes["is_enabled"]) is bool)
            async def verify(_):
                value = await client.get("/api/open/policy/detail", {"id": params["id"]})
                return isinstance(value, dict) and value.get("id") == params["id"] and value.get("is_enabled") is changes["is_enabled"]
            return [("PUT", "/api/open/policy/switch", {"id": params["id"], **changes})], verify
        if section == "delete":
            original = raw["rule"]
            require(not original.get("builtin") and original.get("action") in {0, 1, 999})
            require(set(changes) == {"confirm_name"} and type(changes["confirm_name"]) is str and changes["confirm_name"] == original.get("name"))
            async def verify(_):
                values, total, limited = await self.service.policies(client)
                return not limited and len(values) == total and not any(r["id"] == params["id"] for r in values)
            return [("DELETE", "/api/open/policy", {"id": params["id"]})], verify
        if section == "summary":
            original = raw["rule"]
            require(not original.get("builtin") and not original.get("cloud_id") and original.get("action") in {0, 1})
            require(set(changes) <= {"name", "action", "order", "pattern"})
            metadata = {k: v for k, v in changes.items() if k != "order"}
            writes, check_metadata = self.plan(client, raw, area, "edit", params, metadata) if metadata else ([], None)
            orders = []
            if "order" in changes:
                action = changes.get("action", original["action"])
                other = sorted((r for r in raw["rules"] if r["id"] != params["id"] and r.get("action") == action and not r.get("builtin") and not r.get("cloud_id")), key=lambda r: r.get("order", 0))
                position = integer(changes["order"], 1, len(other) + 1)
                identifiers = [r["id"] for r in other]; identifiers.insert(position - 1, params["id"])
                orders = [{"id": key, "order": i + 1} for i, key in enumerate(identifiers)]
                writes.append(("PUT", "/api/open/policy/orders", {"orders": orders}))
            require(bool(writes))
            async def verify(result):
                if check_metadata and not await check_metadata(result): return False
                if not orders: return True
                values, total, limited = await self.service.policies(client); current = {r["id"]: r for r in values}
                return not limited and len(values) == total and all(current.get(r["id"], {}).get("order") == r["order"] for r in orders)
            return writes, verify
        if section == "order":
            require(set(changes) == {"ids"} and isinstance(changes["ids"], list))
            action = params["action"]; originals = [r for r in raw["rules"] if r.get("action") == action and not r.get("builtin") and not r.get("cloud_id")]
            ids = [integer(v, 1) for v in changes["ids"]]
            require(bool(ids) and len(ids) == len(set(ids)) and set(ids) == {r["id"] for r in originals})
            orders = [{"id": key, "order": i + 1} for i, key in enumerate(ids)]
            async def verify(_):
                values, _, limited = await self.service.policies(client); current = {r["id"]: r for r in values}
                return not limited and all(current.get(row["id"], {}).get("order") == row["order"] for row in orders)
            return [("PUT", "/api/open/policy/orders", {"orders": orders})], verify
        if section == "subscribe":
            require(set(changes) == {"id"}); key = integer(changes["id"], 1)
            item = next((v for v in raw["catalog"] if v.get("id") == key), None)
            require(item is not None and item.get("added") is False)
            async def verify(_): return any(v.get("id") == key and v.get("added") is True for v in bounded(await client.get("/api/open/cloud/policies")))
            return [("POST", "/api/open/cloud/policies/subscribe", {"id": key})], verify
        create = section == "create"; original = raw.get("rule", {})
        allowed = {"action", "name", "is_enabled", "log", "pattern", "insert_position"}
        require(set(changes) <= allowed)
        if not create:
            require(not original.get("builtin") and not original.get("cloud_id") and original.get("action") in {0, 1})
        if "action" in changes: integer(changes["action"], 0, 1)
        for key in ("is_enabled", "log"):
            if key in changes: require(type(changes[key]) is bool)
        if "name" in changes: require(type(changes["name"]) is str and 1 <= len(changes["name"].strip()) <= 1024 and not any(ord(c) < 32 or ord(c) == 127 for c in changes["name"]))
        if "insert_position" in changes: require(changes["insert_position"] in {"first", "last"})
        public = self.public(raw, area, section, client.connection.token)
        if "pattern" in changes:
            require("pattern" not in public["locked"], "safeline_edit_unsupported")
            changes = {**changes, "pattern": self.patterns(changes["pattern"], raw)}
        if create:
            require(set(changes) == allowed)
            payload = copy.deepcopy(changes)
        else:
            payload = {**take(original, ("id", "action", "expire", "is_enabled", "level", "log", "name", "pattern")), **changes}
        existing = {r["id"] for r in raw["rules"]}
        async def verify(result):
            key = result if create else params["id"]
            if type(key) is not int or key <= 0 or create and key in existing: return False
            updated = await client.get("/api/open/policy/detail", {"id": key})
            return isinstance(updated, dict) and updated.get("id") == key and all(updated.get(k) == v for k, v in payload.items() if k != "insert_position")
        return [("POST" if create else "PUT", "/api/open/policy", payload)], verify

    async def save(self, area, section, params, command, actor):
        async with self.editor.lock:
            digest = fingerprint({"area": area, "section": section, "params": params, **command.model_dump()})
            previous = self.store.receipt(command.operation_id, actor, digest)
            if previous is not None:
                return {**previous, "replayed": True, "operation_id": command.operation_id, "connection_revision": command.expected_revision}
            client, revision = self.service.client()
            if client.connection is None or revision != command.expected_revision: raise SafeLineError("safeline_connection_changed", 409)
            raw = await self.raw(client, area, section, params)
            if self.resource_fingerprint(client, revision, raw) != command.fingerprint: raise SafeLineError("safeline_resource_changed", 409)
            try: writes, verify = self.plan(client, raw, area, section, params, command.changes)
            except (TypeError, ValueError, KeyError, AttributeError): raise SafeLineError("invalid_safeline_security", 422) from None
            if self.store.load()[1] != revision: raise SafeLineError("safeline_connection_changed", 409)
            self.store.reserve_edit(command.operation_id, actor, digest, fingerprint({"revision": revision, "area": area, "section": section, "params": params}))
            result = {"outcome": "unknown", "code": "safeline_write_unknown"}; accepted = False
            try:
                reply = None
                for method, path, payload in writes:
                    reply = await client.write(method, path, payload); accepted = True
                if await verify(reply): result = {"outcome": "confirmed", "code": "safeline_write_confirmed"}
            except SafeLineError as error:
                if not accepted and error.code in {"safeline_invalid_credentials", "safeline_permission_denied", "safeline_endpoint_unavailable", "safeline_business_error", "safeline_sso_required"}:
                    result = {"outcome": "failed", "code": error.code}
            finally: self.store.finish_edit(command.operation_id, result)
            return {**result, "operation_id": command.operation_id, "connection_revision": revision, "updated_at": now(), "replayed": False}
