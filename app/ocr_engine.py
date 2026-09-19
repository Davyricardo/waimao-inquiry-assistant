"""本地轻量级 OCR 引擎与外贸智能信息提取器。

设计准则：
1. 本地离线推理：使用 rapidocr-onnxruntime 进行图像 OCR 识别，0 API 费用，0 Token 浪费；
2. 容错回退机制：若无 OCR 依赖或解析图片失败，提供优雅文本启发式提取降级；
3. 外贸垂直解析：专为外贸单证（CI/PI/BL/合同/水单）与客户名片/社媒主页量身定制高精度正则与启发式提取。
"""
import io
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

_ocr_engine = None


def get_ocr_engine():
    """惰性单例加载本地 RapidOCR 引擎。"""
    global _ocr_engine
    if _ocr_engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _ocr_engine = RapidOCR()
        except Exception as e:
            print(f"[OCR] RapidOCR 初始化失败或未安装，使用降级模式: {e}", file=sys.stderr)
            _ocr_engine = False
    return _ocr_engine if _ocr_engine is not False else None


def ocr_image_to_text(image_input: Union[str, Path, bytes, io.BytesIO]) -> Tuple[str, float]:
    """执行本地 OCR 文字识别。

    返回: (识别文本全貌, 平均置信度)
    """
    engine = get_ocr_engine()
    if not engine:
        return "", 0.0

    try:
        if isinstance(image_input, (str, Path)):
            img_path = str(image_input)
            result, elapse_list = engine(img_path)
        elif isinstance(image_input, (bytes, bytearray)):
            result, elapse_list = engine(bytes(image_input))
        elif hasattr(image_input, "read"):
            data = image_input.read()
            result, elapse_list = engine(data)
        else:
            return "", 0.0

        if not result:
            return "", 0.0

        lines = []
        scores = []
        for item in result:
            # item 格式: [box_coordinates, text, score]
            if len(item) >= 2:
                text = str(item[1]).strip()
                if text:
                    lines.append(text)
                if len(item) >= 3 and isinstance(item[2], (int, float)):
                    scores.append(float(item[2]))

        full_text = "\n".join(lines)
        avg_score = sum(scores) / len(scores) if scores else 0.8
        return full_text, avg_score
    except Exception as e:
        print(f"[OCR] 识别过程异常: {e}", file=sys.stderr)
        return "", 0.0


# ==============================================================================
# 外贸单证与凭证提取解析器 (Voucher Parser)
# ==============================================================================

DOC_TYPE_KEYWORDS = [
    (r"(?:PROFORMA\s*INVOICE|P/?I|形式发票)", "proforma_invoice"),
    (r"(?:COMMERCIAL\s*INVOICE|C/?I|商业发票|INVOICE\s*NO)", "commercial_invoice"),
    (r"(?:PACKING\s*LIST|装箱单|WEIGHT\s*LIST)", "packing_list"),
    (r"(?:BILL\s*OF\s*LADING|B/?L|OCEAN\s*BILL|海运提单|提单)", "bill_of_lading"),
    (r"(?:CUSTOMS\s*DECLARATION|报关单|出口货物报关)", "customs_declaration"),
    (r"(?:BANK\s*SLIP|REMITTANCE|SWIFT|PAYMENT\s*ADVICE|付汇水单|结汇水单|电汇凭证|水单)", "bank_slip"),
    (r"(?:SALES\s*CONTRACT|PURCHASE\s*ORDER|SALES\s*CONFIRMATION|P\.O\.|合同|销售确认书)", "contract"),
]

CURRENCIES = ["USD", "EUR", "CNY", "RMB", "GBP", "JPY", "CAD", "AUD", "HKD", "SGD"]


def parse_voucher_info(text: str) -> Dict[str, Any]:
    """从文本或 OCR 结果中智能提取外贸单证关键业务字段。"""
    raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
    normalized_text = " ".join(raw_lines)

    # 1. 识别单证类型
    voucher_type = "commercial_invoice"
    for pattern, vtype in DOC_TYPE_KEYWORDS:
        if re.search(pattern, normalized_text, re.IGNORECASE):
            voucher_type = vtype
            break

    # 2. 单据编号提取
    voucher_no = ""
    no_patterns = [
        r"(?:INVOICE\s*NO\.?|INV\s*NO\.?|P/?I\s*NO\.?|B/?L\s*NO\.?|BILL\s*NO\.?|CONTRACT\s*NO\.?|ORDER\s*NO\.?|REF\s*NO\.?|发票号|单号|提单号|合同号)[:\s#]+([A-Z0-9\-_/]{3,30})",
        r"(?:INVOICE|INV|P/?I|B/?L|BILL|CONTRACT|ORDER|REF|NO\.?|NUMBER|编号)[#:\s]+([A-Z0-9\-_/]{3,30})",
        r"\b(?:INV|PI|PO|BL|SC|ORD)[-_]?[0-9]{4,14}\b",
        r"\b([A-Z]{2,5}[-_]?[0-9]{4,12})\b",
    ]
    for pat in no_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            cand = m.group(1) if m.groups() else m.group(0)
            if cand.upper() not in ("INVOICE", "NUMBER", "DATE", "TOTAL", "AMOUNT", "COMMERCIAL", "PROFORMA"):
                voucher_no = cand
                break

    # 3. 业务发生日期提取
    trade_date = ""
    date_patterns = [
        r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})",
        r"(\d{1,2}[-/.]\d{1,2}[-/.]\d{4})",
        r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})",
    ]
    for pat in date_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            dstr = m.group(1)
            # 简单标准化为 YYYY-MM-DD
            try:
                for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%d-%m-%Y", "%d/%m/%Y"):
                    try:
                        trade_date = datetime.strptime(dstr, fmt).strftime("%Y-%m-%d")
                        break
                    except ValueError:
                        continue
            except Exception:
                trade_date = dstr
            if trade_date:
                break
    if not trade_date:
        trade_date = datetime.now().strftime("%Y-%m-%d")

    # 4. 币种与金额提取
    currency = "USD"
    amount = 0.0
    for cur in CURRENCIES:
        if re.search(r"\b" + cur + r"\b", text, re.IGNORECASE) or (cur in ("CNY", "RMB") and "¥" in text) or (cur == "USD" and "$" in text):
            currency = "CNY" if cur == "RMB" else cur
            break

    amount_patterns = [
        r"(?:TOTAL|AMOUNT|SAY\s*TOTAL|TOTAL\s*AMOUNT|GRAND\s*TOTAL|SUM|金额|总计|合\s*计)[^0-9\n\r]{0,15}(?:USD|EUR|CNY|GBP|\$|¥)?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})|[0-9]+(?:\.[0-9]{1,2})?)",
        r"(?:USD|EUR|CNY|GBP|\$|¥)\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})|[0-9]+(?:\.[0-9]{1,2})?)",
    ]
    for pat in amount_patterns:
        matches = re.findall(pat, text, re.IGNORECASE)
        if matches:
            candidates = []
            for amt_str in matches:
                try:
                    val = float(amt_str.replace(",", ""))
                    if val > 0:
                        candidates.append(val)
                except ValueError:
                    pass
            if candidates:
                amount = max(candidates)  # 通常总金额最大
                break

    # 5. 买卖双方 (Shipper / Consignee) 提取
    shipper = ""
    consignee = ""
    for i, line in enumerate(raw_lines):
        if not shipper and re.search(r"^(?:Shipper|Exporter|Seller|From|卖方|发货人)[:\s]*", line, re.IGNORECASE):
            cleaned = re.sub(r"^(?:Shipper|Exporter|Seller|From|卖方|发货人)[:\s]*", "", line, flags=re.IGNORECASE).strip()
            shipper = cleaned or (raw_lines[i + 1] if i + 1 < len(raw_lines) else "")
        if not consignee and re.search(r"^(?:Consignee|Buyer|To|Messrs|买方|收货人)[:\s]*", line, re.IGNORECASE):
            cleaned = re.sub(r"^(?:Consignee|Buyer|To|Messrs|买方|收货人)[:\s]*", "", line, flags=re.IGNORECASE).strip()
            consignee = cleaned or (raw_lines[i + 1] if i + 1 < len(raw_lines) else "")

    # 6. 品名描述 (Product description)
    product_desc = ""
    for i, line in enumerate(raw_lines):
        if re.search(r"(?:Description|Goods|Commodity|Product|品名|货物名称)[:\s]*", line, re.IGNORECASE):
            cleaned = re.sub(r"^(?:Description|Goods|Commodity|Product|品名|货物名称)[:\s]*", "", line, flags=re.IGNORECASE).strip()
            product_desc = cleaned or (raw_lines[i + 1] if i + 1 < len(raw_lines) else "")
            break

    # 7. 生成智能标题
    type_name_map = {
        "commercial_invoice": "商业发票", "proforma_invoice": "形式发票",
        "bill_of_lading": "海运提单", "packing_list": "装箱单",
        "customs_declaration": "出口报关单", "bank_slip": "电汇水单",
        "contract": "外贸合同", "other": "外贸单据",
    }
    tn = type_name_map.get(voucher_type, "外贸凭证")
    title = f"{tn} {voucher_no}".strip()
    if amount > 0:
        title += f" ({currency} {amount:,.2f})"

    return {
        "voucher_no": voucher_no or f"VCH-{datetime.now().strftime('%y%m%d%H%M')}",
        "voucher_type": voucher_type,
        "title": title,
        "trade_date": trade_date,
        "currency": currency,
        "amount": round(amount, 2),
        "shipper": shipper[:120],
        "consignee": consignee[:120],
        "product_desc": product_desc[:200],
        "ocr_raw_text": text,
    }


# ==============================================================================
# 名片与社媒主页提取解析器 (Business Card & Social Profile Parser)
# ==============================================================================

JOB_TITLES = [
    r"(?:Purchasing|Sourcing|Procurement)\s*(?:Manager|Director|Specialist|Officer|Lead)",
    r"(?:Sales|Marketing|Business\s*Development)\s*(?:Manager|Director|VP|Representative)",
    r"(?:CEO|CTO|COO|CFO|President|Vice\s*President|Owner|Founder|Co-Founder|Partner|Managing\s*Director|General\s*Manager)",
    r"(?:采购经理|采购总监|采购员|总经理|业务总监|销售经理|创始人|买手)",
]

COMPANY_INDICATORS = [
    r"\b(?:Ltd|Inc|Corp|Corporation|Co\.,?\s*Ltd|LLC|GmbH|B\.V\.|S\.A\.|S\.R\.L\.|Pte\s*Ltd)\b",
    r"(?:Trading|Industrial|Technologies|Manufactur|Export|Import|Group|Holdings|Hardware|Electronics|Machinery)",
    r"(?:有限公司|股份有限公司|实业|科技|进出口|国际贸易|制造|工业)",
]

COUNTRY_DIAL_CODES = {
    "+1": "美国/加拿大", "+44": "英国", "+49": "德国", "+33": "法国", "+39": "意大利",
    "+34": "西班牙", "+61": "澳大利亚", "+81": "日本", "+82": "韩国", "+86": "中国",
    "+971": "阿联酋 (迪拜)", "+966": "沙特阿拉伯", "+65": "新加坡", "+60": "马来西亚",
    "+91": "印度", "+55": "巴西", "+52": "墨西哥", "+7": "俄罗斯", "+27": "南非",
}


def parse_contact_info(text: str) -> Dict[str, Any]:
    """从名片或社交媒体截图 OCR 文字中智能提取客户与销售线索字段。"""
    raw_lines = [line.strip() for line in text.splitlines() if line.strip()]

    email = ""
    phone = ""
    website = ""
    linkedin_url = ""
    company = ""
    name = ""
    position = ""
    country = ""

    # 1. 邮箱提取
    email_m = re.search(r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", text)
    if email_m:
        email = email_m.group(1).lower()

    # 2. 网址提取
    web_m = re.search(r"(https?://[^\s/$.?#].[^\s]*|www\.[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", text, re.IGNORECASE)
    if web_m:
        website = web_m.group(1)

    # 3. 电话 / WhatsApp 提取
    phone_patterns = [
        r"(\+\d{1,4}[-.\s]?(?:\(?\d{1,5}\)?[-.\s]?)?\d{3,5}[-.\s]?\d{3,5})",
        r"(?:Tel|Phone|Mobile|WhatsApp|Cell|微信)[:\s]*([+0-9\-.\s()]{7,25})",
    ]
    for pat in phone_patterns:
        pm = re.search(pat, text, re.IGNORECASE)
        if pm:
            phone_cand = pm.group(1).strip()
            # 过滤非数字杂质
            digits = re.sub(r"[^\d+]", "", phone_cand)
            if len(digits) >= 7:
                phone = phone_cand
                # 推断国家
                for code, cname in COUNTRY_DIAL_CODES.items():
                    if digits.startswith(code):
                        country = cname
                        break
                break

    # 4. LinkedIn 提取
    lk_m = re.search(r"(?:https?://(?:www\.)?linkedin\.com/in/[a-zA-Z0-9\-_%]+|linkedin\.com/in/[a-zA-Z0-9\-_%]+)", text, re.IGNORECASE)
    if lk_m:
        linkedin_url = lk_m.group(0)

    # 5. 公司名提取
    for line in raw_lines:
        if line == email or line == website or line == phone:
            continue
        for ind in COMPANY_INDICATORS:
            if re.search(ind, line, re.IGNORECASE):
                company = line
                break
        if company:
            break

    # 6. 职位头衔提取
    for line in raw_lines:
        for jt in JOB_TITLES:
            if re.search(jt, line, re.IGNORECASE):
                position = line
                break
        if position:
            break

    # 7. 姓名提取（优先挑取非公司、非邮箱、非职位、非网址的顶部前 3 行短文本）
    for line in raw_lines[:4]:
        if line in (company, position, email, website, phone):
            continue
        if "@" in line or "www." in line or "http" in line:
            continue
        if len(line) < 30 and re.match(r"^[A-Z][a-zA-Z\s.-]+$|^[\u4e00-\u9fa5]{2,4}$", line.strip()):
            name = line.strip()
            break
    if not name and raw_lines:
        for line in raw_lines:
            if line not in (company, position, email, website, phone) and len(line) <= 24:
                name = line
                break

    # 8. 国家推断（如果电话未命中）
    if not country:
        for cname in ["Germany", "United States", "USA", "UK", "United Kingdom", "France", "Italy", "Dubai", "UAE", "Australia", "Japan", "Canada"]:
            if re.search(r"\b" + cname + r"\b", text, re.IGNORECASE):
                country = cname
                break

    return {
        "name": name or "新客户线索",
        "company": company,
        "position": position,
        "email": email,
        "phone": phone,
        "website": website,
        "country": country,
        "linkedin_url": linkedin_url,
        "ocr_raw_text": text,
    }
