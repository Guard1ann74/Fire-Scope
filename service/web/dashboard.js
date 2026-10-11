/* Расширенная аналитика поверх последнего ответа /api/query.

   Всё считается на клиенте из state.result: ответ уже содержит каждую
   термоточку и каждый контур со всеми атрибутами. Только краткое текстовое
   пояснение запрашивается у серверного LLM API. Графики — инлайновый SVG без библиотек:
   меньше зависимостей и палитра берётся прямо из CSS-переменных темы. */

const DASH = {
  /** Шаг оси времени: "month" или "year". Подбирается автоматически, переключается руками. */
  scale: "month",
  scaleLocked: false,
  llmSignature: null,
  llmSequence: 0,
  size: "normal",
};

const DASH_SIZES = ["compact", "normal", "expanded"];

const MONTHS_SHORT = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
const MONTHS_FULL = [
  "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
  "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
];

/* ------------------------------------------------------------- Утилиты */

/** Короткая запись для осей: 12 400 → «12,4 тыс.». */
function compact(value) {
  if (!Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  if (abs >= 1e6) return `${(value / 1e6).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} млн`;
  if (abs >= 1e4) return `${(value / 1e3).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} тыс.`;
  return value.toLocaleString("ru-RU", { maximumFractionDigits: abs >= 100 ? 0 : 1 });
}

/** Верхняя граница оси: ближайшее «круглое» число не меньше значения. */
function niceMax(value) {
  if (!(value > 0)) return 1;
  const base = 10 ** Math.floor(Math.log10(value));
  const scaled = value / base;
  const step = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 2.5 ? 2.5 : scaled <= 5 ? 5 : 10;
  return step * base;
}

function sum(items, pick) {
  return items.reduce((total, item) => total + (Number(pick(item)) || 0), 0);
}

function countBy(items, pick) {
  const map = new Map();
  items.forEach((item) => {
    const key = pick(item);
    if (key === undefined || key === null || key === "") return;
    map.set(key, (map.get(key) || 0) + 1);
  });
  return map;
}

function monthRange(fromIso, toIso) {
  const out = [];
  let year = Number(fromIso.slice(0, 4));
  let month = Number(fromIso.slice(5, 7));
  const endYear = Number(toIso.slice(0, 4));
  const endMonth = Number(toIso.slice(5, 7));
  while (year < endYear || (year === endYear && month <= endMonth)) {
    out.push(`${year}-${String(month).padStart(2, "0")}`);
    month += 1;
    if (month > 12) {
      month = 1;
      year += 1;
    }
  }
  return out;
}

function yearRange(fromIso, toIso) {
  const out = [];
  for (let year = Number(fromIso.slice(0, 4)); year <= Number(toIso.slice(0, 4)); year += 1) {
    out.push(String(year));
  }
  return out;
}

function bucketLabel(key, scale) {
  if (scale === "year") return key;
  const [year, month] = key.split("-");
  return `${MONTHS_SHORT[Number(month) - 1]} ${year.slice(2)}`;
}

function bucketTitle(key, scale) {
  if (scale === "year") return `${key} год`;
  const [year, month] = key.split("-");
  return `${MONTHS_FULL[Number(month) - 1]} ${year}`;
}

/** Плоские свойства объектов последнего ответа. */
function dashRows() {
  const result = state.result;
  return {
    query: result.query,
    summary: result.summary,
    hotspots: result.hotspots.features.map((feature) => feature.properties),
    burns: result.burns.features.map((feature) => feature.properties),
  };
}

function llmStats(rows) {
  const months = countBy(rows.hotspots, (item) => item.date?.slice(0, 7));
  const years = new Map();
  rows.hotspots.forEach((point) => {
    const year = Number(point.date.slice(0, 4));
    if (!years.has(year)) years.set(year, { year, hotspot_count: 0, burn_area_ha: 0 });
    years.get(year).hotspot_count += 1;
  });
  rows.burns.forEach((burn) => {
    const year = Number(burn.date.slice(0, 4));
    if (!years.has(year)) years.set(year, { year, hotspot_count: 0, burn_area_ha: 0 });
    years.get(year).burn_area_ha += Number(burn.area_ha) || 0;
  });
  let peakMonth = null;
  let peakHotspots = 0;
  months.forEach((count, month) => {
    if (count > peakHotspots) {
      peakMonth = month;
      peakHotspots = count;
    }
  });
  return {
    period_from: rows.summary.period.from,
    period_to: rows.summary.period.to,
    area_ha: rows.query.area_ha,
    hotspot_count: rows.summary.hotspot_count,
    burn_count: rows.summary.burn_count,
    total_area_ha: rows.summary.total_area_ha,
    by_severity: rows.summary.by_severity.map(({ severity, count, area_ha }) => ({ severity, count, area_ha })),
    frp_total_mw: Math.round(sum(rows.hotspots, (item) => item.frp_mw) * 100) / 100,
    peak_month: peakMonth,
    peak_hotspots: peakHotspots,
    monthly_hotspots: Array.from({ length: 12 }, (_, index) =>
      sum(rows.hotspots, (point) => Number(point.date.slice(5, 7)) === index + 1 ? 1 : 0)),
    yearly: [...years.values()].sort((a, b) => a.year - b.year).map((item) => ({
      ...item, burn_area_ha: Math.round(item.burn_area_ha * 100) / 100,
    })),
  };
}

async function loadLlmAnalysis(force = false) {
  if (!state.llmEnabled || !state.result || state.result.summary.empty) return;
  const stats = llmStats(dashRows());
  const signature = JSON.stringify(stats);
  if (!force && DASH.llmSignature === signature) return;
  DASH.llmSignature = signature;
  const sequence = ++DASH.llmSequence;
  const output = $("llmAnalysis");
  const retry = $("llmRetry");
  retry.hidden = true;
  output.textContent = "Формируем пояснение по результатам…";
  try {
    const answer = await api("/api/analysis/llm", { method: "POST", body: signature });
    if (sequence !== DASH.llmSequence) return;
    output.textContent = answer.text;
  } catch (error) {
    if (sequence !== DASH.llmSequence) return;
    output.textContent = error.message;
    retry.hidden = false;
  }
}

/** Диапазон, который реально закрыт наблюдениями (а не весь запрошенный период). */
function observedSpan(summary) {
  const observed = summary.observed_range;
  return {
    from: observed.from || summary.period.from,
    to: observed.to || summary.period.to,
  };
}

const svgOpen = (w, h, extra = "") =>
  `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet" role="img" ${extra}>`;

/** Узкий ли контейнер графика. viewBox масштабируется под ширину карточки, и на
    телефоне подписи в 9,5 единиц превращаются в нечитаемые 4 пикселя — поэтому
    для узких экранов берём вдвое меньший холст, а не сжимаем широкий. */
function isNarrow(id, limit = 520) {
  const node = $(id);
  return node ? node.clientWidth > 0 && node.clientWidth < limit : false;
}

/* ================================================== Плитки показателей */

function renderKpi(rows) {
  const s = rows.summary;
  const strong = s.by_severity.find((item) => item.severity === 3);
  const frpTotal = sum(rows.hotspots, (item) => item.frp_mw);
  const byBucket = countBy(rows.hotspots, (item) => item.date.slice(0, 7));
  let peakKey = null;
  let peakValue = 0;
  byBucket.forEach((value, key) => {
    if (value > peakValue) {
      peakValue = value;
      peakKey = key;
    }
  });
  const days = new Set(rows.hotspots.map((item) => item.date)).size;
  const avgArea = s.burn_count ? s.total_area_ha / s.burn_count : 0;
  const hottest = rows.hotspots
    .filter((item) => Number.isFinite(item.frp_mw))
    .reduce((best, item) => (!best || item.frp_mw > best.frp_mw ? item : best), null);

  const tiles = [
    {
      label: "Площадь гари",
      value: `${compact(s.total_area_ha)}`,
      unit: "га",
      note: `${integer(s.burn_count)} ${plural(s.burn_count, "контур", "контура", "контуров")}`,
      accent: true,
    },
    {
      label: "Термоточек",
      value: integer(s.hotspot_count),
      unit: "",
      note: `${integer(days)} ${plural(days, "день", "дня", "дней")} с наблюдениями`,
    },
    {
      label: "Суммарная мощность",
      value: compact(frpTotal),
      unit: "МВт",
      note: rows.hotspots.length ? `в среднем ${number(frpTotal / rows.hotspots.length, 1)} МВт на точку` : "нет данных FRP",
    },
    {
      label: "Сильное поражение",
      value: strong ? `${(strong.share * 100).toFixed(1)}` : "0",
      unit: "%",
      note: strong ? `${compact(strong.area_ha)} га из ${compact(s.total_area_ha)}` : "нет контуров 3-й степени",
      danger: strong ? strong.share >= 0.5 : false,
    },
    {
      label: "Средний контур",
      value: compact(avgArea),
      unit: "га",
      note: "площадь одной гари",
    },
    {
      label: "Пик активности",
      value: peakKey ? bucketLabel(peakKey, "month") : "—",
      unit: "",
      note: peakKey ? `${integer(peakValue)} ${plural(peakValue, "точка", "точки", "точек")} за месяц` : "нет термоточек",
    },
    {
      label: "Самый мощный очаг",
      value: hottest ? number(hottest.frp_mw, 1) : "—",
      unit: hottest ? "МВт" : "",
      note: hottest ? `${formatDate(hottest.date)} · ${hottest.satellite || "спутник не указан"}` : "нет данных FRP",
    },
    {
      label: "Спутники",
      value: String(s.satellites.length || "—"),
      unit: "",
      note: s.satellites.length ? s.satellites.join(", ") : "нет данных",
    },
  ];

  $("dashKpi").innerHTML = tiles
    .map(
      (tile) =>
        `<div class="kpi${tile.accent ? " kpi-accent" : ""}${tile.danger ? " kpi-danger" : ""}">` +
        `<span class="kpi-label">${escapeHtml(tile.label)}</span>` +
        `<strong class="kpi-value">${escapeHtml(tile.value)}` +
        (tile.unit ? `<span class="kpi-unit">${escapeHtml(tile.unit)}</span>` : "") +
        `</strong><span class="kpi-note">${escapeHtml(tile.note)}</span></div>`,
    )
    .join("");
}

/* ============================================ График 1 · динамика во времени */

function renderTimeline(rows) {
  const span = observedSpan(rows.summary);
  let scale = DASH.scale;
  if (!DASH.scaleLocked) {
    scale = monthRange(span.from, span.to).length > 36 ? "year" : "month";
    DASH.scale = scale;
  }
  // Пятьдесят с лишним столбцов в 780 пикселей не читаются — переходим на годы.
  const keys = scale === "year" ? yearRange(span.from, span.to) : monthRange(span.from, span.to);
  document.querySelectorAll("[data-scale]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.scale === scale));
  });

  const cut = (iso) => (scale === "year" ? iso.slice(0, 4) : iso.slice(0, 7));
  const severities = state.severity.length ? state.severity : SEVERITY_FALLBACK;
  const stacks = new Map(keys.map((key) => [key, new Map(severities.map((item) => [item.severity, 0]))]));
  const hits = new Map(keys.map((key) => [key, 0]));

  rows.burns.forEach((burn) => {
    const slot = stacks.get(cut(burn.date));
    if (!slot) return;
    slot.set(burn.severity, (slot.get(burn.severity) || 0) + burn.area_ha);
  });
  rows.hotspots.forEach((point) => {
    const key = cut(point.date);
    if (hits.has(key)) hits.set(key, hits.get(key) + 1);
  });

  const totals = keys.map((key) => sum([...stacks.get(key).values()], (value) => value));
  const maxArea = niceMax(Math.max(...totals, 0));
  // Правая ось считает штуки, поэтому её потолок кратен числу делений — иначе
  // подписи выходят дробными («3,8 термоточки»).
  const maxHits = Math.max(4, Math.ceil(niceMax(Math.max(...hits.values(), 0)) / 4) * 4);

  // Ширина холста равна фактической ширине карточки: при фиксированных 800
  // единицах viewBox растягивался вместе с высотой и график выходил вдвое выше
  // нужного. Масштаб 1:1 держит высоту ровно H и не раздувает подписи.
  const available = $("chartTimeline").clientWidth;
  const narrow = available > 0 && available < 520;
  const W = narrow ? 380 : Math.min(Math.max(Math.round(available) || 880, 560), 1180);
  const H = narrow ? 240 : 270;
  const pad = narrow ? { l: 40, r: 34, t: 26, b: 30 } : { l: 52, r: 52, t: 30, b: 34 };
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;
  const slotW = plotW / keys.length;
  const barW = Math.max(3, Math.min(narrow ? 18 : 26, slotW * 0.6));
  const xAt = (index) => pad.l + slotW * (index + 0.5);
  const yArea = (value) => pad.t + plotH - (value / maxArea) * plotH;
  const yHits = (value) => pad.t + plotH - (value / maxHits) * plotH;

  const ticks = narrow ? [0, 0.5, 1] : [0, 0.25, 0.5, 0.75, 1];
  const gap = narrow ? 6 : 10;
  const gridLines = ticks
    .map((ratio) => {
      const y = pad.t + plotH - ratio * plotH;
      return (
        `<line class="grid" x1="${pad.l}" y1="${y.toFixed(1)}" x2="${W - pad.r}" y2="${y.toFixed(1)}" />` +
        `<text class="axis axis-left" x="${pad.l - gap}" y="${(y + 3.5).toFixed(1)}">${compact(maxArea * ratio)}</text>` +
        `<text class="axis axis-right" x="${W - pad.r + gap}" y="${(y + 3.5).toFixed(1)}">${integer(Math.round(maxHits * ratio))}</text>`
      );
    })
    .join("");

  const bars = keys
    .map((key, index) => {
      const slot = stacks.get(key);
      let cursor = 0;
      const pieces = severities
        .map((severity) => {
          const value = slot.get(severity.severity) || 0;
          if (value <= 0) return "";
          const top = yArea(cursor + value);
          const height = yArea(cursor) - top;
          cursor += value;
          return (
            `<rect class="bar-mark" x="${(xAt(index) - barW / 2).toFixed(1)}" y="${top.toFixed(1)}" ` +
            `width="${barW.toFixed(1)}" height="${Math.max(height, 0.8).toFixed(1)}" rx="${Math.min(3, barW / 3).toFixed(1)}" ` +
            `fill="${severity.color}"><title>${escapeHtml(bucketTitle(key, scale))} · ${escapeHtml(severity.label)}: ` +
            `${number(value, 1)} га</title></rect>`
          );
        })
        .join("");
      return pieces;
    })
    .join("");

  const linePoints = keys.map((key, index) => `${xAt(index).toFixed(1)},${yHits(hits.get(key)).toFixed(1)}`);
  const line = linePoints.length > 1 ? `<polyline class="spark" points="${linePoints.join(" ")}" />` : "";
  const dots = keys
    .map((key, index) => {
      const value = hits.get(key);
      return (
        `<circle class="spark-dot${value ? "" : " zero"}" cx="${xAt(index).toFixed(1)}" ` +
        `cy="${yHits(value).toFixed(1)}" r="${value ? 3.4 : 2.4}">` +
        `<title>${escapeHtml(bucketTitle(key, scale))}: ${integer(value)} ` +
        `${plural(value, "термоточка", "термоточки", "термоточек")}</title></circle>`
      );
    })
    .join("");

  // Подписи оси X прореживаются; последнюю рисуем, только если она не налезет
  // на предыдущую.
  const every = Math.ceil(keys.length / (narrow ? 6 : 14));
  const last = keys.length - 1;
  const labels = keys
    .map((key, index) => {
      const regular = index % every === 0;
      const tail = index === last && last % every !== 0 && last - Math.floor(last / every) * every > every * 0.6;
      if (!regular && !tail) return "";
      return `<text class="axis axis-x" x="${xAt(index).toFixed(1)}" y="${H - pad.b + (narrow ? 16 : 20)}">${escapeHtml(bucketLabel(key, scale))}</text>`;
    })
    .join("");

  $("chartTimeline").innerHTML =
    svgOpen(
      W,
      H,
      `class="${narrow ? "canvas-scaled" : "canvas-1to1"}" ` +
        'aria-label="Площадь гари по степеням и число термоточек по времени"',
    ) +
    gridLines +
    `<line class="axis-line" x1="${pad.l}" y1="${pad.t + plotH}" x2="${W - pad.r}" y2="${pad.t + plotH}" />` +
    bars +
    line +
    dots +
    labels +
    `<text class="axis-title" x="${pad.l - gap}" y="${pad.t - (narrow ? 12 : 16)}">га</text>` +
    // На узком холсте подпись справа не помещается за делением — прижимаем её
    // к правому краю холста.
    `<text class="axis-title ${narrow ? "axis-end" : "axis-right"}" ` +
    `x="${narrow ? W - 2 : W - pad.r + gap}" y="${pad.t - (narrow ? 12 : 16)}">точек</text>` +
    "</svg>" +
    legendRow([
      ...severities.map((item) => ({ color: item.color, label: `${item.severity} — ${item.label}` })),
      { color: "var(--accent)", label: "термоточки", line: true },
    ]);
}

function legendRow(items) {
  return (
    '<div class="chart-legend">' +
    items
      .map(
        (item) =>
          `<span><i class="${item.line ? "line" : ""}" style="background:${item.color}"></i>${escapeHtml(item.label)}</span>`,
      )
      .join("") +
    "</div>"
  );
}

/* ================================================ График 2 · сезонность */

function renderSeason(rows) {
  const counts = new Array(12).fill(0);
  const areas = new Array(12).fill(0);
  rows.hotspots.forEach((point) => (counts[Number(point.date.slice(5, 7)) - 1] += 1));
  rows.burns.forEach((burn) => (areas[Number(burn.date.slice(5, 7)) - 1] += burn.area_ha));

  const W = 400;
  const H = 210;
  const pad = { l: 30, r: 10, t: 14, b: 26 };
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;
  const max = niceMax(Math.max(...counts, 0));
  const slotW = plotW / 12;
  const barW = slotW * 0.62;

  const grid = [0, 0.5, 1]
    .map((ratio) => {
      const y = pad.t + plotH - ratio * plotH;
      return (
        `<line class="grid" x1="${pad.l}" y1="${y}" x2="${W - pad.r}" y2="${y}" />` +
        `<text class="axis axis-left" x="${pad.l - 8}" y="${y + 3.5}">${compact(max * ratio)}</text>`
      );
    })
    .join("");

  const bars = counts
    .map((value, index) => {
      const height = (value / max) * plotH;
      const x = pad.l + slotW * index + (slotW - barW) / 2;
      const y = pad.t + plotH - height;
      // Цвет столбца тянется от жёлтого к красному вместе с долей месяца в максимуме.
      const heat = max ? value / max : 0;
      const color = heat > 0.66 ? "var(--sev-3)" : heat > 0.33 ? "var(--sev-2)" : "var(--sev-1)";
      return (
        `<rect class="bar-mark" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" ` +
        `height="${Math.max(height, value ? 2 : 0).toFixed(1)}" rx="3" fill="${color}" opacity="${value ? 1 : 0.15}">` +
        `<title>${MONTHS_FULL[index]}: ${integer(value)} ${plural(value, "термоточка", "термоточки", "термоточек")}` +
        (areas[index] ? `, ${number(areas[index], 0)} га гари` : "") +
        "</title></rect>"
      );
    })
    .join("");

  // На узкой карточке двенадцать подписей сливаются — оставляем каждую вторую.
  const everyOther = isNarrow("chartSeason", 360);
  const labels = MONTHS_SHORT.map((name, index) =>
    everyOther && index % 2
      ? ""
      : `<text class="axis axis-x" x="${(pad.l + slotW * (index + 0.5)).toFixed(1)}" y="${H - pad.b + 17}">${name}</text>`,
  ).join("");

  $("chartSeason").innerHTML =
    svgOpen(W, H, 'aria-label="Число термоточек по месяцам года"') +
    grid +
    `<line class="axis-line" x1="${pad.l}" y1="${pad.t + plotH}" x2="${W - pad.r}" y2="${pad.t + plotH}" />` +
    bars +
    labels +
    "</svg>";
}

/* =========================================== График 3 · степени поражения */

function renderSeverityDonut(rows) {
  const items = rows.summary.by_severity.filter((item) => item.area_ha > 0);
  const total = rows.summary.total_area_ha;
  // Квадратный viewBox: кольцо занимает всю свою колонку, без пустого поля справа.
  const W = 210;
  const H = 210;
  const cx = W / 2;
  const cy = H / 2;
  const R = 88;
  const r = 60;

  let slices;
  if (!items.length) {
    slices = `<circle cx="${cx}" cy="${cy}" r="${(R + r) / 2}" fill="none" stroke="var(--line)" stroke-width="${R - r}" />`;
  } else if (items.length === 1) {
    slices =
      `<circle cx="${cx}" cy="${cy}" r="${(R + r) / 2}" fill="none" stroke="${items[0].color}" ` +
      `stroke-width="${R - r}"><title>${escapeHtml(items[0].label)}: ${number(items[0].area_ha)} га</title></circle>`;
  } else {
    let angle = -Math.PI / 2;
    slices = items
      .map((item) => {
        const end = angle + (item.area_ha / total) * Math.PI * 2;
        const path = ringPath(cx, cy, R, r, angle, end);
        angle = end;
        return (
          `<path class="slice" d="${path}" fill="${item.color}">` +
          `<title>${escapeHtml(item.label)}: ${number(item.area_ha)} га · ${(item.share * 100).toFixed(1)} %</title></path>`
        );
      })
      .join("");
  }

  const rowsHtml = rows.summary.by_severity
    .map(
      (item) =>
        `<tr class="${item.count ? "" : "zero"}"><td><span class="sev-cell"><i style="background:${item.color}"></i>` +
        `${item.severity} · ${escapeHtml(item.label)}</span></td>` +
        `<td>${integer(item.count)}</td><td>${number(item.area_ha, 0)}</td>` +
        `<td>${(item.share * 100).toFixed(1)} %</td></tr>`,
    )
    .join("");

  $("chartDonut").innerHTML =
    svgOpen(W, H, 'aria-label="Доли площади по степеням поражения"') +
    slices +
    `<text class="donut-value" x="${cx}" y="${cy + 2}">${escapeHtml(compact(total))}</text>` +
    `<text class="donut-caption" x="${cx}" y="${cy + 20}">га всего</text>` +
    "</svg>" +
    '<table class="severity-table donut-table"><thead><tr>' +
    "<th>Степень</th><th>Контуров</th><th>Га</th><th>Доля</th></tr></thead>" +
    `<tbody>${rowsHtml}</tbody></table>`;
}

function ringPath(cx, cy, R, r, a0, a1) {
  const large = a1 - a0 > Math.PI ? 1 : 0;
  const at = (radius, angle) => [
    (cx + radius * Math.cos(angle)).toFixed(2),
    (cy + radius * Math.sin(angle)).toFixed(2),
  ];
  const [x0, y0] = at(R, a0);
  const [x1, y1] = at(R, a1);
  const [x2, y2] = at(r, a1);
  const [x3, y3] = at(r, a0);
  return `M${x0} ${y0} A${R} ${R} 0 ${large} 1 ${x1} ${y1} L${x2} ${y2} A${r} ${r} 0 ${large} 0 ${x3} ${y3} Z`;
}

/* ============================================ График 4 · спутники и качество */

function renderSatellites(rows) {
  const bySat = [...countBy(rows.hotspots, (item) => item.satellite || "не указан")]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6);
  const total = rows.hotspots.length || 1;

  $("chartSatellites").innerHTML =
    barList(
      bySat.map(([name, count]) => ({
        label: name,
        value: count,
        share: count / total,
        text: `${integer(count)} · ${((count / total) * 100).toFixed(0)} %`,
        color: "var(--accent)",
      })),
      "Нет термоточек в выборке",
    ) + confidenceBar(rows);
}

/** Достоверность классификации — единая полоса с долями. */
function confidenceBar(rows) {
  const order = ["high", "nominal", "low"];
  const titles = { high: "высокая", nominal: "номинальная", low: "низкая" };
  const colors = { high: "var(--accent)", nominal: "#b8552a", low: "#6d4433" };
  const counts = countBy(rows.hotspots, (item) => String(item.confidence || "").toLowerCase());
  const known = order.filter((key) => counts.get(key));
  const other = [...counts.keys()].filter((key) => !order.includes(key));
  if (!known.length && !other.length) return "";
  const total = rows.hotspots.length || 1;
  const parts = [
    ...known.map((key) => ({ label: titles[key], value: counts.get(key), color: colors[key] })),
    ...other.map((key) => ({ label: key, value: counts.get(key), color: "var(--faint)" })),
  ];
  return (
    '<p class="chart-sub">Достоверность классификации</p><div class="stack-bar">' +
    parts
      .map(
        (part) =>
          `<span style="width:${((part.value / total) * 100).toFixed(2)}%;background:${part.color}" ` +
          `title="${escapeHtml(part.label)}: ${integer(part.value)} из ${integer(total)}"></span>`,
      )
      .join("") +
    "</div>" +
    legendRow(parts.map((part) => ({ color: part.color, label: `${part.label} · ${integer(part.value)}` })))
  );
}

/* ================================================ График 5 · мощность FRP */

function renderFrp(rows) {
  const values = rows.hotspots.map((item) => Number(item.frp_mw)).filter((value) => Number.isFinite(value));
  if (values.length < 2) {
    $("chartFrp").innerHTML = '<p class="chart-empty">В выборке нет данных о мощности излучения.</p>';
    return;
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const binCount = Math.min(10, Math.max(5, Math.round(Math.sqrt(values.length))));
  const width = (max - min) / binCount || 1;
  const bins = new Array(binCount).fill(0);
  values.forEach((value) => {
    const index = Math.min(binCount - 1, Math.floor((value - min) / width));
    bins[index] += 1;
  });

  const W = 400;
  const H = 210;
  const pad = { l: 30, r: 10, t: 14, b: 32 };
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;
  const maxBin = niceMax(Math.max(...bins));
  const slotW = plotW / binCount;

  const grid = [0, 0.5, 1]
    .map((ratio) => {
      const y = pad.t + plotH - ratio * plotH;
      return (
        `<line class="grid" x1="${pad.l}" y1="${y}" x2="${W - pad.r}" y2="${y}" />` +
        `<text class="axis axis-left" x="${pad.l - 8}" y="${y + 3.5}">${compact(maxBin * ratio)}</text>`
      );
    })
    .join("");

  const bars = bins
    .map((value, index) => {
      const height = (value / maxBin) * plotH;
      const x = pad.l + slotW * index + slotW * 0.1;
      const barW = slotW * 0.8;
      const lo = min + width * index;
      const hi = lo + width;
      return (
        `<rect class="bar-mark" x="${x.toFixed(1)}" y="${(pad.t + plotH - height).toFixed(1)}" ` +
        `width="${barW.toFixed(1)}" height="${Math.max(height, value ? 2 : 0).toFixed(1)}" rx="3" ` +
        `fill="var(--accent)" opacity="${value ? 0.9 : 0.15}">` +
        `<title>${number(lo, 1)}–${number(hi, 1)} МВт: ${integer(value)} ` +
        `${plural(value, "точка", "точки", "точек")}</title></rect>`
      );
    })
    .join("");

  const mean = sum(values, (value) => value) / values.length;
  const sorted = [...values].sort((a, b) => a - b);
  const median = sorted[Math.floor(sorted.length / 2)];

  $("chartFrp").innerHTML =
    svgOpen(W, H, 'aria-label="Распределение мощности излучения термоточек"') +
    grid +
    `<line class="axis-line" x1="${pad.l}" y1="${pad.t + plotH}" x2="${W - pad.r}" y2="${pad.t + plotH}" />` +
    bars +
    `<text class="axis axis-start" x="${pad.l}" y="${H - pad.b + 17}">${number(min, 1)} МВт</text>` +
    `<text class="axis axis-end" x="${W - pad.r}" y="${H - pad.b + 17}">${number(max, 1)} МВт</text>` +
    "</svg>" +
    `<p class="chart-sub">Медиана ${number(median, 1)} МВт · среднее ${number(mean, 1)} МВт · ` +
    `максимум ${number(max, 1)} МВт</p>`;
}

/* =================================================== График 6 · регионы */

function renderRegions(rows) {
  const areas = new Map();
  const points = new Map();
  rows.burns.forEach((burn) => {
    const key = burn.region || "регион не указан";
    areas.set(key, (areas.get(key) || 0) + burn.area_ha);
  });
  rows.hotspots.forEach((point) => {
    const key = point.region || "регион не указан";
    points.set(key, (points.get(key) || 0) + 1);
  });
  const names = [...new Set([...areas.keys(), ...points.keys()])]
    .sort((a, b) => (areas.get(b) || 0) - (areas.get(a) || 0) || (points.get(b) || 0) - (points.get(a) || 0))
    .slice(0, 7);
  const maxArea = Math.max(...names.map((name) => areas.get(name) || 0), 0);

  $("chartRegions").innerHTML = barList(
    names.map((name) => ({
      label: name,
      value: areas.get(name) || 0,
      share: maxArea ? (areas.get(name) || 0) / maxArea : 0,
      text: `${compact(areas.get(name) || 0)} га · ${integer(points.get(name) || 0)} т.`,
      color: "var(--sev-2)",
    })),
    "В выборке нет объектов с указанным регионом",
  );
}

/** Горизонтальные полосы: подпись, дорожка, значение. */
function barList(items, emptyText) {
  if (!items.length) return `<p class="chart-empty">${escapeHtml(emptyText)}</p>`;
  return (
    '<div class="hbars">' +
    items
      .map(
        (item) =>
          '<div class="hbar">' +
          `<span class="hbar-label" title="${escapeHtml(item.label)}">${escapeHtml(item.label)}</span>` +
          '<span class="hbar-track">' +
          `<span class="hbar-fill" style="width:${(item.share * 100).toFixed(1)}%;background:${item.color}"></span>` +
          "</span>" +
          `<span class="hbar-value">${escapeHtml(item.text)}</span></div>`,
      )
      .join("") +
    "</div>"
  );
}

/* ============================================= Таблица крупнейших гарей */

function renderTopBurns(rows) {
  const top = [...rows.burns].sort((a, b) => b.area_ha - a.area_ha).slice(0, 10);
  if (!top.length) {
    $("dashTop").innerHTML = '<p class="chart-empty">В выборке нет контуров гари.</p>';
    return;
  }
  const max = top[0].area_ha;
  $("dashTop").innerHTML =
    '<table class="dash-table"><thead><tr><th>Контур</th><th>Дата</th><th>Степень</th>' +
    '<th>Регион</th><th class="num">Площадь</th><th></th><th>Действия</th></tr></thead><tbody>' +
    top
      .map(
        (burn) =>
          `<tr class="burn-map-row"><td class="mono">${escapeHtml(burn.id ?? burn.contour_id ?? "—")}</td>` +
          `<td>${escapeHtml(formatDate(burn.date))}</td>` +
          `<td><span class="sev-cell"><i style="background:${burn.color}"></i>` +
          `${burn.severity} · ${escapeHtml(burn.severity_label || severityLabel(burn.severity))}</span></td>` +
          `<td>${escapeHtml(burn.region || "—")}</td>` +
          `<td class="num">${number(burn.area_ha)}${burn.clipped ? '<span class="clip" title="Контур обрезан границей области">*</span>' : ""}</td>` +
          `<td class="cell-bar"><span style="width:${((burn.area_ha / max) * 100).toFixed(1)}%;background:${burn.color}"></span></td>` +
          `<td class="burn-actions"><button type="button" class="burn-map-button" aria-label="Показать контур ${escapeHtml(burn.id ?? burn.contour_id ?? "—")} на карте">На карте ↗</button></td></tr>`,
      )
      .join("") +
    "</tbody></table>" +
    (top.some((burn) => burn.clipped)
      ? '<p class="chart-sub">* площадь посчитана по части контура, попавшей в область запроса</p>'
      : "");
  $("dashTop").querySelectorAll(".burn-map-row").forEach((row, index) => {
    row.onclick = (event) => {
      focusBurn(top[index]);
    };
  });
}

/* ============================================== Параметры выборки */

function renderDashFacts(rows) {
  const s = rows.summary;
  const q = rows.query;
  const facts = [
    ["Период запроса", `${formatDate(s.period.from)} — ${formatDate(s.period.to)}`],
    ["Наблюдения", s.observed_range.from ? `${formatDate(s.observed_range.from)} — ${formatDate(s.observed_range.to)}` : "—"],
    ["Площадь области", `${number(q.area_ha)} га`],
    ["Способ выделения", q.area_source === "polygon" ? "полигон" : "прямоугольник"],
    ["Границы области", q.bbox.map((value) => number(value, 3)).join(", ")],
    ["Проекция площади", q.metric_crs],
    ["Спутники", s.satellites.length ? s.satellites.join(", ") : "—"],
    ["Справка сформирована", formatDateTime(s.generated_at)],
  ];
  $("dashFacts").innerHTML = facts
    .map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd></div>`)
    .join("");
}

/* ============================================================ Сборка */

function renderDashboard() {
  if (!state.result || state.result.summary.empty) return;
  const rows = dashRows();
  const s = rows.summary;
  $("dashSubtitle").textContent =
    `${formatDate(s.period.from)} — ${formatDate(s.period.to)} · ` +
    `${integer(s.hotspot_count)} ${plural(s.hotspot_count, "термоточка", "термоточки", "термоточек")} · ` +
    `${integer(s.burn_count)} ${plural(s.burn_count, "контур", "контура", "контуров")}`;
  const prediction = state.result.prediction;
  if (prediction?.mode === "fire_first" && prediction.burn_area_unreliable_ha) {
    // Графики считают все контуры: прямо говорим, сколько из этого — ненадёжная оценка.
    $("dashSubtitle").textContent += ` · из площади гари ${number(prediction.burn_area_unreliable_ha)} га — ` +
      "ненадёжная оценка (длинный разрыв между снимками или слабый ночной огонь)";
  }
  renderKpi(rows);
  renderTimeline(rows);
  renderSeason(rows);
  renderSeverityDonut(rows);
  renderSatellites(rows);
  renderFrp(rows);
  renderRegions(rows);
  renderTopBurns(rows);
  renderDashFacts(rows);
}

function openDashboard() {
  if (!state.result || state.result.summary.empty) {
    toast("Сначала выполните запрос: аналитике нужны данные по области");
    return;
  }
  DASH.scaleLocked = false;
  // Сначала показываем окно: у закрытого диалога clientWidth равен нулю, и
  // графики не смогли бы выбрать габариты под ширину экрана.
  $("dashboard").showModal();
  renderDashboard();
  loadLlmAnalysis();
}

function setDashScale(scale) {
  DASH.scale = scale;
  DASH.scaleLocked = true;
  renderTimeline(dashRows());
}

/** Кнопка доступна только когда есть что показывать; открытое окно обновляется на лету. */
function syncDashboardButton() {
  const button = $("openDashboardButton");
  if (!button) return;
  const ready = Boolean(state.result) && !state.result.summary.empty;
  button.disabled = !ready;
  const dialog = $("dashboard");
  if (!dialog.open) return;
  if (ready) {
    renderDashboard();
    loadLlmAnalysis();
  }
  else dialog.close();
}

/** Габариты графиков зависят от ширины карточек, поэтому открытый дашборд
    перерисовывается при её изменении: поворот экрана, смена размера окна,
    переход через брейкпоинт. Следим именно за шириной — высота меняется от
    самой перерисовки, и реакция на неё зациклила бы наблюдателя. */
function watchDashboardSize() {
  const body = $("dashboard").querySelector(".dash-body");
  let lastWidth = 0;
  let timer;
  const check = () => {
    const dialog = $("dashboard");
    if (!dialog.open) return;
    const width = body.clientWidth;
    if (!width || Math.abs(width - lastWidth) < 24) return;
    lastWidth = width;
    clearTimeout(timer);
    timer = setTimeout(renderDashboard, 120);
  };
  window.addEventListener("resize", check);
  window.addEventListener("orientationchange", check);
  // Первое открытие тоже фиксирует ширину, чтобы следующий поворот экрана
  // сравнивался с реальным значением, а не с нулём.
  $("openDashboardButton").addEventListener("click", () => {
    lastWidth = body.clientWidth;
  });
}

function bindDashboard() {
  const setDashboardSize = (size) => {
    DASH.size = size;
    $("dashboard").dataset.size = size;
    $("dashSmaller").disabled = size === DASH_SIZES[0];
    $("dashLarger").disabled = size === DASH_SIZES[DASH_SIZES.length - 1];
  };
  $("dashSmaller").onclick = () => setDashboardSize(DASH_SIZES[DASH_SIZES.indexOf(DASH.size) - 1]);
  $("dashLarger").onclick = () => setDashboardSize(DASH_SIZES[DASH_SIZES.indexOf(DASH.size) + 1]);
  setDashboardSize(DASH.size);
  $("llmRetry").onclick = () => loadLlmAnalysis(true);
  const toggle = $("dashDownloadToggle");
  const downloads = $("dashDownloads");
  const setDownloadsOpen = (open) => {
    downloads.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
  };
  toggle.onclick = () => setDownloadsOpen(downloads.hidden);
  document.addEventListener("pointerdown", (event) => {
    if (!event.target.closest(".dash-download")) setDownloadsOpen(false);
  });
  $("dashboard").addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !downloads.hidden) {
      event.preventDefault();
      event.stopPropagation();
      setDownloadsOpen(false);
      toggle.focus();
    }
  });
  $("dashboard").addEventListener("close", () => setDownloadsOpen(false));
  downloads.addEventListener("focusout", (event) => {
    if (!event.relatedTarget?.closest(".dash-download")) setDownloadsOpen(false);
  });
  $("openDashboardButton").onclick = openDashboard;
  watchDashboardSize();
  $("dashClose").onclick = () => $("dashboard").close();
  document.querySelectorAll("[data-scale]").forEach((button) => {
    button.onclick = () => setDashScale(button.dataset.scale);
  });
  document.querySelectorAll("[data-dash-export]").forEach((button) => {
    button.onclick = () => {
      download(button.dataset.dashExport);
      setDownloadsOpen(false);
      toggle.focus();
    };
  });
}
