/* Интерфейс мониторинга природных пожаров.
   Карта, выделение области, слои, легенда, аналитическая справка и выгрузки. */

const $ = (id) => document.getElementById(id);

const state = {
  map: null,
  basemapLayer: null,
  basemapId: "dark",
  hotspotLayer: null,
  burnLayer: null,
  areaLayer: null,
  areaShape: null,
  areaRotationStem: null,
  areaHandles: [],
  areaPoints: [],
  areaKind: null,
  areaMove: null,
  areaRotate: null,
  /** Выбранная область как GeoJSON-геометрия либо {bbox: [...]}. */
  area: null,
  /** Последний успешный ответ /api/query. */
  result: null,
  llmEnabled: false,
  severity: [],
  limits: { max_vertices: 2000 },
  dataRange: null,
  dataUpdatedAt: null,
  sourceKind: null,
  suggestedPeriod: null,
  // Учётная запись и наблюдаемые области.
  user: null,
  areas: [],
  sessionAreas: [],
  regions: [],
  places: [],
  placesFor: "",
  placeLoading: false,
  placeError: "",
  searchVersion: 0,
  searchMatches: [],
  searchIndex: -1,
  placeMarker: null,
  activeAreaId: null,
  liveAvailable: false,
  liveLayer: null,
  authMode: "login",
  // Состояние рисования.
  tool: null,
  drawing: false,
  vertices: [],
  draftLine: null,
  draftMarkers: [],
};

const mapTimeline = { month: null, months: [] };

const SEVERITY_FALLBACK = [
  { severity: 1, label: "слабая", color: "#f2c14e" },
  { severity: 2, label: "средняя", color: "#e8712c" },
  { severity: 3, label: "сильная", color: "#9e1b1b" },
];

const HOTSPOT_COLOR = "#ff6a2b";
const SESSION_AREAS_KEY = "fires.sessionAreas.v1";
const AREA_STYLE = { color: "#ff6a2b", weight: 2, dashArray: "6 5", fill: false, interactive: false };
const EDIT_STYLE = { ...AREA_STYLE, fill: true, fillOpacity: 0.08, interactive: true, className: "editable-area" };

/* ------------------------------------------------------------- Утилиты */

const escapeHtml = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );

const number = (value, digits = 2) =>
  Number.isFinite(value)
    ? value.toLocaleString("ru-RU", { minimumFractionDigits: digits, maximumFractionDigits: digits })
    : "—";

const integer = (value) => (Number.isFinite(value) ? value.toLocaleString("ru-RU") : "—");

function formatDate(iso) {
  if (!iso) return "—";
  const parsed = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleDateString("ru-RU", { day: "2-digit", month: "long", year: "numeric" });
}

/** Склонение русских существительных по числу. */
function plural(count, one, few, many) {
  const mod100 = Math.abs(count) % 100;
  const mod10 = mod100 % 10;
  if (mod100 >= 11 && mod100 <= 14) return many;
  if (mod10 === 1) return one;
  if (mod10 >= 2 && mod10 <= 4) return few;
  return many;
}

let toastTimer;
function toast(message) {
  $("toastMessage").textContent = message;
  $("toast").classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(closeToast, 6000);
}

function closeToast() {
  clearTimeout(toastTimer);
  $("toast").classList.remove("show");
}

/* Поиск сохранённых областей, регионов набора данных и географических мест. */
let placeSearchTimer;

function searchKey(value) {
  return String(value || "").trim().toLocaleLowerCase("ru-RU").replaceAll("ё", "е");
}

function closeRegionSearch() {
  clearTimeout(placeSearchTimer);
  state.searchVersion += 1;
  state.placeLoading = false;
  $("regionSearchResults").hidden = true;
  $("regionSearchCredit").hidden = true;
  $("regionSearchInput").setAttribute("aria-expanded", "false");
  $("regionSearchInput").removeAttribute("aria-activedescendant");
  state.searchIndex = -1;
}

function renderRegionSearch() {
  const input = $("regionSearchInput");
  const list = $("regionSearchResults");
  if (list.hidden && document.activeElement !== input) return;
  const query = searchKey(input.value);
  const entries = [
    ...state.areas.map((area) => ({ kind: "area", name: area.name, id: area.id,
      detail: "Сохранённая область" })),
    ...state.regions.map((region) => ({ kind: "region", name: region.name, region,
      detail: `${region.count} ${plural(region.count, "объект", "объекта", "объектов")} в данных` })),
    ...(state.placesFor === query ? state.places.map((place) => ({ kind: "place", name: place.name,
      place, detail: place.detail || "Место на карте" })) : []),
  ];
  state.searchMatches = entries.filter((entry) => entry.kind === "place" ||
    searchKey(entry.name).includes(query)).slice(0, 12);
  state.searchIndex = -1;
  input.removeAttribute("aria-activedescendant");
  list.replaceChildren();
  if (!state.searchMatches.length) {
    const empty = document.createElement("p");
    empty.className = "region-search-empty";
    empty.textContent = state.placeLoading ? "Ищем места…" : state.placeError ||
      (query ? "Совпадений нет. Попробуйте другое название." : "Введите название города, страны или области.");
    list.append(empty);
  }
  state.searchMatches.forEach((entry, index) => {
    const option = document.createElement("button");
    option.type = "button";
    option.id = `regionSearchOption${index}`;
    option.className = "region-search-result";
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", "false");
    const title = document.createElement("span");
    title.textContent = entry.name;
    const detail = document.createElement("small");
    detail.textContent = entry.detail;
    option.append(title, detail);
    option.onclick = () => selectRegionSearchResult(entry);
    list.append(option);
  });
  list.hidden = false;
  $("regionSearchCredit").hidden = query.length < 2;
  input.setAttribute("aria-expanded", "true");
}

function queuePlaceSearch({ immediate = false } = {}) {
  clearTimeout(placeSearchTimer);
  const query = searchKey($("regionSearchInput").value);
  const version = ++state.searchVersion;
  state.places = [];
  state.placesFor = "";
  state.placeError = "";
  state.placeLoading = query.length >= 2;
  renderRegionSearch();
  if (query.length < 2) return;
  placeSearchTimer = setTimeout(() => loadPlaces(query, version), immediate ? 0 : 450);
}

async function loadPlaces(query, version) {
  try {
    const response = await api(`/api/places?q=${encodeURIComponent(query)}`);
    if (version !== state.searchVersion) return;
    state.places = response.places || [];
    state.placesFor = query;
  } catch (error) {
    if (version !== state.searchVersion) return;
    state.placeError = error.message;
  } finally {
    if (version === state.searchVersion) {
      state.placeLoading = false;
      renderRegionSearch();
    }
  }
}

function highlightRegionSearchResult(index) {
  state.searchIndex = index;
  $("regionSearchResults").querySelectorAll('[role="option"]').forEach((option, position) => {
    option.setAttribute("aria-selected", String(position === index));
    if (position === index) option.scrollIntoView({ block: "nearest" });
  });
  $("regionSearchInput").setAttribute("aria-activedescendant", `regionSearchOption${index}`);
}

function selectRegionSearchResult(entry) {
  $("regionSearchInput").value = entry.name;
  closeRegionSearch();
  if (entry.kind === "place") {
    const place = entry.place;
    if (state.placeMarker) state.placeMarker.remove();
    state.placeMarker = L.circleMarker([place.lat, place.lon], {
      radius: 7, color: "#fff4e8", weight: 2, fillColor: "#ff6a2b", fillOpacity: 1,
    }).addTo(state.map).bindPopup(escapeHtml(place.name));
    if (place.bbox && place.bbox[2] - place.bbox[0] < 180) {
      const [west, south, east, north] = place.bbox;
      state.map.fitBounds([[south, west], [north, east]], { padding: [64, 64], maxZoom: 12 });
    } else {
      state.map.setView([place.lat, place.lon], place.type === "country" ? 3 : 11);
    }
    state.placeMarker.openPopup();
    return;
  }
  if (entry.kind === "area") {
    const area = state.areas.find((item) => String(item.id) === String(entry.id));
    if (!area) return;
    const geometry = savedAreaGeometry(area);
    if (geometry) {
      const bounds = L.geoJSON(geometry).getBounds();
      if (bounds.isValid()) state.map.fitBounds(bounds, { padding: [48, 48], maxZoom: 12 });
    }
    openArea(entry.id);
    return;
  }
  const [west, south, east, north] = entry.region.bbox;
  const padX = Math.max((east - west) * 0.08, 0.05);
  const padY = Math.max((north - south) * 0.08, 0.05);
  const bbox = [Math.max(-180, west - padX), Math.max(-85, south - padY),
    Math.min(180, east + padX), Math.min(85, north + padY)];
  cancelDrawing();
  setArea({ bbox }, `район наблюдений: ${entry.name}`);
  state.map.fitBounds([[bbox[1], bbox[0]], [bbox[3], bbox[2]]], { padding: [48, 48], maxZoom: 12 });
  setStatus(`Найден регион «${entry.name}». Нажмите «Показать пожары» для анализа.`);
}

/** Реальные случаи из configs/demo_cases.yaml: одна кнопка задаёт область, даты и запускает запрос. */
async function loadDemoCases() {
  let cases = [];
  try {
    cases = (await api("/api/demo-cases")).cases || [];
  } catch (error) {
    cases = [];
  }
  $("demoCases").hidden = !cases.length;
  const list = $("demoCaseList");
  list.replaceChildren();
  cases.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "quiet";
    button.innerHTML = `${escapeHtml(item.name)}<small>${escapeHtml(item.shows || "")}</small>`;
    button.onclick = () => {
      const [west, south, east, north] = item.bbox;
      cancelDrawing();
      setArea({ bbox: item.bbox }, `пример: ${item.name}`);
      $("dateFrom").value = item.from;
      $("dateTo").value = item.to;
      state.map.fitBounds([[south, west], [north, east]], { padding: [48, 48], maxZoom: 12 });
      runQuery();
    };
    list.append(button);
  });
}

async function loadRegions() {
  try {
    state.regions = (await api("/api/regions")).regions || [];
  } catch (error) {
    state.regions = [];
    toast(`Не удалось загрузить регионы: ${error.message}`);
  }
  if (!$("regionSearchResults").hidden) renderRegionSearch();
}

/** Обёртка над fetch: таймаут, разбор JSON-ошибок FastAPI, понятный текст. */
async function api(path, options = {}) {
  const controller = new AbortController();
  // Запрос с несколькими событиями огня может идти несколько минут: снимки качаются по каждому.
  const timer = setTimeout(() => controller.abort(), path === "/api/query" ? 900000 : 60000);
  try {
    const response = await fetch(path, {
      ...options,
      headers: options.body ? { "Content-Type": "application/json" } : undefined,
      signal: controller.signal,
    });
    if (!response.ok) {
      const failure = new Error(await errorText(response));
      failure.status = response.status;
      throw failure;
    }
    return await response.json();
  } catch (error) {
    if (error.name === "AbortError") throw new Error("Запрос выполнялся слишком долго и был прерван");
    if (error instanceof TypeError) throw new Error("Сервис недоступен. Проверьте, что он запущен");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

async function errorText(response) {
  let body;
  try {
    body = await response.json();
  } catch {
    return `Ошибка запроса (${response.status})`;
  }
  const detail = body?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => `${item.loc?.slice(-1)[0] ?? "поле"}: ${item.msg}`).join("; ");
  }
  return `Ошибка запроса (${response.status})`;
}

/* --------------------------------------------------------------- Карта */

const BASEMAPS = {
  dark: { url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png", palette: "dark", maxNativeZoom: 19,
          attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>' },
  standard: { url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png", palette: "standard", maxNativeZoom: 19,
              attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>' },
  satellite: { url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
               palette: "standard", maxNativeZoom: 18,
               attribution: 'Снимки &copy; <a href="https://www.esri.com/">Esri</a>, Maxar, Earthstar Geographics' },
};

function initMap() {
  if (!window.L) {
    $("map").innerHTML = '<div class="map-fallback">Карта не загрузилась: нет доступа к Leaflet. Проверьте интернет.</div>';
    document.querySelectorAll("[data-tool], #useViewButton").forEach((node) => (node.disabled = true));
    return false;
  }
  $("map").replaceChildren();
  state.map = L.map("map", { zoomControl: true, doubleClickZoom: true }).setView([45.2, 39.0], 7);
  // Оставляем только обязательную атрибуцию картографической подложки.
  state.map.attributionControl.setPrefix(false);
  L.control.scale({ position: "bottomleft", imperial: false }).addTo(state.map);

  // Создаётся раньше гарей, чтобы лежать под ними.
  state.unobservedLayer = L.geoJSON(null, {
    style: { className: "unobserved-area", color: "#d3dae2", weight: 1, opacity: 0.7, dashArray: "4 3", fillOpacity: 1 },
    onEachFeature: (feature, layer) => layer.bindTooltip(
      "Облака или тени на снимках: поверхность не видна, гарь здесь не оценивалась", { sticky: true }),
  }).addTo(state.map);
  state.burnLayer = L.geoJSON(null, { style: burnStyle, onEachFeature: bindBurnPopup }).addTo(state.map);
  state.hotspotLayer = L.geoJSON(null, { pointToLayer: hotspotMarker, onEachFeature: bindHotspotPopup }).addTo(state.map);
  state.liveLayer = L.geoJSON(null, { pointToLayer: liveMarker, onEachFeature: bindLivePopup });
  state.areaLayer = L.layerGroup().addTo(state.map);

  setBasemap("dark");
  document.querySelectorAll("[data-basemap]").forEach((button) => {
    button.onclick = () => setBasemap(button.dataset.basemap);
  });

  state.map.on("click", onMapClick);
  state.map.on("mousemove", onMapMove);
  watchMapSize();
  return true;
}

/** Контейнер карты меняет размер не только при resize окна: раскладка
    перестраивается по медиазапросам, а высота в dvh плавает вместе с адресной
    строкой мобильного браузера. Без invalidateSize карта остаётся с прежними
    размерами и плитки съезжают. */
function watchMapSize() {
  if (!window.ResizeObserver) return;
  let frame = 0;
  const observer = new ResizeObserver(() => {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => state.map?.invalidateSize({ animate: false }));
  });
  observer.observe($("map"));
}

function setBasemap(requested) {
  const id = Object.hasOwn(BASEMAPS, requested) ? requested : "dark";
  const config = BASEMAPS[id];
  state.basemapId = id;
  $("basemapStatus").textContent = "";
  if (state.basemapLayer) state.basemapLayer.remove();
  const layer = L.tileLayer(config.url, {
    maxZoom: 19,
    maxNativeZoom: config.maxNativeZoom,
    attribution: config.attribution,
  });
  let warned = false;
  layer.on("tileerror", () => {
    if (state.basemapLayer !== layer || warned) return;
    warned = true;
    $("basemapStatus").textContent = "Часть плиток не загрузилась. Попробуйте другую подложку.";
  });
  layer.addTo(state.map);
  state.basemapLayer = layer;
  state.map.getPane("tilePane").dataset.palette = config.palette;
  document.querySelectorAll("[data-basemap]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.basemap === id));
  });
  for (const preview of areaPreviewMaps.values()) setAreaPreviewBasemap(preview, id);
}

/* ------------------------------------------------- Отрисовка результата */

function severityColor(severity) {
  const found = state.severity.find((item) => item.severity === severity);
  return found ? found.color : "#c2410c";
}

function severityLabel(severity) {
  const found = state.severity.find((item) => item.severity === severity);
  return found ? found.label : "неизвестна";
}

function burnStyle(feature) {
  const color = feature.properties.color || severityColor(feature.properties.severity);
  // Ненадёжная оценка рисуется бледно и пунктиром: её нельзя путать с проверенной гарью.
  if (feature.properties.reliable === false) {
    return { color, weight: 1.2, opacity: 0.8, dashArray: "5 4", fillColor: color, fillOpacity: 0.14 };
  }
  return { color, weight: 1.6, opacity: 0.95, fillColor: color, fillOpacity: 0.42 };
}

function hotspotMarker(feature, latlng) {
  return L.circleMarker(latlng, {
    radius: 5,
    color: "#2a1008",
    weight: 1,
    fillColor: HOTSPOT_COLOR,
    fillOpacity: 0.95,
  });
}

function bindBurnPopup(feature, layer) {
  const p = feature.properties;
  const color = p.color || severityColor(p.severity);
  const rows = [
    ["Идентификатор", escapeHtml(p.id)],
    ["Степень поражения", `${p.severity} — ${escapeHtml(p.severity_label || severityLabel(p.severity))}`],
    ["Площадь", `${number(p.area_ha)} га`],
    [p.fire_first_detected ? "Начало пожара" : "Дата съёмки", formatDate(p.date)],
    ["Источник", escapeHtml(p.source || "—")],
  ];
  if (p.pre_image_date) rows.push(["Снимки до → после", `${formatDate(p.pre_image_date)} → ${formatDate(p.post_image_date)}`]);
  if (p.reliable === false) rows.push(["Надёжность", "низкая — см. оговорки события"]);
  if (p.fire_evidence) {
    const evidence = p.fire_evidence;
    rows.push(["Подтверждение огнём",
      `${evidence.n_points} ${plural(evidence.n_points, "термоточка", "термоточки", "термоточек")} FIRMS, ` +
      `${formatDate(evidence.first_detected)} — ${formatDate(evidence.last_detected)}`]);
  }
  if (p.region) rows.push(["Регион", escapeHtml(p.region)]);
  const note = p.clipped ? '<p class="note">Контур обрезан границей области — площадь дана по видимой части.</p>' : "";
  layer.bindPopup(
    `<div class="popup"><h4><i style="background:${color}"></i>Контур гари</h4>` +
      `<dl>${rows.map(([key, value]) => `<dt>${key}</dt><dd>${value}</dd>`).join("")}</dl>${note}</div>`,
  );
}

/** Достоверность VIIRS (FIRMS: l/n/h или low/nominal/high) по-русски. */
const CONFIDENCE_LABELS = { l: "низкая", low: "низкая", n: "средняя", nominal: "средняя", h: "высокая", high: "высокая" };

function confidenceLabel(value) {
  if (value === null || value === undefined || value === "") return "—";
  return CONFIDENCE_LABELS[String(value).toLowerCase()] || String(value);
}

function bindHotspotPopup(feature, layer) {
  const p = feature.properties;
  const rows = [
    ["Идентификатор", escapeHtml(p.id)],
    ["Дата", formatDate(p.date)],
    ["Спутник", escapeHtml(p.satellite || "не указан")],
    ["Прибор", escapeHtml(p.instrument || "—")],
  ];
  if (p.confidence) rows.push(["Достоверность", escapeHtml(confidenceLabel(p.confidence))]);
  if (Number.isFinite(p.frp_mw)) rows.push(["Мощность (FRP)", `${number(p.frp_mw, 1)} МВт`]);
  if (p.region) rows.push(["Регион", escapeHtml(p.region)]);
  layer.bindPopup(
    `<div class="popup"><h4><i style="background:${HOTSPOT_COLOR}"></i>Термоточка</h4>` +
      `<dl>${rows.map(([key, value]) => `<dt>${key}</dt><dd>${value}</dd>`).join("")}</dl></div>`,
  );
}

function renderResult(result) {
  state.result = result;
  // Файлы могли смениться после загрузки страницы — обновляем бейдж состава данных.
  if (state.sourceKind !== "satellite" && result.data_updated_at && result.data_updated_at !== state.dataUpdatedAt) {
    state.dataUpdatedAt = result.data_updated_at;
    loadHealth();
    loadRegions();
  }
  renderUnobserved(result.prediction);
  renderFireEvents(result.prediction);
  renderMapTimeline();
  renderMapFeatures();
  renderLive(result.live);
  if (!result.summary.empty) {
    const bounds = L.latLngBounds([]);
    [state.burnLayer, state.hotspotLayer, state.liveLayer].forEach((layer) => {
      const layerBounds = layer.getBounds();
      if (layerBounds.isValid()) bounds.extend(layerBounds);
    });
    if (bounds.isValid()) state.map.fitBounds(bounds, { padding: [48, 48], maxZoom: 12 });
  }
}

const EVENT_STATUS = {
  ok: { label: "гарь оценена", color: "#2fbf71" },
  ok_unreliable: { label: "гарь оценена · ненадёжно", color: "#e8a33c" },
  ok_confirm: { label: "гарь оценена · требует подтверждения", color: "#e8a33c" },
  insufficient_data: { label: "недостаточно данных", color: "#e8a33c" },
  source_unavailable: { label: "источник недоступен", color: "#e5534b" },
  not_processed: { label: "не обработано", color: "#8a8f98" },
  no_scar: { label: "огонь есть, гари на снимках нет", color: "#6fa8dc" },
};

function shortDate(value) {
  return new Date(`${value}T00:00:00`).toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
}

function shortPeriod(from, to) {
  return from === to ? shortDate(from) : `${shortDate(from)} — ${shortDate(to)}`;
}

const DANGER_CLASSES = ["", "I", "II", "III", "IV", "V"];

/** Погода в очаге одной строкой (Open-Meteo); null — погоды нет. */
function weatherLine(weather) {
  if (!weather || weather.error) return null;
  const parts = [];
  if (Number.isFinite(weather.temperature_c)) parts.push(`${number(weather.temperature_c, 0)} °C`);
  if (Number.isFinite(weather.humidity_pct)) parts.push(`влажн. ${number(weather.humidity_pct, 0)} %`);
  if (Number.isFinite(weather.wind_speed_ms)) {
    parts.push(`ветер ${weather.wind_from || ""} ${number(weather.wind_speed_ms, 0)} м/с`.replace("  ", " ") +
      (Number.isFinite(weather.wind_gusts_ms) ? ` (порывы ${number(weather.wind_gusts_ms, 0)})` : "") +
      (weather.spread_to && weather.wind_speed_ms >= 1 ? `, сносит на ${weather.spread_to}` : ""));
  }
  if (weather.danger_class) parts.push(`пожароопасность ${DANGER_CLASSES[weather.danger_class]} кл.`);
  return parts.length ? parts.join(" · ") : null;
}

function weatherTitle(weather) {
  if (!weather || weather.error) return "";
  return `${weather.source}, ячейка ${weather.cell?.join(", ")}; на ${weather.at} местного времени. ` +
    `Показатель Нестерова ${weather.nesterov} (${weather.dry_days} дн. без дождя > 3 мм), ` +
    `осадки за сутки ${weather.precip_24h_mm} мм. Модель погоды, не метеостанция: класс ориентировочный.`;
}

/** Список событий огня и честный статус каждого (режим «огонь прежде гари»). */
function renderFireEvents(prediction) {
  const section = $("fireEvents");
  if (prediction?.mode !== "fire_first") {
    section.hidden = true;
    return;
  }
  const events = prediction.events || [];
  $("fireEventList").innerHTML = events.map((event) => {
    const reasons = event.reliability_reasons || [];
    const needsConfirmation = reasons.some((reason) => reason.startsWith("требует подтверждения"));
    const key = event.status === "ok" && event.reliability === "unreliable"
      ? (needsConfirmation && reasons.length === 1 ? "ok_confirm" : "ok_unreliable") : event.status;
    const status = EVENT_STATUS[key] || { label: event.status, color: "#8a8f98" };
    const evidence = event.fire_evidence;
    const lines = [
      `${evidence.n_points} ${plural(evidence.n_points, "термоточка", "термоточки", "термоточек")} · ` +
        shortPeriod(evidence.first_detected, evidence.last_detected),
    ];
    if (event.pair) {
      lines.push(`снимки ${shortDate(event.pair.pre)} → ${shortDate(event.pair.post)} (${event.pair.gap_days} дн.)` +
        (Number.isFinite(event.burn_area_ha) && event.status === "ok" ? ` · гарь ${number(event.burn_area_ha, 0)} га` : ""));
      // Площадь на точку FIRMS — только предупреждение: один пролёт мог поймать часть фронта.
      if (event.status === "ok" && Number.isFinite(event.area_per_point_ha)) {
        lines.push(`${number(event.area_per_point_ha, event.area_per_point_ha < 100 ? 1 : 0)} га гари на одну термоточку`);
      }
    }
    const weather = weatherLine(event.weather);
    // Только то, что отличает это событие; общие оговорки — одной строкой под списком.
    const notes = [];
    if (event.status === "ok" && event.reliability === "unreliable") {
      notes.push(`Ненадёжно: ${(event.reliability_reasons || []).join(", ")}`);
    } else if (event.status === "ok" && event.burn_area_ha < 0.5) {
      notes.push("Гари почти нет: у этих точек она меньше 0,5 га или уже учтена в соседнем событии");
    } else if (event.status === "no_scar") {
      notes.push("В местах детекций гари на снимках не видно");
    } else if (event.status !== "ok" && event.message) {
      notes.push(event.message);
    }
    const objects = event.industrial_context || [];
    if (objects.length && objects[0].points_near_any * 2 >= objects[0].points_total) {
      notes.push(`Рядом ${objects.map((o) => o.kind + (o.name ? ` «${o.name}»` : "")).slice(0, 2).join(", ")} ` +
        "(OpenStreetMap): огонь может быть техногенным");
    }
    const harvested = event.harvest_check;
    if (harvested?.removed_ha >= 0.5) {
      notes.push(`${number(harvested.removed_ha, 0)} га посветлели после огня — уборка или дымка, а не гарь`);
    }
    const before = event.pre_fire_check;
    if (before?.removed_ha >= 0.5) {
      notes.push(`${number(before.removed_ha, 0)} га потемнели ещё до огня (снимок ` +
        `${shortDate(before.scenes[before.scenes.length - 1])}) — убрано или вспахано, не гарь`);
    }
    const after = event.post_fire_check;
    if (after?.removed_ha >= 0.5) {
      notes.push(`${number(after.removed_ha, 0)} га потемнели уже после огня (на снимке ` +
        `${shortDate(after.scenes[0])} гари там не было) — не гарь`);
    }
    if (Number.isFinite(event.observed_fraction) && event.observed_fraction < 0.95) {
      notes.push(`${number((1 - event.observed_fraction) * 100, 0)}% района под облаками — там не оценивалось`);
    }
    const landcover = Object.entries(event.burn_by_landcover_ha || {})
      .map(([name, row]) => [name, row["1"] || 0, row["2"] || 0, row["3"] || 0])
      .filter((row) => row[1] + row[2] + row[3] >= 0.5)
      .sort((a, b) => (b[1] + b[2] + b[3]) - (a[1] + a[2] + a[3]));
    const table = landcover.length && event.status === "ok"
      ? "<table><tr><th>тип земель, га</th><th>слаб.</th><th>сред.</th><th>сильн.</th></tr>" +
        landcover.map(([name, a, b, c]) => `<tr><td>${escapeHtml(name)}</td><td>${number(a, 0)}</td>` +
          `<td>${number(b, 0)}</td><td>${number(c, 0)}</td></tr>`).join("") + "</table>"
      : "";
    // Снимки есть у события, для которого нашлась пара: видно, что увидела модель.
    const preview = event.pair
      ? `<button type="button" class="event-preview" data-event-preview="${escapeHtml(event.event_id)}">` +
        "Снимки до/после</button>"
      : "";
    return `<li style="--event-color:${status.color}"><strong>${escapeHtml(status.label)}</strong>` +
      lines.map((line) => `<span>${escapeHtml(line)}</span>`).join("<br>") +
      (weather ? `<br><span class="weather" title="${escapeHtml(weatherTitle(event.weather))}">` +
        `${escapeHtml(weather)}</span>` : "") +
      notes.map((note) => `<br><span class="muted">${escapeHtml(note)}</span>`).join("") +
      `${table}${preview && !table ? "<br>" : ""}${preview}</li>`;
  }).join("");
  $("fireEventList").querySelectorAll("[data-event-preview]").forEach((button) => {
    button.onclick = () => openEventPreview(button.dataset.eventPreview);
  });
  const evidence = prediction.evidence;
  const excluded = evidence?.excluded || {};
  const dropped = (excluded.low_confidence || 0) + (excluded.static_type || 0) + (excluded.persistent || 0);
  $("fireEvidenceNote").textContent = evidence
    ? `FIRMS: ${evidence.points_total} ${plural(evidence.points_total, "точка", "точки", "точек")}, ` +
      `как огонь принято ${evidence.points_kept}` +
      (dropped ? ` (отброшено ${dropped}: низкая достоверность, статические и постоянные источники)` : "") +
      ". Точка VIIRS (375 м) подтверждает огонь, но не задаёт границу гари; пороги гари в крае не откалиброваны."
    : prediction.message || "";
  section.hidden = !events.length && !evidence;
}

/** Снимки события (до/после, SWIR, контур) в новой вкладке — те же, по которым считалась гарь. */
async function openEventPreview(eventId) {
  const path = `/api/events/${encodeURIComponent(eventId)}/preview.png`;
  const params = queryParams();
  if (params) {
    window.open(`${path}?${params}`, "_blank", "noopener");
    return;
  }
  const tab = window.open("", "_blank");
  try {
    const response = await fetch(path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: requestBody(),
    });
    if (!response.ok) throw new Error(await errorText(response));
    tab.location = URL.createObjectURL(await response.blob());
  } catch (error) {
    tab?.close();
    toast(`Не удалось получить снимки события: ${error.message}`);
  }
}

/** Доля участка, где поверхность была видна на обоих снимках; null — не спутниковый режим. */
function observedFraction(prediction) {
  const value = prediction?.observed_fraction;
  return typeof value === "number" ? value : null;
}

function renderUnobserved(prediction) {
  state.unobservedLayer.clearLayers();
  if (prediction?.unobserved) {
    state.unobservedLayer.addData({ type: "Feature", geometry: prediction.unobserved, properties: {} });
  }
  const observed = observedFraction(prediction);
  $("shareUnobserved").textContent = observed === null ? "—" : `${number((1 - observed) * 100, 0)}%`;
}

function mapFeatures(kind) {
  const features = state.result?.[kind]?.features || [];
  const visible = mapTimeline.month
    ? features.filter((feature) => feature.properties.date?.slice(0, 7) === mapTimeline.month)
    : features;
  if (kind !== "burns") return visible;
  // Ненадёжные оценки скрыты по умолчанию: на карте видна только гарь, которой можно доверять.
  const shown = $("layerUnreliable").checked
    ? visible : visible.filter((feature) => feature.properties.reliable !== false);
  return shown;
}

function monthTitle(month) {
  return new Date(`${month}-01T00:00:00`).toLocaleDateString("ru-RU", { month: "long", year: "numeric" });
}

function renderMapFeatures() {
  const burns = mapFeatures("burns");
  const hotspots = mapFeatures("hotspots");
  state.map.closePopup();
  state.burnLayer.clearLayers().addData({ type: "FeatureCollection", features: burns });
  state.hotspotLayer.clearLayers().addData({ type: "FeatureCollection", features: hotspots });
  $("countHotspots").textContent = integer(hotspots.length);
  $("countBurns").textContent = integer(burns.length);
  $("countUnreliable").textContent = integer((state.result?.burns?.features || [])
    .filter((feature) => feature.properties.reliable === false).length);
  applyLayerVisibility();
  $("timelineStatus").textContent =
    `${mapTimeline.month ? monthTitle(mapTimeline.month) : "Весь период"} · ` +
    `${integer(hotspots.length)} ${plural(hotspots.length, "термоточка", "термоточки", "термоточек")} · ` +
    `${integer(burns.length)} ${plural(burns.length, "контур", "контура", "контуров")}`;
  $("reportMapScope").textContent = mapTimeline.month
    ? `На карте: ${monthTitle(mapTimeline.month)} Справка, подробная аналитика и выгрузки — за весь период запроса.`
    : "";
  $("reportMapScope").hidden = !mapTimeline.month;
}

function selectTimelineMonth(month) {
  if (month !== null && !mapTimeline.months.includes(month)) return;
  mapTimeline.month = month;
  renderMapFeatures();
  $("timelineMonths").querySelectorAll("[data-month]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.month === month));
  });
}

function syncTimelineScroll() {
  const strip = $("timelineMonths");
  $("timelineBack").disabled = strip.scrollLeft <= 1;
  $("timelineForward").disabled = strip.scrollLeft + strip.clientWidth >= strip.scrollWidth - 1;
}

function resetMapTimeline() {
  mapTimeline.month = null;
  mapTimeline.months = [];
  $("timelineMonths").replaceChildren();
  $("mapTimeline").hidden = true;
  $("reportMapScope").hidden = true;
}

function renderMapTimeline() {
  resetMapTimeline();
  const result = state.result;
  if (!result || result.summary.empty) return;
  const range = result.summary.observed_range;
  if (!range?.from || !range?.to) return;
  mapTimeline.months = monthRange(range.from, range.to);
  const counts = new Map(mapTimeline.months.map((month) => [month, { hotspots: 0, burns: 0 }]));
  for (const kind of ["hotspots", "burns"]) {
    for (const feature of result[kind].features) {
      const bin = counts.get(feature.properties.date?.slice(0, 7));
      if (bin) bin[kind] += 1;
    }
  }
  const peak = Math.max(1, ...[...counts.values()].map((bin) => bin.hotspots));
  $("timelineMonths").innerHTML = mapTimeline.months.map((month) => {
    const bin = counts.get(month);
    const label = `${monthTitle(month)}: термоточек — ${bin.hotspots}, контуров — ${bin.burns}`;
    return `<button type="button" class="month-button${bin.burns ? " has-burns" : ""}" ` +
      `data-month="${month}" aria-pressed="false" aria-label="${escapeHtml(label)}" title="${escapeHtml(label)}">` +
      `<span class="month-count" aria-hidden="true">${bin.hotspots || ""}</span>` +
      `<span class="month-track" aria-hidden="true"><i style="height:${bin.hotspots ? Math.max(3, 20 * bin.hotspots / peak) : 0}px"></i></span>` +
      `<span>${MONTHS_SHORT[Number(month.slice(5)) - 1]}</span><small>${month.slice(0, 4)}</small></button>`;
  }).join("");
  $("mapTimeline").hidden = false;
  requestAnimationFrame(syncTimelineScroll);
}

function bindMapTimeline() {
  $("toggleTimeline").onclick = () => {
    const details = $("timelineDetails");
    details.hidden = !details.hidden;
    $("toggleTimeline").setAttribute("aria-expanded", String(!details.hidden));
    $("toggleTimeline").textContent = details.hidden ? "Развернуть ▴" : "Свернуть ▾";
    if (!details.hidden) requestAnimationFrame(syncTimelineScroll);
  };
  $("timelineMonths").onclick = (event) => {
    const button = event.target.closest("[data-month]");
    if (button) selectTimelineMonth(mapTimeline.month === button.dataset.month ? null : button.dataset.month);
  };
  for (const [id, direction] of [["timelineBack", -1], ["timelineForward", 1]]) {
    $(id).onclick = () => {
      const strip = $("timelineMonths");
      strip.scrollBy({ left: direction * Math.max(120, strip.clientWidth * 0.8), behavior: "smooth" });
    };
  }
  $("timelineMonths").addEventListener("scroll", syncTimelineScroll);
  if (window.ResizeObserver) new ResizeObserver(syncTimelineScroll).observe($("timelineMonths"));
}

function focusBurn(properties) {
  if (mapTimeline.month) selectTimelineMonth(null);
  const layer = state.burnLayer.getLayers().find((item) => item.feature.properties === properties);
  if (!layer) {
    toast("Контур больше не доступен. Обновите выборку.");
    return;
  }
  $("layerBurns").checked = true;
  applyLayerVisibility();
  $("dashboard").close();
  const bounds = layer.getBounds();
  if (!bounds.isValid()) return;
  state.map.invalidateSize({ animate: false });
  state.map.fitBounds(bounds, { padding: [44, 44], maxZoom: 15, animate: false });
  layer.openPopup(bounds.getCenter());
}

function applyLayerVisibility() {
  toggleLayer(state.liveLayer, $("layerLive").checked && state.liveAvailable);
  toggleLayer(state.hotspotLayer, $("layerHotspots").checked);
  toggleLayer(state.unobservedLayer, $("layerUnobserved").checked);
  toggleLayer(state.burnLayer, $("layerBurns").checked);
  toggleLayer(state.areaLayer, $("layerArea").checked);
  // Заливка редактируемой области не должна перехватывать клики по гарям.
  if (state.map.hasLayer(state.burnLayer)) state.burnLayer.bringToFront();
  if (state.map.hasLayer(state.hotspotLayer)) state.hotspotLayer.bringToFront();
  if (state.map.hasLayer(state.liveLayer)) state.liveLayer.bringToFront();
}

function toggleLayer(layer, visible) {
  if (!layer) return;
  if (visible && !state.map.hasLayer(layer)) layer.addTo(state.map);
  if (!visible && state.map.hasLayer(layer)) state.map.removeLayer(layer);
}

/* -------------------------------------------- Аналитическая справка */

const PANES = ["reportIdle", "reportLoading", "reportError", "reportEmpty", "reportBody"];

function setRightPanel(panel) {
  $("reportPanel").hidden = panel !== "report";
  $("archivePanel").hidden = panel !== "archive";
  $("monitorPanel").hidden = panel !== "monitor";
  $("workspace").classList.toggle("has-right-panel", Boolean(panel));
  for (const [id, kind, title] of [
    ["reportToggle", "report", "аналитическую панель"],
    ["archiveToggle", "archive", "архив областей"],
    ["monitorToggle", "monitor", "мониторинг огня"],
  ]) {
    const toggle = $(id);
    const open = panel === kind;
    const label = `${open ? "Скрыть" : "Открыть"} ${title}`;
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", label);
    toggle.title = label;
  }
  if (panel === "archive") requestAnimationFrame(() => {
    for (const preview of areaPreviewMaps.values()) preview.map.invalidateSize({ animate: false });
    if (!window.IntersectionObserver) initVisibleAreaPreviews();
  });
}

function showPane(id) {
  if (id !== "reportIdle") setRightPanel("report");
  else if (!$("reportPanel").hidden) setRightPanel(null);
  PANES.forEach((pane) => ($(pane).hidden = pane !== id));
  // Кнопка подробной аналитики живёт в reportBody, но её состояние зависит от
  // наличия данных, а не от видимой панели.
  syncDashboardButton();
}

function renderReport(result) {
  const s = result.summary;
  const observed = observedFraction(result.prediction);
  const hiddenShare = observed === null ? 0 : 1 - observed;
  $("compareNotice").hidden = !result.comparison;
  $("compareWithoutFire").hidden = true;
  if (s.empty && result.prediction?.mode === "fire_first") {
    $("reportEmptyHint").textContent = result.prediction.message;
    // Огня нет — можно показать, что без проверки огня модель приняла бы за гарь.
    $("compareWithoutFire").hidden = result.prediction.status !== "no_fire_evidence";
    showPane("reportEmpty");
    return;
  }
  if (s.empty) {
    // «Гари нет» допустимо только там, где поверхность была видна.
    $("reportEmptyHint").textContent = observed !== null && hiddenShare >= 0.995
      ? "Участок полностью закрыт облаками на доступных снимках: гарь не оценивалась. " +
        "Это не означает, что пожара не было — попробуйте другой период."
      : `За период ${formatDate(s.period.from)} — ${formatDate(s.period.to)} в выбранной области ` +
        "нет ни термоточек, ни контуров гарей." +
        (hiddenShare >= 0.005
          ? ` Вывод относится только к ${number(observed * 100, 0)}% участка: остальная часть ` +
            "закрыта облаками и не оценивалась (заштрихована на карте)."
          : " Расширьте область или период.");
    showPane("reportEmpty");
    return;
  }
  $("reportObserved").hidden = hiddenShare < 0.005;
  $("reportObserved").textContent =
    `Гарь оценена на ${number(observed * 100, 0)}% участка. На остальных ` +
    `${number(hiddenShare * 100, 0)}% поверхность закрыта облаками или тенями — там гарь ` +
    "не оценивалась (заштриховано на карте), и площадь может быть занижена.";

  $("totalArea").textContent = `${number(s.total_area_ha)} га`;
  const prediction = result.prediction;
  $("totalLabel").textContent = result.comparison
    ? "Изменения без проверки огня · не гарь"
    : prediction?.mode === "fire_first" ? "Площадь гари · надёжная оценка" : "Суммарная площадь гари";
  if (prediction?.mode === "fire_first") {
    // Главная цифра — только надёжная площадь; ненадёжная показана отдельно.
    $("totalArea").textContent = `${number(prediction.burn_area_reliable_ha || 0)} га`;
  }
  const burnWord = plural(s.burn_count, "контур", "контура", "контуров");
  $("totalMeta").textContent =
    `${s.burn_count} ${burnWord} · ${formatDate(s.period.from)} — ${formatDate(s.period.to)}`;
  if (prediction?.mode === "fire_first") {
    const events = (prediction.events || [])
      .filter((event) => event.status === "ok" && event.reliability === "reliable").length;
    $("totalMeta").textContent = `событий с надёжной оценкой гари: ${events}` +
      (prediction.burn_area_unreliable_ha ? ` · ещё ${number(prediction.burn_area_unreliable_ha)} га — ненадёжная оценка (слой «Ненадёжные оценки гари»)` : "");
  }

  const maxArea = Math.max(...s.by_severity.map((item) => item.area_ha), 0);
  $("severityChart").innerHTML = s.by_severity
    .map((item) => {
      const width = maxArea > 0 ? (item.area_ha / maxArea) * 100 : 0;
      return (
        `<div class="bar-row"><span>${escapeHtml(item.label)}</span>` +
        `<span class="bar-track"><span class="bar-fill" style="width:${width.toFixed(1)}%;background:${item.color}"></span></span>` +
        `<span class="bar-value">${number(item.area_ha, 0)} га</span></div>`
      );
    })
    .join("");

  $("factHotspots").textContent = integer(s.hotspot_count);
  $("factBurns").textContent = integer(s.burn_count);
  $("factObserved").textContent = s.observed_range.from
    ? `${shortDate(s.observed_range.from)} — ${shortDate(s.observed_range.to)}`
    : "—";
  renderChanges(result.changes);
  // Подробности — в отдельном окне: справка в панели держится короткой.
  showPane("reportBody");
}

/** Компактная дата для плиток: «11.08.19». */
function shortDate(iso) {
  if (!iso) return "—";
  const parsed = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "2-digit" });
}

function renderLegend() {
  $("legend").innerHTML = state.severity
    .map(
      (item) =>
        `<li><i style="background:${item.color}"></i>${item.severity} — ${escapeHtml(item.label)}` +
        `<b data-legend="${item.severity}">—</b></li>`,
    )
    .join("");
  updateLegendCounts();
}

function updateLegendCounts() {
  const bySeverity = state.result?.summary?.by_severity ?? [];
  document.querySelectorAll("[data-legend]").forEach((node) => {
    const found = bySeverity.find((item) => String(item.severity) === node.dataset.legend);
    node.textContent = found && found.count ? `${number(found.area_ha, 0)} га` : "—";
  });
}

/* ------------------------------------------------------ Выделение области */

function setTool(tool) {
  state.tool = tool;
  document.querySelectorAll("[data-tool]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.tool === tool));
  });
}

function startDrawing(kind) {
  if (!state.map) return;
  cancelDrawing();
  setTool(kind);
  state.drawing = kind;
  state.vertices = [];
  document.body.classList.add("drawing");
  state.map.doubleClickZoom.disable();
  $("drawHint").hidden = false;
  $("drawFinish").hidden = kind !== "polygon";
  $("drawHintText").textContent =
    kind === "rectangle"
      ? "Кликните два противоположных угла прямоугольника"
      : "Кликайте вершины. Замкните по первой точке или кнопкой «Замкнуть»";
}

function cancelDrawing() {
  clearDraft();
  state.drawing = false;
  state.vertices = [];
  setTool(null);
  document.body.classList.remove("drawing");
  state.map?.doubleClickZoom.enable();
  $("drawHint").hidden = true;
}

function clearDraft() {
  if (state.draftLine) state.map.removeLayer(state.draftLine);
  state.draftLine = null;
  state.draftMarkers.forEach((marker) => state.map.removeLayer(marker));
  state.draftMarkers = [];
}

function onMapClick(event) {
  if (!state.drawing) return;
  if (state.vertices.length >= state.limits.max_vertices) {
    toast(`Достигнут предел вершин: ${state.limits.max_vertices}`);
    return;
  }
  if (state.drawing === "rectangle") {
    state.vertices.push([event.latlng.lat, event.latlng.lng]);
    if (state.vertices.length === 2) {
      finishRectangle(state.vertices[0], state.vertices[1]);
      return;
    }
  } else {
    state.vertices.push([event.latlng.lat, event.latlng.lng]);
  }
  renderDraft();
}

function onMapMove(event) {
  if (state.drawing !== "rectangle" || state.vertices.length !== 1) return;
  const corners = rectangleRing(state.vertices[0], [event.latlng.lat, event.latlng.lng]);
  // После первого клика renderDraft оставляет polyline: вершина всего одна.
  // Полилиния по четырём углам рисует три отрезка из четырёх — замыкающая грань
  // на долготе первого клика пропадала. Превью прямоугольника обязано быть полигоном.
  if (state.draftLine instanceof L.Polygon) {
    state.draftLine.setLatLngs(corners);
    return;
  }
  if (state.draftLine) state.map.removeLayer(state.draftLine);
  state.draftLine = L.polygon(corners, {
    ...AREA_STYLE,
    fill: true,
    fillOpacity: 0.08,
    interactive: false,
  }).addTo(state.map);
}

function rectangleRing(first, second) {
  const [lat1, lng1] = first;
  const [lat2, lng2] = second;
  return [
    [lat1, lng1],
    [lat1, lng2],
    [lat2, lng2],
    [lat2, lng1],
  ];
}

function renderDraft() {
  clearDraft();
  if (!state.vertices.length) return;
  const shape = state.vertices.length > 2 ? L.polygon : L.polyline;
  state.draftLine = shape(state.vertices, {
    ...AREA_STYLE, fill: state.vertices.length > 2, fillOpacity: 0.08,
  }).addTo(state.map);
  // Замыкание по первой вершине есть только у полигона: у прямоугольника второй
  // клик задаёт противоположный угол, и перехват клика мешал бы поставить его
  // рядом с первым.
  const closable = state.drawing === "polygon";
  state.vertices.forEach((point, index) => {
    const first = index === 0 && closable;
    const marker = L.marker(point, {
      icon: L.divIcon({
        className: `vertex-handle ${first ? "first" : ""}`,
        iconSize: [12, 12],
        iconAnchor: [6, 6],
      }),
      title: first ? "Замкнуть контур" : "Вершина",
      keyboard: false,
    }).addTo(state.map);
    if (first) marker.on("click", (event) => {
      L.DomEvent.stop(event);
      finishPolygon();
    });
    state.draftMarkers.push(marker);
  });
}

function finishRectangle(first, second) {
  const ring = rectangleRing(first, second);
  const lats = ring.map((point) => point[0]);
  const lngs = ring.map((point) => point[1]);
  const bbox = [Math.min(...lngs), Math.min(...lats), Math.max(...lngs), Math.max(...lats)];
  if (bbox[0] === bbox[2] || bbox[1] === bbox[3]) {
    toast("Прямоугольник вырожден: выберите два разных угла");
    state.vertices = [];
    clearDraft();
    return;
  }
  cancelDrawing();
  setArea({ bbox }, "прямоугольник");
}

function finishPolygon() {
  if (state.vertices.length < 3) {
    toast("Нужно минимум три вершины");
    return;
  }
  const count = state.vertices.length;
  const ring = state.vertices.map(([lat, lng]) => [Number(lng.toFixed(6)), Number(lat.toFixed(6))]);
  ring.push([...ring[0]]);
  cancelDrawing();
  setArea(
    { geometry: { type: "Polygon", coordinates: [ring] } },
    `полигон, ${count} ${plural(count, "вершина", "вершины", "вершин")}`,
  );
}

function useCurrentView() {
  if (!state.map) return;
  const bounds = state.map.getBounds();
  cancelDrawing();
  setArea(
    {
      bbox: [
        Math.max(bounds.getWest(), -180),
        Math.max(bounds.getSouth(), -85),
        Math.min(bounds.getEast(), 180),
        Math.min(bounds.getNorth(), 85),
      ].map((value) => Number(value.toFixed(6))),
    },
    "экран карты",
  );
}

function setArea(area, description) {
  stopAreaRotate();
  stopAreaMove();
  state.area = area;
  state.activeAreaId = null;
  state.areaKind = area.bbox ? "rectangle" : "polygon";
  state.areaPoints = area.bbox
    ? rectangleRing([area.bbox[3], area.bbox[0]], [area.bbox[1], area.bbox[2]]).map(([lat, lng]) => L.latLng(lat, lng))
    : area.geometry?.type === "Polygon" && area.geometry.coordinates.length === 1
      ? area.geometry.coordinates[0].slice(0, -1).map(([lng, lat]) => L.latLng(lat, lng))
      : [];
  renderEditableArea();
  invalidateAreaResult();
  applyLayerVisibility();
  $("areaHint").textContent = `Область задана: ${description}.`;
  $("areaEditHint").hidden = state.areaPoints.length < 3;
  $("clearAreaButton").disabled = false;
  $("runButton").disabled = false;
  if ($("monitorAddZone")) $("monitorAddZone").disabled = false;
  renderAreas();
}

function renderEditableArea() {
  state.areaLayer.clearLayers();
  state.areaHandles = [];
  state.areaRotationStem = null;
  if (state.areaPoints.length < 3) {
    state.areaShape = L.geoJSON(state.area.geometry, { style: () => AREA_STYLE }).addTo(state.areaLayer);
    return;
  }
  state.areaShape = L.polygon(state.areaPoints, EDIT_STYLE).addTo(state.areaLayer);
  state.areaShape.on("mousedown", startAreaMove);
  state.areaPoints.forEach((point, index) => {
    const handle = L.marker(point, {
      draggable: true,
      keyboard: false,
      title: state.areaKind === "rectangle" ? "Изменить размер области" : "Переместить вершину",
      icon: L.divIcon({ className: "area-edit-handle", iconSize: [16, 16], iconAnchor: [8, 8] }),
    }).addTo(state.areaLayer);
    handle.on("drag", () => dragAreaVertex(index, handle.getLatLng()));
    handle.on("dragend", commitAreaEdit);
    state.areaHandles.push(handle);
  });
  state.areaRotationStem = L.polyline(rotationStemPoints(state.areaPoints), {
    color: "#365f2d", weight: 1, interactive: false,
  }).addTo(state.areaLayer);
  const rotation = L.marker(rotationHandlePoint(state.areaPoints), {
    keyboard: true,
    title: "Повернуть фигуру",
    zIndexOffset: 1000,
    icon: L.divIcon({ className: "area-rotate-handle", html: "↻", iconSize: [28, 28], iconAnchor: [14, 14] }),
  }).addTo(state.areaLayer);
  rotation.on("mousedown", startAreaRotate);
  state.areaHandles.push(rotation);
}

function startAreaRotate(event) {
  if (state.drawing || state.areaRotate || event.originalEvent.button !== 0) return;
  L.DomEvent.stop(event.originalEvent);
  const points = state.areaPoints.map((point) => L.latLng(point));
  state.areaRotate = {
    points,
    center: projectedCenter(points),
    origin: state.map.latLngToLayerPoint(rotationHandlePoint(points)),
    moved: false,
  };
  state.map.dragging.disable();
  window.addEventListener("mousemove", rotateArea);
  window.addEventListener("mouseup", stopAreaRotate);
}

function rotateArea(event) {
  if (!state.areaRotate) return;
  event.preventDefault();
  const { points, center, origin } = state.areaRotate;
  const target = state.map.mouseEventToLayerPoint(event);
  const angle = Math.atan2(target.y - center.y, target.x - center.x)
    - Math.atan2(origin.y - center.y, origin.x - center.x);
  if (Math.abs(angle) < 0.001) return;
  state.areaRotate.moved = true;
  updateAreaPreview(rotatedAreaPoints(points, angle));
}

function stopAreaRotate() {
  if (!state.areaRotate) return;
  const moved = state.areaRotate.moved;
  state.areaRotate = null;
  window.removeEventListener("mousemove", rotateArea);
  window.removeEventListener("mouseup", stopAreaRotate);
  state.map.dragging.enable();
  if (moved) commitAreaEdit();
}

function projectedCenter(points) {
  const pixels = points.map((point) => state.map.latLngToLayerPoint(point));
  return L.bounds(pixels).getCenter();
}

function rotationHandlePoint(points) {
  const { midpoint, top } = rotationAnchor(points);
  return state.map.layerPointToLatLng(L.point(midpoint, top - 38));
}

function rotationStemPoints(points) {
  const { midpoint, top } = rotationAnchor(points);
  return [state.map.layerPointToLatLng(L.point(midpoint, top)), rotationHandlePoint(points)];
}

function rotationAnchor(points) {
  const pixels = points.map((point) => state.map.latLngToLayerPoint(point));
  const left = Math.min(...pixels.map((point) => point.x));
  const right = Math.max(...pixels.map((point) => point.x));
  const top = Math.min(...pixels.map((point) => point.y));
  return { midpoint: (left + right) / 2, top };
}

function updateAreaPreview(points, activeHandle = null) {
  state.areaPoints = points;
  state.areaShape.setLatLngs(points);
  state.areaRotationStem?.setLatLngs(rotationStemPoints(points));
  state.areaHandles.forEach((handle, index) => {
    if (handle === activeHandle) return;
    handle.setLatLng(index < points.length ? points[index] : rotationHandlePoint(points));
  });
}

function dragAreaVertex(index, position) {
  const points = state.areaPoints.map((point) => state.map.latLngToLayerPoint(point));
  const dragged = state.map.latLngToLayerPoint(position);
  if (state.areaKind === "rectangle" && points.length === 4) {
    const opposite = (index + 2) % 4;
    const next = (index + 1) % 4;
    const previous = (index + 3) % 4;
    const origin = points[opposite];
    const axis = (point) => {
      const delta = point.subtract(origin);
      return delta.divideBy(Math.hypot(delta.x, delta.y) || 1);
    };
    const u = axis(points[next]);
    const v = axis(points[previous]);
    const delta = dragged.subtract(origin);
    const width = Math.max(12, delta.x * u.x + delta.y * u.y);
    const height = Math.max(12, delta.x * v.x + delta.y * v.y);
    points[next] = origin.add(u.multiplyBy(width));
    points[previous] = origin.add(v.multiplyBy(height));
    points[index] = points[next].add(v.multiplyBy(height));
  } else {
    points[index] = dragged;
  }
  updateAreaPreview(points.map((point) => state.map.layerPointToLatLng(point)), state.areaHandles[index]);
}

function rotatedAreaPoints(original, angle) {
  const center = projectedCenter(original);
  const cosine = Math.cos(angle);
  const sine = Math.sin(angle);
  return original.map((point) => {
    const delta = state.map.latLngToLayerPoint(point).subtract(center);
    return state.map.layerPointToLatLng(L.point(
      center.x + delta.x * cosine - delta.y * sine,
      center.y + delta.x * sine + delta.y * cosine,
    ));
  });
}

function startAreaMove(event) {
  if (state.drawing || state.areaMove || !state.areaPoints.length || event.originalEvent.button !== 0) return;
  L.DomEvent.stop(event.originalEvent);
  state.map.dragging.disable();
  state.areaMove = {
    start: state.map.mouseEventToLayerPoint(event.originalEvent),
    points: state.areaPoints.map((point) => state.map.latLngToLayerPoint(point)),
    moved: false,
  };
  window.addEventListener("mousemove", moveArea);
  window.addEventListener("mouseup", stopAreaMove);
}

function moveArea(event) {
  if (!state.areaMove) return;
  event.preventDefault();
  const delta = state.map.mouseEventToLayerPoint(event).subtract(state.areaMove.start);
  if (Math.hypot(delta.x, delta.y) < 1) return;
  state.areaMove.moved = true;
  const points = state.areaMove.points.map((point) => state.map.layerPointToLatLng(point.add(delta)));
  updateAreaPreview(points);
}

function stopAreaMove() {
  if (!state.areaMove) return;
  const moved = state.areaMove.moved;
  state.areaMove = null;
  window.removeEventListener("mousemove", moveArea);
  window.removeEventListener("mouseup", stopAreaMove);
  state.map.dragging.enable();
  if (moved) commitAreaEdit();
}

function commitAreaEdit() {
  if (!state.areaPoints.length) return;
  const ring = state.areaPoints.map((point) => [Number(point.lng.toFixed(6)), Number(point.lat.toFixed(6))]);
  ring.push([...ring[0]]);
  state.area = { geometry: { type: "Polygon", coordinates: [ring] } };
  state.activeAreaId = null;
  invalidateAreaResult();
  $("areaHint").textContent = "Область изменена. Нажмите «Показать пожары», чтобы обновить данные.";
  renderAreas();
}

function invalidateAreaResult() {
  cancelDrawing();
  state.result = null;
  resetMapTimeline();
  state.burnLayer.clearLayers();
  state.hotspotLayer.clearLayers();
  state.unobservedLayer.clearLayers();
  $("shareUnobserved").textContent = "—";
  $("fireEvents").hidden = true;
  state.liveLayer?.clearLayers();
  $("countHotspots").textContent = "0";
  $("countBurns").textContent = "0";
  $("countUnreliable").textContent = "0";
  $("countLive").textContent = "—";
  $("requestStatus").textContent = "";
  updateLegendCounts();
  showPane("reportIdle");
}

function clearArea() {
  cancelDrawing();
  stopAreaRotate();
  stopAreaMove();
  state.area = null;
  state.areaPoints = [];
  state.areaHandles = [];
  state.areaShape = null;
  state.areaRotationStem = null;
  state.result = null;
  resetMapTimeline();
  state.areaLayer.clearLayers();
  state.burnLayer.clearLayers();
  state.hotspotLayer.clearLayers();
  state.unobservedLayer.clearLayers();
  $("shareUnobserved").textContent = "—";
  $("fireEvents").hidden = true;
  $("countHotspots").textContent = "0";
  $("countBurns").textContent = "0";
  $("countUnreliable").textContent = "0";
  $("areaHint").textContent = "Область не выбрана. Нарисуйте её на карте или возьмите текущий экран.";
  $("areaEditHint").hidden = true;
  $("clearAreaButton").disabled = true;
  $("runButton").disabled = true;
  $("requestStatus").textContent = "";
  $("requestStatus").classList.remove("error");
  updateLegendCounts();
  state.activeAreaId = null;
  state.liveLayer?.clearLayers();
  $("countLive").textContent = "—";
  renderAreas();
  showPane("reportIdle");
}

/* ------------------------------------------------------------- Запрос */

function periodOrThrow() {
  const from = $("dateFrom").value;
  const to = $("dateTo").value;
  if (!from || !to) throw new Error("Укажите начало и конец периода");
  if (from > to) throw new Error("Начало периода позже конца");
  return { from, to };
}

/** Параметры для GET-эндпоинтов. Доступны только если область — прямоугольник. */
function queryParams() {
  if (!state.area?.bbox) return null;
  const { from, to } = periodOrThrow();
  return new URLSearchParams({ bbox: state.area.bbox.join(","), from, to });
}

function requestBody(extra = {}) {
  const { from, to } = periodOrThrow();
  const area = state.area.bbox ? { bbox: state.area.bbox.join(",") } : { geometry: state.area.geometry };
  return JSON.stringify({ ...area, from, to, ...extra });
}

async function runQuery({ comparison = false } = {}) {
  if (!state.area) {
    toast("Сначала выделите область на карте");
    return;
  }
  let body;
  try {
    body = requestBody({ live: $("layerLive").checked, ...(comparison ? { fire_first: false } : {}) });
  } catch (error) {
    $("reportErrorText").textContent = error.message;
    showPane("reportError");
    setStatus(error.message, true);
    return;
  }

  document.body.classList.add("busy");
  $("runButton").disabled = true;
  setStatus(state.sourceKind === "satellite"
    ? "Ищем спутниковые снимки и запускаем модель…"
    : "Запрашиваем данные…");
  showPane("reportLoading");

  try {
    const result = await api("/api/query", { method: "POST", body });
    if (comparison) result.comparison = true;
    state.activeAreaId = null;
    renderResult(result);
    renderReport(result);
    updateLegendCounts();
    markActiveArea();
    const s = result.summary;
    const scenes = result.prediction?.scenes || [];
    const imageDates = scenes.length
      ? ` · снимки ${formatDate(scenes[0].pre_date)} → ${formatDate(scenes[0].post_date)}`
      : "";
    const observed = observedFraction(result.prediction);
    const visibility = observed !== null && observed < 0.995
      ? ` · видно ${number(observed * 100, 0)}% участка`
      : "";
    setStatus(comparison
      ? `Сравнение без проверки огня: модель выдала ${s.burn_count} ` +
        `${plural(s.burn_count, "контур", "контура", "контуров")} на ${number(s.total_area_ha, 0)} га — ` +
        "огня NASA FIRMS здесь не видел, это не гарь"
      : result.prediction?.mode === "fire_first"
      ? result.prediction.message
      : state.sourceKind === "satellite"
      ? `Прогноз: ${s.burn_count} ${plural(s.burn_count, "контур гари", "контура гари", "контуров гари")}${imageDates}${visibility}`
      : s.empty
        ? "Ничего не найдено"
        : `Найдено: ${s.hotspot_count} ${plural(s.hotspot_count, "термоточка", "термоточки", "термоточек")}, ` +
          `${s.burn_count} ${plural(s.burn_count, "контур", "контура", "контуров")}`);
    return result;
  } catch (error) {
    $("reportErrorText").textContent = error.message;
    showPane("reportError");
    setStatus(error.message, true);
    toast(error.message);
    return null;
  } finally {
    document.body.classList.remove("busy");
    $("runButton").disabled = false;
  }
}

function setStatus(message, isError = false) {
  const node = $("requestStatus");
  node.textContent = message;
  node.classList.toggle("error", isError);
}

/* ----------------------------------------------------------- Выгрузка */

async function download(kind) {
  if (!state.result) return;
  const names = {
    burns: "burns.geojson",
    hotspots: "hotspots.geojson",
    "report.json": "report.json",
    "report.csv": "report.csv",
    kml: "fires.kml",
  };
  const params = queryParams();
  try {
    // Прямоугольник уходит в GET — такую ссылку можно показать жюри и повторить в curl.
    const response = params
      ? await fetch(`${getPath(kind)}?${params}`)
      : await fetch(`/api/export?kind=${encodeURIComponent(kind)}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: requestBody(),
        });
    if (!response.ok) {
      const failure = new Error(await errorText(response));
      failure.status = response.status;
      throw failure;
    }
    const blob = await response.blob();
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = names[kind];
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  } catch (error) {
    toast(`Не удалось выгрузить файл: ${error.message}`);
  }
}

function getPath(kind) {
  if (kind === "burns") return "/api/export/burns.geojson";
  if (kind === "hotspots") return "/api/export/hotspots.geojson";
  if (kind === "kml") return "/api/export/fires.kml";
  return `/api/${kind}`;
}

/* ============================================ Живой слой NASA FIRMS ==== */

const LIVE_COLOR = "#ffe14d";

function liveMarker(feature, latlng) {
  return L.circleMarker(latlng, {
    radius: 6,
    color: "#3a2a05",
    weight: 1,
    fillColor: LIVE_COLOR,
    fillOpacity: 0.95,
  });
}

function bindLivePopup(feature, layer) {
  const p = feature.properties;
  const rows = [
    ["Дата", formatDate(p.date)],
    ["Время UTC", escapeHtml(p.time_utc || "—")],
    ["Спутник", escapeHtml(p.satellite || "не указан")],
    ["Прибор", escapeHtml(p.instrument || "VIIRS")],
  ];
  if (p.confidence) rows.push(["Достоверность", escapeHtml(confidenceLabel(p.confidence))]);
  if (Number.isFinite(p.frp_mw)) rows.push(["Мощность (FRP)", `${number(p.frp_mw, 1)} МВт`]);
  // Яркостная температура пикселя 375 м в канале I4, а не температура пламени.
  if (Number.isFinite(p.brightness_k)) rows.push(["Яркостная t° пикселя", `${number(p.brightness_k - 273.15, 1)} °C`]);
  if (p.daynight) rows.push(["Съёмка", escapeHtml(p.daynight)]);
  layer.bindPopup(
    `<div class="popup"><h4><i style="background:${LIVE_COLOR}"></i>Активный очаг · NASA FIRMS</h4>` +
      `<dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>` +
      '<p class="note">Оперативные данные, задержка около 3 часов. Не результат наших моделей.</p></div>',
  );
}

function renderLive(live) {
  const counter = $("countLive");
  state.liveLayer.clearLayers();
  if (!live) {
    state.liveAvailable = false;
    counter.textContent = "—";
    return;
  }
  state.liveAvailable = live.available;
  if (live.available) {
    state.liveLayer.addData(live.hotspots);
    counter.textContent = integer(live.count);
    $("liveSwitchRow").title = `${live.sources.join(", ")} · ${live.latency_note}`;
  } else {
    counter.textContent = "—";
    $("liveSwitchRow").title = live.error || "Живые данные недоступны";
    if ($("layerLive").checked) toast(`Живые данные недоступны: ${live.error}`);
  }
}

/* =================================================== Учётная запись ==== */

function renderAccount() {
  const holder = $("account");
  const icon = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="3.5"/><path d="M5 20v-1.5a7 7 0 0 1 14 0V20"/></svg>';
  if (state.user) {
    const login = escapeHtml(state.user.login);
    holder.innerHTML =
      `<button type="button" id="accountButton" class="profile-trigger" aria-label="Профиль: ${login}" aria-haspopup="true" aria-controls="profileMenu" aria-expanded="false">${login[0].toUpperCase()}</button>` +
      `<div id="profileMenu" class="profile-menu" hidden><span class="profile-menu-name" title="${login}">${login}</span>` +
      '<button type="button" id="profileAreasButton">Мои области</button>' +
      '<button type="button" id="logoutButton">Выйти</button></div>';
    $("profileAreasButton").onclick = () => {
      closeProfileMenu();
      setRightPanel("archive");
    };
    $("logoutButton").onclick = () => {
      closeProfileMenu();
      logout();
    };
  } else {
    holder.innerHTML =
      `<button type="button" id="accountButton" class="profile-trigger" aria-label="Личный профиль" aria-haspopup="true" aria-controls="profileMenu" aria-expanded="false">${icon}</button>` +
      '<div id="profileMenu" class="profile-menu" hidden>' +
      '<button type="button" id="profileLoginButton">Войти</button>' +
      '<button type="button" id="profileRegisterButton">Создать аккаунт</button></div>';
    $("profileLoginButton").onclick = () => {
      closeProfileMenu();
      openAuth("login");
    };
    $("profileRegisterButton").onclick = () => {
      closeProfileMenu();
      openAuth("register");
    };
  }
  $("accountButton").onclick = toggleProfileMenu;
  $("watchStorageHint").hidden = Boolean(state.user);
  renderAreas();
}

function closeProfileMenu({ focus = false } = {}) {
  const menu = $("profileMenu");
  if (!menu || menu.hidden) return;
  menu.hidden = true;
  $("accountButton").setAttribute("aria-expanded", "false");
  if (focus) $("accountButton").focus();
}

function toggleProfileMenu() {
  const menu = $("profileMenu");
  menu.hidden = !menu.hidden;
  $("accountButton").setAttribute("aria-expanded", String(!menu.hidden));
  if (!menu.hidden) menu.querySelector("button")?.focus();
}

function openAuth(mode) {
  setAuthMode(mode);
  $("authError").textContent = "";
  $("authForm").reset();
  $("authDialog").showModal();
  $("authLogin").focus();
}

function setAuthMode(mode) {
  state.authMode = mode;
  const registering = mode === "register";
  $("authTitle").textContent = registering ? "Регистрация" : "Вход";
  $("authSubmit").textContent = registering ? "Создать учётную запись" : "Войти";
  $("authPassword").autocomplete = registering ? "new-password" : "current-password";
  $("tabLogin").setAttribute("aria-selected", String(!registering));
  $("tabRegister").setAttribute("aria-selected", String(registering));
}

async function submitAuth(event) {
  event.preventDefault();
  const login = $("authLogin").value.trim();
  const password = $("authPassword").value;
  const submit = $("authSubmit");
  const registering = state.authMode === "register";
  submit.disabled = true;
  $("authError").textContent = "";
  try {
    const path = registering ? "/api/auth/register" : "/api/auth/login";
    const data = await api(path, { method: "POST", body: JSON.stringify({ login, password }) });
    state.user = data.user;
    $("authDialog").close();
    state.areas = [];
    renderAccount();
    await loadAreas();
    toast(registering ? `Учётная запись ${data.user.login} создана` : `Вы вошли как ${data.user.login}`);
  } catch (error) {
    $("authError").textContent = error.message;
  } finally {
    submit.disabled = false;
  }
}

async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST", body: "{}" });
  } catch (error) {
    toast(error.message);
  }
  state.user = null;
  state.sessionAreas = loadSessionAreas();
  state.areas = state.sessionAreas;
  state.activeAreaId = null;
  renderAccount();
}

/* ============================================== Наблюдаемые области ==== */

function loadSessionAreas() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(SESSION_AREAS_KEY) || "[]");
    return Array.isArray(saved)
      ? saved.filter((area) => typeof area?.id === "string" &&
          /^session-[0-9a-f-]{36}$/.test(area.id) &&
          typeof area.name === "string" && typeof area.from === "string" &&
          typeof area.to === "string" && (area.geometry || area.bbox))
      : [];
  } catch {
    return [];
  }
}

function storeSessionAreas(areas) {
  sessionStorage.setItem(SESSION_AREAS_KEY, JSON.stringify(areas));
  state.sessionAreas = areas;
  state.areas = areas;
}

function localChanges(seen, summary, stamp) {
  if (!seen) return { first_visit: true, data_changed: true };
  const hotspots = summary.hotspot_count - seen.hotspots;
  const burns = summary.burn_count - seen.burns;
  const area = Math.round((summary.total_area_ha - seen.area_ha) * 100) / 100;
  return { first_visit: false, hotspots, burns, area_ha: area,
    data_changed: stamp !== seen.data_stamp,
    nothing_new: hotspots === 0 && burns === 0 && Math.abs(area) < 0.01 };
}

async function loadAreas() {
  if (!state.user) return;
  try {
    state.areas = (await api("/api/areas")).areas;
  } catch (error) {
    state.areas = [];
    toast(`Не удалось загрузить области: ${error.message}`);
  }
  renderAreas();
}

/** Геометрия карточки: сервер хранит полигон либо bbox-строку. */
function savedAreaGeometry(area) {
  let geometry = area.geometry;
  if (!geometry && area.bbox) {
    const bounds = (Array.isArray(area.bbox) ? area.bbox : String(area.bbox).split(",")).map(Number);
    if (bounds.length === 4 && bounds.every(Number.isFinite)) {
      const [west, south, east, north] = bounds;
      geometry = { type: "Polygon", coordinates: [[[west, south], [east, south],
        [east, north], [west, north], [west, south]]] };
    }
  }
  return geometry;
}

function savedAreaBody(area) {
  return { from: area.from, to: area.to,
    ...(area.geometry ? { geometry: area.geometry } : { bbox: area.bbox }) };
}

const areaPreviewMaps = new Map();
let areaPreviewObserver = null;

function clearAreaPreviews() {
  areaPreviewObserver?.disconnect();
  areaPreviewObserver = null;
  for (const preview of areaPreviewMaps.values()) preview.map.remove();
  areaPreviewMaps.clear();
}

function setAreaPreviewBasemap(preview, id) {
  const config = BASEMAPS[id];
  if (!preview.layer || BASEMAPS[preview.basemapId].url !== config.url) {
    preview.layer?.remove();
    preview.layer = L.tileLayer(config.url, {
      maxZoom: 19, maxNativeZoom: config.maxNativeZoom,
    }).addTo(preview.map);
  }
  preview.map.getPane("tilePane").dataset.palette = config.palette;
  preview.basemapId = id;
}

async function initAreaPreview(node) {
  if (areaPreviewMaps.has(node) || !node.isConnected || !node.clientWidth || !window.L) return;
  const area = state.areas[Number(node.dataset.preview)];
  const geometry = area && savedAreaGeometry(area);
  if (!geometry) return;
  let map;
  try {
    map = L.map(node, {
      zoomControl: false, attributionControl: false,
      dragging: false, scrollWheelZoom: false, doubleClickZoom: false,
      boxZoom: false, keyboard: false, touchZoom: false,
      zoomSnap: 0.25,
    });
    const preview = { map, layer: null, basemapId: null };
    setAreaPreviewBasemap(preview, state.basemapId);
    const shape = L.geoJSON(geometry, { style: {
      color: "#ff9065", weight: 2, fillColor: "#ff6a2b", fillOpacity: 0.06,
      interactive: false,
    } }).addTo(map);
    const bounds = shape.getBounds();
    if (!bounds.isValid()) throw new Error("Некорректная геометрия области");
    map.fitBounds(bounds, { padding: [9, 9], maxZoom: 14, animate: false });
    areaPreviewMaps.set(node, preview);
    try {
      const data = await api("/api/area-preview", {
        method: "POST", body: JSON.stringify(savedAreaBody(area)),
      });
      if (!node.isConnected || areaPreviewMaps.get(node) !== preview) return;
      const burnPreview = L.geoJSON(data.burns || [], { style: (feature) => ({
        color: feature.properties?.color || severityColor(feature.properties?.severity),
        weight: 1.4, fillOpacity: 0.78, interactive: false,
      }) }).addTo(map);
      burnPreview.eachLayer((layer) => {
        if (!layer.getBounds) return;
        const color = layer.feature?.properties?.color ||
          severityColor(layer.feature?.properties?.severity);
        L.circleMarker(layer.getBounds().getCenter(), {
          radius: 3.5, color, weight: 1.2, fillColor: color, fillOpacity: 0.5,
          interactive: false,
        }).addTo(map);
      });
      for (const feature of data.hotspots || []) {
        const [lon, lat] = feature.geometry.coordinates;
        L.circleMarker([lat, lon], { radius: 2.5, weight: 0.5, color: "#3a1009",
          fillColor: HOTSPOT_COLOR, fillOpacity: 1, interactive: false }).addTo(map);
      }
      const counts = node.closest("li")?.querySelector(".watch-counts");
      if (counts) counts.textContent = data.prediction_available
        ? `${data.hotspots?.length || 0} точек · ${data.burns?.length || 0} гарей`
        : "Прогноз появится при открытии";
    } catch {
      // Мини-карта остаётся доступной даже при временной ошибке выборки.
    }
  } catch {
    map?.remove();
    node.textContent = "◇";
    node.classList.add("watch-thumbnail-empty");
  }
}

function initVisibleAreaPreviews() {
  if ($("archivePanel").hidden) return;
  $("archivePanel").querySelectorAll(".watch-thumbnail").forEach(initAreaPreview);
}

function observeAreaPreviews() {
  const previews = $("archivePanel").querySelectorAll(".watch-thumbnail");
  if (!window.IntersectionObserver) {
    requestAnimationFrame(initVisibleAreaPreviews);
    return;
  }
  areaPreviewObserver = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      initAreaPreview(entry.target);
      areaPreviewObserver?.unobserve(entry.target);
    }
  }, { root: $("archivePanel"), rootMargin: "100px" });
  previews.forEach((node) => areaPreviewObserver.observe(node));
}

function renderAreas() {
  const list = $("watchList");
  clearAreaPreviews();
  $("saveAreaButton").disabled = !state.area;
  $("watchEmpty").hidden = state.areas.length > 0;
  list.innerHTML = state.areas
    .map((area, index) => {
      const active = String(area.id) === String(state.activeAreaId) ? ' class="active"' : "";
      const period = `${area.from.slice(8, 10)}.${area.from.slice(5, 7)}.${area.from.slice(2, 4)}` +
        ` — ${area.to.slice(8, 10)}.${area.to.slice(5, 7)}.${area.to.slice(2, 4)}`;
      return (
        `<li data-area="${area.id}"${active}>` +
        `<button type="button" class="watch-open" data-open="${area.id}">` +
        `<span class="watch-thumbnail" data-preview="${index}" aria-hidden="true"></span>` +
        `<span class="watch-card-text">` +
        `<span class="watch-name">${escapeHtml(area.name)}</span>` +
        `<span class="watch-meta">${escapeHtml(period)}</span>` +
        `<span class="watch-counts">Загрузка объектов…</span></span></button>` +
        `<button type="button" class="watch-remove quiet" data-remove="${area.id}" ` +
        'aria-label="Удалить область" title="Удалить">×</button></li>'
      );
    })
    .join("");
  list.querySelectorAll("[data-open]").forEach((node) => {
    node.onclick = () => openArea(node.dataset.open);
  });
  list.querySelectorAll("[data-remove]").forEach((node) => {
    node.onclick = () => removeArea(node.dataset.remove);
  });
  observeAreaPreviews();
  if (!$("regionSearchResults").hidden) renderRegionSearch();
}

function markActiveArea() {
  document.querySelectorAll("[data-area]").forEach((node) => {
    node.classList.toggle("active", node.dataset.area === String(state.activeAreaId));
  });
}

function openSaveDialog() {
  if (!state.area) {
    toast("Сначала выделите область на карте");
    return;
  }
  $("saveError").textContent = "";
  $("saveForm").reset();
  $("savePeriod").textContent =
    `Период: ${formatDate($("dateFrom").value)} — ${formatDate($("dateTo").value)}`;
  $("saveDialog").showModal();
  $("saveName").focus();
}

async function submitSave(event) {
  event.preventDefault();
  const name = $("saveName").value.trim();
  $("saveError").textContent = "";
  try {
    if (!name || name.length > 80) throw new Error("Название: от 1 до 80 символов");
    if (!state.user) {
      if (state.sessionAreas.length >= 50) throw new Error("Достигнут предел: 50 областей");
      const request = JSON.parse(requestBody({ name }));
      const area = {
        id: `session-${crypto.randomUUID()}`, name,
        from: request.from, to: request.to,
        geometry: request.geometry || null, bbox: request.bbox || null,
        checked_at: null, seen: null,
      };
      storeSessionAreas([area, ...state.sessionAreas]);
      $("saveDialog").close();
      renderAreas();
      toast(`Область «${name}» сохранена в этой вкладке`);
      return;
    }
    const data = await api("/api/areas", { method: "POST", body: requestBody({ name }) });
    state.areas.unshift(data.area);
    $("saveDialog").close();
    renderAreas();
    toast(`Область «${data.area.name}» сохранена`);
  } catch (error) {
    $("saveError").textContent = error.message;
  }
}

async function removeArea(id) {
  const area = state.areas.find((item) => String(item.id) === String(id));
  if (!area || !confirm(`Удалить область «${area.name}»?`)) return;
  try {
    if (state.user) {
      await api(`/api/areas/${id}`, { method: "DELETE" });
      state.areas = state.areas.filter((item) => String(item.id) !== String(id));
    } else {
      storeSessionAreas(state.sessionAreas.filter((item) => item.id !== id));
    }
    if (String(state.activeAreaId) === String(id)) state.activeAreaId = null;
    renderAreas();
  } catch (error) {
    toast(error.message);
  }
}

async function openArea(id) {
  const area = state.areas.find((item) => String(item.id) === String(id));
  if (!area) return;
  if (!state.user) {
    setArea(
      area.geometry ? { geometry: area.geometry } : { bbox: area.bbox.split(",").map(Number) },
      `сохранённая область «${area.name}»`,
    );
    $("dateFrom").value = area.from;
    $("dateTo").value = area.to;
    const result = await runQuery();
    if (!result) return;
    result.changes = localChanges(area.seen, result.summary, result.data_updated_at);
    area.checked_at = new Date().toISOString();
    area.seen = { hotspots: result.summary.hotspot_count, burns: result.summary.burn_count,
      area_ha: result.summary.total_area_ha, data_stamp: result.data_updated_at };
    try {
      storeSessionAreas([...state.sessionAreas]);
    } catch (error) {
      toast(`Не удалось запомнить просмотр: ${error.message}`);
    }
    state.activeAreaId = id;
    renderReport(result);
    renderAreas();
    setStatus(describeChanges(result.changes, result.summary));
    return;
  }
  document.body.classList.add("busy");
  setStatus(`Открываем «${area.name}»…`);
  showPane("reportLoading");
  try {
    const live = $("layerLive").checked ? "true" : "false";
    const result = await api(`/api/areas/${id}/status?live=${live}`);
    // Восстанавливаем геометрию и период: выгрузки должны работать по той же области.
    setArea(
      area.geometry ? { geometry: area.geometry } : { bbox: area.bbox.split(",").map(Number) },
      `сохранённая область «${area.name}»`,
    );
    $("dateFrom").value = area.from;
    $("dateTo").value = area.to;
    state.activeAreaId = id;
    renderResult(result);
    renderReport(result);
    updateLegendCounts();
    area.checked_at = new Date().toISOString();
    renderAreas();
    setStatus(describeChanges(result.changes, result.summary));
  } catch (error) {
    $("reportErrorText").textContent = error.message;
    showPane("reportError");
    setStatus(error.message, true);
    if (error.status === 401) {
      state.user = null;
      renderAccount();
    }
  } finally {
    document.body.classList.remove("busy");
  }
}

/** Словесное описание дельты для строки состояния. */
function describeChanges(changes, summary) {
  if (!changes || changes.first_visit) {
    return (
      `Найдено: ${summary.hotspot_count} ` +
      `${plural(summary.hotspot_count, "термоточка", "термоточки", "термоточек")}, ` +
      `${summary.burn_count} ${plural(summary.burn_count, "контур", "контура", "контуров")}`
    );
  }
  if (changes.nothing_new) return "С прошлого раза ничего не изменилось";
  return `С прошлого раза: ${changeParts(changes).join(", ")}`;
}

function changeParts(changes) {
  const parts = [];
  if (changes.hotspots) {
    parts.push(
      `${signed(changes.hotspots)} ${plural(Math.abs(changes.hotspots), "термоточка", "термоточки", "термоточек")}`,
    );
  }
  if (changes.burns) {
    parts.push(
      `${signed(changes.burns)} ${plural(Math.abs(changes.burns), "контур", "контура", "контуров")}`,
    );
  }
  if (Math.abs(changes.area_ha) >= 0.01) parts.push(`${signed(changes.area_ha, 2)} га гари`);
  return parts;
}

function renderChanges(changes) {
  const banner = $("changesBanner");
  if (!changes || changes.first_visit) {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;
  banner.classList.toggle("quiet-state", Boolean(changes.nothing_new));
  if (changes.nothing_new) {
    $("changesTitle").textContent = "Без изменений";
    $("changesDetail").textContent = changes.data_changed
      ? "Данные обновились, но в этой области ничего нового."
      : `Данные не обновлялись с ${formatDateTime(state.dataUpdatedAt)}.`;
    return;
  }
  $("changesTitle").textContent = "Что изменилось";
  $("changesDetail").textContent = `${changeParts(changes).join(" · ")} с прошлого просмотра`;
}

function signed(value, digits = 0) {
  return `${value > 0 ? "+" : "−"}${number(Math.abs(value), digits)}`;
}

function formatDateTime(iso) {
  if (!iso) return "—";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/* ------------------------------------------------------------- Запуск */

const PRESET_LABELS = {
  satellite: { all: "Рекомендуемый", month: "Месяц назад", year: "Год назад" },
  files: { all: "Все данные", month: "Последний месяц", year: "Последний год" },
};

function applyPreset(preset) {
  const day = 86400000;
  const shift = (iso, days) => new Date(new Date(iso).getTime() - days * day).toISOString().slice(0, 10);
  let ranges;
  if (state.sourceKind === "satellite") {
    // Период события: снимок «после» должен уже существовать, поэтому всё в прошлом.
    const { from, to } = state.suggestedPeriod;
    ranges = {
      all: [from, to],
      month: [shift(from, 30), shift(to, 30)],
      year: [shift(from, 365), shift(to, 365)],
    };
  } else {
    const range = state.dataRange?.from ? state.dataRange : null;
    if (!range) return;
    ranges = {
      all: [range.from, range.to],
      month: [shift(range.to, 30) < range.from ? range.from : shift(range.to, 30), range.to],
      year: [shift(range.to, 365) < range.from ? range.from : shift(range.to, 365), range.to],
    };
  }
  const [from, to] = ranges[preset] || ranges.all;
  $("dateFrom").value = from;
  $("dateTo").value = to;
  if (state.result) invalidateAreaResult();
}

function showSourceNotice(health) {
  const node = $("sourceNotice");
  let text = "";
  if (health?.source?.kind === "satellite" && !health.prediction?.enabled) {
    text = `Прогноз по снимкам недоступен: ${health.prediction?.detail || "источник не настроен"}. ` +
      "Без реального источника и рабочей модели сервис ничего не показывает.";
  } else if (health?.source?.kind === "files" && !health.counts?.hotspots && !health.counts?.burns) {
    text = "В каталоге подготовленных прогнозов нет объектов.";
  }
  node.textContent = text;
  node.hidden = !text;
}

async function loadHealth({ initial = false } = {}) {
  try {
    const health = await api("/api/health");
    if (health.status !== "ok") throw new Error(health.detail || "источник данных недоступен");
    state.severity = health.severity?.length ? health.severity : SEVERITY_FALLBACK;
    // Без ключа провайдера карточку не показываем: подменять разбор шаблоном нельзя.
    state.llmEnabled = health.llm?.enabled === true;
    $("llmCard").hidden = !state.llmEnabled;
    state.limits = health.limits || state.limits;
    state.dataRange = health.date_range;
    state.dataUpdatedAt = health.source?.updated_at || null;
    state.sourceKind = health.source.kind;
    state.suggestedPeriod = health.suggested_period || null;
    $("satelliteHint").hidden = state.sourceKind !== "satellite";
    showSourceNotice(health);
    applyLiveSupport(health.live);
    const labels = PRESET_LABELS[state.sourceKind] || PRESET_LABELS.files;
    document.querySelectorAll("[data-preset]").forEach((button) => {
      button.textContent = labels[button.dataset.preset];
    });
    if (initial && state.sourceKind === "satellite" && state.suggestedPeriod) applyPreset("all");
    if (health.date_range?.from) {
      $("dateFrom").min = health.date_range.from;
      $("dateTo").max = health.date_range.to;
      if (initial) {
        $("dateFrom").value = health.date_range.from;
        $("dateTo").value = health.date_range.to;
      }
    }
  } catch (error) {
    state.severity = SEVERITY_FALLBACK;
    state.llmEnabled = false;
    $("llmCard").hidden = true;
    applyLiveSupport(null);
    toast(`Не удалось получить состояние сервиса: ${error.message}`);
  }
  renderLegend();
}

function bindControls() {
  bindMapTimeline();
  $("regionSearchInput").addEventListener("input", () => queuePlaceSearch());
  $("regionSearchInput").addEventListener("focus", renderRegionSearch);
  $("regionSearchButton").onclick = () => {
    $("regionSearchInput").focus();
    queuePlaceSearch({ immediate: true });
  };
  $("regionSearchInput").addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeRegionSearch();
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if ($("regionSearchResults").hidden) renderRegionSearch();
      if (!state.searchMatches.length) return;
      const direction = event.key === "ArrowDown" ? 1 : -1;
      const current = state.searchIndex < 0 && direction < 0 ? 0 : state.searchIndex;
      highlightRegionSearchResult((current + direction + state.searchMatches.length) % state.searchMatches.length);
    }
    if (event.key === "Enter") {
      event.preventDefault();
      if (state.searchIndex >= 0 && state.searchMatches[state.searchIndex]) {
        selectRegionSearchResult(state.searchMatches[state.searchIndex]);
      } else if (state.placesFor === searchKey(event.target.value) && state.searchMatches.length) {
        selectRegionSearchResult(state.searchMatches[0]);
      } else {
        queuePlaceSearch({ immediate: true });
      }
    }
  });
  document.querySelectorAll("[data-tool]").forEach((button) => {
    button.onclick = () => (state.drawing === button.dataset.tool ? cancelDrawing() : startDrawing(button.dataset.tool));
  });
  $("useViewButton").onclick = useCurrentView;
  $("clearAreaButton").onclick = clearArea;
  $("drawCancel").onclick = cancelDrawing;
  $("drawFinish").onclick = finishPolygon;
  $("runButton").onclick = runQuery;
  ["dateFrom", "dateTo"].forEach((id) => {
    $(id).addEventListener("change", () => { if (state.result) invalidateAreaResult(); });
  });
  $("reportToggle").onclick = () => setRightPanel($("reportPanel").hidden ? "report" : null);
  $("archiveToggle").onclick = () => setRightPanel($("archivePanel").hidden ? "archive" : null);
  $("retryButton").onclick = runQuery;
  $("toastClose").onclick = closeToast;
  document.querySelectorAll("[data-preset]").forEach((button) => {
    button.onclick = () => applyPreset(button.dataset.preset);
  });
  $("compareWithoutFire").onclick = () => runQuery({ comparison: true });
  document.querySelectorAll("[data-export]").forEach((button) => {
    button.onclick = () => download(button.dataset.export);
  });
  $("layerUnreliable").onchange = () => { if (state.result) renderMapFeatures(); };
  ["layerHotspots", "layerBurns", "layerUnobserved", "layerArea"].forEach((id) => {
    $(id).onchange = applyLayerVisibility;
  });
  // Живой слой приходит с сервера, поэтому включение требует нового запроса.
  $("layerLive").onchange = () => {
    if ($("layerLive").checked && !state.liveAvailable && state.area) {
      state.activeAreaId ? openArea(state.activeAreaId) : runQuery();
      return;
    }
    applyLayerVisibility();
  };
  $("saveAreaButton").onclick = openSaveDialog;
  $("tabLogin").onclick = () => setAuthMode("login");
  $("tabRegister").onclick = () => setAuthMode("register");
  $("authForm").onsubmit = submitAuth;
  $("saveForm").onsubmit = submitSave;
  renderAccount();
  bindDashboard();
  document.addEventListener("pointerdown", (event) => {
    if (!$("account").contains(event.target)) closeProfileMenu();
    if (!$("regionSearch").contains(event.target)) closeRegionSearch();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("profileMenu")?.hidden) {
      closeProfileMenu({ focus: true });
      return;
    }
    if (event.key === "Escape" && state.drawing) cancelDrawing();
    if (event.key === "Enter" && state.drawing === "polygon") finishPolygon();
    if (event.key !== "Delete" || !state.area || state.drawing || event.repeat ||
        event.altKey || event.ctrlKey || event.metaKey || event.shiftKey ||
        document.querySelector("dialog[open]") ||
        (event.target instanceof Element && event.target.closest("input, textarea, select, [contenteditable]"))) return;
    event.preventDefault();
    clearArea();
  });
}

/** Живой слой без ключа FIRMS выключен и подписан причиной, а не молча сломан. */
function applyLiveSupport(live) {
  const row = $("liveSwitchRow");
  const input = $("layerLive");
  const enabled = Boolean(live?.enabled);
  input.disabled = !enabled;
  row.classList.toggle("unavailable", !enabled);
  row.title = enabled
    ? `${live.provider} · глубина ${live.day_range} сут. · ${live.note}`
    : `Живой слой выключен: ${live?.detail || "не задан ключ FIRMS_MAP_KEY"}`;
  if (!enabled) input.checked = false;
}

async function loadAccount() {
  state.sessionAreas = loadSessionAreas();
  try {
    state.user = (await api("/api/auth/me")).user;
  } catch {
    state.user = null;
  }
  if (!state.user) state.areas = state.sessionAreas;
  renderAccount();
  if (state.user) await loadAreas();
}

async function boot() {
  bindControls();
  if (!initMap()) return;
  await loadHealth({ initial: true });
  loadDemoCases();
  await loadAccount();
  await loadRegions();
  showPane("reportIdle");
  window.initMonitor?.();
}

boot();
