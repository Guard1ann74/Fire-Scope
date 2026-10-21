/* Мониторинг огня: районы наблюдения, оповещения о новых очагах NASA FIRMS.
 * Сервер сам опрашивает FIRMS (configs/service.yaml → monitor); страница раз в
 * минуту забирает состояние и показывает новые очаги. Использует общие функции app.js. */

const monitorState = { data: null, unread: 0, layer: null, timer: null, available: true };

function monitorTime(iso) {
  if (!iso) return "—";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

async function loadMonitor({ notify = false } = {}) {
  let data;
  try {
    data = await api("/api/monitor");
  } catch (error) {
    if (error.status === 404) {
      monitorState.available = false;
      $("monitorToggle").hidden = true;
      return;
    }
    $("monitorStatus").textContent = `Мониторинг недоступен: ${error.message}`;
    return;
  }
  const previous = monitorState.data;
  monitorState.data = data;
  if (notify && previous) {
    const known = new Map(previous.alerts.map((alert) => [alert.id, alert.updated_at]));
    const fresh = data.alerts.filter((alert) => !alert.read && known.get(alert.id) !== alert.updated_at);
    if (fresh.length) {
      const alert = fresh[0];
      toast(`Новый огонь · ${alert.zone_name}: ${alert.n_points} ` +
        `${plural(alert.n_points, "термоточка", "термоточки", "термоточек")}, ${monitorTime(alert.last_seen)}`);
    }
  }
  renderMonitor();
}

function renderMonitor() {
  const data = monitorState.data;
  if (!data) return;
  const badge = $("monitorBadge");
  badge.hidden = !data.unread;
  badge.textContent = data.unread > 99 ? "99+" : String(data.unread);
  $("monitorStatus").textContent = !data.enabled
    ? "Ключ NASA FIRMS не задан: мониторинг не работает."
    : `Сервер проверяет районы каждые ${data.poll_minutes} мин` +
      (data.running ? "" : " (фоновая проверка выключена — только кнопкой)") +
      (data.last_run ? ` · последняя проверка ${monitorTime(data.last_run)}` : "") +
      ". Точка появляется в FIRMS примерно через 3 ч после пролёта спутника; пролётов около 6 в сутки.";
  $("monitorAddZone").disabled = !state.area || !data.enabled;

  $("monitorZonesEmpty").hidden = data.zones.length > 0;
  $("monitorZones").innerHTML = data.zones.map((zone) => {
    const check = zone.last_check;
    const result = !check
      ? "ещё не проверялся"
      : !check.ok
      ? check.error
      : `${monitorTime(check.at)} · точек за ${data.lookback_hours} ч: ${check.points_total}, ` +
        `принято как огонь: ${check.points_kept}, новых: ${check.new_points}`;
    return `<li><strong>${escapeHtml(zone.name)}</strong>` +
      `<span class="${check && !check.ok ? "error-text" : "muted"}">${escapeHtml(result)}</span>` +
      `<button type="button" class="quiet remove" data-zone-remove="${escapeHtml(zone.id)}" ` +
      `title="Перестать следить" aria-label="Перестать следить">×</button></li>`;
  }).join("");

  $("monitorReadAll").hidden = !data.unread;
  const shown = data.alerts.slice(0, 40);
  $("monitorAlertsEmpty").hidden = data.alerts.length <= shown.length && data.alerts.length > 0;
  $("monitorAlertsEmpty").textContent = data.alerts.length
    ? `Показаны 40 последних из ${data.alerts.length}. Для крупного района в сезон палов очагов много — ` +
      "следите за меньшими районами (лесничество, заказник)."
    : "Новых очагов нет.";
  $("monitorAlerts").innerHTML = shown.map((alert) => {
    const notes = [];
    if (alert.night_only && (alert.max_frp_mw ?? 0) < 5) notes.push("только ночью и слабо — возможно тление");
    const title = alert.updates ? "Огонь продолжается" : "Новый огонь";
    return `<li class="${alert.read ? "" : "unread"}"><strong>${escapeHtml(title)}</strong>` +
      `<span class="muted">${escapeHtml(alert.zone_name)}</span><br>` +
      `<span>${alert.n_points} ${plural(alert.n_points, "термоточка", "термоточки", "термоточек")} · ` +
      `${escapeHtml(monitorTime(alert.first_seen))}` +
      (alert.last_seen !== alert.first_seen ? ` — ${escapeHtml(monitorTime(alert.last_seen))}` : "") +
      ` · FRP до ${number(alert.max_frp_mw, 1)} МВт</span>` +
      (weatherLine(alert.weather) ? `<br><span class="weather" title="${escapeHtml(weatherTitle(alert.weather))}">` +
        `${escapeHtml(weatherLine(alert.weather))}</span>` : "") +
      notes.map((note) => `<br><span class="muted">${escapeHtml(note)}</span>`).join("") +
      `<div class="row-actions">` +
      `<button type="button" data-alert-show="${escapeHtml(alert.id)}">На карте</button>` +
      `<button type="button" class="quiet" data-alert-open="${escapeHtml(alert.id)}">Оценить гарь</button>` +
      `</div></li>`;
  }).join("");

  document.querySelectorAll("[data-zone-remove]").forEach((button) => {
    button.onclick = () => removeMonitorZone(button.dataset.zoneRemove);
  });
  document.querySelectorAll("[data-alert-show]").forEach((button) => {
    button.onclick = () => showMonitorAlert(button.dataset.alertShow);
  });
  document.querySelectorAll("[data-alert-open]").forEach((button) => {
    button.onclick = () => openMonitorAlert(button.dataset.alertOpen);
  });
}

function findAlert(id) {
  return monitorState.data?.alerts.find((alert) => alert.id === id);
}

async function markAlertRead(id) {
  const alert = findAlert(id);
  if (!alert || alert.read) return;
  try {
    await api(`/api/monitor/alerts/read?alert_id=${encodeURIComponent(id)}`, { method: "POST" });
  } catch {
    return;
  }
  await loadMonitor();
}

function alertBounds(alert) {
  const lons = alert.points.map((p) => p.lon);
  const lats = alert.points.map((p) => p.lat);
  return [[Math.min(...lats), Math.min(...lons)], [Math.max(...lats), Math.max(...lons)]];
}

/** Точки очага на карте; приближение к ним. */
function showMonitorAlert(id) {
  const alert = findAlert(id);
  if (!alert) return;
  monitorState.layer.clearLayers();
  alert.points.forEach((point) => {
    L.circleMarker([point.lat, point.lon], {
      radius: 7, color: "#3a0a05", weight: 1, fillColor: "#ff3b30", fillOpacity: 0.9,
    }).bindPopup(`<div class="popup"><h4>Огонь · мониторинг</h4><dl>` +
      `<dt>Время (UTC)</dt><dd>${escapeHtml(point.date)} ${escapeHtml(point.time_utc || "")}</dd>` +
      `<dt>Спутник</dt><dd>${escapeHtml(point.platform)}</dd>` +
      `<dt>Достоверность</dt><dd>${escapeHtml(confidenceLabel(point.confidence))}</dd>` +
      `<dt>FRP</dt><dd>${number(point.frp_mw, 1)} МВт</dd></dl></div>`)
      .addTo(monitorState.layer);
  });
  state.map.fitBounds(alertBounds(alert), { padding: [80, 80], maxZoom: 13 });
  markAlertRead(id);
}

/** Область вокруг очага и период для оценки гари обычным запросом. */
function openMonitorAlert(id) {
  const alert = findAlert(id);
  if (!alert) return;
  const [[south, west], [north, east]] = alertBounds(alert);
  const pad = 0.04;  // ~3–4 км вокруг точек: там сервис ищет гарь
  const bbox = [west - pad, south - pad, east + pad, north + pad].map((value) => Number(value.toFixed(4)));
  const yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
  const first = alert.first_seen.slice(0, 10);
  const last = alert.last_seen.slice(0, 10);
  const to = last < yesterday ? last : yesterday;
  cancelDrawing();
  setArea({ bbox }, `очаг из мониторинга (${alert.zone_name})`);
  $("dateFrom").value = first < to ? first : to;
  $("dateTo").value = to;
  window.updatePeriod?.();
  state.map.fitBounds([[bbox[1], bbox[0]], [bbox[3], bbox[2]]], { padding: [48, 48], maxZoom: 13 });
  markAlertRead(id);
  if (last >= yesterday) {
    toast("Очаг свежий: гарь видна только на ясном снимке Sentinel-2 после пожара — обычно через 2–5 дней. " +
      "Область и даты заданы, нажмите «Показать пожары» позже.");
    return;
  }
  runQuery();
}

async function addMonitorZone() {
  if (!state.area) return;
  const name = window.prompt("Название района наблюдения", "Мой район");
  if (name === null) return;
  const body = state.area.bbox ? { bbox: state.area.bbox, name } : { geometry: state.area.geometry, name };
  $("monitorAddZone").disabled = true;
  $("monitorStatus").textContent = "Добавляем район и проверяем FIRMS…";
  try {
    const result = await api("/api/monitor/zones", { method: "POST", body: JSON.stringify(body) });
    const found = result.alerts?.length || 0;
    toast(found ? `Район добавлен: за последние сутки ${found} ${plural(found, "очаг", "очага", "очагов")}`
      : "Район добавлен: за последние сутки огня нет");
  } catch (error) {
    toast(`Не удалось добавить район: ${error.message}`);
  }
  await loadMonitor();
}

async function removeMonitorZone(id) {
  try {
    await api(`/api/monitor/zones/${encodeURIComponent(id)}`, { method: "DELETE" });
  } catch (error) {
    toast(`Не удалось удалить район: ${error.message}`);
  }
  await loadMonitor();
}

async function checkMonitorNow() {
  $("monitorCheck").disabled = true;
  $("monitorStatus").textContent = "Проверяем FIRMS по всем районам…";
  try {
    const result = await api("/api/monitor/check", { method: "POST" });
    const found = result.alerts?.length || 0;
    toast(found ? `Новых или продолжающихся очагов: ${found}` : "Новых очагов нет");
  } catch (error) {
    toast(`Проверка не удалась: ${error.message}`);
  }
  $("monitorCheck").disabled = false;
  await loadMonitor();
}

window.initMonitor = function initMonitor() {
  monitorState.layer = L.layerGroup().addTo(state.map);
  $("monitorToggle").onclick = () => setRightPanel($("monitorPanel").hidden ? "monitor" : null);
  $("monitorAddZone").onclick = addMonitorZone;
  $("monitorCheck").onclick = checkMonitorNow;
  $("monitorReadAll").onclick = async () => {
    try {
      await api("/api/monitor/alerts/read", { method: "POST" });
    } catch (error) {
      toast(error.message);
    }
    await loadMonitor();
  };
  loadMonitor();
  monitorState.timer = setInterval(() => {
    if (monitorState.available) loadMonitor({ notify: true });
  }, 60000);
};
