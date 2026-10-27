"""码内容解析：把解码出来的原始字符串变成人类可读的结构化信息。

只做纯本地、确定性的解析，不访问网络：
- URL            -> 域名 / 路径 / 协议
- WIFI:          -> SSID / 加密方式
- vCard / MECARD -> 姓名 / 电话 / 邮箱 / 组织
- EAN/UPC/GTIN   -> 校验位是否合法 + GS1 国家前缀
- 其他           -> 截断后的纯文本
"""

import re
from dataclasses import dataclass, field
from typing import List, Tuple, Optional


@dataclass
class Payload:
    """一条码内容的解析结果。"""

    kind: str                                   # url | wifi | contact | gtin | phone | text
    title: str                                  # 卡片主标题
    subtitle: str = ""                          # 卡片副标题
    fields: List[Tuple[str, str]] = field(default_factory=list)


@dataclass
class ScanRecord:
    """一次成功识别的记录。"""

    data: str
    symbology: str
    latency: float
    ts: float
    payload: Payload


# ---------------------------------------------------------------- 码制归类

_2D_KEYWORDS = ("QR", "DATAMATRIX", "DATA MATRIX", "AZTEC", "PDF417", "MAXICODE", "MICROQR")
_1D_KEYWORDS = ("EAN", "UPC", "CODE", "ITF", "CODABAR", "39", "93", "128")


def is_2d(symbology: str) -> bool:
    s = symbology.upper().replace(" ", "")
    if any(k.replace(" ", "") in s for k in _2D_KEYWORDS):
        return True
    if any(k in s for k in _1D_KEYWORDS):
        return False
    return True


# ---------------------------------------------------------------- GTIN / GS1

# (起始前缀, 结束前缀, 国家/地区)，按三位数字前缀归类
_GS1_RANGES = [
    (0, 19, "United States"), (30, 39, "United States"), (60, 139, "United States"),
    (300, 379, "France"), (380, 380, "Bulgaria"), (383, 383, "Slovenia"),
    (385, 385, "Croatia"), (387, 387, "Bosnia"), (389, 389, "Montenegro"),
    (400, 440, "Germany"), (450, 459, "Japan"), (460, 469, "Russia"),
    (471, 471, "Taiwan"), (474, 474, "Estonia"), (475, 475, "Latvia"),
    (477, 477, "Lithuania"), (479, 479, "Sri Lanka"), (480, 480, "Philippines"),
    (482, 482, "Ukraine"), (484, 484, "Moldova"), (486, 486, "Georgia"),
    (487, 487, "Kazakhstan"), (489, 489, "Hong Kong"), (490, 499, "Japan"),
    (500, 509, "United Kingdom"), (520, 520, "Greece"), (528, 528, "Lebanon"),
    (531, 531, "North Macedonia"), (535, 535, "Malta"), (539, 539, "Ireland"),
    (540, 549, "Belgium / Luxembourg"), (550, 550, "Portugal"), (560, 560, "EU"),
    (569, 569, "Iceland"), (570, 579, "Denmark"), (590, 590, "Poland"),
    (594, 594, "Romania"), (599, 599, "Hungary"), (600, 601, "South Africa"),
    (609, 609, "Mauritius"), (611, 611, "Morocco"), (613, 613, "Algeria"),
    (615, 615, "Nigeria"), (616, 616, "Kenya"), (619, 619, "Tunisia"),
    (621, 621, "Syria"), (622, 622, "Egypt"), (624, 624, "Libya"),
    (625, 625, "Jordan"), (626, 626, "Iran"), (627, 627, "Kuwait"),
    (628, 628, "Saudi Arabia"), (629, 629, "United Arab Emirates"),
    (640, 649, "Finland"), (690, 699, "China"), (700, 709, "Norway"),
    (729, 729, "Israel"), (730, 739, "Sweden"), (740, 740, "Guatemala"),
    (741, 741, "El Salvador"), (742, 742, "Honduras"), (743, 743, "Nicaragua"),
    (744, 744, "Costa Rica"), (745, 745, "Panama"), (746, 746, "Dominican Republic"),
    (750, 750, "Mexico"), (754, 755, "Canada"), (759, 759, "Venezuela"),
    (760, 769, "Switzerland"), (770, 770, "Colombia"), (773, 773, "Uruguay"),
    (775, 775, "Peru"), (777, 777, "Bolivia"), (779, 779, "Argentina"),
    (780, 780, "Chile"), (784, 784, "Paraguay"), (786, 786, "Ecuador"),
    (789, 790, "Brazil"), (800, 839, "Italy"), (840, 849, "Spain"),
    (858, 858, "Slovakia"), (859, 859, "Czechia"), (860, 860, "Serbia"),
    (865, 865, "Mongolia"), (868, 869, "Türkiye"), (870, 879, "Netherlands"),
    (880, 880, "South Korea"), (885, 885, "Thailand"), (888, 888, "Singapore"),
    (890, 890, "India"), (893, 893, "Vietnam"), (896, 896, "Pakistan"),
    (899, 899, "Indonesia"), (900, 919, "Austria"), (930, 939, "Australia"),
    (940, 949, "New Zealand"), (955, 955, "Malaysia"), (958, 958, "Macau"),
]


def _gs1_country(digits: str) -> Optional[str]:
    prefix = int(digits[:3].ljust(3, "0"))
    for low, high, name in _GS1_RANGES:
        if low <= prefix <= high:
            return name
    return None


def _gtin_check_ok(digits: str) -> bool:
    """GTIN-8/12/13/14 的模 10 校验位。"""
    body, check = digits[:-1], int(digits[-1])
    total = 0
    for i, ch in enumerate(reversed(body)):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    return (10 - total % 10) % 10 == check


def _parse_gtin(data: str, symbology: str) -> Optional[Payload]:
    if not (data.isdigit() and len(data) in (8, 12, 13, 14)):
        return None
    if not any(k in symbology.upper() for k in ("EAN", "UPC", "GTIN")):
        return None

    ok = _gtin_check_ok(data)
    country = _gs1_country(data) if len(data) >= 3 else None
    isbn = data.startswith(("978", "979")) and len(data) == 13

    title = data
    sub_parts = []
    if isbn:
        sub_parts.append("Book (ISBN)")
    elif country:
        sub_parts.append(country)
    sub_parts.append("checksum OK" if ok else "checksum FAILED")

    fields = [
        ("GTIN", data),
        ("Format", f"{symbology} · {len(data)} digits"),
        ("Country", country or "unknown"),
        ("Checksum", "valid" if ok else "invalid"),
    ]
    if isbn:
        fields.append(("Note", "978/979 ISBN prefix"))
    return Payload(kind="gtin", title=title, subtitle=" · ".join(sub_parts), fields=fields)


# ---------------------------------------------------------------- 各类文本

def _parse_wifi(data: str) -> Payload:
    body = data[5:]
    parts = {}
    i = 0
    while i < len(body):
        if body[i] in "SPTH" and i + 1 < len(body) and body[i + 1] == ":":
            key = body[i]
            i += 2
            val = []
            while i < len(body) and body[i] != ";":
                if body[i] == "\\" and i + 1 < len(body):
                    val.append(body[i + 1])
                    i += 2
                else:
                    val.append(body[i])
                    i += 1
            parts[key] = "".join(val)
            i += 1
        else:
            i += 1

    ssid = parts.get("S", "(hidden)")
    sec = {"WPA": "WPA/WPA2", "WEP": "WEP", "nopass": "Open"}.get(parts.get("T", ""), "Unknown")
    fields = [("SSID", ssid), ("Security", sec)]
    if parts.get("P"):
        fields.append(("Password", parts["P"]))
    return Payload(kind="wifi", title=ssid, subtitle=f"Wi-Fi network · {sec}", fields=fields)


def _parse_url(data: str) -> Payload:
    m = re.match(r"^(https?)://([^/?#]+)([^?#]*)?(\?[^#]*)?(?:#(.*))?$", data, re.I)
    if not m:
        return Payload(kind="text", title=data[:60], subtitle="Text", fields=[("Payload", data)])
    scheme, host, path, query, _frag = m.groups()
    path = path or "/"
    fields = [("Host", host), ("Path", path), ("Scheme", scheme.upper())]
    if query:
        fields.append(("Query", query[1:]))
    fields.append(("Full URL", data))
    return Payload(kind="url", title=host, subtitle=(path if path != "/" else "Web address"), fields=fields)


def _parse_vcard(data: str) -> Payload:
    info = {}
    for line in data.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.split(";")[0].strip().upper()
        if key in ("FN", "TEL", "EMAIL", "ORG", "TITLE", "URL", "ADR"):
            info.setdefault(key, value.strip())

    name = info.get("FN") or "(no name)"
    fields = [(k.title(), v) for k, v in info.items()]
    sub = " · ".join(x for x in (info.get("ORG"), info.get("TITLE")) if x) or "Contact card"
    return Payload(kind="contact", title=name, subtitle=sub, fields=fields)


def _parse_mecard(data: str) -> Payload:
    info = {}
    for item in data[7:].split(";"):
        if ":" in item:
            k, v = item.split(":", 1)
            info[k.strip().upper()] = v.strip()
    name = info.get("N", "(no name)")
    fields = [("Name", name)]
    for k in ("TEL", "EMAIL", "ORG", "URL"):
        if k in info:
            fields.append((k.title(), info[k]))
    return Payload(kind="contact", title=name, subtitle=info.get("ORG", "Contact card"), fields=fields)


def _parse_text(data: str, symbology: str) -> Payload:
    text = data.replace("\r", " ").replace("\n", " ")
    if text.lower().startswith("tel:"):
        return Payload(kind="phone", title=data[4:], subtitle="Phone number",
                       fields=[("Number", data[4:])])
    if text.lower().startswith("mailto:"):
        addr = data[7:]
        return Payload(kind="contact", title=addr, subtitle="Email address",
                       fields=[("Email", addr)])
    title = text if len(text) <= 60 else text[:57] + "..."
    return Payload(kind="text", title=title or "(empty)",
                   subtitle=f"{symbology} · {len(data)} chars",
                   fields=[("Payload", data), ("Length", str(len(data)))])


def parse(data: str, symbology: str) -> Payload:
    """按内容前缀/码制选择解析器；永远返回一个可展示的 Payload。"""
    raw = data or ""
    try:
        if raw.upper().startswith("WIFI:"):
            return _parse_wifi(raw)
        if raw.upper().startswith("BEGIN:VCARD"):
            return _parse_vcard(raw)
        if raw.upper().startswith("MECARD:"):
            return _parse_mecard(raw)
        if re.match(r"^https?://", raw, re.I):
            return _parse_url(raw)
        gtin = _parse_gtin(raw, symbology)
        if gtin is not None:
            return gtin
    except Exception:
        pass
    return _parse_text(raw, symbology)
