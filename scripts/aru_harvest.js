// Сбор карточек АРУ из разделов, которых нет в /vse-tovary/ (только чтение, под сессией владельца).
// Запускать в консоли/расширении на странице https://aru.ooo/ при выполненном входе.
//
// Почему нужен: /vse-tovary/ обрывается на id 67586 — всё новее (Электроинструмент, Инструмент
// для пайки, новинки других разделов) видно только в «Новинки» (/novinki/, по убыванию id)
// и в самих разделах. Проверено 2026-10-09: id <= 67586 из «Новинок» уже есть в основном обходе.
//
// 1) harvest(['/novinki/'], {stopBelowId: 67000}) — страницы по убыванию id, стоп после 3 страниц
//    с минимальным id ниже порога (порог = максимальный id основного обхода минус запас);
// 2) harvest(['/elektroinstrument/', '/instrument-dlya-payki/'], {tree: true}) — обход дерева
//    разделов до конечных категорий с пагинацией.
// Результат: window.__aru (id → строка карточки) — отдать scripts/aru_merge_supplement.py.
window.__aru = {};
window.__aruState = {state: 'idle', pages: 0, errs: 0, auth: true};

async function harvest(roots, opts = {}) {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const txt = (c, s) => (c.querySelector(s)?.textContent || '').trim().replace(/\s+/g, ' ');
  const st = window.__aruState; st.state = 'running';
  // Портал придерживает соединения при частых запросах: таймаут, повторы, пауза.
  const get = async url => {
    for (let a = 0; a < 3; a++) {
      const ac = new AbortController(); const t = setTimeout(() => ac.abort(), 25000);
      try { const r = await fetch(url, {credentials: 'same-origin', signal: ac.signal}); clearTimeout(t); if (r.ok) return await r.text(); }
      catch (e) { clearTimeout(t); }
      st.errs++; await sleep(2000 * (a + 1));
    }
    return null;
  };
  const cards = doc => [...doc.querySelectorAll('.v-products-list__item[data-product-id]')];
  const pagesOf = doc => Math.max(1, ...[...doc.querySelectorAll('.pagination a')].map(a => Number(new URL(a.href, location.origin).searchParams.get('page')) || 1));
  const take = (doc, cat) => {
    const captured = new Date().toISOString(); let minId = Infinity;
    for (const c of cards(doc)) {
      const id = c.getAttribute('data-product-id'); minId = Math.min(minId, +id);
      if (window.__aru[id]) continue;
      const specs = Object.fromEntries([...c.querySelectorAll('.v-products-list__features-tr')].map(r => [(r.querySelector('.v-products-list__features-name')?.textContent || '').trim(), (r.querySelector('.v-products-list__features-value')?.textContent || '').trim()]));
      const nameEl = c.querySelector('.v-products-list__name');
      window.__aru[id] = {id, article: txt(c, '.v-products-list-card__sku').replace(/^Артикул\s*-\s*/, ''), name: txt(c, '.v-products-list__name'), url: nameEl ? new URL(nameEl.getAttribute('href'), location.origin).href : '', stock_text: txt(c, '.v-products-list-card__stock'), price_text: txt(c, '.v-products-list__price'), old_price_text: txt(c, '.v-products-list__price-old'), specifications: specs, brand: specs['Бренд'] || '', image_urls: [...c.querySelectorAll('img')].map(i => new URL(i.getAttribute('data-src') || i.getAttribute('src'), location.origin).href), description: txt(c, '.v-products-list__text'), captured_at: captured, cat};
    }
    return minId;
  };
  const queue = roots.map(u => ({u})); const seen = new Set(); let low = 0;
  while (queue.length) {
    const {u} = queue.shift();
    if (seen.has(u)) continue; seen.add(u);
    const first = await get(u); if (first === null) { st.state = 'failed ' + u; return; }
    let doc = new DOMParser().parseFromString(first, 'text/html');
    if (!/Выйти/.test(doc.body.innerText)) st.auth = false;
    const total = pagesOf(doc), onPage = cards(doc);
    if (!onPage.length && opts.tree) {              // раздел без карточек — спускаемся в подразделы
      const productHrefs = new Set();
      [...new Set([...doc.querySelectorAll('a')].map(a => a.getAttribute('href') || '').filter(h => h.startsWith(u) && h !== u && h.endsWith('/') && h.slice(u.length).split('/').filter(Boolean).length === 1 && !productHrefs.has(h)))].forEach(s => queue.push({u: s}));
      await sleep(opts.pause || 1000); continue;
    }
    for (let p = 1; p <= total; p++) {
      if (p > 1) { const html = await get(`${u}?page=${p}`); if (html === null) { st.state = 'failed ' + u + ' p' + p; return; } doc = new DOMParser().parseFromString(html, 'text/html'); }
      const minId = take(doc, u); st.pages++;
      if (opts.stopBelowId) { low = minId <= opts.stopBelowId ? low + 1 : 0; if (low >= 3) { st.state = 'done'; return; } }
      await sleep(opts.pause || 1000);
    }
  }
  st.state = 'done';
}
