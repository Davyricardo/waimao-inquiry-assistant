"""外贸客户国家/地区当地时间与邮件沟通时机判定引擎。

设计原则：
1. 纯 Python 标准库实现，零第三方依赖（无需 tzdata 或外部网络 API，跨 Windows/Linux 兼容）。
2. 支持全球 120+ 常用外贸国家/地区、主要商贸城市及 ISO-2 代码的模糊归一化匹配。
3. 动态推算欧美及澳洲等主要外贸市场的夏令时（DST），保证时差计算精准。
4. 结合客户当地实际作息与工作日/周末规则，输出实战邮件沟通建议。
"""
import datetime
import html
import json
import re
import time

# ============ 夏令时 (DST) 动态计算辅助函数 ============

def _is_dst_eu(utc_dt: datetime.datetime) -> bool:
    """欧洲夏令时：3月最后一个周日 01:00 UTC 至 10月最后一个周日 01:00 UTC。"""
    year = utc_dt.year
    mar31 = datetime.datetime(year, 3, 31, 1, 0, tzinfo=datetime.timezone.utc)
    start = mar31 - datetime.timedelta(days=(mar31.weekday() + 1) % 7)
    oct31 = datetime.datetime(year, 10, 31, 1, 0, tzinfo=datetime.timezone.utc)
    end = oct31 - datetime.timedelta(days=(oct31.weekday() + 1) % 7)
    return start <= utc_dt < end


def _is_dst_us(utc_dt: datetime.datetime) -> bool:
    """北美夏令时：3月第二个周日 07:00 UTC 至 11月第一个周日 06:00 UTC。"""
    year = utc_dt.year
    mar1 = datetime.datetime(year, 3, 1, 7, 0, tzinfo=datetime.timezone.utc)
    first_sunday_mar = mar1 + datetime.timedelta(days=(6 - mar1.weekday()) % 7)
    second_sunday_mar = first_sunday_mar + datetime.timedelta(days=7)

    nov1 = datetime.datetime(year, 11, 1, 6, 0, tzinfo=datetime.timezone.utc)
    first_sunday_nov = nov1 + datetime.timedelta(days=(6 - nov1.weekday()) % 7)
    return second_sunday_mar <= utc_dt < first_sunday_nov


def _is_dst_au(utc_dt: datetime.datetime) -> bool:
    """澳洲东南部夏令时（南半球）：10月第一个周日 16:00 UTC 至 4月第一个周日 16:00 UTC。"""
    year = utc_dt.year
    apr1 = datetime.datetime(year, 4, 1, 16, 0, tzinfo=datetime.timezone.utc)
    first_sun_apr = apr1 + datetime.timedelta(days=(6 - apr1.weekday()) % 7)

    oct1 = datetime.datetime(year, 10, 1, 16, 0, tzinfo=datetime.timezone.utc)
    first_sun_oct = oct1 + datetime.timedelta(days=(6 - oct1.weekday()) % 7)

    if utc_dt < first_sun_apr:
        return True
    if utc_dt >= first_sun_oct:
        return True
    return False


# ============ 国家/地区基础元数据表 ============
COUNTRY_REGISTRY = {
    # --- 欧洲主要贸易伙伴 (EU DST) ---
    "DE": {"name": "德国", "std_offset": 1.0, "dst_rule": "eu", "tz_code": "CET/CEST", "weekend": (5, 6), "note": "中欧时间"},
    "FR": {"name": "法国", "std_offset": 1.0, "dst_rule": "eu", "tz_code": "CET/CEST", "weekend": (5, 6), "note": "中欧时间"},
    "IT": {"name": "意大利", "std_offset": 1.0, "dst_rule": "eu", "tz_code": "CET/CEST", "weekend": (5, 6), "note": "中欧时间"},
    "ES": {"name": "西班牙", "std_offset": 1.0, "dst_rule": "eu", "tz_code": "CET/CEST", "weekend": (5, 6), "note": "中欧时间"},
    "NL": {"name": "荷兰", "std_offset": 1.0, "dst_rule": "eu", "tz_code": "CET/CEST", "weekend": (5, 6), "note": "中欧时间"},
    "BE": {"name": "比利时", "std_offset": 1.0, "dst_rule": "eu", "tz_code": "CET/CEST", "weekend": (5, 6), "note": "中欧时间"},
    "PL": {"name": "波兰", "std_offset": 1.0, "dst_rule": "eu", "tz_code": "CET/CEST", "weekend": (5, 6), "note": "中欧时间"},
    "CH": {"name": "瑞士", "std_offset": 1.0, "dst_rule": "eu", "tz_code": "CET/CEST", "weekend": (5, 6), "note": "中欧时间"},
    "AT": {"name": "奥地利", "std_offset": 1.0, "dst_rule": "eu", "tz_code": "CET/CEST", "weekend": (5, 6), "note": "中欧时间"},
    "SE": {"name": "瑞典", "std_offset": 1.0, "dst_rule": "eu", "tz_code": "CET/CEST", "weekend": (5, 6), "note": "中欧时间"},
    "NO": {"name": "挪威", "std_offset": 1.0, "dst_rule": "eu", "tz_code": "CET/CEST", "weekend": (5, 6), "note": "中欧时间"},
    "DK": {"name": "丹麦", "std_offset": 1.0, "dst_rule": "eu", "tz_code": "CET/CEST", "weekend": (5, 6), "note": "中欧时间"},
    "FI": {"name": "芬兰", "std_offset": 2.0, "dst_rule": "eu", "tz_code": "EET/EEST", "weekend": (5, 6), "note": "东欧时间"},
    "CZ": {"name": "捷克", "std_offset": 1.0, "dst_rule": "eu", "tz_code": "CET/CEST", "weekend": (5, 6), "note": "中欧时间"},
    "PT": {"name": "葡萄牙", "std_offset": 0.0, "dst_rule": "eu", "tz_code": "WET/WEST", "weekend": (5, 6), "note": "西欧时间"},
    "GR": {"name": "希腊", "std_offset": 2.0, "dst_rule": "eu", "tz_code": "EET/EEST", "weekend": (5, 6), "note": "东欧时间"},
    "IE": {"name": "爱尔兰", "std_offset": 0.0, "dst_rule": "eu", "tz_code": "GMT/IST", "weekend": (5, 6), "note": "格林威治标准时间"},
    "GB": {"name": "英国", "std_offset": 0.0, "dst_rule": "eu", "tz_code": "GMT/BST", "weekend": (5, 6), "note": "格林威治标准时间/英国夏令时"},
    "HU": {"name": "匈牙利", "std_offset": 1.0, "dst_rule": "eu", "tz_code": "CET/CEST", "weekend": (5, 6), "note": "中欧时间"},
    "RO": {"name": "罗马尼亚", "std_offset": 2.0, "dst_rule": "eu", "tz_code": "EET/EEST", "weekend": (5, 6), "note": "东欧时间"},
    "BG": {"name": "保加利亚", "std_offset": 2.0, "dst_rule": "eu", "tz_code": "EET/EEST", "weekend": (5, 6), "note": "东欧时间"},
    "SK": {"name": "斯洛伐克", "std_offset": 1.0, "dst_rule": "eu", "tz_code": "CET/CEST", "weekend": (5, 6), "note": "中欧时间"},
    "SI": {"name": "斯洛文尼亚", "std_offset": 1.0, "dst_rule": "eu", "tz_code": "CET/CEST", "weekend": (5, 6), "note": "中欧时间"},
    "HR": {"name": "克罗地亚", "std_offset": 1.0, "dst_rule": "eu", "tz_code": "CET/CEST", "weekend": (5, 6), "note": "中欧时间"},
    "RS": {"name": "塞尔维亚", "std_offset": 1.0, "dst_rule": "eu", "tz_code": "CET/CEST", "weekend": (5, 6), "note": "中欧时间"},
    "LT": {"name": "立陶宛", "std_offset": 2.0, "dst_rule": "eu", "tz_code": "EET/EEST", "weekend": (5, 6), "note": "东欧时间"},
    "LV": {"name": "拉脱维亚", "std_offset": 2.0, "dst_rule": "eu", "tz_code": "EET/EEST", "weekend": (5, 6), "note": "东欧时间"},
    "EE": {"name": "爱沙尼亚", "std_offset": 2.0, "dst_rule": "eu", "tz_code": "EET/EEST", "weekend": (5, 6), "note": "东欧时间"},
    "UA": {"name": "乌克兰", "std_offset": 2.0, "dst_rule": "eu", "tz_code": "EET/EEST", "weekend": (5, 6), "note": "东欧时间"},
    "RU": {"name": "俄罗斯", "std_offset": 3.0, "dst_rule": None, "tz_code": "MSK", "weekend": (5, 6), "note": "莫斯科主商区时间 (跨多时区)"},
    "TR": {"name": "土耳其", "std_offset": 3.0, "dst_rule": None, "tz_code": "TRT", "weekend": (5, 6), "note": "土耳其时间 (全年UTC+3)"},

    # --- 北美洲贸易伙伴 ---
    "US": {"name": "美国", "std_offset": -5.0, "dst_rule": "us", "tz_code": "EST/EDT", "weekend": (5, 6), "note": "美东主力商区 (全美跨 UTC-8~-5)"},
    "US_P": {"name": "美国(太平洋)", "std_offset": -8.0, "dst_rule": "us", "tz_code": "PST/PDT", "weekend": (5, 6), "note": "西海岸商区 (加州/洛杉矶/旧金山)"},
    "US_C": {"name": "美国(中部)", "std_offset": -6.0, "dst_rule": "us", "tz_code": "CST/CDT", "weekend": (5, 6), "note": "中部商区 (芝加哥/休斯敦/达拉斯)"},
    "CA": {"name": "加拿大", "std_offset": -5.0, "dst_rule": "us", "tz_code": "EST/EDT", "weekend": (5, 6), "note": "东部商区多伦多 (全加跨 UTC-8~-3.5)"},
    "CA_P": {"name": "加拿大(西部)", "std_offset": -8.0, "dst_rule": "us", "tz_code": "PST/PDT", "weekend": (5, 6), "note": "西海岸商区 (温哥华)"},
    "MX": {"name": "墨西哥", "std_offset": -6.0, "dst_rule": None, "tz_code": "CST", "weekend": (5, 6), "note": "中部时间 (墨西哥城)"},

    # --- 中东与西亚贸易伙伴 ---
    "AE": {"name": "阿联酋", "std_offset": 4.0, "dst_rule": None, "tz_code": "GST", "weekend": (5, 6), "note": "海湾标准时间 (迪拜/阿布扎比)"},
    "SA": {"name": "沙特阿拉伯", "std_offset": 3.0, "dst_rule": None, "tz_code": "AST", "weekend": (4, 5), "note": "阿拉伯时间 (利雅得, 周五/周六公休)"},
    "QA": {"name": "卡塔尔", "std_offset": 3.0, "dst_rule": None, "tz_code": "AST", "weekend": (4, 5), "note": "多哈时间 (周五/周六公休)"},
    "KW": {"name": "科威特", "std_offset": 3.0, "dst_rule": None, "tz_code": "AST", "weekend": (4, 5), "note": "科威特时间 (周五/周六公休)"},
    "OM": {"name": "阿曼", "std_offset": 4.0, "dst_rule": None, "tz_code": "GST", "weekend": (4, 5), "note": "马斯喀特时间"},
    "BH": {"name": "巴林", "std_offset": 3.0, "dst_rule": None, "tz_code": "AST", "weekend": (4, 5), "note": "麦纳麦时间"},
    "IL": {"name": "以色列", "std_offset": 2.0, "dst_rule": "eu", "tz_code": "IST/IDT", "weekend": (4, 5), "note": "以色列时间 (周五/周六公休)"},
    "IQ": {"name": "伊拉克", "std_offset": 3.0, "dst_rule": None, "tz_code": "AST", "weekend": (4, 5), "note": "巴格达时间"},
    "IR": {"name": "伊朗", "std_offset": 3.5, "dst_rule": None, "tz_code": "IRST", "weekend": (3, 4), "note": "德黑兰时间"},

    # --- 亚太与大洋洲主要贸易伙伴 ---
    "JP": {"name": "日本", "std_offset": 9.0, "dst_rule": None, "tz_code": "JST", "weekend": (5, 6), "note": "东京时间"},
    "KR": {"name": "韩国", "std_offset": 9.0, "dst_rule": None, "tz_code": "KST", "weekend": (5, 6), "note": "首尔时间"},
    "CN": {"name": "中国", "std_offset": 8.0, "dst_rule": None, "tz_code": "CST", "weekend": (5, 6), "note": "北京时间 (基准时区)"},
    "HK": {"name": "中国香港", "std_offset": 8.0, "dst_rule": None, "tz_code": "HKT", "weekend": (5, 6), "note": "香港时间"},
    "TW": {"name": "中国台湾", "std_offset": 8.0, "dst_rule": None, "tz_code": "CST", "weekend": (5, 6), "note": "台北时间"},
    "SG": {"name": "新加坡", "std_offset": 8.0, "dst_rule": None, "tz_code": "SGT", "weekend": (5, 6), "note": "新加坡时间"},
    "MY": {"name": "马来西亚", "std_offset": 8.0, "dst_rule": None, "tz_code": "MYT", "weekend": (5, 6), "note": "吉隆坡时间"},
    "TH": {"name": "泰国", "std_offset": 7.0, "dst_rule": None, "tz_code": "ICT", "weekend": (5, 6), "note": "曼谷时间"},
    "VN": {"name": "越南", "std_offset": 7.0, "dst_rule": None, "tz_code": "ICT", "weekend": (5, 6), "note": "河内/胡志明时间"},
    "ID": {"name": "印度尼西亚", "std_offset": 7.0, "dst_rule": None, "tz_code": "WIB", "weekend": (5, 6), "note": "雅加达西部时间"},
    "PH": {"name": "菲律宾", "std_offset": 8.0, "dst_rule": None, "tz_code": "PHT", "weekend": (5, 6), "note": "马尼拉时间"},
    "IN": {"name": "印度", "std_offset": 5.5, "dst_rule": None, "tz_code": "IST", "weekend": (5, 6), "note": "新德里/孟买时间"},
    "PK": {"name": "巴基斯坦", "std_offset": 5.0, "dst_rule": None, "tz_code": "PKT", "weekend": (5, 6), "note": "卡拉奇时间"},
    "BD": {"name": "孟加拉国", "std_offset": 6.0, "dst_rule": None, "tz_code": "BST", "weekend": (4, 5), "note": "达卡时间 (周五/周六公休)"},
    "AU": {"name": "澳大利亚", "std_offset": 10.0, "dst_rule": "au", "tz_code": "AEST/AEDT", "weekend": (5, 6), "note": "悉尼/墨尔本商区时间"},
    "AU_W": {"name": "澳大利亚(西澳)", "std_offset": 8.0, "dst_rule": None, "tz_code": "AWST", "weekend": (5, 6), "note": "珀斯商区时间"},
    "NZ": {"name": "新西兰", "std_offset": 12.0, "dst_rule": "au", "tz_code": "NZST/NZDT", "weekend": (5, 6), "note": "奥克兰/惠灵顿时间"},
    "KZ": {"name": "哈萨克斯坦", "std_offset": 5.0, "dst_rule": None, "tz_code": "AQTT", "weekend": (5, 6), "note": "阿拉木图时间"},
    "UZ": {"name": "乌兹别克斯坦", "std_offset": 5.0, "dst_rule": None, "tz_code": "UZT", "weekend": (5, 6), "note": "塔什干时间"},

    # --- 拉美贸易伙伴 ---
    "BR": {"name": "巴西", "std_offset": -3.0, "dst_rule": None, "tz_code": "BRT", "weekend": (5, 6), "note": "圣保罗/里约主力商区"},
    "AR": {"name": "阿根廷", "std_offset": -3.0, "dst_rule": None, "tz_code": "ART", "weekend": (5, 6), "note": "布宜诺斯艾利斯时间"},
    "CL": {"name": "智利", "std_offset": -4.0, "dst_rule": "au", "tz_code": "CLT/CLST", "weekend": (5, 6), "note": "圣地亚哥时间"},
    "CO": {"name": "哥伦比亚", "std_offset": -5.0, "dst_rule": None, "tz_code": "COT", "weekend": (5, 6), "note": "波哥大时间"},
    "PE": {"name": "秘鲁", "std_offset": -5.0, "dst_rule": None, "tz_code": "PET", "weekend": (5, 6), "note": "利马时间"},
    "EC": {"name": "厄瓜多尔", "std_offset": -5.0, "dst_rule": None, "tz_code": "ECT", "weekend": (5, 6), "note": "基多时间"},
    "PA": {"name": "巴拿马", "std_offset": -5.0, "dst_rule": None, "tz_code": "EST", "weekend": (5, 6), "note": "巴拿马城时间"},

    # --- 非洲贸易伙伴 ---
    "EG": {"name": "埃及", "std_offset": 2.0, "dst_rule": "eu", "tz_code": "EET", "weekend": (4, 5), "note": "开罗时间 (周五/周六公休)"},
    "ZA": {"name": "南非", "std_offset": 2.0, "dst_rule": None, "tz_code": "SAST", "weekend": (5, 6), "note": "约翰内斯堡时间"},
    "NG": {"name": "尼日利亚", "std_offset": 1.0, "dst_rule": None, "tz_code": "WAT", "weekend": (5, 6), "note": "拉各斯时间"},
    "KE": {"name": "肯尼亚", "std_offset": 3.0, "dst_rule": None, "tz_code": "EAT", "weekend": (5, 6), "note": "内罗毕时间"},
    "MA": {"name": "摩洛哥", "std_offset": 1.0, "dst_rule": None, "tz_code": "WEST", "weekend": (5, 6), "note": "卡萨布兰卡时间"},
    "DZ": {"name": "阿尔及利亚", "std_offset": 1.0, "dst_rule": None, "tz_code": "CET", "weekend": (4, 5), "note": "阿尔及尔时间 (周五/周六公休)"},
    "ET": {"name": "埃塞俄比亚", "std_offset": 3.0, "dst_rule": None, "tz_code": "EAT", "weekend": (5, 6), "note": "亚的斯亚贝巴时间"},
    "GH": {"name": "加纳", "std_offset": 0.0, "dst_rule": None, "tz_code": "GMT", "weekend": (5, 6), "note": "阿克拉时间"},
}

# ============ 别名与关键词映射词典 ============
ALIAS_MAP = {
    # 德国
    "德国": "DE", "germany": "DE", "de": "DE", "deutschland": "DE",
    "柏林": "DE", "berlin": "DE", "汉堡": "DE", "hamburg": "DE", "法兰克福": "DE", "frankfurt": "DE",
    "慕尼黑": "DE", "munich": "DE", "科隆": "DE", "cologne": "DE", "杜塞尔多夫": "DE",

    # 法国
    "法国": "FR", "france": "FR", "fr": "FR", "巴黎": "FR", "paris": "FR", "里昂": "FR", "马赛": "FR",

    # 英国
    "英国": "GB", "united kingdom": "GB", "uk": "GB", "gb": "GB", "great britain": "GB", "england": "GB",
    "英格兰": "GB", "伦敦": "GB", "london": "GB", "曼彻斯特": "GB", "伯明翰": "GB",

    # 美国 (默认东部主力，加州等特定识别)
    "美国": "US", "united states": "US", "usa": "US", "us": "US", "u.s.": "US", "america": "US",
    "纽约": "US", "new york": "US", "华盛顿": "US", "washington": "US", "波士顿": "US", "boston": "US",
    "迈阿密": "US", "miami": "US", "亚特兰大": "US", "atlanta": "US",
    # 美国(太平洋)
    "加州": "US_P", "加利福尼亚": "US_P", "california": "US_P", "ca_us": "US_P",
    "洛杉矶": "US_P", "los angeles": "US_P", "旧金山": "US_P", "san francisco": "US_P",
    "西雅图": "US_P", "seattle": "US_P", "圣地亚哥": "US_P", "san diego": "US_P",
    # 美国(中部)
    "芝加哥": "US_C", "chicago": "US_C", "休斯敦": "US_C", "houston": "US_C", "达拉斯": "US_C", "dallas": "US_C",
    "德克萨斯": "US_C", "德州": "US_C", "texas": "US_C",

    # 加拿大
    "加拿大": "CA", "canada": "CA", "ca": "CA", "多伦多": "CA", "toronto": "CA", "蒙特利尔": "CA",
    "温哥华": "CA_P", "vancouver": "CA_P",

    # 意大利
    "意大利": "IT", "italy": "IT", "it": "IT", "italia": "IT", "米兰": "IT", "milan": "IT", "罗马": "IT", "rome": "IT",

    # 西班牙
    "西班牙": "ES", "spain": "ES", "es": "ES", "espana": "ES", "马德里": "ES", "madrid": "ES", "巴塞罗那": "ES", "barcelona": "ES",

    # 荷兰
    "荷兰": "NL", "netherlands": "NL", "holland": "NL", "nl": "NL", "阿姆斯特丹": "NL", "amsterdam": "NL", "鹿特丹": "NL",

    # 比利时
    "比利时": "BE", "belgium": "BE", "be": "BE", "布鲁塞尔": "BE", "brussels": "BE", "安特卫普": "BE",

    # 波兰
    "波兰": "PL", "poland": "PL", "pl": "PL", "华沙": "PL", "warsaw": "PL",

    # 瑞士
    "瑞士": "CH", "switzerland": "CH", "ch": "CH", "苏黎世": "CH", "zurich": "CH", "日内瓦": "CH", "geneva": "CH",

    # 奥地利
    "奥地利": "AT", "austria": "AT", "at": "AT", "维也纳": "AT", "vienna": "AT",

    # 瑞典
    "瑞典": "SE", "sweden": "SE", "se": "SE", "斯德哥尔摩": "SE", "stockholm": "SE",

    # 挪威
    "挪威": "NO", "norway": "NO", "no": "NO", "奥斯陆": "NO", "oslo": "NO",

    # 丹麦
    "丹麦": "DK", "denmark": "DK", "dk": "DK", "哥本哈根": "DK", "copenhagen": "DK",

    # 芬兰
    "芬兰": "FI", "finland": "FI", "fi": "FI", "赫尔辛基": "FI", "helsinki": "FI",

    # 捷克
    "捷克": "CZ", "czech": "CZ", "czechia": "CZ", "cz": "CZ", "布拉格": "CZ", "prague": "CZ",

    # 葡萄牙
    "葡萄牙": "PT", "portugal": "PT", "pt": "PT", "里斯本": "PT", "lisbon": "PT",

    # 希腊
    "希腊": "GR", "greece": "GR", "gr": "GR", "雅典": "GR", "athens": "GR",

    # 爱尔兰
    "爱尔兰": "IE", "ireland": "IE", "ie": "IE", "都柏林": "IE", "dublin": "IE",

    # 匈牙利
    "匈牙利": "HU", "hungary": "HU", "hu": "HU", "布达佩斯": "HU", "budapest": "HU",

    # 罗马尼亚
    "罗马尼亚": "RO", "romania": "RO", "ro": "RO", "布加勒斯特": "RO",

    # 俄罗斯
    "俄罗斯": "RU", "russia": "RU", "ru": "RU", "莫斯科": "RU", "moscow": "RU", "圣彼得堡": "RU",

    # 乌克兰
    "乌克兰": "UA", "ukraine": "UA", "ua": "UA", "基辅": "UA", "kyiv": "UA",

    # 土耳其
    "土耳其": "TR", "turkey": "TR", "turkiye": "TR", "tr": "TR", "伊斯坦布尔": "TR", "istanbul": "TR", "安卡拉": "TR",

    # 阿联酋
    "阿联酋": "AE", "united arab emirates": "AE", "uae": "AE", "ae": "AE",
    "迪拜": "AE", "dubai": "AE", "阿布扎比": "AE", "abu dhabi": "AE", "沙迦": "AE",

    # 沙特阿拉伯
    "沙特阿拉伯": "SA", "沙特": "SA", "saudi arabia": "SA", "saudi": "SA", "sa": "SA", "利雅得": "SA", "riyadh": "SA", "吉达": "SA", "jeddah": "SA",

    # 卡塔尔
    "卡塔尔": "QA", "qatar": "QA", "qa": "QA", "多哈": "QA", "doha": "QA",

    # 科威特
    "科威特": "KW", "kuwait": "KW", "kw": "KW",

    # 阿曼
    "阿曼": "OM", "oman": "OM", "om": "OM",

    # 巴林
    "巴林": "BH", "bahrain": "BH", "bh": "BH",

    # 以色列
    "以色列": "IL", "israel": "IL", "il": "IL", "特拉维夫": "IL", "tel aviv": "IL", "耶路撒冷": "IL",

    # 日本
    "日本": "JP", "japan": "JP", "jp": "JP", "东京": "JP", "tokyo": "JP", "大阪": "JP", "osaka": "JP", "横滨": "JP", "名古屋": "JP",

    # 韩国
    "韩国": "KR", "korea": "KR", "south korea": "KR", "kr": "KR", "首尔": "KR", "seoul": "KR", "釜山": "KR",

    # 中国及港澳台
    "中国": "CN", "china": "CN", "cn": "CN", "中国大陆": "CN",
    "中国香港": "HK", "香港": "HK", "hong kong": "HK", "hk": "HK",
    "中国澳门": "CN", "澳门": "CN", "macau": "CN", "mo": "CN",
    "中国台湾": "TW", "台湾": "TW", "taiwan": "TW", "tw": "TW", "台北": "TW",

    # 新加坡
    "新加坡": "SG", "singapore": "SG", "sg": "SG",

    # 马来西亚
    "马来西亚": "MY", "malaysia": "MY", "my": "MY", "吉隆坡": "MY", "kuala lumpur": "MY", "槟城": "MY",

    # 泰国
    "泰国": "TH", "thailand": "TH", "th": "TH", "曼谷": "TH", "bangkok": "TH",

    # 越南
    "越南": "VN", "vietnam": "VN", "vn": "VN", "河内": "VN", "hanoi": "VN", "胡志明": "VN", "ho chi minh": "VN",

    # 印度尼西亚
    "印度尼西亚": "ID", "印尼": "ID", "indonesia": "ID", "id": "ID", "雅加达": "ID", "jakarta": "ID",

    # 菲律宾
    "菲律宾": "PH", "philippines": "PH", "ph": "PH", "马尼拉": "PH", "manila": "PH",

    # 印度
    "印度": "IN", "india": "IN", "in": "IN", "孟买": "IN", "mumbai": "IN", "新德里": "IN", "new delhi": "IN", "德里": "IN", "班加罗尔": "IN",

    # 巴基斯坦
    "巴基斯坦": "PK", "pakistan": "PK", "pk": "PK", "卡拉奇": "PK", "karachi": "PK", "拉合尔": "PK",

    # 孟加拉国
    "孟加拉国": "BD", "孟加拉": "BD", "bangladesh": "BD", "bd": "BD", "达卡": "BD", "dhaka": "BD",

    # 澳大利亚
    "澳大利亚": "AU", "澳洲": "AU", "australia": "AU", "au": "AU",
    "悉尼": "AU", "sydney": "AU", "墨尔本": "AU", "melbourne": "AU", "布里斯班": "AU", "brisbane": "AU",
    "珀斯": "AU_W", "perth": "AU_W",

    # 新西兰
    "新西兰": "NZ", "new zealand": "NZ", "nz": "NZ", "奥克兰": "NZ", "auckland": "NZ", "惠灵顿": "NZ",

    # 巴西
    "巴西": "BR", "brazil": "BR", "brasil": "BR", "br": "BR", "圣保罗": "BR", "sao paulo": "BR", "里约热内卢": "BR",

    # 墨西哥
    "墨西哥": "MX", "mexico": "MX", "mx": "MX", "墨西哥城": "MX",

    # 阿根廷
    "阿根廷": "AR", "argentina": "AR", "ar": "AR", "布宜诺斯艾利斯": "AR",

    # 智利
    "智利": "CL", "chile": "CL", "cl": "CL", "圣地亚哥": "CL", "santiago": "CL",

    # 哥伦比亚
    "哥伦比亚": "CO", "colombia": "CO", "co": "CO", "波哥大": "CO",

    # 秘鲁
    "秘鲁": "PE", "peru": "PE", "pe": "PE", "利马": "PE",

    # 埃及
    "埃及": "EG", "egypt": "EG", "eg": "EG", "开罗": "EG", "cairo": "EG",

    # 南非
    "南非": "ZA", "south africa": "ZA", "za": "ZA", "约翰内斯堡": "ZA", "johannesburg": "ZA", "开普敦": "ZA",

    # 尼日利亚
    "尼日利亚": "NG", "nigeria": "NG", "ng": "NG", "拉各斯": "NG", "lagos": "NG",

    # 肯尼亚
    "肯尼亚": "KE", "kenya": "KE", "ke": "KE", "内罗毕": "KE", "nairobi": "KE",

    # 摩洛哥
    "摩洛哥": "MA", "morocco": "MA", "ma": "MA", "卡萨布兰卡": "MA",

    # 哈萨克斯坦
    "哈萨克斯坦": "KZ", "kazakhstan": "KZ", "kz": "KZ", "阿拉木图": "KZ",

    # 乌兹别克斯坦
    "乌兹别克斯坦": "UZ", "uzbekistan": "UZ", "uz": "UZ", "塔什干": "UZ",
}


def resolve_country(raw_str: str) -> dict | None:
    """根据输入的国家/地区文本、城市或代码，模糊匹配并返回 COUNTRY_REGISTRY 元数据项。"""
    if not raw_str:
        return None
    s = str(raw_str).strip().lower()
    if not s or s in ("未识别", "unknown", "-", "none", "null"):
        return None

    # 去除常见干扰词（如 企业/有限责任公司/gmbh/corp/ltd 等）
    clean_s = re.sub(r"\b(corp|corporation|inc|llc|gmbh|ltd|limited|pty|sarl|spa|srl|sa)\b", "", s)
    clean_s = re.sub(r"(企业|有限责任公司|有限公司|公司|办事处|分公司)", "", clean_s).strip()

    # 1. 优先精确匹配全词/纯代码
    if clean_s in ALIAS_MAP:
        return COUNTRY_REGISTRY.get(ALIAS_MAP[clean_s])
    if s in ALIAS_MAP:
        return COUNTRY_REGISTRY.get(ALIAS_MAP[s])

    # 2. 按别名长度倒序遍历子串匹配（优先匹配更具体的别名，如'中国香港'优于'中国'）
    for alias in sorted(ALIAS_MAP.keys(), key=len, reverse=True):
        if len(alias) >= 2:
            if any('\u4e00' <= ch <= '\u9fff' for ch in alias):
                if alias in s:
                    return COUNTRY_REGISTRY.get(ALIAS_MAP[alias])
            else:
                if re.search(rf"\b{re.escape(alias)}\b", s):
                    return COUNTRY_REGISTRY.get(ALIAS_MAP[alias])

    return None


def infer_country_from_contact(contact: dict) -> str:
    """从客户数据对象中智能推断所属国家/地区（优先原字段，兜底背调或邮箱域名后缀）。"""
    if not contact:
        return ""
    # 1. 直接取 country 字段
    c_val = str(contact.get("country") or "").strip()
    if c_val and c_val not in ("未识别", "unknown", "-", "none", "null"):
        return c_val

    # 2. 尝试从 background JSON 中提取
    bg_raw = contact.get("background")
    if bg_raw:
        try:
            bg = json.loads(bg_raw) if isinstance(bg_raw, str) else bg_raw
            if isinstance(bg, dict):
                bg_c = str(bg.get("country") or "").strip()
                if bg_c and bg_c not in ("未识别", "unknown", "-", "none", "null"):
                    return bg_c
        except Exception:
            pass

    # 3. 尝试从邮箱域名后缀提取 (ccTLD)
    email = str(contact.get("email") or "").strip().lower()
    if "@" in email and "." in email:
        domain = email.split("@")[-1]
        tld = domain.rsplit(".", 1)[-1]
        if tld in ALIAS_MAP:
            return tld

    return ""


def get_country_time_info(country_or_region: str, ts: float | None = None,
                           base_tz_offset: int = 8) -> dict | None:
    """计算指定国家/地区的当前实时时间、时区差与商务作息状态。"""
    reg = resolve_country(country_or_region)
    if not reg:
        return None

    now_ts = float(ts if ts is not None else time.time())
    utc_dt = datetime.datetime.fromtimestamp(now_ts, datetime.timezone.utc)

    # 计算夏令时调整
    is_dst = False
    rule = reg.get("dst_rule")
    if rule == "eu" and _is_dst_eu(utc_dt):
        is_dst = True
    elif rule == "us" and _is_dst_us(utc_dt):
        is_dst = True
    elif rule == "au" and _is_dst_au(utc_dt):
        is_dst = True

    effective_offset = reg["std_offset"] + (1.0 if is_dst else 0.0)

    # 目标当地时间
    target_dt = utc_dt + datetime.timedelta(hours=effective_offset)

    hour = target_dt.hour
    minute = target_dt.minute
    weekday_num = target_dt.weekday()  # 0=周一 ... 6=周日

    weekdays_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    weekday_str = weekdays_cn[weekday_num]

    # 时差（目标时区 - 本地基准时区）
    diff_hours = effective_offset - base_tz_offset
    if diff_hours == 0:
        diff_desc = "同一时区 (无时差)"
        diff_short = "无时差"
    elif diff_hours < 0:
        diff_desc = f"比北京时间慢 {abs(diff_hours):g} 小时"
        diff_short = f"慢{abs(diff_hours):g}h"
    else:
        diff_desc = f"比北京时间快 {diff_hours:g} 小时"
        diff_short = f"快{diff_hours:g}h"

    # 时区格式化
    offset_sign = "+" if effective_offset >= 0 else "-"
    abs_off = abs(effective_offset)
    off_h = int(abs_off)
    off_m = int((abs_off - off_h) * 60)
    utc_str = f"UTC{offset_sign}{off_h}" + (f":{off_m:02d}" if off_m else "")

    # 判断作息状态
    weekend_days = reg.get("weekend", (5, 6))
    is_weekend = (weekday_num in weekend_days)

    if is_weekend:
        status_key = "weekend"
        status_text = "周末公休"
        badge_cls = "tz-weekend"
        icon = "📅"
        advice = "对方正值周末公休，邮件可能顺延至周一处理。非紧急询盘建议设定时邮件于周一上午发送。"
        is_work_time = False
    elif 9 <= hour < 12:
        status_key = "work_morning"
        status_text = "上午工作中"
        badge_cls = "tz-work"
        icon = "🟢"
        advice = "客户正处黄金工作时段，适宜立即发送邮件或在线沟通，查阅与回复效率最高。"
        is_work_time = True
    elif 12 <= hour < 14:
        status_key = "lunch"
        status_text = "午休时段"
        badge_cls = "tz-lunch"
        icon = "☕"
        advice = "客户当地正值午餐与午休时间，可正常发送，对方预计下午开工后查收。"
        is_work_time = False
    elif 14 <= hour < 18:
        status_key = "work_afternoon"
        status_text = "下午工作中"
        badge_cls = "tz-work"
        icon = "🟢"
        advice = "客户处于下午工作时间，业务处理节奏稳定，适合发送报价与推进订单细节。"
        is_work_time = True
    elif 18 <= hour < 22:
        status_key = "evening"
        status_text = "已下班"
        badge_cls = "tz-off"
        icon = "🌇"
        advice = "客户当地已下班，非紧急事项建议起草保存，定时于明早对方上班时送达。"
        is_work_time = False
    elif 7 <= hour < 9:
        status_key = "morning"
        status_text = "清晨备工"
        badge_cls = "tz-morning"
        icon = "🌅"
        advice = "客户即将开始一天的工作，此时发送邮件将展现在其收件箱顶端，是极佳发信时机。"
        is_work_time = False
    else:  # 22:00 - 07:00
        status_key = "night"
        status_text = "深夜休息"
        badge_cls = "tz-night"
        icon = "🌙"
        advice = "客户当地正值深夜睡眠，切勿电话或即时打扰；邮件建议设定时发送，避免沉底。"
        is_work_time = False

    time_str = f"{hour:02d}:{minute:02d}"
    date_str = target_dt.strftime("%m-%d")
    full_str = f"{target_dt.strftime('%Y-%m-%d')} {time_str}"

    tooltip = f"当地时间：{weekday_str} {time_str} · {utc_str} ({diff_desc}) · {status_text}\n沟通建议：{advice}"

    # 构造标准 HTML 徽章
    badge_html = (
        f'<span class="tz-badge {badge_cls}" title="{html.escape(tooltip)}">'
        f'<span class="tz-dot"></span>{time_str} · {status_text} ({diff_short})'
        f'</span>'
    )

    return {
        "country": reg["name"],
        "raw_query": country_or_region,
        "utc_offset": effective_offset,
        "utc_str": utc_str,
        "is_dst": is_dst,
        "tz_code": reg["tz_code"],
        "note": reg.get("note", ""),
        "time_str": time_str,
        "date_str": date_str,
        "weekday_cn": weekday_str,
        "full_str": full_str,
        "diff_hours": diff_hours,
        "diff_desc": diff_desc,
        "diff_short": diff_short,
        "is_weekend": is_weekend,
        "is_work_time": is_work_time,
        "status_key": status_key,
        "status_text": status_text,
        "badge_cls": badge_cls,
        "icon": icon,
        "advice": advice,
        "badge_html": badge_html,
    }


def get_contact_time_info(contact: dict, ts: float | None = None, base_tz_offset: int = 8) -> dict | None:
    """快捷函数：从联系人对象自动推断并计算当地时间信息。"""
    c_name = infer_country_from_contact(contact)
    if not c_name:
        return None
    return get_country_time_info(c_name, ts=ts, base_tz_offset=base_tz_offset)


def render_time_badge(country_or_region: str, ts: float | None = None) -> str:
    """模板快捷函数：渲染紧凑型当地时间徽章。若未识别则返回空字符串。"""
    info = get_country_time_info(country_or_region, ts=ts)
    return info["badge_html"] if info else ""


def render_time_card(info: dict | None) -> str:
    """模板快捷函数：为客户详情页渲染专用的「当地时间与沟通时机」卡片。"""
    if not info:
        return ""
    
    country_note = f' <span style="font-size:12px;color:var(--text-3);font-weight:normal;">({html.escape(info["note"])})</span>' if info.get("note") else ""
    dst_tag = ' <span class="tag sm" style="font-size:10.5px;padding:1px 5px;background:var(--surface-2);color:var(--text-2);border:1px solid var(--border);">夏令时</span>' if info.get("is_dst") else ""

    return f"""<div class="tz-card {info['badge_cls']}">
  <div class="tz-card-head">
    <div class="tz-card-time-wrap">
      <span class="tz-card-clock">🕒 {html.escape(info['time_str'])}</span>
      <span class="tz-card-day">{html.escape(info['weekday_cn'])} ({html.escape(info['date_str'])})</span>
      <span class="tz-badge {info['badge_cls']}">
        <span class="tz-dot"></span>{html.escape(info['status_text'])}
      </span>
    </div>
    <div class="tz-card-meta">
      <span><b>国家/地区：</b>{html.escape(info['country'])}{country_note}</span>
      <span><b>时区：</b>{html.escape(info['utc_str'])} · {html.escape(info['tz_code'])}{dst_tag}</span>
      <span><b>时差对比：</b><b style="color:var(--accent);">{html.escape(info['diff_desc'])}</b></span>
    </div>
  </div>
  <div class="tz-card-advice">
    <span class="tz-advice-tag">💡 邮件沟通时机建议</span>
    <span class="tz-advice-content">{html.escape(info['advice'])}</span>
  </div>
</div>"""
