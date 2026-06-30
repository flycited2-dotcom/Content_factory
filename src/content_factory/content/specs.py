"""Блок «ключевые особенности» серии (B2B-формат, как в stock_report_bot).
ЧИСТЫЕ функции над списком tech-строк [{'title','value'}] — без БД/сети.

Названия характеристик у Бриз/Daichi/Русклимат разные → матчим по ключевым словам.
Значения «сырые» (да/нет, диапазоны, шум '18,5/25/29') → нормализуем здесь.
Перенос из Splithub stock_report_bot/specs.py. ✓-УТП Бриза (utp_raw) пока не подаём
(добавим, когда подключим Breez API) — `_utp_extras` без данных вернёт []."""
import html
import re

BREEZE_SOURCE = "breeze"

_NEG_RE = re.compile(r"^\s*(нет|no|n|0|[-−–—]|отсут\w*|false|не\s)", re.I)
_NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
_CLASS_RE = re.compile(r"[A-G]\+{0,3}")


def _affirm(value):
    v = (value or "").strip()
    return bool(v) and not _NEG_RE.match(v)


def _nums(s):
    out = []
    for m in _NUM_RE.findall(s or ""):
        try:
            out.append(float(m.replace(",", ".")))
        except ValueError:
            pass
    return out


def _fmt_num(x):
    return str(int(x)) if float(x).is_integer() else ("%g" % x)


def _min_num(s):
    ns = _nums(s)
    return _fmt_num(min(ns)) if ns else None


def _class(s):
    m = _CLASS_RE.search(s or "")
    return m.group(0) if m else None


def _clean_utp(raw):
    """HTML-список преимуществ → список строк-пунктов (двойной unescape — для Бриза)."""
    s = html.unescape(html.unescape(raw or ""))
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    items = []
    for part in re.split(r"[;\n]+", s):
        p = part.strip().strip("•●·*-–—").strip()
        if p:
            items.append(p)
    return items


class _Tech:
    def __init__(self, rows, titles=None):
        self.rows = [((r.get("title") or ""), (r.get("value") or "").strip()) for r in rows]
        self.titles = [t for t in (titles or []) if t]

    def value(self, pattern):
        rx = re.compile(pattern, re.I)
        for title, val in self.rows:
            if val and rx.search(title):
                return val
        return None


def _energy_class(t):
    for title, val in t.rows:
        if re.search(r"нагрев|обогрев|\bcop\b|scop", title, re.I):
            continue
        if re.search(r"класс.*энергоэфф|энергоэффективность|\bseer\b|eer.*класс", title, re.I):
            c = _class(val)
            if c:
                return f"⚡ Класс энергоэффективности {c}"
    return None


def _inverter(t):
    explicit_no = False
    for title, val in t.rows:
        if re.search(r"технология работы", title, re.I):
            if re.search(r"inverter|инвертор", val, re.I):
                return "❄️ Инверторная технология"
            explicit_no = True
        elif re.search(r"инверторн", title, re.I):
            if _affirm(val):
                return "❄️ Инверторная технология"
            explicit_no = True
    if not explicit_no and any(re.search(r"инвертор|inverter", s, re.I) for s in t.titles):
        return "❄️ Инверторная технология"
    return None


def _compressor(t):
    val = t.value(r"марка компрессора")
    return f"⚙️ Компрессор {val}" if val else None


def _heat_min(t):
    nums = []
    for title, val in t.rows:
        if (re.search(r"(границ|диапазон).*нагрев", title, re.I)
                or re.search(r"мин.*температ.*(внешн|наруж)", title, re.I)):
            nums += _nums(val)
    if not nums:
        return None
    return f"🌡 Работа на обогрев до {_fmt_num(min(nums)).replace('-', '−')} °C"


def _noise_min(t):
    nums = []
    for title, val in t.rows:
        if re.search(r"шум|звуков", title, re.I) and re.search(r"внутр|вб|iu", title, re.I):
            nums += [n for n in _nums(val) if n > 0]
    return f"🔇 Уровень шума от {_fmt_num(min(nums))} дБ" if nums else None


def _warranty(t):
    val = t.value(r"гаранти")
    if not val:
        return None
    val = val.strip()
    if val.isdigit():
        val = f"{val} мес"
    return f"🛡 Гарантия {val}"


def _wifi(t):
    for title, val in t.rows:
        if re.search(r"wi[\s-]?fi|вай[\s-]?фай|облачн|удал.{0,5}управл", title, re.I):
            if not _affirm(val):
                continue
            if re.search(r"ready|опц|доп", val, re.I):
                return "📶 Wi-Fi (опция)"
            return "📶 Wi-Fi управление"
    return None


def _flag(t, pattern, label):
    for title, val in t.rows:
        if re.search(pattern, title, re.I) and _affirm(val):
            return label
    return None


_FEATURES = [
    _energy_class,
    _inverter,
    _compressor,
    _heat_min,
    _noise_min,
    _warranty,
    _wifi,
    lambda t: _flag(t, r"ионизатор|ионизац", "🌿 Ионизация воздуха"),
    lambda t: _flag(t, r"плазмен", "✨ Плазменная очистка"),
    lambda t: _flag(t, r"ультрафиолет|\bуф[\s-]", "☀️ УФ-обеззараживание"),
    lambda t: _flag(t, r"автоочист|самоочист", "🧼 Самоочистка"),
    lambda t: _flag(t, r"режим sleep|ночной режим", "🌙 Ночной режим (SLEEP)"),
    lambda t: _flag(t, r"авторестарт|перезапуск.*питан", "🔄 Авторестарт после отключения"),
]

# Концепты, которые показываются структурными буллетами. Пункт из УТП с таким
# концептом берём, ТОЛЬКО если соответствующий буллет НЕ сформирован (иначе дубль).
# Так wifi/инвертор/… из УТП покажутся, когда отдельного tech-поля нет (грабля владельца).
_CONCEPTS = ("wi-fi", "wifi", "вай", "инвертор", "ионизац", "плазм", "ультрафиолет",
             "уф-", "самоочист", "автоочист", "sleep", "ночн", "авторестарт",
             "перезапуск", "энергоэфф", "класс энерг", "гаранти", "компрессор", "шум",
             "голос", "обогрев")

# Ключевые слова «фич» — чтобы из прозы «Описание» вытащить только пункты-преимущества,
# а не маркетинговую воду.
_DESC_FEATURE_RE = re.compile(
    r"режим|функц|технолог|защит|фильтр|управл|таймер|очистк|тих|эконом|класс|"
    r"компрессор|гаранти|инвертор|wi[\s-]?fi|вай|обогрев|охлажд|осушен|ионизац|самоочист|пульт",
    re.I,
)


def _dup_of_produced(item_low, produced_low):
    for c in _CONCEPTS:
        if c in item_low and c in produced_low:
            return True
    return False


def _desc_features(desc):
    """Из прозы «Описание» вытащить пункты-преимущества (предложения с фич-словами)."""
    s = html.unescape(html.unescape(desc or ""))
    s = re.sub(r"<[^>]+>", " ", s)
    out = []
    for part in re.split(r"[;\n.!]+", s):
        p = part.strip().strip("•●·*-–—").strip()
        if 8 <= len(p) <= 90 and _DESC_FEATURE_RE.search(p):
            out.append(p)
    return out


def _utp_extras(t, source, utp_raw, produced):
    """Доп. ✓-фишки: Бриз — utp_raw из API; иначе tech-поле «УТП»; иначе проза «Описание».
    Пропускаем пункт, если он уже показан структурным буллетом (дедуп). До 5 шт."""
    if source == BREEZE_SOURCE and utp_raw:
        items = _clean_utp(utp_raw)
    elif t.value(r"^\s*утп\b"):
        items = _clean_utp(t.value(r"^\s*утп\b"))
    else:
        items = _desc_features(t.value(r"^\s*описание\b"))
    produced_low = " ".join(produced).lower()
    out, seen = [], set()
    for it in items:
        low = it.lower()
        if _dup_of_produced(low, produced_low) or low in seen:
            continue
        seen.add(low)
        out.append(f"✓ {it}")
        if len(out) >= 5:
            break
    return out


def build_specs_for_card(tech_rows, brand, series, source, utp_raw=None, titles=None):
    """Список строк-преимуществ серии (plain text). Пусто → []."""
    t = _Tech(tech_rows, titles)
    lines = [ln for extract in _FEATURES if (ln := extract(t))]
    lines += _utp_extras(t, source, utp_raw, lines)
    return lines
