"""Светлая товарная карточка-плашка для VK (стиль приложения владельца).

Шапка с брендом и названием, плитка с фото товара, до четырёх плиток с цифрами
из характеристик и плашка цены. Перенесено из ручного инструмента пачек
(scripts/tile_batch архивной копии): в октябре 2026 посты с такими карточками
владелец одобрил, а голое фото на белом назвал «не тем».
"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from content_factory.storefront.product_posts import CatalogItem, facts_for_hook, money

SIZE = 1080
BG = (243, 246, 250)
TEAL, BLUE = (20, 128, 110), (43, 108, 176)
# акцент (цвет числа), цвет рамки, оттенок заливки
TILES = [
    ((20, 128, 110), (168, 214, 206), (226, 243, 240)),
    ((140, 92, 24), (222, 196, 160), (248, 238, 226)),
    ((43, 108, 176), (176, 200, 232), (228, 238, 250)),
    ((110, 76, 190), (205, 192, 238), (238, 232, 250)),
]
PRICE_BOX = (40, 848, 1040, 1040)

# Сначала шрифты Windows (превью на машине владельца), затем серверные.
_FONTS = {
    "bold": ("C:/Windows/Fonts/segoeuib.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    "semi": ("C:/Windows/Fonts/seguisb.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    "regular": ("C:/Windows/Fonts/segoeui.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
}

GROUP_LABEL = {
    "ac": "сплит-системы", "vent": "вентиляция", "air": "климат дома",
    "heater": "обогреватели", "radiator": "отопление", "water": "водонагреватели",
    "floor": "тёплые полы",
}

# Плитки из характеристик: (шаблон ключа, подпись, единица, если в значении её нет).
_VALUE_TILES = (
    (r"номинальная производительность охлаждения|^холодопроизводительность", "охлаждение", "кВт"),
    (r"^теплопроизводительность", "обогрев", "кВт"),
    (r"для помещения площадью", "площадь помещения", "м²"),
    (r"мин\. рабочая температура воздуха", "работа на улице до", ""),
    (r"макс\. площадь обогрева", "площадь обогрева", ""),
    (r"удельная мощность", "удельная мощность", ""),
    (r"мощность тэна", "мощность ТЭНа", ""),
    (r"^производительность$|макс\. производительность", "производительность", ""),
    (r"нагрев воздуха", "нагрев воздуха", ""),
    (r"уровень шума", "уровень шума", ""),
    (r"теплоотдача", "теплоотдача", ""),
    (r"макс\. температура теплоносителя", "теплоноситель до", ""),
    (r"максимальное рабочее давление", "рабочее давление", ""),
    (r"объем воды в радиаторе", "объём воды", ""),
    (r"гарантийный срок", "гарантия", ""),
    (r"срок эксплуатации|срок службы", "срок службы", ""),
)
_BOOL_TILES = (
    (r"тепловой насос", "Обогрев", "режим теплового насоса"),
    (r"инверторн", "Инвертор", "плавная работа"),
    (r"wi-?fi", "Wi-Fi", "управление со смартфона"),
    (r"защита от перегрева", "Защита", "от перегрева"),
)
_YES = {"да", "есть", "имеется"}
MAX_VALUE = 12


def _font(kind: str, size: int):
    for path in _FONTS[kind]:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def _clean_value(value: str, unit: str) -> str:
    value = re.sub(r"\s+", " ", str(value)).strip(" .")
    value = value.replace("м2", "м²").replace("°С", "°C").replace("м3", "м³")
    if unit and not re.search(r"[A-Za-zА-Яа-я°²³%]", value):
        value = f"{value} {unit}"
    return value


def card_tiles(item: CatalogItem, limit: int = 4) -> list[tuple[str, str]]:
    """Цифры для плиток: только из характеристик и названия, ничего не выдумываем."""
    tiles: list[tuple[str, str]] = []
    labels: set[str] = set()

    def add(value: str, label: str) -> None:
        if value and len(value) <= MAX_VALUE and label not in labels and len(tiles) < limit:
            tiles.append((value, label))
            labels.add(label)

    attrs = {key.casefold(): value for key, value in item.attrs.items()}
    for pattern, label, unit in _VALUE_TILES:
        for key, value in attrs.items():
            low = str(value).casefold().strip(" .")
            if re.search(pattern, key) and low not in {"нет", "-", "", "0"} \
                    and re.search(r"\d", low):
                add(_clean_value(value, unit), label)
                break
    facts = facts_for_hook(item)
    if facts["liters"]:
        add(f"{facts['liters']} л", "объём бака")
    if facts["kw"]:
        add(f"{facts['kw']} кВт", "мощность")
    if facts["area"]:
        add(f"до {facts['area']} м²", "площадь помещения")
    if facts["sections"]:
        add(facts["sections"], "секций")
    if facts["noise"]:
        add(f"от {facts['noise']} дБ", "уровень шума")
    for pattern, value, label in _BOOL_TILES:
        if any(re.search(pattern, key) and str(v).casefold().strip(" .") in _YES
               for key, v in attrs.items()):
            add(value, label)
    return tiles


def _diag_gradient(w, h, c1, c2):
    vertical = Image.linear_gradient("L").resize((w, h), Image.Resampling.BILINEAR)
    horizontal = Image.linear_gradient("L").rotate(90, expand=True).resize(
        (w, h), Image.Resampling.BILINEAR)
    t = ImageChops.add(vertical, horizontal, scale=2)
    return Image.composite(Image.new("RGB", (w, h), c2), Image.new("RGB", (w, h), c1), t)


def _rounded_mask(w, h, r):
    m = Image.new("L", (w * 2, h * 2), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w * 2 - 1, h * 2 - 1), radius=r * 2, fill=255)
    return m.resize((w, h), Image.Resampling.LANCZOS)


def _shadow(canvas, box, r, alpha=30, blur=18, dy=10):
    x0, y0, x1, y1 = box
    layer = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(layer).rounded_rectangle((x0, y0 + dy, x1, y1 + dy), radius=r, fill=alpha)
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    canvas.paste(Image.new("RGB", canvas.size, (40, 70, 110)), (0, 0), layer)


def _fit(draw, text, kind, size, max_width, minimum=20):
    while size > minimum and draw.textlength(text, font=_font(kind, size)) > max_width:
        size -= 2
    return _font(kind, size)


def _tile(canvas, box, tint, border, r=34):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    _shadow(canvas, box, r)
    canvas.paste(_diag_gradient(w, h, tint, (255, 255, 255)), (x0, y0), _rounded_mask(w, h, r))
    ImageDraw.Draw(canvas).rounded_rectangle(box, radius=r, outline=border, width=3)


def _hero(canvas, box, r=46):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    _shadow(canvas, box, r, alpha=45, blur=22, dy=12)
    canvas.paste(_diag_gradient(w, h, TEAL, BLUE), (x0, y0), _rounded_mask(w, h, r))


def _wrap(draw, text, kind, size, width, lines=2):
    words, rows = text.split(), [""]
    for word in words:
        trial = f"{rows[-1]} {word}".strip()
        if draw.textlength(trial, font=_font(kind, size)) <= width or not rows[-1]:
            rows[-1] = trial
        elif len(rows) < lines:
            rows.append(word)
        else:
            rows[-1] = rows[-1].rstrip(",") + "…"
            break
    return rows


def _draw_price(canvas, price: int) -> None:
    _hero(canvas, PRICE_BOX)
    d = ImageDraw.Draw(canvas)
    d.text((84, 872), "Цена", font=_font("regular", 28), fill=(214, 234, 240))
    d.text((84, 904), money(price), font=_font("bold", 86), fill=(255, 255, 255))
    right = 996
    t1, f1 = "в наличии · Симферополь", _font("semi", 30)
    w1 = d.textlength(t1, font=f1)
    d.ellipse((right - w1 - 36, 937, right - w1 - 16, 957), fill=(132, 232, 170))
    d.text((right - w1, 924), t1, font=f1, fill=(255, 255, 255))
    t2, f2 = "доставка по Крыму", _font("regular", 28)
    d.text((right - d.textlength(t2, font=f2), 974), t2, font=f2, fill=(224, 238, 244))


def _product_image(path: str | Path) -> Image.Image:
    flat = Image.open(path).convert("RGB")
    box = flat.convert("L").point(lambda v: 255 if v < 244 else 0).getbbox()
    if box:
        m = int(0.02 * max(flat.size))
        flat = flat.crop((max(0, box[0] - m), max(0, box[1] - m),
                          min(flat.width, box[2] + m), min(flat.height, box[3] + m)))
    return flat


def render_tile_card(item: CatalogItem, price: int, photo_path: str | Path,
                     out: str | Path) -> Path | None:
    """Нарисовать карточку. None — характеристик меньше двух, остаётся фото на белом."""
    tiles = card_tiles(item)
    if len(tiles) < 2:
        return None
    canvas = Image.new("RGB", (SIZE, SIZE), BG)

    # шапка: бренд и группа, название в одну-две строки
    _hero(canvas, (40, 40, 1040, 238))
    d = ImageDraw.Draw(canvas)
    brand = next((str(v).strip() for k, v in item.attrs.items()
                  if k.casefold() == "бренд" and str(v).strip()), item.brand)
    kicker = f"{brand} · {GROUP_LABEL.get(item.group, 'в наличии')}".upper()
    d.text((84, 66), kicker, font=_fit(d, kicker, "semi", 26, 900), fill=(205, 232, 236))
    title = re.sub(r"\s+", " ", item.name).strip()
    if d.textlength(title, font=_font("bold", 50)) <= 912:
        d.text((84, 112), title, font=_font("bold", 50), fill=(255, 255, 255))
    else:
        rows = _wrap(d, title, "bold", 40, 912)
        for n, row in enumerate(rows):
            d.text((84, 104 + n * 50), row, font=_font("bold", 40), fill=(255, 255, 255))

    # плитка с товаром
    box = (40, 266, 604, 822)
    _tile(canvas, box, (236, 242, 248), (203, 216, 232), r=40)
    product = _product_image(photo_path)
    w, h = box[2] - box[0], box[3] - box[1]
    scale = min((w - 110) / product.width, (h - 130) / product.height, 3.0)
    product = product.resize((max(1, round(product.width * scale)),
                              max(1, round(product.height * scale))), Image.Resampling.LANCZOS)
    px, py = box[0] + (w - product.width) // 2, box[1] + (h - product.height) // 2
    sh = Image.new("L", canvas.size, 0)
    sw = int(product.width * .78)
    ImageDraw.Draw(sh).ellipse((px + (product.width - sw) // 2, py + product.height - 12,
                                px + (product.width + sw) // 2, py + product.height + 24), fill=85)
    canvas.paste(Image.new("RGB", canvas.size, (120, 134, 154)), (0, 0),
                 sh.filter(ImageFilter.GaussianBlur(14)))
    layer = Image.new("RGB", canvas.size, (255, 255, 255))
    layer.paste(product, (px, py))
    mask = Image.new("L", canvas.size, 0)
    mask.paste(_rounded_mask(w, h, 40), (box[0], box[1]))
    canvas.paste(ImageChops.multiply(canvas, layer), (0, 0), mask)

    # плитки с цифрами
    gap = 14
    th = (822 - 266 - gap * (len(tiles) - 1)) // len(tiles)
    y = 266
    for (value, label), (accent, border, tint) in zip(tiles, TILES):
        _tile(canvas, (624, y, 1040, y + th), tint, border)
        d = ImageDraw.Draw(canvas)
        d.ellipse((652, y + 24, 670, y + 42), fill=accent)
        d.text((682, y + 14), label, font=_fit(d, label, "regular", 27, 330), fill=(86, 100, 118))
        size = 54 if th >= 150 else 46
        d.text((650, y + th - size - 24), value, font=_fit(d, value, "bold", size, 360), fill=accent)
        y += th + gap

    _draw_price(canvas, price)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, "JPEG", quality=93)
    return out


def repaint_price(card_path: str | Path, price: int) -> bool:
    """Цена на сайте сменилась: перерисовать плашку цены, остальное не трогать."""
    path = Path(card_path)
    if not path.is_file():
        return False
    canvas = Image.open(path).convert("RGB")
    if canvas.size != (SIZE, SIZE):
        return False
    x0, y0, x1, y1 = PRICE_BOX
    # Плашку и её тень закрываем фоном, затем рисуем заново.
    ImageDraw.Draw(canvas).rectangle((0, y0 - 14, SIZE, SIZE), fill=BG)
    _draw_price(canvas, price)
    canvas.save(path, "JPEG", quality=93)
    return True
