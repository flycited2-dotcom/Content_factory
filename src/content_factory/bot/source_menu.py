"""Price freshness and source toggles, independent of changing list numbers."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from content_factory.ingest.excel_price import load_price_slots, get_markups, tg_disabled, set_tg_enabled
from content_factory.ingest.source_links import source_refresh_metadata


def source_key(label: str) -> str:
    return hashlib.sha256(label.encode('utf-8')).hexdigest()[:12]


def sources_markup(prices_dir) -> dict:
    off = tg_disabled(prices_dir)
    buttons = [{'text': f"{'⚪' if label in off else '🟢'} {n}",
                'callback_data': f'srctg:{source_key(label)}'}
               for n, (label, _) in enumerate(load_price_slots(prices_dir), 1)]
    return {'inline_keyboard': [buttons[i:i+4] for i in range(0, len(buttons), 4)]}


def toggle_tg_source(prices_dir, data: str) -> str:
    key = data.partition(':')[2]
    label = next((label for label, _ in load_price_slots(prices_dir) if source_key(label) == key), None)
    if label is None:
        return 'Список источников изменился. Откройте /sources и нажмите новую кнопку.'
    on = label in tg_disabled(prices_dir)
    set_tg_enabled(prices_dir, label, on)
    return f"{'🟢 включён' if on else '⚪ выключен'} для /task и /find: {label}"


def make_sources_fn(prices_dir):
    def sources_fn() -> str:
        pdir = Path(prices_dir)
        slots = load_price_slots(pdir)
        if not slots:
            return '❌ Источников нет — пришлите .xlsx прайс файлом.'
        metadata = source_refresh_metadata(pdir)
        try:
            mail = json.loads((pdir / 'mail_sources.json').read_text(encoding='utf-8'))
            if not isinstance(mail, dict):
                mail = {}
        except (OSError, ValueError):
            mail = {}
        markups, off = get_markups(pdir), tg_disabled(pdir)
        lines = ['📦 Прайсы: 🟢 включён в /task и /find; ⚪ исключён из них.']
        try:
            sync = json.loads((pdir / 'price-sync-status.json').read_text(encoding='utf-8'))
            if isinstance(sync, dict) and sync.get('status') == 'deferred':
                lines.append('⚠️ Автообновление БытТехОпт отложено: свежий выпуск не подтверждён '
                             'или источник недоступен. Ниже дата последнего сохранённого прайса.')
        except (OSError, ValueError):
            pass
        for n, (label, items) in enumerate(slots, 1):
            meta = metadata.get(label, {})
            if not isinstance(meta, dict):
                meta = {}
            name = meta.get('display_name', label)
            count = len(items)
            pct = markups.get(label, 0)
            extra = f" · {pct:+g}%" if pct else ''
            path = pdir / f'{label}.xlsx'
            saved = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).astimezone()
            if meta.get('issue_date'):
                details = f"авто · прайс {meta['issue_date']}"
            elif label.startswith('mail__'):
                entry = mail.get(label, {})
                date = entry.get('mail_date', '') if isinstance(entry, dict) else ''
                if date:
                    from email.utils import parsedate_to_datetime
                    try:
                        date = parsedate_to_datetime(date).strftime('%d.%m.%Y')
                    except (TypeError, ValueError):
                        date = ''
                details = f"почта · письмо {date or saved.strftime('%d.%m.%Y')}"
            else:
                details = 'ручная копия · автообновление не подключено'
            lines.append(f"{'⚪' if label in off else '🟢'} {n}. {name}: {count} поз.{extra}\n"
                         f"   {details}; сохранён {saved.strftime('%d.%m.%Y %H:%M')}")
        lines += ['\nКнопки ниже включают/выключают поиск и постановку задач, а не загрузку прайса.',
                  'Новая почтовая цена появляется после новой рассылки поставщика.',
                  'Наценка: /markup <слот> <±число>']
        return '\n'.join(lines)[:3900]
    return sources_fn
