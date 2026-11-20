// «Показ за 3 минуты»: тур по проекту для жюри. Сам переключает персонажей, вкладки и случаи, подсвечивает,
// куда смотреть. Шаг хранится в sessionStorage, поэтому тур переходит между страницами (клиент → сотрудник →
// эффективность). Подключается на всех страницах; работает, только если тур запущен.
(() => {
  const KEY = 'ca_tour';
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const $ = id => document.getElementById(id);

  // Экран клиента: войти под персонажем так же, как через демо-панель, и дождаться, пока экран обновится.
  async function persona(id, tab, focus) {
    if (typeof me === 'undefined' || me !== id) {
      const sel = $('who');
      for (let i = 0; i < 40 && !sel.querySelector(`option[value="${id}"]`); i++) await wait(100);
      sel.value = String(id);
      sel.dispatchEvent(new Event('change'));
      for (let i = 0; i < 40 && me !== id; i++) await wait(100);
      await wait(900);
    }
    if (tab) showTab(tab, focus);
    await wait(200);
  }
  async function post(path, body) {
    const r = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
    return r.ok ? r.json() : null;
  }
  // Гульнар жалуется на перевод Данияру, если в показе этот шаг пропустили.
  async function ensureComplaint() {
    await post('/login', {role: 'client', client_id: 1006});
    const feed = await (await fetch('/clients/1006')).json();
    const t = (feed.feed || []).find(x => x.direction === 'out' && x.receiver_id === 1005);
    if (t && !(feed.complaints || []).length) await post(`/transfers/${t.id}/complaint`);
  }
  async function analystReady() {
    const s = await (await fetch('/me')).json().catch(() => ({}));
    return !!(s && s.analyst);
  }

  const STEPS = [
    {page: '/client', target: 'alerts', text: 'Айдане пришли 150 000 ₸ от незнакомого человека. Приложение сразу предупреждает: возможно, это чужие деньги, — и даёт вернуть их отправителю одной кнопкой.',
     run: () => persona(1001, 'home')},
    {page: '/client', target: 'recipientNote', text: 'Мошенник пишет: «Я ошибся, верните на другую карту». Ещё до перевода «светофор получателя» горит красным. Нажмите «Перевести» — перевод остановится, и Айдана увидит, почему это схема.',
     run: async () => { await persona(1001, 'pay'); $('to').value = '1003'; $('amount').value = '150000'; await checkRecipient(); }},
    {page: '/client', target: '[data-report]', text: 'Гульнар купила телефон по объявлению, а деньги ушли мошенникам. Нажмите «Это были мошенники?» у перевода Данияру: банк пойдёт по цепочке переводов, а Гульнар получит готовый текст заявления в полицию.',
     run: async function () {
       await persona(1006, 'home');
       const t = ((await (await fetch('/clients/1006')).json()).feed || []).find(x => x.direction === 'out' && x.receiver_id === 1005);
       this.sel = t ? `[data-report="${t.id}"]` : 'feed';
     }},
    {page: '/client', target: 'blockCard', text: 'Данияр по правилам похож на невольного участника: у него заморожена только спорная сумма, остальными деньгами он пользуется. Причина — простыми словами, апелляция и срок ответа — прямо в приложении.',
     run: async () => { await ensureComplaint(); await persona(1005, 'home'); }},
    {page: '/client', target: 'safeCard', text: 'Сбережения Мамы лежат в «Сейфе». Даже если мошенник уговорит её по телефону, перевести их через приложение нельзя: открыть сейф можно через сутки (дочь узнает и сможет отменить) или в отделении.',
     run: () => persona(1004, 'safe', 'safeCard')},
    {page: '/client', target: 'idCards', text: 'На имя Венеры по её данным и поддельному видео открыли счёт. Она узнаёт об этом сразу и нажимает «Это не я»: счёт закрыт, а Венера получает подтверждение для полиции и суда.',
     run: () => persona(1022, 'home')},
    {page: '/analyst', target: 'graph', analyst: true, text: 'Сотрудник банка видит граф денег и подсказку «невольный участник или организатор». Крупный возврат пострадавшей подтверждает второй сотрудник — правило двух ключей. ИИН и телефон клиента скрыты, просмотр записывается.',
     run: async () => {
       let all = await api('/analyst/cases');
       let k = all.find(c => c.client_id === 1003 && c.kind === 'block') || all.find(c => c.client_id === 1005);
       if (!k) { await ensureComplaint(); all = await api('/analyst/cases'); k = all.find(c => c.client_id === 1003 && c.kind === 'block'); }
       if (k) await open(k.case_id);
     }},
    {page: '/analyst', target: 'graph', analyst: true, text: 'Сеть дропперов: с двух «дроп-телефонов» входят в приложение счета десяти разных людей, среди них — счёт, открытый на имя Венеры. Приостановить их можно одной кнопкой, после подтверждения второго сотрудника.',
     run: async () => {
       const nets = await api('/analyst/networks');
       const n = nets.find(x => x.farms.length) || nets[0];
       if (n) { goNetwork(n.key); await wait(900); }
     }},
    {page: '/analyst/stats', target: 'journal', text: 'Журнал действий нельзя подделать задним числом: каждая запись хранит отпечаток предыдущей. Нажмите «Демо: изменить запись задним числом» — проверка сразу найдёт подделку.',
     run: () => wait(1200)},
    {page: '/analyst/stats', target: 'calc', text: 'Что даст внедрение в банке: впишите число клиентов и переводов — калькулятор посчитает сохранённые деньги и нагрузку на сотрудников по долям, которые система показала на синтетике. Всё это — работающий прототип: попробуйте любого из 20 персонажей сами.',
     run: () => wait(600)},
  ];

  const css = document.createElement('style');
  css.textContent = `
    .tour-panel { position:fixed; right:16px; bottom:16px; width:380px; max-width:calc(100vw - 16px); z-index:1000;
      background:#fff; border:2px solid #00ab75; border-radius:16px; padding:14px 16px; color:#1d1d1e;
      font:15px/1.45 "Roboto Flex", Roboto, "Segoe UI", system-ui, sans-serif; }
    .tour-panel .tour-step { color:#587366; font-size:13px; margin-bottom:4px; }
    .tour-panel p { margin:0 0 10px; }
    .tour-panel .tour-actions { display:flex; gap:8px; flex-wrap:wrap; }
    .tour-panel button { border:1px solid #00ab75; background:#fff; color:#00875c; border-radius:12px; padding:8px 14px;
      font:inherit; font-size:14px; font-weight:500; cursor:pointer; }
    .tour-panel button.tour-next { background:#00ab75; color:#fff; flex:1; }
    .tour-panel button:disabled { opacity:.5; cursor:default; }
    .tour-hl { outline:3px solid #00ab75 !important; outline-offset:4px; border-radius:12px; scroll-margin-top:90px; }
    body.tour-on { padding-bottom:280px; }   /* под панелью тура остаётся место: её не закрыть собой нужную кнопку */
    @media (max-width:520px) { .tour-panel { right:8px; left:8px; bottom:8px; width:auto; } }`;
  document.head.appendChild(css);

  const step = () => { const v = sessionStorage.getItem(KEY); return v === null ? null : +v; };
  let running = false;

  function panel(i, text, ready) {
    let p = document.querySelector('.tour-panel');
    if (!p) { p = document.createElement('div'); p.className = 'tour-panel'; p.setAttribute('role', 'dialog'); document.body.appendChild(p); }
    document.body.classList.add('tour-on');
    const last = i === STEPS.length - 1;
    p.innerHTML = `<div class="tour-step">Показ за 3 минуты · шаг ${i + 1} из ${STEPS.length}</div><p></p>
      <div class="tour-actions">${i ? '<button class="tour-back">Назад</button>' : ''}
        <button class="tour-next"${ready ? '' : ' disabled'}>${last ? 'Закончить' : 'Дальше'}</button>
        <button class="tour-stop">Выйти из показа</button></div>`;
    p.querySelector('p').textContent = text;
    p.querySelector('.tour-next').onclick = () => go(last ? null : i + 1);
    if (i) p.querySelector('.tour-back').onclick = () => go(i - 1);
    p.querySelector('.tour-stop').onclick = () => go(null);
  }
  function highlight(target) {
    document.querySelectorAll('.tour-hl').forEach(e => e.classList.remove('tour-hl'));
    const el = target.startsWith('[') ? document.querySelector(target) : $(target);
    if (!el) return;
    el.classList.add('tour-hl');
    el.scrollIntoView({block: 'start', behavior: 'smooth'});
  }
  function go(i) {
    if (i === null) {
      sessionStorage.removeItem(KEY);
      document.querySelectorAll('.tour-hl').forEach(e => e.classList.remove('tour-hl'));
      const p = document.querySelector('.tour-panel'); if (p) p.remove();
      document.body.classList.remove('tour-on');
      if (location.pathname !== '/') location.href = '/';
      return;
    }
    sessionStorage.setItem(KEY, String(i));
    if (STEPS[i].page !== location.pathname) location.href = STEPS[i].page;
    else window.tourResume();
  }

  window.tourStart = () => go(0);
  window.tourResume = async () => {
    const i = step();
    if (i === null || !STEPS[i] || STEPS[i].page !== location.pathname || running) return;
    const s = STEPS[i];
    running = true;
    try {
      if (s.analyst && !(await analystReady())) {
        panel(i, 'Войдите как сотрудник банка: выберите сотрудника и введите код доступа. Он указан в описании проекта (при запуске на своём компьютере — 2026).', false);
        return;
      }
      panel(i, 'Готовим шаг…', false);
      await s.run();
      panel(i, s.text, true);
      await wait(150);
      highlight(s.sel || s.target);
    } catch (e) {
      panel(i, s.text, true);
    } finally {
      running = false;
    }
  };
  if (step() !== null) addEventListener('load', () => setTimeout(window.tourResume, 600));
})();
