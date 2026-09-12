const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

let page = "stats";
let period = "day";
let cache = {};

const tg = window.Telegram && window.Telegram.WebApp;
function initData() {
  return (tg && tg.initData) || "";
}
function isWebApp() {
  return Boolean(initData());
}

if (tg) {
  try {
    tg.ready();
    tg.expand();
    document.documentElement.classList.add("webapp");
  } catch {}
}

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (initData()) headers["X-Telegram-Init-Data"] = initData();
  const res = await fetch(path, {
    ...opts,
    headers,
  });
  if (res.status === 401 || res.status === 403) {
    if (isWebApp()) showDenied();
    else showLogin();
    throw new Error("auth");
  }
  if (!res.ok) {
    let msg = "Ошибка";
    try {
      const j = await res.json();
      msg = j.detail || JSON.stringify(j);
    } catch {}
    throw new Error(typeof msg === "string" ? msg : "Ошибка");
  }
  return res.json();
}

function hideAll() {
  $("#login").classList.add("hidden");
  $("#denied").classList.add("hidden");
  $("#app").classList.add("hidden");
}

function showLogin() {
  hideAll();
  if (isWebApp()) {
    showDenied();
    return;
  }
  $("#login").classList.remove("hidden");
}

function showDenied() {
  hideAll();
  $("#denied").classList.remove("hidden");
}

function showApp() {
  hideAll();
  $("#app").classList.remove("hidden");
  if (isWebApp()) {
    const btn = $("#logout-btn");
    if (btn) btn.classList.add("hidden");
  }
  render();
}

async function doLogin(e) {
  e.preventDefault();
  $("#login-err").textContent = "";
  try {
    await api("/api/login", { method: "POST", body: JSON.stringify({ password: $("#password").value }) });
    showApp();
  } catch (err) {
    $("#login-err").textContent = "Неверный пароль";
  }
  return false;
}

async function logout() {
  await api("/api/logout", { method: "POST" });
  showLogin();
}

$$(".nav button").forEach((b) =>
  b.addEventListener("click", () => {
    $$(".nav button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    page = b.dataset.page;
    render();
  })
);

function esc(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function money(n) {
  return Number(n || 0).toLocaleString("ru-RU");
}

async function render() {
  const root = $("#page");
  root.innerHTML = "Загрузка…";
  try {
    if (page === "stats") return renderStats(root);
    if (page === "ads") return renderAds(root);
    if (page === "shows") return renderShows(root);
    if (page === "offers") return renderOffers(root);
    if (page === "campaigns") return renderCampaigns(root);
    if (page === "broadcast") return renderBroadcast(root);
    if (page === "userbots") return renderUserbots(root);
    if (page === "withdrawals") return renderWithdrawals(root);
    if (page === "users") return renderUsers(root);
    if (page === "settings") return renderSettings(root);
  } catch (e) {
    root.innerHTML = `<p class="err">${esc(e.message)}</p>`;
  }
}

async function renderStats(root) {
  const s = await api("/api/stats?period=" + period);
  root.innerHTML = `
    <div class="top"><h2>Статистика</h2></div>
    <div class="pills">
      ${["day", "week", "all"].map((p) => `<button class="pill ${p===period?"active":""}" data-p="${p}">${p==="day"?"День":p==="week"?"Неделя":"Всё время"}</button>`).join("")}
    </div>
    <div class="cards">
      ${card("Люди (всего)", s.total_users)}
      ${card("Новые", s.new_users)}
      ${card("Активные", s.active_users)}
      ${card("Премиум", s.premium, s.premium_percent + "%")}
      ${card("Клики", s.clicks)}
      ${card("Начислено ⭐", s.stars_accrued.toFixed(2))}
      ${card("Выведено ⭐", s.stars_withdrawn, s.withdraw_count + " заявок")}
      ${card("В обработке", s.pending_withdrawals)}
      ${card("Входящие ⭐", s.stars_income, s.income_fiat + " " + s.fiat_currency)}
      ${card("Оплат", s.payments_count, s.conversion_pay + "% от людей")}
      ${card("Рефералы", s.referrals)}
      ${card("Задания", s.tasks_done)}
      ${card("Показы офферов", s.offer_views)}
      ${card("Показы после старта", s.shows_sent || 0)}
      ${card("Кликали", s.users_clicked, s.conversion_click + "%")}
      ${card("Дошли до вывода", s.users_withdraw, s.conversion_withdraw + "%")}
      ${card("Ср. кликов / чел", s.avg_clicks_per_user)}
    </div>
    <div class="panel">
      <h3>Воронка</h3>
      <div class="funnel">
        <div><div class="k">Старт</div><b>${s.funnel.starts}</b></div>
        <div><div class="k">Кликер</div><b>${s.funnel.clicked}</b><div class="s">${pct(s.funnel.clicked, s.funnel.starts)}</div></div>
        <div><div class="k">Задания</div><b>${s.funnel.tasks}</b></div>
        <div><div class="k">Заявки вывода</div><b>${s.funnel.withdraw_started}</b><div class="s">${pct(s.funnel.withdraw_started, s.funnel.starts)}</div></div>
        <div><div class="k">Оплата 3⭐</div><b>${s.funnel.paid_check}</b><div class="s">${pct(s.funnel.paid_check, s.funnel.starts)}</div></div>
        <div><div class="k">Выведено</div><b>${s.funnel.withdraw_done}</b></div>
      </div>
    </div>`;
  $$(".pill", root).forEach((b) =>
    b.addEventListener("click", () => {
      period = b.dataset.p;
      render();
    })
  );
}

function card(k, v, s) {
  return `<div class="card"><div class="k">${k}</div><div class="v">${v}</div>${s ? `<div class="s">${s}</div>` : ""}</div>`;
}
function pct(a, b) {
  if (!b) return "0%";
  return ((a * 100) / b).toFixed(1) + "%";
}

function editorFields(prefix, item = {}) {
  return `
    <div class="field"><label>Заголовок (для себя)</label><input id="${prefix}-title" value="${esc(item.title || "")}"></div>
    <div class="toolbar">
      <button type="button" onclick="wrap('${prefix}-text','<b>','</b>')"><b>B</b></button>
      <button type="button" onclick="wrap('${prefix}-text','<i>','</i>')"><i>I</i></button>
      <button type="button" onclick="wrap('${prefix}-text','<u>','</u>')"><u>U</u></button>
      <button type="button" onclick="wrap('${prefix}-text','<s>','</s>')"><s>S</s></button>
      <button type="button" onclick="wrap('${prefix}-text','<tg-spoiler>','</tg-spoiler>')">Спойлер</button>
      <button type="button" onclick="wrap('${prefix}-text','<code>','</code>')">Код</button>
      <button type="button" onclick="wrap('${prefix}-text','<blockquote>','</blockquote>')">Цитата</button>
      <button type="button" onclick="insertLink('${prefix}-text')">Ссылка</button>
      <button type="button" onclick="insertEmoji('${prefix}-text')">Прем эмодзи</button>
    </div>
    <div class="field"><label>Текст (HTML / Markdown / MarkdownV2)</label><textarea id="${prefix}-text">${esc(item.text || "")}</textarea></div>
    <div class="grid2">
      <div class="field"><label>Форматирование</label>
        <select id="${prefix}-parse">
          ${["HTML","Markdown","MarkdownV2","NONE"].map(m => `<option ${((item.parse_mode||"HTML")===m)?"selected":""}>${m}</option>`).join("")}
        </select>
      </div>
      <div class="field"><label>Медиа</label>
        <select id="${prefix}-media">${["none","photo","video","animation"].map(m => `<option ${((item.media_type||"none")===m)?"selected":""}>${m}</option>`).join("")}</select>
      </div>
    </div>
    <div class="grid2">
      <div class="field"><label>file_id Telegram</label><input id="${prefix}-fileid" value="${esc(item.media_file_id || "")}"></div>
      <div class="field"><label>Или загрузить файл</label><input id="${prefix}-file" type="file"></div>
    </div>
    <input type="hidden" id="${prefix}-mediapath" value="${esc(item.media_path || "")}">
  `;
}

window.wrap = function (id, a, b) {
  const el = document.getElementById(id);
  const s = el.selectionStart, e = el.selectionEnd;
  const v = el.value;
  el.value = v.slice(0, s) + a + v.slice(s, e) + b + v.slice(e);
  el.focus();
};
window.insertLink = function (id) {
  const url = prompt("URL", "https://");
  if (url) wrap(id, `<a href="${url}">`, "</a>");
};
window.insertEmoji = function (id) {
  const eid = prompt("ID премиум-эмодзи (custom_emoji_id)", "");
  const fb = prompt("Запасной эмодзи", "⭐") || "⭐";
  if (eid) {
    const el = document.getElementById(id);
    el.value += `<tg-emoji emoji-id="${eid}">${fb}</tg-emoji>`;
  }
};

async function maybeUpload(prefix) {
  const file = document.getElementById(prefix + "-file").files[0];
  if (!file) return document.getElementById(prefix + "-mediapath").value;
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch("/api/upload", { method: "POST", body: fd });
  const j = await res.json();
  return j.path;
}

function adPayload(prefix) {
  return {
    title: $(`#${prefix}-title`).value,
    text: $(`#${prefix}-text`).value,
    parse_mode: $(`#${prefix}-parse`).value,
    media_type: $(`#${prefix}-media`).value,
    media_file_id: $(`#${prefix}-fileid`).value,
    button_type: $(`#${prefix}-btype`).value,
    button_text: $(`#${prefix}-btext`).value,
    button_url: $(`#${prefix}-burl`).value,
    button_color: $(`#${prefix}-bcolor`).value,
    is_active: $(`#${prefix}-active`).checked,
    sort_order: Number($(`#${prefix}-sort`).value || 0),
  };
}

async function renderAds(root) {
  const ads = await api("/api/ads");
  cache.ads = Object.fromEntries(ads.map((a) => [a.id, a]));
  root.innerHTML = `
    <div class="top"><h2>Реклама после /start</h2><button class="btn" onclick="toggle('#ad-form')">+ Добавить</button></div>
    <p class="muted">Сообщение уходит сразу после старта. Можно HTML, Markdown, MarkdownV2, прем-эмодзи, обычную или цветную кнопку.</p>
    <div id="ad-form" class="panel hidden">
      ${editorFields("ad")}
      <div class="grid2">
        <div class="field"><label>Кнопка</label>
          <select id="ad-btype">
            <option value="none">Без кнопки</option>
            <option value="url">Обычная кнопка</option>
            <option value="colored">Цветная кнопка</option>
          </select>
        </div>
        <div class="field"><label>Цвет</label><input id="ad-bcolor" type="color" value="#2AABEE"></div>
      </div>
      <div class="grid2">
        <div class="field"><label>Текст кнопки</label><input id="ad-btext"></div>
        <div class="field"><label>URL кнопки</label><input id="ad-burl" placeholder="https://t.me/..."></div>
      </div>
      <div class="grid2">
        <div class="field"><label>Порядок</label><input id="ad-sort" type="number" value="0"></div>
        <div class="field"><label>Активна</label><input id="ad-active" type="checkbox" checked></div>
      </div>
      <button class="btn" onclick="saveAd()">Сохранить</button>
    </div>
    <div class="panel">
      <table>
        <tr><th>ID</th><th>Название</th><th>Кнопка</th><th>Режим</th><th></th></tr>
        ${ads.map((a) => `<tr>
          <td>${a.id}</td>
          <td>${esc(a.title)} ${a.is_active ? "<span class='ok'>●</span>" : "<span class='muted'>○</span>"}</td>
          <td>${esc(a.button_type)}</td>
          <td>${esc(a.parse_mode)}</td>
          <td>
            <button class="btn ghost small" onclick="editAd(${a.id})">Изменить</button>
            <button class="btn red small" onclick="delAd(${a.id})">Удалить</button>
          </td>
        </tr>`).join("")}
      </table>
    </div>`;
}

window.toggle = (sel) => $(sel).classList.toggle("hidden");

window.saveAd = async function (id) {
  const payload = adPayload("ad");
  payload.media_path = await maybeUpload("ad");
  if (id) await api("/api/ads/" + id, { method: "PUT", body: JSON.stringify(payload) });
  else await api("/api/ads", { method: "POST", body: JSON.stringify(payload) });
  render();
};
window.delAd = async function (id) {
  if (!confirm("Удалить рекламу?")) return;
  await api("/api/ads/" + id, { method: "DELETE" });
  render();
};
window.editAd = function (id) {
  const a = cache.ads[id];
  if (!a) return;
  $("#ad-form").classList.remove("hidden");
  $("#ad-title").value = a.title;
  $("#ad-text").value = a.text;
  $("#ad-parse").value = a.parse_mode;
  $("#ad-media").value = a.media_type;
  $("#ad-fileid").value = a.media_file_id;
  $("#ad-mediapath").value = a.media_path;
  $("#ad-btype").value = a.button_type;
  $("#ad-btext").value = a.button_text;
  $("#ad-burl").value = a.button_url;
  $("#ad-bcolor").value = a.button_color || "#2AABEE";
  $("#ad-sort").value = a.sort_order;
  $("#ad-active").checked = a.is_active;
  $(".btn", $("#ad-form")).onclick = () => saveAd(a.id);
};

async function renderShows(root) {
  const items = await api("/api/shows");
  cache.shows = Object.fromEntries(items.map((s) => [s.id, s]));
  root.innerHTML = `
    <div class="top"><h2>Показы после старта</h2><button class="btn" onclick="toggle('#sh-form')">+ Добавить</button></div>
    <p class="muted">Пост уходит через N секунд после /start. Можно несколько — каждый со своей задержкой. Если спонсоров нет, задания кликера крутятся по кругу.</p>
    <div id="sh-form" class="panel hidden">
      ${editorFields("sh")}
      <div class="grid2">
        <div class="field"><label>Задержка, сек</label><input id="sh-delay" type="number" value="10"></div>
        <div class="field"><label>Кнопка</label>
          <select id="sh-btype">
            <option value="none">Без кнопки</option>
            <option value="url">Обычная</option>
            <option value="colored">Цветная</option>
          </select>
        </div>
      </div>
      <div class="grid2">
        <div class="field"><label>Текст кнопки</label><input id="sh-btext"></div>
        <div class="field"><label>URL</label><input id="sh-burl"></div>
      </div>
      <div class="grid2">
        <div class="field"><label>Порядок</label><input id="sh-sort" type="number" value="0"></div>
        <div class="field"><label>Активен</label><input id="sh-active" type="checkbox" checked></div>
      </div>
      <input type="hidden" id="sh-bcolor" value="#2AABEE">
      <button class="btn" onclick="saveShow()">Сохранить</button>
    </div>
    <div class="panel">
      <table>
        <tr><th>ID</th><th>Название</th><th>Сек</th><th></th></tr>
        ${items.map((s) => `<tr>
          <td>${s.id}</td>
          <td>${esc(s.title)} ${s.is_active ? "<span class='ok'>●</span>" : ""}</td>
          <td>${s.delay_seconds}</td>
          <td>
            <button class="btn ghost small" onclick="editShow(${s.id})">Изменить</button>
            <button class="btn red small" onclick="delShow(${s.id})">Удалить</button>
          </td>
        </tr>`).join("")}
      </table>
    </div>`;
}

window.saveShow = async function (id) {
  const payload = {
    title: $("#sh-title").value,
    text: $("#sh-text").value,
    parse_mode: $("#sh-parse").value,
    media_type: $("#sh-media").value,
    media_file_id: $("#sh-fileid").value,
    media_path: await maybeUpload("sh"),
    button_type: $("#sh-btype").value,
    button_text: $("#sh-btext").value,
    button_url: $("#sh-burl").value,
    button_color: $("#sh-bcolor").value,
    delay_seconds: Number($("#sh-delay").value || 10),
    sort_order: Number($("#sh-sort").value || 0),
    is_active: $("#sh-active").checked,
  };
  if (id) await api("/api/shows/" + id, { method: "PUT", body: JSON.stringify(payload) });
  else await api("/api/shows", { method: "POST", body: JSON.stringify(payload) });
  render();
};
window.delShow = async function (id) {
  if (!confirm("Удалить показ?")) return;
  await api("/api/shows/" + id, { method: "DELETE" });
  render();
};
window.editShow = function (id) {
  const s = cache.shows[id];
  if (!s) return;
  $("#sh-form").classList.remove("hidden");
  $("#sh-title").value = s.title;
  $("#sh-text").value = s.text;
  $("#sh-parse").value = s.parse_mode;
  $("#sh-media").value = s.media_type;
  $("#sh-fileid").value = s.media_file_id;
  $("#sh-mediapath").value = s.media_path;
  $("#sh-btype").value = s.button_type;
  $("#sh-btext").value = s.button_text;
  $("#sh-burl").value = s.button_url;
  $("#sh-delay").value = s.delay_seconds;
  $("#sh-sort").value = s.sort_order;
  $("#sh-active").checked = s.is_active;
  $(".btn", $("#sh-form")).onclick = () => saveShow(s.id);
};

async function renderOffers(root) {
  const items = await api("/api/offers");
  cache.offers = Object.fromEntries(items.map((o) => [o.id, o]));
  const kinds = { my_op: "Моё ОП", service_op: "Сервисы ОП", greeting: "Приветы / показы" };
  root.innerHTML = `
    <div class="top"><h2>Офферы и задания</h2><button class="btn" onclick="toggle('#of-form')">+ Добавить</button></div>
    <p class="muted">Каждые N тапов — задание с проверкой подписки. Показы после старта — отдельно, без проверки.</p>
    <div id="of-form" class="panel hidden">
      ${editorFields("of")}
      <div class="grid2">
        <div class="field"><label>Тип</label>
          <select id="of-kind">
            <option value="my_op">Задание (ОП)</option>
          </select>
        </div>
        <div class="field"><label>Проверка</label>
          <select id="of-check">
            <option value="subscribe" selected>Подписка на канал</option>
            <option value="bot">Старт бота / членство</option>
          </select>
        </div>
      </div>
      <div class="grid2">
        <div class="field"><label>chat_id / @username</label><input id="of-chat" placeholder="@mychannel или -100..."></div>
        <div class="field"><label>Сервис (позже)</label><input id="of-service" placeholder="subgram / flyer / ..."></div>
      </div>
      <div class="grid2">
        <div class="field"><label>Текст кнопки</label><input id="of-btext"></div>
        <div class="field"><label>URL</label><input id="of-burl"></div>
      </div>
      <div class="grid2">
        <div class="field"><label>Порядок</label><input id="of-sort" type="number" value="0"></div>
        <div class="field"><label>Активен</label><input id="of-active" type="checkbox" checked></div>
      </div>
      <button class="btn" onclick="saveOffer()">Сохранить</button>
    </div>
    <div class="panel">
      <table>
        <tr><th>ID</th><th>Тип</th><th>Название</th><th>Проверка</th><th></th></tr>
        ${items.map((o) => `<tr>
          <td>${o.id}</td>
          <td>${kinds[o.kind] || o.kind}</td>
          <td>${esc(o.title)} ${o.is_active ? "<span class='ok'>●</span>" : ""}</td>
          <td>${esc(o.check_type)} ${esc(o.chat_id)}</td>
          <td>
            <button class="btn ghost small" onclick="editOffer(${o.id})">Изменить</button>
            <button class="btn red small" onclick="delOffer(${o.id})">Удалить</button>
          </td>
        </tr>`).join("")}
      </table>
    </div>`;
}

window.saveOffer = async function (id) {
  const payload = {
    title: $("#of-title").value,
    text: $("#of-text").value,
    parse_mode: $("#of-parse").value,
    media_type: $("#of-media").value,
    media_file_id: $("#of-fileid").value,
    media_path: await maybeUpload("of"),
    kind: $("#of-kind").value,
    check_type: $("#of-check").value,
    chat_id: $("#of-chat").value,
    service_name: $("#of-service").value,
    button_text: $("#of-btext").value,
    button_url: $("#of-burl").value,
    sort_order: Number($("#of-sort").value || 0),
    is_active: $("#of-active").checked,
  };
  if (id) await api("/api/offers/" + id, { method: "PUT", body: JSON.stringify(payload) });
  else await api("/api/offers", { method: "POST", body: JSON.stringify(payload) });
  render();
};
window.delOffer = async function (id) {
  if (!confirm("Удалить оффер?")) return;
  await api("/api/offers/" + id, { method: "DELETE" });
  render();
};
window.editOffer = function (id) {
  const o = cache.offers[id];
  if (!o) return;
  $("#of-form").classList.remove("hidden");
  $("#of-title").value = o.title;
  $("#of-text").value = o.text;
  $("#of-parse").value = o.parse_mode;
  $("#of-media").value = o.media_type;
  $("#of-fileid").value = o.media_file_id;
  $("#of-mediapath").value = o.media_path;
  $("#of-kind").value = o.kind;
  $("#of-check").value = o.check_type;
  $("#of-chat").value = o.chat_id;
  $("#of-service").value = o.service_name;
  $("#of-btext").value = o.button_text;
  $("#of-burl").value = o.button_url;
  $("#of-sort").value = o.sort_order;
  $("#of-active").checked = o.is_active;
  $(".btn", $("#of-form")).onclick = () => saveOffer(o.id);
};

async function renderCampaigns(root) {
  const items = await api("/api/campaigns");
  root.innerHTML = `
    <div class="top"><h2>Рекламные реф-ссылки</h2></div>
    <p class="muted">Переход — каждый /start, даже повторный одним человеком. Уники — те, кого раньше не было в боте. Финансы считаются от цены закупа.</p>
    <div class="panel">
      <div class="grid2">
        <div class="field"><label>Название</label><input id="c-name" placeholder="YouTube / Telegram Ads"></div>
        <div class="field"><label>Код в ссылке</label><input id="c-code" placeholder="yt1"></div>
        <div class="field"><label>Цена закупа</label><input id="c-price" type="number" step="0.01" value="0"></div>
        <div class="field"><label>Комментарий</label><input id="c-comment"></div>
      </div>
      <button class="btn" onclick="saveCamp()">Создать ссылку</button>
    </div>
    ${items.map((c) => `
      <div class="panel">
        <div class="top"><h3>${esc(c.name)} <span class="muted">c_${esc(c.code)}</span></h3>
          <button class="btn red small" onclick="delCamp(${c.id})">Удалить</button></div>
        <div class="link">${esc(c.link)}</div>
        <div class="cards" style="margin-top:12px">
          ${card("Переходы", c.transitions)}
          ${card("Уники", c.uniques, c.conv_unique + "% от переходов")}
          ${card("Премиум", c.premiums, c.conv_premium + "% от уников")}
          ${card("Оплатили", c.payers, c.conv_pay + "% от уников")}
          ${card("Цена", money(c.price))}
          ${card("CPT", c.cpt.toFixed(2), "цена / переход")}
          ${card("CPU", c.cpu.toFixed(2), "цена / уник")}
          ${card("CPP", c.cpp.toFixed(2), "цена / прем")}
          ${card("Входящие ⭐", c.paid_stars)}
          ${card("Начислено ⭐", c.earned_stars.toFixed(2))}
        </div>
        <div class="field" style="margin-top:10px"><label>Обновить цену</label>
          <input id="price-${c.id}" type="number" step="0.01" value="${c.price}" style="max-width:160px;display:inline-block">
          <button class="btn ghost small" onclick="updCampPrice(${c.id})">Сохранить</button>
        </div>
      </div>`).join("") || "<p class='muted'>Пока нет ссылок</p>"}`;
}

window.saveCamp = async function () {
  await api("/api/campaigns", {
    method: "POST",
    body: JSON.stringify({
      name: $("#c-name").value,
      code: $("#c-code").value,
      price: $("#c-price").value,
      comment: $("#c-comment").value,
    }),
  });
  render();
};
window.delCamp = async function (id) {
  if (!confirm("Удалить ссылку и её статистику?")) return;
  await api("/api/campaigns/" + id, { method: "DELETE" });
  render();
};
window.updCampPrice = async function (id) {
  await api("/api/campaigns/" + id, {
    method: "PUT",
    body: JSON.stringify({ price: $("#price-" + id).value }),
  });
  render();
};

async function renderBroadcast(root) {
  const items = await api("/api/broadcasts");
  root.innerHTML = `
    <div class="top"><h2>Рассылка</h2></div>
    <div class="panel">
      ${editorFields("br")}
      <div class="grid2">
        <div class="field"><label>Кнопка</label><input id="br-btext"></div>
        <div class="field"><label>URL</label><input id="br-burl"></div>
      </div>
      <button class="btn gold" onclick="sendBr()">Запустить рассылку</button>
    </div>
    <div class="panel">
      <table>
        <tr><th>ID</th><th>Статус</th><th>Отправлено</th><th>Ошибки</th><th>Всего</th></tr>
        ${items.map((b) => `<tr>
          <td>${b.id}</td><td>${esc(b.status)}</td><td>${b.sent}</td><td>${b.failed}</td><td>${b.total}</td>
        </tr>`).join("")}
      </table>
    </div>`;
}

window.sendBr = async function () {
  if (!confirm("Отправить всем пользователям?")) return;
  await api("/api/broadcast", {
    method: "POST",
    body: JSON.stringify({
      text: $("#br-text").value,
      parse_mode: $("#br-parse").value,
      media_type: $("#br-media").value,
      media_file_id: $("#br-fileid").value,
      media_path: await maybeUpload("br"),
      button_text: $("#br-btext").value,
      button_url: $("#br-burl").value,
    }),
  });
  render();
};

async function renderWithdrawals(root) {
  const items = await api("/api/withdrawals");
  root.innerHTML = `
    <div class="top"><h2>Выводы</h2></div>
    <div class="panel">
      <table>
        <tr><th>ID</th><th>Пользователь</th><th>Сумма</th><th>Статус</th><th></th></tr>
        ${items.map((w) => `<tr>
          <td>${w.id}</td>
          <td>${w.user ? `${w.user.tg_id} @${esc(w.user.username || "—")}` : "—"}</td>
          <td>${w.amount} ⭐</td>
          <td>${esc(w.status)}</td>
          <td>${w.status === "pending_review" ? `
            <button class="btn green small" onclick="wd(${w.id},'approve')">Ок</button>
            <button class="btn red small" onclick="wd(${w.id},'reject')">Отклонить</button>` : ""}
          </td>
        </tr>`).join("")}
      </table>
    </div>`;
}

window.wd = async function (id, act) {
  const body = act === "reject" ? { comment: prompt("Комментарий") || "" } : {};
  await api(`/api/withdrawals/${id}/${act}`, { method: "POST", body: JSON.stringify(body) });
  render();
};

async function renderUsers(root) {
  const q = window._uq || "";
  const items = await api("/api/users?q=" + encodeURIComponent(q));
  root.innerHTML = `
    <div class="top"><h2>Пользователи</h2>
      <input id="uq" placeholder="ID или username" value="${esc(q)}" style="max-width:240px">
      <button class="btn" onclick="window._uq=$('#uq').value;render()">Найти</button>
    </div>
    <div class="panel">
      <table>
        <tr><th>TG</th><th>Имя</th><th>⭐</th><th>Клики</th><th>Прем</th><th></th></tr>
        ${items.map((u) => `<tr>
          <td>${u.tg_id}<br><span class="muted">@${esc(u.username || "—")}</span></td>
          <td>${esc(u.first_name)}</td>
          <td>${u.balance.toFixed(2)}</td>
          <td>${u.clicks_total}</td>
          <td>${u.is_premium ? "⭐" : ""}</td>
          <td>
            <button class="btn ghost small" onclick="blockUser(${u.id})">${u.blocked ? "Разблок" : "Бан"}</button>
            <button class="btn ghost small" onclick="starsUser(${u.id}, 1)">+⭐</button>
            <button class="btn ghost small" onclick="starsUser(${u.id}, -1)">-⭐</button>
          </td>
        </tr>`).join("")}
      </table>
    </div>`;
}

window.blockUser = async function (id) {
  await api("/api/users/" + id + "/block", { method: "POST" });
  render();
};

window.starsUser = async function (id, sign) {
  const raw = prompt(sign > 0 ? "Сколько начислить?" : "Сколько списать?");
  if (!raw) return;
  const amount = Number(String(raw).replace(",", ".")) * sign;
  if (!amount) return;
  await api("/api/users/" + id + "/stars", { method: "POST", body: JSON.stringify({ amount }) });
  render();
};

async function renderSettings(root) {
  const s = await api("/api/settings");
  const fields = [
    ["click_reward", "Награда за клик (⭐)", "0.25"],
    ["click_cooldown", "Кулдаун в секундах (0 = нет)", "0"],
    ["click_daily_limit", "Лимит кликов в день (0 = нет)", "0"],
    ["task_every_n", "Задание каждые N кликов", "5"],
    ["withdraw_min", "Минимум вывода", "100"],
    ["withdraw_friends", "Сколько друзей для вывода", "3"],
    ["withdraw_check_stars", "Инвойс-проверка, ⭐", "3"],
    ["ref_bonus", "Бонус за друга (⭐)", "5"],
    ["ref_threshold", "Порог заработка друга", "10"],
    ["ref_percent", "% с заработка друга", "5"],
    ["star_fiat_rate", "Курс ⭐ → фиат (для финансов)", "1.8"],
    ["fiat_currency", "Валюта закупа", "RUB"],
    ["welcome_text", "Текст меню после рекламы", ""],
    ["subgram_enabled", "SubGram вкл (1/0)", "1"],
    ["tgrass_enabled", "Tgrass вкл (1/0)", "1"],
    ["botohub_enabled", "BotoHub вкл (1/0)", "1"],
    ["subgram_key", "SubGram ключ (пусто = из .env)", ""],
    ["tgrass_key", "Tgrass ключ (пусто = из .env)", ""],
    ["botohub_key", "BotoHub ключ (пусто = из .env)", ""],
  ];
  root.innerHTML = `
    <div class="top"><h2>Настройки</h2></div>
    <div class="panel">
      ${fields.map(([k, label]) => `<div class="field"><label>${label}</label><input id="s-${k}" value="${esc(s[k] || "")}"></div>`).join("")}
      <button class="btn" onclick="saveSettings()">Сохранить</button>
    </div>`;
}

window.saveSettings = async function () {
  const keys = [
    "click_reward","click_cooldown","click_daily_limit","task_every_n","withdraw_min",
    "withdraw_friends","withdraw_check_stars","ref_bonus","ref_threshold","ref_percent",
    "star_fiat_rate","fiat_currency","welcome_text",
    "subgram_enabled","tgrass_enabled","botohub_enabled",
    "subgram_key","tgrass_key","botohub_key"
  ];
  const body = {};
  keys.forEach((k) => (body[k] = $("#s-" + k).value));
  await api("/api/settings", { method: "PUT", body: JSON.stringify(body) });
  alert("Сохранено");
};

let ubPeriod = "day";
let ubAccountId = "";

async function renderUserbots(root) {
  const data = await api("/api/userbots");
  const g = data.globals || {};
  const items = data.items || [];
  cache.userbots = Object.fromEntries(items.map((a) => [a.id, a]));

  let statsUrl = "/api/userbots/stats?period=" + ubPeriod;
  if (ubAccountId) statsUrl += "&account_id=" + ubAccountId;
  const st = await api(statsUrl);
  let chartSrc = "";
  try {
    const chartPath =
      "/api/userbots/stats/chart?period=" +
      ubPeriod +
      (ubAccountId ? "&account_id=" + ubAccountId : "");
    const headers = {};
    if (initData()) headers["X-Telegram-Init-Data"] = initData();
    const cres = await fetch(chartPath, { headers });
    if (cres.ok) {
      const blob = await cres.blob();
      chartSrc = URL.createObjectURL(blob);
    }
  } catch {}

  const periodBtns = [
    ["hour", "Час"],
    ["day", "Сутки"],
    ["week", "Неделя"],
    ["month", "Месяц"],
    ["all", "Всё время"],
  ]
    .map(
      ([p, label]) =>
        `<button class="pill ${p === ubPeriod ? "active" : ""}" data-ubp="${p}">${label}</button>`
    )
    .join("");

  root.innerHTML = `
    <div class="top"><h2>Юзер-боты</h2><button class="btn" onclick="toggle('#ub-add')">+ Аккаунт</button></div>
    <div class="panel">
      <h3>Глобальные</h3>
      <p class="muted">API: ${data.api_configured ? "заданы (" + esc(g.api_hash_masked || "") + ")" : "не заданы"} · SLA ${esc(g.max_reply_sec)}с · пауза ${esc(g.min_send_gap)}с</p>
      <div class="grid2">
        <div class="field"><label>API_ID</label><input id="ub-api-id" value="${esc(g.api_id || "")}"></div>
        <div class="field"><label>API_HASH</label><input id="ub-api-hash" placeholder="оставить пустым = не менять"></div>
        <div class="field"><label>Макс. ответ, сек (≤60)</label><input id="ub-sla" value="${esc(g.max_reply_sec)}"></div>
        <div class="field"><label>Мин. пауза, сек</label><input id="ub-gap" value="${esc(g.min_send_gap)}"></div>
      </div>
      <button class="btn" onclick="saveUbGlobals()">Сохранить глобальные</button>
    </div>

    <div class="panel">
      <h3>Статистика воронки</h3>
      <div class="pills" id="ub-periods">${periodBtns}</div>
      <div class="field" style="margin-top:10px">
        <label>Аккаунт</label>
        <select id="ub-stat-acc">
          <option value="">Все</option>
          ${items.map((a) => `<option value="${a.id}" ${String(a.id) === String(ubAccountId) ? "selected" : ""}>#${a.id} ${esc(a.username || a.phone)}</option>`).join("")}
        </select>
      </div>
      <div class="cards">
        ${card("Написали", st.total)}
        ${card("Заблокировали", st.blocked, (st.pct?.blocked ?? 0) + "%")}
        ${card("Ответ на 1-е", st.hello_replied, (st.pct?.hello_replied ?? 0) + "%")}
        ${card("Ответ на 2-е", st.gift_replied, (st.pct?.gift_replied ?? 0) + "%")}
        ${card("Подарок ушёл", st.gift_sent, (st.pct?.gift_sent ?? 0) + "%")}
        ${card("Напомин. 30м", st.nudge_30, (st.pct?.nudge_30 ?? 0) + "%")}
        ${card("Напомин. 6ч", st.nudge_6h, (st.pct?.nudge_6h ?? 0) + "%")}
        ${card("Напомин. 24ч", st.nudge_24h, (st.pct?.nudge_24h ?? 0) + "%")}
        ${card("Завершили", st.done, (st.pct?.done ?? 0) + "%")}
        ${card("Молчат", st.silent_after_hello, (st.pct?.silent ?? 0) + "%")}
        ${card("Ответ/подарок", (st.pct?.gift_reply_of_gifted ?? 0) + "%")}
        ${card("Онлайн", (st.online || 0) + " / " + (st.accounts || 0))}
      </div>
      <div style="margin-top:14px;overflow:auto">
        ${
          chartSrc
            ? `<img src="${chartSrc}" alt="графики" style="max-width:100%;border-radius:12px;border:1px solid var(--line);background:#0f1720"/>`
            : `<p class="muted">График пока недоступен (нужен matplotlib / данные).</p>`
        }
      </div>
    </div>

    <div id="ub-add" class="panel hidden">
      <div class="field"><label>Телефон (+7…)</label><input id="ub-phone"></div>
      <div class="field"><label>Привет</label><textarea id="ub-hello-new">привеееет, ты хочешь подарочек?</textarea></div>
      <div class="field"><label>Подарок (HTML)</label><textarea id="ub-gift-new"></textarea></div>
      <button class="btn" onclick="createUb()">Создать</button>
    </div>

    ${items
      .map((a) => {
        const mark = a.online ? "🟢" : "⚪";
        return `<div class="panel">
          <div class="top">
            <h3>${mark} #${a.id} ${esc(a.username || a.phone)} · ${esc(a.status)} · лидов ${a.leads || 0}</h3>
            <div>
              <button class="btn small ${a.online ? "red" : "green"}" onclick="ubToggle(${a.id},${a.online})">${a.online ? "Стоп" : "Старт"}</button>
              <button class="btn small ghost" onclick="ubLogin(${a.id})">Код</button>
              <button class="btn small red" onclick="ubDelete(${a.id})">Удалить</button>
            </div>
          </div>
          ${a.last_error ? `<p class="err">${esc(a.last_error)}</p>` : ""}
          <div class="field"><label>Привет</label><textarea id="ub-h-${a.id}">${esc(a.hello_text || "")}</textarea></div>
          <div class="field"><label>Подарок</label><textarea id="ub-g-${a.id}">${esc(a.gift_text || "")}</textarea></div>
          <div class="grid2">
            <div class="field"><label>Дожим 30м</label><textarea id="ub-n30-${a.id}">${esc(a.nudge_30_text || "")}</textarea></div>
            <div class="field"><label>Дожим 6ч</label><textarea id="ub-n6-${a.id}">${esc(a.nudge_6h_text || "")}</textarea></div>
          </div>
          <div class="grid2">
            <div class="field"><label>Дожим 24ч</label><textarea id="ub-n24-${a.id}">${esc(a.nudge_24h_text || "")}</textarea></div>
            <div class="field"><label>Лимит приветов/час</label><input id="ub-lim-${a.id}" value="${esc(a.max_greets_hour || 1000)}"></div>
          </div>
          <button class="btn" onclick="saveUb(${a.id})">Сохранить тексты</button>
        </div>`;
      })
      .join("")}
  `;

  $$("#ub-periods .pill", root).forEach((b) =>
    b.addEventListener("click", () => {
      ubPeriod = b.dataset.ubp;
      render();
    })
  );
  const sel = $("#ub-stat-acc", root);
  if (sel) {
    sel.addEventListener("change", () => {
      ubAccountId = sel.value;
      render();
    });
  }
}

window.saveUbGlobals = async function () {
  const body = {
    max_reply_sec: Number($("#ub-sla").value || 60),
    min_send_gap: Number($("#ub-gap").value || 2),
  };
  const apiId = ($("#ub-api-id").value || "").trim();
  const apiHash = ($("#ub-api-hash").value || "").trim();
  if (apiId && apiHash) {
    body.api_id = Number(apiId);
    body.api_hash = apiHash;
  }
  await api("/api/userbots/globals", { method: "PUT", body: JSON.stringify(body) });
  alert("Сохранено");
  render();
};

window.createUb = async function () {
  await api("/api/userbots", {
    method: "POST",
    body: JSON.stringify({
      phone: $("#ub-phone").value,
      hello_text: $("#ub-hello-new").value,
      gift_text: $("#ub-gift-new").value,
    }),
  });
  alert("Создан. Дальше — кнопка «Код».");
  render();
};

window.saveUb = async function (id) {
  await api("/api/userbots/" + id, {
    method: "PUT",
    body: JSON.stringify({
      hello_text: $("#ub-h-" + id).value,
      gift_text: $("#ub-g-" + id).value,
      nudge_30_text: $("#ub-n30-" + id).value,
      nudge_6h_text: $("#ub-n6-" + id).value,
      nudge_24h_text: $("#ub-n24-" + id).value,
      max_greets_hour: Number($("#ub-lim-" + id).value || 1000),
      gift_parse_mode: "HTML",
    }),
  });
  alert("Сохранено");
};

window.ubToggle = async function (id, online) {
  await api("/api/userbots/" + id + (online ? "/stop" : "/start"), { method: "POST" });
  render();
};

window.ubDelete = async function (id) {
  if (!confirm("Удалить аккаунт #" + id + "?")) return;
  await api("/api/userbots/" + id, { method: "DELETE" });
  render();
};

window.ubLogin = async function (id) {
  try {
    await api("/api/userbots/" + id + "/send-code", { method: "POST" });
  } catch (e) {
    alert(e.message);
    return;
  }
  const code = prompt("Код из Telegram / SMS");
  if (!code) return;
  try {
    const r = await api("/api/userbots/" + id + "/confirm-code", {
      method: "POST",
      body: JSON.stringify({ code }),
    });
    if (r.status === "wait_2fa") {
      const password = prompt("Облачный пароль 2FA");
      if (!password) return;
      await api("/api/userbots/" + id + "/confirm-2fa", {
        method: "POST",
        body: JSON.stringify({ password }),
      });
    }
    alert("Готово");
    render();
  } catch (e) {
    alert(e.message);
  }
};

(async function init() {
  try {
    if (isWebApp()) {
      await api("/api/webapp-auth", { method: "POST", body: JSON.stringify({ init_data: initData() }) });
      showApp();
      return;
    }
    const me = await api("/api/me");
    if (me.admin) showApp();
    else showLogin();
  } catch {
    if (isWebApp()) showDenied();
    else showLogin();
  }
})();
