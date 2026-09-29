"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const icon = (name) =>
    `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const esc = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const safeURL = (value) => {
    try {
      const u = new URL(value);
      return /^https?:$/.test(u.protocol) ? u.href : "";
    } catch {
      return "";
    }
  };
  const normalize = (value) =>
    String(value || "")
      .normalize("NFKC")
      .toLowerCase()
      .trim();
  const number = (value) => Number(value || 0).toLocaleString("ja-JP");
  function readStore(key, fallback) {
    try {
      const value = JSON.parse(localStorage.getItem(key));
      return value ?? fallback;
    } catch {
      return fallback;
    }
  }
  let storageWarning = false;
  function writeStore(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      if (!storageWarning) {
        storageWarning = true;
        toast("ブラウザに保存できません。この画面では操作を続けられます。");
      }
    }
  }
  const readArray = (key) => {
    const v = readStore(key, []);
    return Array.isArray(v) ? v.filter((x) => typeof x === "string") : [];
  };
  const readObject = (key) => {
    const v = readStore(key, {});
    return v && typeof v === "object" && !Array.isArray(v) ? v : {};
  };
  const saved = new Set(readArray("fc2favs"));
  const later = new Set(readArray("fc2watchlater"));
  const watched = new Set(readArray("fc2watched"));
  const watchTimes = readObject("fc2watchtimes");
  const progress = readObject("fc2progress");
  let searches = readArray("fc2searchhist").slice(0, 10);
  let items = [],
    byCode = new Map(),
    list = [],
    ready = false,
    failedLoad = false;
  const PAGE_SIZE = 48;
  const CATALOG_REFRESH_MS = 5 * 60 * 1000;
  const PREVIEW_TIMEOUT_MS = 15000;
  const previewSourceCache = new Map();
  let catalogLoading = false,
    checkingUpdates = false,
    catalogVersion = "",
    lastCatalogCheck = 0;
  const homePeriods = ["day", "week", "month", "total"];
  let homeRank = "day",
    homeRankingKey = "",
    homeRankingCache = {};
  let loaded = PAGE_SIZE,
    windowKey = "",
    metrics = null,
    scrollFrame = 0,
    resizeFrame = 0;
  let searchTimer = 0,
    composing = false,
    hoverTimer = 0,
    toastTimer = 0,
    progressTimer = 0;
  let activePreview = null,
    returnFocus = null;
  const defaults = {
    page: "home",
    chip: "all",
    sort: "new",
    q: "",
    rank: "day",
    rise: "24h",
    rating: "",
    dur: "",
    seen: "",
    vmin: "",
    src: "",
  };
  let state = { ...defaults };
  const pageNames = {
    home: "すべての作品",
    new: "新着",
    popular: "人気ランキング",
    rising: "急上昇",
    history: "履歴",
    later: "後で見る",
    saved: "保存済み",
    library: "マイライブラリ",
  };
  const filterDefs = [
    [
      "rating",
      "FC2評価",
      [
        ["", "すべて"],
        ["4", "4.0 以上"],
        ["4.5", "4.5 以上"],
      ],
    ],
    [
      "dur",
      "動画時間",
      [
        ["", "すべて"],
        ["lt30", "30分未満"],
        ["30-60", "30〜60分"],
        ["60-120", "60〜120分"],
        ["gte120", "120分以上"],
      ],
    ],
    [
      "seen",
      "検出日",
      [
        ["", "すべて"],
        ["today", "今日"],
        ["24h", "24時間以内"],
        ["7d", "1週間以内"],
        ["30d", "1か月以内"],
      ],
    ],
    [
      "vmin",
      "再生数",
      [
        ["", "すべて"],
        ["10000", "1万以上"],
        ["100000", "10万以上"],
        ["500000", "50万以上"],
      ],
    ],
    [
      "src",
      "掲載サイト数",
      [
        ["", "すべて"],
        ["2", "2サイト以上"],
        ["3", "3サイト以上"],
      ],
    ],
  ];
  const filterKeys = filterDefs.map((x) => x[0]);
  const chips = new Set(
    [...document.querySelectorAll("[data-chip]")].map((x) => x.dataset.chip),
  );
  const sorts = new Set([...$("sortSelect").options].map((x) => x.value));
  const rankLabels = {
    day: "今日",
    week: "週間",
    month: "月間",
    total: "全期間",
    rating: "FC2評価",
    views: "再生数",
  };
  function restoreURL() {
    const params = new URLSearchParams(location.search);
    state = { ...defaults };
    for (const key of Object.keys(defaults))
      if (params.has(key)) state[key] = params.get(key);
    if (!pageNames[state.page]) state.page = "home";
    if (!chips.has(state.chip)) state.chip = "all";
    if (!sorts.has(state.sort)) state.sort = "new";
    if (!rankLabels[state.rank]) state.rank = "day";
    if (!["6h", "24h"].includes(state.rise)) state.rise = "24h";
    for (const [key, , options] of filterDefs)
      if (!options.some(([v]) => v === state[key])) state[key] = "";
    state.q = state.q.slice(0, 300);
    $("q").value = state.q;
  }
  function updateURL(push = false) {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(state))
      if (v !== defaults[k]) params.set(k, v);
    const url = location.pathname + (params.size ? "?" + params : "");
    if (push && url !== location.pathname + location.search) {
      history.replaceState({ scroll: scrollY }, "", location.href);
      history.pushState({ scroll: 0 }, "", url);
    } else history.replaceState({ scroll: scrollY }, "", url);
  }
  function toast(message) {
    clearTimeout(toastTimer);
    $("toast").textContent = message;
    $("toast").hidden = false;
    toastTimer = setTimeout(() => ($("toast").hidden = true), 3000);
  }
  const parseSeen = (value) => {
    const s = String(value || "");
    const t = Date.parse(
      /[Zz]|[+-]\d\d:\d\d$/.test(s) ? s : s.replace(" ", "T") + "+09:00",
    );
    return Number.isFinite(t) ? t : 0;
  };
  function relativeTime(ts) {
    if (!ts) return "";
    const hours = Math.max(0, Math.floor((Date.now() - ts) / 3600000));
    if (hours < 1) return "1時間以内";
    if (hours < 24) return hours + "時間前";
    const days = Math.floor(hours / 24);
    if (days < 7) return days + "日前";
    return new Date(ts).toLocaleDateString("ja-JP", { timeZone: "Asia/Tokyo" });
  }
  function viewsLabel(v) {
    return v >= 10000
      ? `${Math.round(v / 1000) / 10}万回視聴`
      : v > 0
        ? `${number(v)}回視聴`
        : "";
  }
  function newItem(it, now) {
    return it.is_new || (it._ts > 0 && now - it._ts <= 48 * 3600000);
  }
  const trend = (it) => Number(it["trend_" + state.rise] || 0);
  const rankValue = (it) => Number(it["missav_rank_" + state.rank] || 0);
  const bestRank = (it) =>
    it.missav_rank_day ||
    it.missav_rank_week ||
    it.missav_rank_month ||
    it.missav_rank_total ||
    0;
  function hasQuery(it, query) {
    if (!query) return true;
    const code = query.match(/^(?:fc2[-_\s]*(?:ppv[-_\s]*)?)?(\d{6,8})$/i);
    return code
      ? it.code_num.includes(code[1])
      : query.split(/\s+/).every((word) => it._search.includes(word));
  }
  function selectList() {
    const query = normalize(state.q),
      now = Date.now(),
      today = new Date(now + 32400000).toISOString().slice(0, 10);
    const found = items.filter((it) => {
      if (!hasQuery(it, query)) return false;
      const age = now - it._ts,
        sec = it.duration_sec || 0;
      if (
        state.rating &&
        (it.fc2_rating == null || it.fc2_rating < Number(state.rating))
      )
        return false;
      if (state.vmin && (it.views || 0) < Number(state.vmin)) return false;
      if (state.src && it._sources < Number(state.src)) return false;
      if (state.dur === "lt30" && !(sec > 0 && sec < 1800)) return false;
      if (state.dur === "30-60" && !(sec >= 1800 && sec < 3600)) return false;
      if (state.dur === "60-120" && !(sec >= 3600 && sec < 7200)) return false;
      if (state.dur === "gte120" && sec < 7200) return false;
      if (
        (state.seen === "today" || state.chip === "today") &&
        String(it.first_seen).slice(0, 10) !== today
      )
        return false;
      if (state.seen === "24h" && (!it._ts || age > 86400000)) return false;
      if (
        (state.seen === "7d" || state.chip === "week") &&
        (!it._ts || age > 7 * 86400000)
      )
        return false;
      if (state.seen === "30d" && (!it._ts || age > 30 * 86400000))
        return false;
      if ((state.chip === "new" || state.page === "new") && !newItem(it, now))
        return false;
      if (state.chip === "rated" && !(it.fc2_rating >= 4)) return false;
      if (state.chip === "ranked" && !bestRank(it)) return false;
      if (state.chip === "dur60" && sec < 3600) return false;
      if (state.chip === "views10" && !(it.views >= 100000)) return false;
      if (
        (state.chip === "saved" || state.page === "saved") &&
        !saved.has(it.code)
      )
        return false;
      if (state.page === "later" && !later.has(it.code)) return false;
      if (state.page === "history" && !watched.has(it.code)) return false;
      if (
        state.page === "library" &&
        !saved.has(it.code) &&
        !later.has(it.code) &&
        !watched.has(it.code)
      )
        return false;
      if (state.page === "rising" && trend(it) <= 0) return false;
      if (state.page === "popular") {
        if (state.rank === "rating") return it.fc2_rating != null;
        if (state.rank === "views") return it.views > 0;
        return rankValue(it) > 0;
      }
      return true;
    });
    const newest = (a, b) =>
      b._ts - a._ts || Number(b.code_num) - Number(a.code_num);
    found.sort((a, b) => {
      if (state.page === "history")
        return (
          Number(watchTimes[b.code] || 0) - Number(watchTimes[a.code] || 0) ||
          newest(a, b)
        );
      if (state.page === "rising") return trend(b) - trend(a) || newest(a, b);
      if (state.page === "popular") {
        if (state.rank === "rating")
          return (
            b.fc2_rating - a.fc2_rating ||
            (b.fc2_review_count || 0) - (a.fc2_review_count || 0) ||
            newest(a, b)
          );
        if (state.rank === "views") return b.views - a.views || newest(a, b);
        return rankValue(a) - rankValue(b) || newest(a, b);
      }
      if (state.sort === "old") return -newest(a, b);
      if (state.sort === "rating")
        return (
          (b.fc2_rating ?? -1) - (a.fc2_rating ?? -1) ||
          (b.fc2_review_count || 0) - (a.fc2_review_count || 0) ||
          newest(a, b)
        );
      if (state.sort === "reviews")
        return (
          (b.fc2_review_count ?? -1) - (a.fc2_review_count ?? -1) ||
          newest(a, b)
        );
      if (state.sort === "views")
        return (b.views || 0) - (a.views || 0) || newest(a, b);
      if (state.sort === "long")
        return (b.duration_sec || 0) - (a.duration_sec || 0) || newest(a, b);
      if (state.sort === "short")
        return (
          (a.duration_sec || Infinity) - (b.duration_sec || Infinity) ||
          newest(a, b)
        );
      return newest(a, b);
    });
    return found;
  }
  function externalLink(url, label, code, cls = "") {
    const safe = safeURL(url);
    return safe
      ? `<a href="${esc(safe)}" target="_blank" rel="noopener noreferrer" data-external="${esc(code)}" class="${cls}">${label}</a>`
      : "";
  }
  function cardHTML(it, index, explicitRank = 0) {
    const code = esc(it.code),
      url = safeURL(it.url),
      thumb = safeURL(it.thumb);
    const ratingLabel =
      it.fc2_rating_source === "FC2公式"
        ? "FC2公式"
        : it.fc2_rating_source === "FC2ウォーカー"
          ? "FC2ウォーカー経由"
          : "FC2評価";
    const rating =
      it.fc2_rating != null
        ? `<span class="rating" title="${esc(ratingLabel)}">${icon("star")}${Number(it.fc2_rating).toFixed(1)} <small>${it.fc2_rating_source === "FC2公式" ? "公式" : it.fc2_rating_source === "FC2ウォーカー" ? "ウォーカー" : ""}</small></span>`
        : "";
    const reviews =
      it.fc2_review_count != null
        ? `レビュー ${number(it.fc2_review_count)}件`
        : "";
    const metric =
      state.page === "rising"
        ? `${state.rise}で +${number(trend(it))}回`
        : [rating, reviews].filter(Boolean).join(" · ");
    const sources = Object.entries(it.sources || {})
      .filter(([, u]) => safeURL(u))
      .slice(0, 3)
      .map(([name, u]) => externalLink(u, esc(name), it.code))
      .join(" · ");
    const rank =
      explicitRank ||
      (state.page === "popular" && !["rating", "views"].includes(state.rank)
        ? rankValue(it)
        : state.page === "popular" || state.page === "rising"
          ? index + 1
          : 0);
    const badge = rank
      ? `<span class="card-badge">#${rank}</span>`
      : it.is_new
        ? '<span class="card-badge new">新着</span>'
        : "";
    const p = Math.min(1, Math.max(0, Number(progress[it.code]) || 0));
    const thumbButton = `<button class="thumb-link" type="button" data-preview="${code}" aria-label="${esc(it.title)} のプレビューを再生">${thumb ? `<img src="${esc(thumb)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" width="480" height="270">` : ""}</button>`;
    return `<article class="video-card" data-code="${code}" aria-label="${code}"><div class="thumb-shell">${thumbButton}<div class="card-badges">${badge}${saved.has(it.code) ? `<span class="card-badge">保存済み</span>` : ""}</div>${it.duration ? `<span class="duration">${esc(it.duration)}</span>` : ""}<button class="quick-later${later.has(it.code) ? " selected" : ""}" data-later="${code}" aria-label="${later.has(it.code) ? "後で見るから外す" : "後で見るに追加"}" title="後で見る">${icon(later.has(it.code) ? "check" : "clock")}</button>${p ? `<span class="progress-bar" style="width:${p * 100}%"></span>` : ""}</div><div class="card-body">${externalLink(url, esc(it.title), it.code, "card-title") || `<div class="card-title">${esc(it.title)}</div>`}<div class="card-code">${code}${watched.has(it.code) ? '<span class="watched-mark">✓ 視聴済み</span>' : ""}</div><div class="card-meta">${[viewsLabel(it.views), relativeTime(it._ts)].filter(Boolean).join(" · ")}</div><div class="card-meta">${metric || (bestRank(it) ? `MissAV ランキング #${bestRank(it)}` : "評価未取得")}</div><div class="source-row">${sources}</div><button class="icon-button card-more" data-more="${code}" aria-label="${code} のメニュー">${icon("more")}</button></div></article>`;
  }
  function measure() {
    const grid = $("grid"),
      style = getComputedStyle(grid);
    const cols =
      style.gridTemplateColumns.split(" ").filter(Boolean).length || 1;
    const gapX = parseFloat(style.columnGap) || 0,
      gapY = parseFloat(style.rowGap) || 0;
    const width = (grid.clientWidth - gapX * (cols - 1)) / cols;
    const bodyHeight =
      parseFloat(
        getComputedStyle(document.documentElement).getPropertyValue(
          "--body-height",
        ),
      ) || 128;
    grid.style.setProperty("--card-thumb-height", `${(width * 9) / 16}px`);
    metrics = { cols, gapY, row: (width * 9) / 16 + bodyHeight + gapY };
  }
  // Keep only nearby rows mounted, even after thousands of items have been loaded.
  // Spacers retain the page height; focus keeps its row mounted for keyboard users.
  function renderWindow(force = false) {
    if (!ready || !list.length) return;
    if (!metrics) measure();
    const { cols, gapY, row } = metrics;
    const total = Math.min(loaded, list.length),
      rows = Math.ceil(total / cols);
    const top = $("feedWindow").getBoundingClientRect().top + scrollY;
    const firstRow = Math.max(
      0,
      Math.min(rows - 1, Math.floor((scrollY - top) / row) - 2),
    );
    let start = firstRow * cols,
      end = Math.min(
        total,
        (firstRow + Math.ceil(innerHeight / row) + 6) * cols,
      );
    const focusCode =
      document.activeElement?.closest(".video-card")?.dataset.code;
    if (focusCode) {
      const ix = list.findIndex((it) => it.code === focusCode);
      if (ix >= 0 && Math.abs(ix - start) < cols * 30) {
        start = Math.min(start, Math.floor(ix / cols) * cols);
        end = Math.max(
          end,
          Math.min(total, (Math.floor(ix / cols) + 1) * cols),
        );
      }
    }
    const key = `${start}:${end}:${total}:${cols}`;
    if (!force && key === windowKey) return;
    windowKey = key;
    $("topSpacer").style.height = `${Math.floor(start / cols) * row}px`;
    const endRow = Math.ceil(end / cols);
    $("bottomSpacer").style.height =
      `${endRow < rows ? (rows - endRow) * row - gapY : 0}px`;
    $("grid").style.paddingBottom = endRow < rows ? `${gapY}px` : "0px";
    // Keyed reuse prevents image reloads and keeps keyboard focus on surviving cards.
    const current = new Map(
      [...$("grid").children].map((el) => [el.dataset.code, el]),
    );
    const keep = new Set(list.slice(start, end).map((it) => it.code));
    for (const [code, node] of current)
      if (!keep.has(code)) {
        if (activePreview?.code === code) stopActivePreview();
        node.remove();
      }
    let previous = null;
    for (let i = start; i < end; i++) {
      const it = list[i];
      let node = current.get(it.code);
      if (!node) {
        const tpl = document.createElement("template");
        tpl.innerHTML = cardHTML(it, i);
        node = tpl.content.firstElementChild;
      }
      const reference = previous
        ? previous.nextElementSibling
        : $("grid").firstElementChild;
      if (node !== reference) $("grid").insertBefore(node, reference);
      previous = node;
    }
    $("loadMoreWrap").hidden = total >= list.length;
    $("loadMore").textContent =
      `さらに表示（残り ${number(list.length - total)} 件）`;
    $("feedEnd").hidden = total < list.length;
  }
  function loadMore() {
    if (!ready || loaded >= list.length) return;
    loaded += PAGE_SIZE;
    renderWindow();
  }
  function queueScroll() {
    if (scrollFrame) return;
    scrollFrame = requestAnimationFrame(() => {
      scrollFrame = 0;
      renderWindow();
      if (
        ready &&
        loaded < list.length &&
        $("loadMoreWrap").getBoundingClientRect().top < innerHeight + 300
      )
        loadMore();
    });
  }
  function updateRankingArrows() {
    const rail = $("rankingRail");
    $("rankingPrev").disabled = rail.scrollLeft < 2;
    $("rankingNext").disabled =
      rail.scrollLeft + rail.clientWidth >= rail.scrollWidth - 2;
  }
  function renderHomeRankings(force = false) {
    const visible =
      ready &&
      state.page === "home" &&
      !state.q &&
      state.chip === "all" &&
      !filterKeys.some((key) => state[key]);
    $("homeRankings").hidden = !visible;
    if (!visible) {
      if (activePreview?.shell?.closest("#rankingRail")) stopActivePreview();
      $("rankingRail").replaceChildren();
      homeRankingKey = "";
      return;
    }
    document.querySelectorAll("[data-home-rank]").forEach((button) => {
      const selected = button.dataset.homeRank === homeRank;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
    if (force || homeRankingKey !== homeRank) {
      if (activePreview?.shell?.closest("#rankingRail")) stopActivePreview();
      const rows = homeRankingCache[homeRank] || [];
      $("rankingRail").innerHTML = rows.length
        ? rows
            .map((it, index) =>
              cardHTML(
                it,
                index,
                Number(it["missav_rank_" + homeRank]),
              ).replace('class="video-card"', 'class="ranking-card"'),
            )
            .join("")
        : '<p class="ranking-empty">この期間のランキングデータはまだありません。</p>';
      $("rankingRail").setAttribute(
        "aria-label",
        `MissAV ${rankLabels[homeRank]}のランキング`,
      );
      $("rankingRail").scrollLeft = 0;
      homeRankingKey = homeRank;
    }
    updateRankingArrows();
  }
  function refreshCards() {
    // Storage actions only repaint the affected visible window, not the full data feed.
    stopActivePreview();
    $("grid").replaceChildren();
    windowKey = "";
    renderWindow(true);
    renderHomeRankings(true);
    updateCounts();
  }
  function updateCounts() {
    $("savedCount").textContent = saved.size || "";
    $("laterCount").textContent = later.size || "";
  }
  function showEmpty() {
    const names = {
      later: [
        "後で見る作品を追加しましょう",
        "カードの時計マーク、または「︙」メニューから追加できます。",
      ],
      saved: [
        "保存した作品はまだありません",
        "「︙」メニューの「保存」を使うと、ここからすぐに開けます。",
      ],
      history: [
        "視聴履歴はまだありません",
        "作品ページを開くと、履歴に追加されます。",
      ],
      library: [
        "ライブラリはまだ空です",
        "気になる作品を保存して、自分の一覧を作れます。",
      ],
    };
    let [title, detail] = names[state.page] || [
      "該当する作品がありません",
      "キーワードやフィルターを変えて探してみてください。",
    ];
    if (state.page === "rising")
      [title, detail] = [
        "急上昇データはまだありません",
        "指定時間の再生数の増加を確認できた作品が表示されます。",
      ];
    if (state.page === "popular")
      [title, detail] = [
        "このランキングのデータはまだありません",
        "別の期間や「FC2評価」タブを選んでください。",
      ];
    if (state.q || state.chip !== "all" || filterKeys.some((k) => state[k]))
      [title, detail] = [
        "一致する作品が見つかりません",
        "キーワードや絞り込み条件を変更してください。",
      ];
    $("emptyState").innerHTML =
      `${icon("search")}<h2>${title}</h2><p>${detail}</p><button class="pill-button" data-reset-all>条件をリセット</button>`;
  }
  function contextHTML() {
    if (state.page === "popular")
      return Object.entries(rankLabels)
        .map(
          ([k, label]) =>
            `<button data-rank="${k}" class="${state.rank === k ? "active" : ""}" aria-pressed="${state.rank === k}">${label}</button>`,
        )
        .join("");
    if (state.page === "rising")
      return ["6h", "24h"]
        .map(
          (k) =>
            `<button data-rise="${k}" class="${state.rise === k ? "active" : ""}" aria-pressed="${state.rise === k}">${k === "6h" ? "6時間" : "24時間"}</button>`,
        )
        .join("");
    if (["library", "later", "saved", "history"].includes(state.page))
      return ["library", "later", "saved", "history"]
        .map(
          (k) =>
            `<button data-page="${k}" class="${state.page === k ? "active" : ""}" aria-pressed="${state.page === k}">${k === "library" ? "すべて" : pageNames[k]}</button>`,
        )
        .join("");
    return "";
  }
  function render(reset = true) {
    if (!ready) return;
    stopActivePreview();
    clearTimeout(hoverTimer);
    if (reset) loaded = PAGE_SIZE;
    windowKey = "";
    list = selectList();
    renderHomeRankings();
    $("q").value = state.q;
    $("clearSearch").hidden = !state.q;
    $("sortSelect").value = state.sort;
    $("sortSelect").disabled = ["popular", "rising", "history"].includes(
      state.page,
    );
    $("listTitle").textContent = state.q
      ? `「${state.q}」の検索結果`
      : state.page === "popular" && homePeriods.includes(state.rank)
        ? `MissAV ${rankLabels[state.rank]}ランキング`
        : pageNames[state.page];
    document.title = `${state.q ? "検索結果" : pageNames[state.page]} — FC2-PPV`;
    $("resultCount").textContent = `${number(list.length)} 作品`;
    const tabs = contextHTML();
    $("contextTabs").innerHTML = tabs;
    $("contextTabs").hidden = !tabs;
    const note =
      state.page === "popular"
        ? state.rank === "rating"
          ? "評価の取得元は各カードに表示しています。同評価はレビュー数の多い順です。"
          : state.rank === "views"
            ? "取得済みの再生数で並べています。"
            : "MissAV の公開ランキングに掲載された作品です。"
        : state.page === "rising"
          ? "再生数の増加を確認できた作品を表示します。"
          : "";
    $("contextNote").textContent = note;
    $("contextNote").hidden = !note;
    document.querySelectorAll("[data-page]").forEach((el) => {
      const on =
        el.dataset.page === state.page ||
        (el.dataset.page === "library" &&
          ["saved", "later"].includes(state.page));
      el.classList.toggle("active", on);
      if (on) el.setAttribute("aria-current", "page");
      else el.removeAttribute("aria-current");
    });
    document.querySelectorAll("[data-chip]").forEach((el) => {
      const on = el.dataset.chip === state.chip;
      el.classList.toggle("active", on);
      el.setAttribute("aria-pressed", String(on));
    });
    const active = filterDefs.filter(([k]) => state[k]);
    $("filterCount").textContent = active.length;
    $("filterCount").hidden = !active.length;
    $("activeFilters").innerHTML =
      active
        .map(
          ([k, label, options]) =>
            `<button data-remove-filter="${k}">${esc(label)}: ${esc(options.find(([v]) => v === state[k])?.[1])} ×</button>`,
        )
        .join("") + "<button data-reset-filters>すべて解除</button>";
    $("activeFilters").hidden = !active.length;
    $("feedWindow").hidden = !list.length;
    $("emptyState").hidden = !!list.length;
    $("grid").replaceChildren();
    $("topSpacer").style.height = "0px";
    $("bottomSpacer").style.height = "0px";
    $("loadMoreWrap").hidden = true;
    $("feedEnd").hidden = true;
    $("grid").setAttribute("aria-busy", "false");
    if (!list.length) showEmpty();
    else {
      measure();
      renderWindow(true);
    }
    updateCounts();
    updateFilterButtons();
  }
  function navigate(page, options = {}) {
    if (!pageNames[page]) return;
    clearTimeout(searchTimer);
    hideSuggest();
    state = { ...defaults, ...options, page };
    $("q").value = "";
    document.body.classList.remove("mobile-search", "drawer");
    $("navBackdrop").hidden = true;
    updateURL(true);
    window.scrollTo(0, 0);
    render();
  }
  function markWatched(code) {
    watched.add(code);
    watchTimes[code] = Date.now();
    writeStore("fc2watched", [...watched]);
    writeStore("fc2watchtimes", watchTimes);
  }
  function toggleSet(set, key, code, label) {
    const remove = set.has(code);
    remove ? set.delete(code) : set.add(code);
    writeStore(key, [...set]);
    toast(remove ? `${label}から外しました` : `${label}に追加しました`);
    if (
      ["saved", "later", "library"].includes(state.page) ||
      state.chip === "saved"
    )
      render(false);
    else refreshCards();
  }
  function openDialog(dialog) {
    stopActivePreview();
    hideSuggest();
    returnFocus = document.activeElement;
    if (!dialog.open) dialog.showModal();
  }
  function closeDialog(dialog) {
    dialog.close();
    if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
  }
  function openMenu(code) {
    const it = byCode.get(code);
    if (!it) return;
    const action = (name, text, sym) =>
      `<button class="menu-row" data-action="${name}" data-code="${esc(code)}">${icon(sym)}${text}</button>`;
    const sourceLinks = Object.entries(it.sources || {})
      .map(([name, url]) =>
        externalLink(url, icon("link") + esc(name), code, "menu-row"),
      )
      .join("");
    // Search URLs are templates, so thousands of repeated URLs are not downloaded.
    const searchLinks = Object.entries(searchTemplates)
      .filter(([name]) => !(it.sources || {})[name])
      .map(([name, url]) =>
        externalLink(
          url.replaceAll("{code}", encodeURIComponent(it.code_num)),
          icon("search") + esc(name) + "で検索",
          code,
          "menu-row",
        ),
      )
      .join("");
    $("menuDialog").innerHTML =
      `<div class="menu-title"><span>${esc(code)}</span><button class="icon-button" data-close aria-label="閉じる">${icon("close")}</button></div>${action("later", later.has(code) ? "後で見るから外す" : "後で見る", "clock")}${action("save", saved.has(code) ? "保存を解除" : "保存", "save")}${action("watched", watched.has(code) ? "履歴から削除" : "視聴済みにする", "history")}${action("copy", "番号をコピー", "copy")}${previewSources(it).length ? action("preview", "サムネイルでプレビュー再生", "play") : ""}${it.fc2_market_url ? externalLink(it.fc2_market_url, icon("link") + "FC2公式", code, "menu-row") : ""}<div class="menu-subheading">掲載サイト</div>${sourceLinks}${searchLinks ? '<div class="menu-subheading">ほかのサイトで探す</div>' + searchLinks : ""}`;
    openDialog($("menuDialog"));
  }
  function saveProgress(it, video) {
    if (!it || !Number.isFinite(video.duration) || video.duration <= 0) return;
    progress[it.code] = Math.min(
      1,
      Math.max(0, video.currentTime / video.duration),
    );
    if (!progressTimer)
      progressTimer = setTimeout(() => {
        progressTimer = 0;
        writeStore("fc2progress", progress);
      }, 2000);
    if (progress[it.code] > 0.9 && !watched.has(it.code)) markWatched(it.code);
  }
  function clearInlineState(shell) {
    shell?.querySelector(".inline-preview-state")?.remove();
  }
  function showInlineState(shell, message, error = false) {
    if (!shell?.isConnected) return;
    clearInlineState(shell);
    const state = document.createElement("div");
    state.className = `inline-preview-state${error ? " error" : ""}`;
    state.setAttribute("role", "status");
    state.textContent = message;
    shell.append(state);
  }
  function previewSources(it) {
    const alternatives = Array.isArray(it?.preview_fallbacks) ? it.preview_fallbacks : [];
    const sources = [...new Set([it?.preview, ...alternatives].map(safeURL).filter((url) => {
      if (!url) return false;
      const parsed = new URL(url);
      return !parsed.username && !parsed.password;
    }))].slice(0, 3);
    const remembered = previewSourceCache.get(it?.code);
    return sources.includes(remembered)
      ? [remembered, ...sources.filter((url) => url !== remembered)] : sources;
  }
  function releasePreviewVideo(video) {
    if (!video) return;
    video.pause();
    video.removeAttribute("src");
    video.load();
    video.remove();
  }
  function stopActivePreview() {
    clearTimeout(hoverTimer);
    if (!activePreview) return;
    const { video, shell, timeout } = activePreview;
    activePreview = null;
    clearTimeout(timeout);
    releasePreviewVideo(video);
    if (shell?.isConnected) clearInlineState(shell);
  }
  function stopHover() {
    clearTimeout(hoverTimer);
    if (activePreview?.persistent) return;
    stopActivePreview();
  }
  function playInlinePreview(shell, it, persistent = false) {
    const sources = previewSources(it);
    if (!it || !sources.length || !shell?.isConnected) {
      if (persistent) showInlineState(shell, "プレビュー未取得", true);
      return;
    }
    stopActivePreview();
    const session = { video: null, code: it.code, shell, persistent, timeout: 0,
      started: false, deadline: Date.now() + PREVIEW_TIMEOUT_MS, index: 0 };
    const finish = (message) => {
      if (activePreview !== session) return;
      const showError = session.persistent;
      stopActivePreview();
      if (showError) showInlineState(shell, message, true);
    };
    const trySource = () => {
      if (activePreview !== session) return;
      if (!shell.isConnected) { stopActivePreview(); return; }
      const previous = session.video;
      session.video = null;
      clearTimeout(session.timeout);
      session.timeout = 0;
      releasePreviewVideo(previous);
      const url = sources[session.index];
      const video = document.createElement("video");
      session.video = video;
      session.started = false;
      const current = () => activePreview === session && session.video === video;
      const fail = (message) => {
        if (!current()) return;
        if (previewSourceCache.get(it.code) === url) previewSourceCache.delete(it.code);
        if (!session.deadline) session.deadline = Date.now() + PREVIEW_TIMEOUT_MS;
        if (session.index + 1 < sources.length && Date.now() < session.deadline) {
          session.index++;
          trySource();
        } else finish(message);
      };
      const waitForPlayback = () => {
        if (!current() || session.timeout) return;
        session.started = false;
        if (!session.deadline) session.deadline = Date.now() + PREVIEW_TIMEOUT_MS;
        if (session.persistent) showInlineState(shell,
          session.index ? "別のプレビューを読み込み中…" : "読み込み中…");
        // All sources share one waiting budget, rather than a 15-second wait
        // for every URL. A hanging first source must leave time for its backup.
        const remaining = Math.max(1, session.deadline - Date.now());
        const delay = Math.max(1, Math.floor(remaining / (sources.length - session.index)));
        session.timeout = setTimeout(() => fail("読み込みが止まりました。タップで再試行"), delay);
      };
      video.defaultMuted = true;
      video.muted = true;
      video.loop = true;
      video.playsInline = true;
      video.preload = session.persistent ? "auto" : "metadata";
      video.poster = safeURL(it.thumb) || "";
      // Native playback without a proxy or authentication/referrer workarounds.
      // Requiring anonymous CORS would reject otherwise playable responses.
      video.src = url;
      video.setAttribute("aria-hidden", "true");
      video.addEventListener("timeupdate", () => { if (current()) saveProgress(it, video); });
      video.addEventListener("playing", () => {
        if (!current()) return;
        session.started = true;
        session.deadline = 0;
        clearTimeout(session.timeout);
        session.timeout = 0;
        clearInlineState(shell);
        previewSourceCache.delete(it.code);
        previewSourceCache.set(it.code, url);
        if (previewSourceCache.size > 200) previewSourceCache.delete(previewSourceCache.keys().next().value);
      });
      video.addEventListener("waiting", waitForPlayback);
      video.addEventListener("error", () => fail("プレビューを読み込めません。タップで再試行"), { once: true });
      shell.append(video);
      waitForPlayback();
      const result = video.play();
      if (result && typeof result.catch === "function") result.catch((error) => {
        if (!current()) return;
        if (error.name === "NotAllowedError") finish("再生が許可されませんでした。タップで再試行");
        else fail("再生できません。タップで再試行");
      });
    };
    activePreview = session;
    trySource();
  }
  function toggleInlinePreview(code, shell) {
    const it = byCode.get(code);
    if (!it || !previewSources(it).length) {
      stopActivePreview();
      showInlineState(shell, "プレビュー未取得", true);
      return;
    }
    if (activePreview?.shell === shell) {
      if (activePreview.persistent) stopActivePreview();
      else {
        // A click on a hovered preview keeps the same loaded player alive.
        activePreview.persistent = true;
        if (!activePreview.started) showInlineState(shell, "読み込み中…");
      }
      return;
    }
    playInlinePreview(shell, it, true);
  }
  function startHover(shell) {
    const it = byCode.get(shell.closest(".video-card")?.dataset.code);
    if (
      !it ||
      !previewSources(it).length ||
      !shell.isConnected ||
      navigator.connection?.saveData ||
      document.hidden ||
      activePreview?.persistent
    )
      return;
    if (activePreview?.code === it.code) return;
    playInlinePreview(shell, it);
  }
  function addSearch(query) {
    query = query.trim();
    if (!query) return;
    searches = [query, ...searches.filter((x) => x !== query)].slice(0, 10);
    writeStore("fc2searchhist", searches);
  }
  function hideSuggest() {
    $("suggest").hidden = true;
    $("q").setAttribute("aria-expanded", "false");
  }
  function showSuggest() {
    const q = normalize($("q").value);
    let html = "";
    if (!q)
      html =
        searches
          .map(
            (s) =>
              `<div class="suggest-row"><button class="suggest-choice" data-search="${esc(s)}">${icon("history")}<span>${esc(s)}</span></button><button class="icon-button" data-delete-search="${esc(s)}" aria-label="検索履歴を削除">${icon("close")}</button></div>`,
          )
          .join("") ||
        '<div class="suggest-note">番号またはタイトルを入力</div>';
    else {
      const found = [];
      for (const it of items) {
        if (hasQuery(it, q)) found.push(it);
        if (found.length >= 6) break;
      }
      html =
        found
          .map(
            (it) =>
              `<div class="suggest-row"><button class="suggest-choice" data-search="${esc(it.code)}">${icon("search")}<span>${esc(it.code)}　${esc(it.title)}</span></button></div>`,
          )
          .join("") || '<div class="suggest-note">Enter キーで検索</div>';
    }
    $("suggest").innerHTML = html;
    $("suggest").hidden = false;
    $("q").setAttribute("aria-expanded", "true");
  }
  function applySearch(submit) {
    clearTimeout(searchTimer);
    state.q = $("q").value.trim();
    // Searching always covers the catalog; a hidden library filter must not restrict results.
    if (state.page !== "home") state = { ...defaults, q: state.q };
    if (submit) {
      addSearch(state.q);
      hideSuggest();
      $("q").blur();
    } else showSuggest();
    updateURL();
    window.scrollTo(0, 0);
    render();
  }
  function updateFilterButtons() {
    document.querySelectorAll("[data-filter]").forEach((b) => {
      const active = state[b.dataset.filter] === b.dataset.value;
      b.classList.toggle("active", active);
      b.setAttribute("aria-pressed", String(active));
    });
  }
  $("filterGroups").innerHTML = filterDefs
    .map(
      ([key, label, options]) =>
        `<fieldset class="filter-group"><legend>${label}</legend><div class="filter-options">${options.map(([value, text]) => `<button data-filter="${key}" data-value="${value}">${text}</button>`).join("")}</div></fieldset>`,
    )
    .join("");
  function resetFilters() {
    for (const key of filterKeys) state[key] = "";
    updateURL();
    window.scrollTo(0, 0);
    render();
  }
  $("searchForm").addEventListener("submit", (e) => {
    e.preventDefault();
    if (!composing) applySearch(true);
  });
  $("q").addEventListener("compositionstart", () => {
    composing = true;
    clearTimeout(searchTimer);
  });
  $("q").addEventListener("compositionend", () => {
    composing = false;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => applySearch(false), 160);
  });
  $("q").addEventListener("input", () => {
    $("clearSearch").hidden = !$("q").value;
    clearTimeout(searchTimer);
    if (!composing) searchTimer = setTimeout(() => applySearch(false), 160);
  });
  $("q").addEventListener("focus", showSuggest);
  $("q").addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideSuggest();
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if ($("suggest").hidden) showSuggest();
      $("suggest").querySelector("button")?.focus();
    }
  });
  $("suggest").addEventListener("keydown", (e) => {
    const buttons = [...$("suggest").querySelectorAll(".suggest-choice")];
    const i = buttons.indexOf(document.activeElement);
    if (e.key === "ArrowDown") {
      e.preventDefault();
      buttons[Math.min(buttons.length - 1, i + 1)]?.focus();
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      if (i <= 0) $("q").focus();
      else buttons[i - 1].focus();
    }
    if (e.key === "Escape") {
      hideSuggest();
      $("q").focus();
      hideSuggest();
    }
  });
  $("clearSearch").addEventListener("click", () => {
    $("q").value = "";
    applySearch(false);
    $("q").focus();
  });
  $("mobileSearch").addEventListener("click", () => {
    document.body.classList.add("mobile-search");
    $("q").focus();
  });
  $("searchBack").addEventListener("click", () => {
    document.body.classList.remove("mobile-search");
    hideSuggest();
    $("q").blur();
  });
  $("sortSelect").addEventListener("change", () => {
    state.sort = $("sortSelect").value;
    updateURL();
    window.scrollTo(0, 0);
    render();
  });
  $("filterButton").addEventListener("click", () =>
    openDialog($("filterDialog")),
  );
  $("infoButton").addEventListener("click", () => openDialog($("infoDialog")));
  $("resetFilters").addEventListener("click", resetFilters);
  $("loadMore").addEventListener("click", loadMore);
  $("rankingAll").addEventListener("click", () =>
    navigate("popular", { rank: homeRank }),
  );
  document.querySelectorAll("[data-home-rank]").forEach((button) =>
    button.addEventListener("click", () => {
      homeRank = button.dataset.homeRank;
      renderHomeRankings();
    }),
  );
  $("rankingRail").addEventListener("scroll", updateRankingArrows, {
    passive: true,
  });
  for (const [id, direction] of [
    ["rankingPrev", -1],
    ["rankingNext", 1],
  ]) {
    $(id).addEventListener("click", () =>
      $("rankingRail").scrollBy({
        left: direction * $("rankingRail").clientWidth * 0.8,
        behavior: matchMedia("(prefers-reduced-motion: reduce)").matches
          ? "auto"
          : "smooth",
      }),
    );
  }
  new ResizeObserver(updateRankingArrows).observe($("rankingRail"));
  $("sidebarToggle").addEventListener("click", () => {
    if (innerWidth < 1200) {
      const open = document.body.classList.toggle("drawer");
      $("navBackdrop").hidden = !open;
    } else {
      document.body.classList.toggle("compact");
      $("sidebarToggle").setAttribute(
        "aria-expanded",
        String(!document.body.classList.contains("compact")),
      );
    }
    metrics = null;
    requestAnimationFrame(() => renderWindow(true));
  });
  $("navBackdrop").addEventListener("click", () => {
    document.body.classList.remove("drawer");
    $("navBackdrop").hidden = true;
  });
  document.addEventListener("click", async (e) => {
    const button = e.target.closest("button,a");
    if (!e.target.closest(".search-form")) hideSuggest();
    if (!button) return;
    if (button.hasAttribute("data-close")) {
      closeDialog(button.closest("dialog"));
      return;
    }
    if (button.dataset.page) {
      e.preventDefault();
      navigate(button.dataset.page);
      return;
    }
    if (button.dataset.external) {
      markWatched(button.dataset.external);
      if ($("menuDialog").open) closeDialog($("menuDialog"));
      return;
    }
    if (button.dataset.chip) {
      state.chip = button.dataset.chip;
      state.page = "home";
      updateURL();
      window.scrollTo(0, 0);
      render();
      return;
    }
    if (button.dataset.more) {
      e.preventDefault();
      openMenu(button.dataset.more);
      return;
    }
    if (button.dataset.later) {
      toggleSet(later, "fc2watchlater", button.dataset.later, "後で見る");
      return;
    }
    if (button.dataset.preview) {
      e.preventDefault();
      const shell = button.closest(".thumb-shell");
      if (shell) toggleInlinePreview(button.dataset.preview, shell);
      return;
    }
    if (button.dataset.rank) {
      state.rank = button.dataset.rank;
      updateURL();
      window.scrollTo(0, 0);
      render();
      return;
    }
    if (button.dataset.rise) {
      state.rise = button.dataset.rise;
      updateURL();
      window.scrollTo(0, 0);
      render();
      return;
    }
    if (button.hasAttribute("data-search")) {
      $("q").value = button.dataset.search;
      applySearch(true);
      return;
    }
    if (button.hasAttribute("data-delete-search")) {
      searches = searches.filter((s) => s !== button.dataset.deleteSearch);
      writeStore("fc2searchhist", searches);
      showSuggest();
      return;
    }
    if (button.dataset.filter) {
      state[button.dataset.filter] = button.dataset.value;
      updateURL();
      window.scrollTo(0, 0);
      render();
      return;
    }
    if (button.dataset.removeFilter) {
      state[button.dataset.removeFilter] = "";
      updateURL();
      window.scrollTo(0, 0);
      render();
      return;
    }
    if (button.hasAttribute("data-reset-filters")) {
      resetFilters();
      return;
    }
    if (button.hasAttribute("data-reset-all")) {
      navigate("home");
      return;
    }
    if (button.hasAttribute("data-retry")) {
      loadCatalog();
      return;
    }
    const code = button.dataset.code,
      action = button.dataset.action;
    if (!action || !byCode.has(code)) return;
    if (action === "preview") {
      closeDialog($("menuDialog"));
      const shell = document.querySelector(
        `.video-card[data-code="${CSS.escape(code)}"] .thumb-shell`,
      );
      if (shell) toggleInlinePreview(code, shell);
      return;
    }
    closeDialog($("menuDialog"));
    if (action === "later") toggleSet(later, "fc2watchlater", code, "後で見る");
    if (action === "save") toggleSet(saved, "fc2favs", code, "保存済み");
    if (action === "watched") {
      if (watched.has(code)) {
        watched.delete(code);
        delete watchTimes[code];
        writeStore("fc2watched", [...watched]);
        writeStore("fc2watchtimes", watchTimes);
        toast("履歴から削除しました");
      } else {
        markWatched(code);
        toast("視聴済みにしました");
      }
      render(false);
    }
    if (action === "copy") {
      try {
        await navigator.clipboard.writeText(code);
        toast("番号をコピーしました");
      } catch {
        toast("コピーできませんでした: " + code);
      }
    }
  });
  for (const d of document.querySelectorAll("dialog"))
    d.addEventListener("click", (e) => {
      if (e.target === d) {
        const r = d.getBoundingClientRect();
        if (
          e.clientX < r.left ||
          e.clientX > r.right ||
          e.clientY < r.top ||
          e.clientY > r.bottom
        )
          closeDialog(d);
      }
    });
  $("grid").addEventListener(
    "error",
    (e) => {
      if (e.target.tagName === "IMG")
        e.target.parentElement.classList.add("image-missing");
    },
    true,
  );
  $("grid").addEventListener("pointerover", (e) => {
    const actionButton = e.target.closest("button");
    if (
      e.pointerType !== "mouse" ||
      !matchMedia("(hover:hover)").matches ||
      (actionButton && !actionButton.classList.contains("thumb-link"))
    )
      return;
    const shell = e.target.closest(".thumb-shell");
    if (!shell || shell.contains(e.relatedTarget)) return;
    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(() => startHover(shell), 650);
  });
  $("grid").addEventListener("pointerout", (e) => {
    const shell = e.target.closest(".thumb-shell");
    if (shell && !shell.contains(e.relatedTarget)) stopHover();
  });
  document.addEventListener("keydown", (e) => {
    if (
      e.key === "/" &&
      !e.target.matches("input,textarea,select") &&
      !document.querySelector("dialog[open]")
    ) {
      e.preventDefault();
      if (innerWidth < 768) document.body.classList.add("mobile-search");
      $("q").focus();
    }
    if (e.key === "Escape") {
      hideSuggest();
      document.body.classList.remove("drawer");
      $("navBackdrop").hidden = true;
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopActivePreview();
    } else {
      checkForCatalogUpdate();
    }
  });
  window.addEventListener("online", () => checkForCatalogUpdate());
  window.addEventListener("pagehide", () => {
    stopActivePreview();
    if (progressTimer) {
      clearTimeout(progressTimer);
      writeStore("fc2progress", progress);
      progressTimer = 0;
    }
  });
  window.addEventListener("scroll", queueScroll, { passive: true });
  new ResizeObserver(() => {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => {
      metrics = null;
      windowKey = "";
      renderWindow(true);
    });
  }).observe($("grid"));
  window.addEventListener("popstate", (e) => {
    restoreURL();
    loaded = Math.max(
      PAGE_SIZE,
      Math.ceil((e.state?.scroll || 0) / (metrics?.row || 300)) *
        (metrics?.cols || 3) +
        PAGE_SIZE,
    );
    render(false);
    window.scrollTo(0, e.state?.scroll || 0);
  });
  let searchTemplates = {};
  function catalogRefreshBusy() {
    return !!(activePreview || composing || document.querySelector("dialog[open]") ||
      document.activeElement?.matches("input,textarea,select"));
  }
  async function checkForCatalogUpdate() {
    if (!ready || catalogLoading || checkingUpdates || document.hidden ||
        navigator.onLine === false || catalogRefreshBusy() ||
        Date.now() - lastCatalogCheck < 60000) return;
    checkingUpdates = true;
    lastCatalogCheck = Date.now();
    try {
      // Poll a tiny manifest; only download the full catalog when it changes.
      const response = await fetch(document.body.dataset.updates, { cache: "no-cache" });
      if (!response.ok) return;
      const update = await response.json();
      if (update.version && update.version !== catalogVersion)
        await loadCatalog(true, update.version);
    } catch {
      // A background outage must leave the currently usable feed intact.
    } finally {
      checkingUpdates = false;
    }
  }
  async function loadCatalog(background = false, expectedVersion = "") {
    if (catalogLoading || (background && catalogRefreshBusy())) return;
    catalogLoading = true;
    if (!background) {
      failedLoad = false;
      $("emptyState").hidden = true;
      $("resultCount").textContent = "読み込み中…";
    }
    try {
      const url = new URL(document.body.dataset.catalog, document.baseURI);
      if (expectedVersion) url.searchParams.set("v", expectedVersion);
      const response = await fetch(url, { cache: "no-cache" });
      if (!response.ok) throw new Error("HTTP " + response.status);
      const payload = await response.json();
      if (!Array.isArray(payload.items)) throw new Error("Invalid catalog");
      if (expectedVersion && payload.version !== expectedVersion)
        throw new Error("Catalog deployment in progress");
      if (background && (document.hidden || catalogRefreshBusy())) return;
      if (background && payload.version === catalogVersion) return;
      const position = scrollY;
      const anchor = background && position > 0
        ? [...$("grid").children].find((el) => el.getBoundingClientRect().bottom > 64)
        : null;
      const anchorCode = anchor?.dataset.code;
      const anchorTop = anchor?.getBoundingClientRect().top;
      searchTemplates = payload.search_templates || {};
      items = payload.items.map((it) => ({
        ...it,
        code_num: String(it.code_num || it.code?.split("-").pop() || ""),
        _ts: parseSeen(it.first_seen),
        _sources: Object.keys(it.sources || {}).length,
        _search: normalize(
          `${it.code} ${it.title} ${Object.keys(it.sources || {}).join(" ")}`,
        ),
      }));
      homeRankingCache = Object.fromEntries(
        homePeriods.map((period) => {
          const field = "missav_rank_" + period;
          return [
            period,
            items
              .filter((it) => it[field] > 0)
              .sort((a, b) => a[field] - b[field])
              .slice(0, 10),
          ];
        }),
      );
      homeRankingKey = "";
      byCode = new Map(items.map((it) => [it.code, it]));
      catalogVersion = payload.version || "";
      for (const node of document.querySelectorAll("[data-updated-at]"))
        node.textContent = payload.updated_at || "";
      $("catalogItemCount").textContent = number(items.length);
      $("catalogNewCount").textContent = number(items.filter((it) => it.is_new).length);
      ready = true;
      failedLoad = false;
      if (background) $("grid").replaceChildren();
      render(!background);
      if (background) {
        const replacement = anchorCode && document.querySelector(
          `#grid .video-card[data-code="${CSS.escape(anchorCode)}"]`,
        );
        window.scrollTo(0, replacement
          ? position + replacement.getBoundingClientRect().top - anchorTop
          : position);
      }
    } catch (err) {
      if (background) return;
      failedLoad = true;
      ready = false;
      $("grid").replaceChildren();
      $("emptyState").hidden = false;
      $("resultCount").textContent = "読み込めませんでした";
      $("emptyState").innerHTML =
        `${icon("info")}<h2>作品データを読み込めませんでした</h2><p>通信状態を確認して、もう一度お試しください。</p><button class="pill-button" data-retry>再読み込み</button>`;
    } finally {
      catalogLoading = false;
    }
  }
  restoreURL();
  updateCounts();
  loadCatalog();
  setInterval(checkForCatalogUpdate, CATALOG_REFRESH_MS);
})();
