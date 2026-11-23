/* Euro-Hire front end. Plain JavaScript, no build step. Job text is rendered with textContent. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const PAGE_SIZE = 25;
  const STORAGE_KEY = "eurohirefr:saved-job-ids:v1";
  const ROLES = ["engineering", "data", "product", "design", "sales", "marketing", "support", "operations", "finance", "legal", "people", "procurement", "admin", "business", "healthcare", "education", "leadership"];
  const ROLE_LABEL = {
    engineering: "Engineering", data: "Data & ML", product: "Product", design: "Design",
    sales: "Sales", marketing: "Marketing", support: "Customer success", operations: "Operations",
    finance: "Finance", legal: "Legal & compliance", people: "People & HR",
    procurement: "Procurement", admin: "Administration", business: "Business & strategy",
    healthcare: "Healthcare professionals", education: "Education", leadership: "Executive leadership", other: "Other",
  };
  const LEVEL_LABEL = { intern: "Internship", junior: "Junior / graduate", mid: "Mid-level", senior: "Senior", lead: "Lead / manager" };
  const LEVEL_ORDER = ["intern", "junior", "mid", "senior", "lead"];
  const STOP_WORDS = new Set((
    "the and for with you our are will your have this that from they their about into more than who all can " +
    "has was but not any out new use using work working team role job experience years year ability strong looking skills " +
    "skill including etc per well also across within join help build building company"
  ).split(" "));

  const countryName = (() => {
    try {
      const displayNames = new Intl.DisplayNames(["en"], { type: "region" });
      return (code) => code === "EU" ? "Europe" : (displayNames.of(code) || code);
    } catch {
      return (code) => code;
    }
  })();

  let jobs = [];
  let filteredJobs = [];
  let visibleCount = PAGE_SIZE;
  let cityFilter = null;
  let profile = null;
  let map = null;
  let markerLayer = null;
  let markersByCity = new Map();
  let selectedJobId = null;
  let savedJobIds = loadSavedIds();
  let onlySaved = false;
  let mapWasFit = false;
  let dataLoadError = false;
  let generatedAt = null;

  function element(tag, attributes = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attributes)) {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "dataset" && value && typeof value === "object") Object.assign(node.dataset, value);
      else if (value !== false && value != null) node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of children) {
      if (child == null) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function safeUrl(value) {
    try {
      const url = new URL(value);
      return url.protocol === "https:" ? url.href : null;
    } catch {
      return null;
    }
  }

  function loadSavedIds() {
    try {
      const stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
      return new Set(Array.isArray(stored) ? stored.filter((id) => typeof id === "string") : []);
    } catch {
      return new Set();
    }
  }

  function persistSavedIds() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify([...savedJobIds]));
    } catch {
      // Saved jobs remain available for this tab even when browser storage is unavailable.
    }
    updateSavedCount();
  }

  function updateSavedCount() {
    $("saved-count").textContent = String(savedJobIds.size);
    $("saved-toggle").setAttribute("aria-pressed", String(onlySaved));
    $("saved-toggle").classList.toggle("saved-nav-active", onlySaved);
  }

  function relativeDate(value) {
    if (!value) return "";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "";
    const days = Math.floor((Date.now() - parsed.getTime()) / 864e5);
    if (days <= 0) return "today";
    if (days === 1) return "yesterday";
    if (days < 31) return `${days} days ago`;
    return `${Math.floor(days / 30)} mo ago`;
  }

  function formattedDate(value) {
    if (!value) return "";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "";
    return new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" }).format(parsed);
  }

  function hasCoordinates(job) {
    return job?.lat !== null && job?.lat !== undefined && job?.lon !== null && job?.lon !== undefined &&
      Number.isFinite(Number(job.lat)) && Number.isFinite(Number(job.lon));
  }

  function money(salary) {
    if (!salary || salary.min == null || salary.max == null ||
        !Number.isFinite(Number(salary.min)) || !Number.isFinite(Number(salary.max))) return null;
    const symbols = { EUR: "€", GBP: "£", CHF: "CHF ", DKK: "DKK ", SEK: "SEK ", NOK: "NOK", PLN: "PLN " };
    const symbol = symbols[salary.currency] || `${salary.currency || ""} `;
    const compact = (value) => {
      const n = Number(value);
      if (n >= 10000) {
        const thousands = n / 1000;
        return `${Number.isInteger(thousands) ? thousands : thousands.toFixed(1)}k`;
      }
      return new Intl.NumberFormat("en-GB", { maximumFractionDigits: 0 }).format(n);
    };
    return `${symbol}${compact(salary.min)}–${symbol.trim() ? compact(salary.max) : compact(salary.max)}`;
  }

  function tokens(text) {
    return (String(text || "").toLowerCase().match(/[a-z0-9][a-z0-9+#.\-]{1,}/g) || [])
      .map((token) => token.replace(/[.\-]+$/, ""));
  }

  /* Local-only CV matching: indicative relevance, not a hiring or eligibility score. */
  function levelFromYears(years) {
    if (years < 1) return "intern";
    if (years < 2) return "junior";
    if (years < 5) return "mid";
    if (years < 9) return "senior";
    return "lead";
  }

  function scoreJob(job, candidate) {
    const titleTerms = new Set(tokens(job.title).filter((term) => !STOP_WORDS.has(term)));
    const bodyTerms = new Set(tokens(`${job.snippet || ""} ${job.department || ""}`).filter((term) => term.length > 2 && !STOP_WORDS.has(term)));
    const titleHits = [...titleTerms].filter((term) => candidate.tokens.has(term));
    const bodyHits = [...bodyTerms].filter((term) => candidate.tokens.has(term));
    let points = 0;
    points += titleTerms.size ? Math.min(1, titleHits.length / Math.min(titleTerms.size, 3)) * 30 : 0;
    points += bodyTerms.size ? Math.min(1, bodyHits.length / Math.min(bodyTerms.size, 8)) * 30 : 0;
    const roleMatches = candidate.roles.length ? candidate.roles.includes(job.role) : true;
    points += roleMatches ? 25 : 0;
    const distance = Math.abs(LEVEL_ORDER.indexOf(candidate.level) - LEVEL_ORDER.indexOf(job.level));
    points += distance === 0 ? 15 : distance === 1 ? 8 : 0;
    if (candidate.roles.length && !roleMatches) points *= 0.6;

    const reasons = [];
    const matches = [...new Set([...titleHits, ...bodyHits])].slice(0, 5);
    if (matches.length) reasons.push(`Matches ${matches.join(", ")}`);
    if (candidate.roles.length && roleMatches) reasons.push(`${ROLE_LABEL[job.role] || "Relevant"} role selected`);
    if (distance === 0) reasons.push("Experience level aligns");
    return { score: Math.round(Math.min(100, points)), why: reasons.join(" · ") };
  }

  function currentFilters() {
    return {
      query: $("q").value.trim().toLowerCase(),
      place: $("where").value.trim().toLowerCase(),
      country: $("country").value,
      role: $("role").value,
      level: $("level").value,
      remote: $("remote").checked,
      salary: $("salary").checked,
      fresh: $("fresh").checked,
      sort: $("sort").value,
    };
  }

  function jobMatchesPlace(job, query) {
    if (!query) return true;
    const listedLocations = Array.isArray(job.locations) ? job.locations.flatMap((place) => [
      place.city, place.country, place.country ? countryName(place.country) : ""
    ]) : [];
    const locationText = [job.city, job.location, job.country, job.country ? countryName(job.country) : "", ...listedLocations]
      .filter(Boolean).join(" ").toLowerCase();
    return locationText.includes(query);
  }

  function applyFilters() {
    const filters = currentFilters();
    const freshCutoff = Date.now() - 7 * 864e5;

    filteredJobs = jobs.filter((job) => {
      const searchable = `${job.title || ""} ${job.company || ""} ${job.location || ""} ${job.city || ""} ${job.snippet || ""}`.toLowerCase();
      return (!filters.query || searchable.includes(filters.query)) &&
        jobMatchesPlace(job, filters.place) &&
        (!filters.country || job.country === filters.country) &&
        (!filters.role || job.role === filters.role) &&
        (!filters.level || job.level === filters.level) &&
        (!filters.remote || Boolean(job.remote)) &&
        (!filters.salary || Boolean(job.salary)) &&
        (!filters.fresh || new Date(job.first_seen || job.posted) >= freshCutoff) &&
        (!cityFilter || job.city === cityFilter || (Array.isArray(job.locations) && job.locations.some((place) => place.city === cityFilter))) &&
        (!onlySaved || savedJobIds.has(job.id));
    });

    if (profile) filteredJobs.forEach((job) => { job._match = scoreJob(job, profile); });
    const sortMode = filters.sort === "match" && profile ? "match" : filters.sort === "company" ? "company" : "new";
    const sorters = {
      new: (a, b) => String(b.posted || b.first_seen || "").localeCompare(String(a.posted || a.first_seen || "")),
      match: (a, b) => (b._match?.score ?? -1) - (a._match?.score ?? -1),
      company: (a, b) => String(a.company || "").localeCompare(String(b.company || "")),
    };
    filteredJobs.sort(sorters[sortMode]);
    visibleCount = PAGE_SIZE;
    renderResults();
    drawMap();
  }

  function detailItem(symbol, text) {
    return element("span", { class: "detail-item" },
      element("span", { class: "detail-icon", "aria-hidden": "true", text: symbol }),
      element("span", { text }));
  }

  function makeJobCard(job) {
    const url = safeUrl(job.url);
    const salaryText = money(job.salary);
    const locationText = job.location || (job.country ? countryName(job.country) : "Location not specified");
    const dateLabel = job.posted ? `Posted ${relativeDate(job.posted)}` : `Added ${relativeDate(job.first_seen)}`;
    const card = element("li", { class: `job-card${selectedJobId === job.id ? " selected" : ""}`, "data-job-id": job.id });

    const titleWrap = element("div", { class: "job-title-wrap" },
      element("h3", { text: job.title || "Untitled vacancy" }),
      element("p", { class: "job-company", text: job.company || "Employer not specified" }));
    const actions = element("div", { class: "job-card-actions" });
    const saveButton = element("button", {
      class: "save-button", type: "button", "aria-pressed": String(savedJobIds.has(job.id)),
      "aria-label": savedJobIds.has(job.id) ? `Remove ${job.title} from saved jobs` : `Save ${job.title}`,
      text: savedJobIds.has(job.id) ? "Saved" : "Save",
    });
    saveButton.addEventListener("click", () => toggleSaved(job.id));
    actions.append(saveButton);
    if (job._match) actions.append(element("span", { class: "match-score", text: `${job._match.score}% match` }));

    const head = element("div", { class: "job-card-head" }, titleWrap, actions);
    const details = element("div", { class: "job-details" },
      detailItem("⌖", locationText),
      ...(salaryText ? [detailItem("€", `Salary as listed: ${salaryText}`)] : []),
      ...(dateLabel ? [detailItem("◷", dateLabel)] : []));

    const chips = element("div", { class: "job-chips" });
    if (ROLE_LABEL[job.role]) chips.append(element("span", { class: "job-chip", text: ROLE_LABEL[job.role] }));
    if (LEVEL_LABEL[job.level]) chips.append(element("span", { class: "job-chip", text: LEVEL_LABEL[job.level] }));
    if (job.remote) chips.append(element("span", { class: "job-chip", text: "Remote" }));
    if (job.language) chips.append(element("span", { class: "job-chip", text: `${String(job.language).toUpperCase()} posting` }));

    const snippet = job.snippet ? element("p", { class: "job-snippet", text: job.snippet }) : null;
    const matchReason = job._match?.why ? element("p", { class: "job-match-reason", text: job._match.why }) : null;

    const freshness = element("div", { class: "job-freshness" });
    const lastSeen = formattedDate(job.last_seen);
    freshness.textContent = lastSeen ? `Last found in employer feed ${lastSeen}` : "Listed from an employer career source";
    if (job.career_source_verified) {
      const careerDate = formattedDate(job.source_last_checked || job.last_seen);
      freshness.append(element("span", { class: "source-check-label", text: careerDate ? `Employer career feed checked ${careerDate}` : "Employer career feed checked" }));
    }
    if (job.company_registry_verified) {
      const registryDate = formattedDate(job.company_registry_checked_at);
      freshness.append(element("span", { class: "source-check-label", text: registryDate ? `Company registration checked ${registryDate}` : "Company registration checked" }));
    }

    const footActions = element("div", { class: "job-foot-actions" });
    if (job.city && hasCoordinates(job)) {
      const locate = element("button", { class: "locate-button", type: "button", text: "Show on map" });
      locate.addEventListener("click", () => locateJob(job));
      footActions.append(locate);
    }
    if (url) {
      footActions.append(element("a", {
        class: "apply-link", href: url, target: "_blank", rel: "noopener noreferrer",
        text: "Apply on employer site ↗",
      }));
    } else {
      footActions.append(element("span", { class: "job-freshness", text: "Application link unavailable" }));
    }

    const foot = element("div", { class: "job-card-foot" }, freshness, footActions);
    card.append(head, details, chips);
    if (snippet) card.append(snippet);
    if (matchReason) card.append(matchReason);
    card.append(foot);
    return card;
  }

  function renderResults() {
    const list = $("list");
    list.replaceChildren();
    const companyCount = new Set(filteredJobs.map((job) => job.company).filter(Boolean)).size;
    const countText = `${filteredJobs.length.toLocaleString()} ${filteredJobs.length === 1 ? "role" : "roles"}${companyCount ? ` · ${companyCount} ${companyCount === 1 ? "employer" : "employers"}` : ""}`;
    $("count").textContent = countText;

    if (!filteredJobs.length) {
      const empty = element("li", { class: "empty-state" });
      const heading = onlySaved ? "No saved jobs yet" : "No jobs match these filters";
      const message = onlySaved
        ? "Save a vacancy to keep it here on this device."
        : "Try a broader search, choose another location or clear some filters.";
      empty.append(element("strong", { text: heading }), element("span", { text: message }));
      if (!onlySaved) {
        const clear = element("button", { class: "text-action", type: "button", text: "Clear all filters" });
        clear.addEventListener("click", resetFilters);
        empty.append(clear);
      }
      list.append(empty);
    } else {
      filteredJobs.slice(0, visibleCount).forEach((job) => list.append(makeJobCard(job)));
    }
    $("more").hidden = visibleCount >= filteredJobs.length;

    const cityFilterBox = $("cityfilter");
    cityFilterBox.hidden = !cityFilter;
    cityFilterBox.replaceChildren();
    if (cityFilter) {
      const clearCity = element("button", { type: "button", text: `Showing ${cityFilter} ×` });
      clearCity.addEventListener("click", () => { cityFilter = null; selectedJobId = null; applyFilters(); });
      cityFilterBox.append(clearCity);
    }
  }

  function mapPopup(city, count) {
    const content = element("div", {},
      element("p", { class: "map-popup-title", text: city }),
      element("p", { class: "map-popup-copy", text: `${count} ${count === 1 ? "role" : "roles"} · Select marker to filter` }));
    return content;
  }

  function drawMap() {
    if (!map || !markerLayer) return;
    markerLayer.clearLayers();
    markersByCity = new Map();
    const byCity = new Map();

    for (const job of filteredJobs) {
      if (!hasCoordinates(job) || !job.city) continue;
      const lat = Number(job.lat);
      const lon = Number(job.lon);
      const current = byCity.get(job.city) || { count: 0, lat, lon };
      current.count += 1;
      byCity.set(job.city, current);
    }

    for (const [city, info] of byCity) {
      const selected = cityFilter === city || filteredJobs.some((job) => job.id === selectedJobId && job.city === city);
      const size = Math.round(32 + Math.min(18, Math.sqrt(info.count) * 4));
      const markerIcon = L.divIcon({
        className: "pin-wrap",
        iconSize: [size, size],
        iconAnchor: [size / 2, size / 2],
        popupAnchor: [0, -size / 2],
        html: `<div class="map-pin${selected ? " is-selected" : ""}" style="--pin-size:${size}px"><span>${info.count}</span></div>`,
      });
      const marker = L.marker([info.lat, info.lon], { icon: markerIcon, title: `${city}: ${info.count} jobs`, alt: `${city}: ${info.count} jobs` });
      marker.bindPopup(mapPopup(city, info.count));
      marker.on("click", () => {
        cityFilter = city;
        selectedJobId = null;
        applyFilters();
        if (window.matchMedia("(max-width: 860px)").matches) setMobileView("list");
      });
      marker.addTo(markerLayer);
      markersByCity.set(city, marker);
    }
  }

  function locateJob(job) {
    if (!map || !hasCoordinates(job)) return;
    selectedJobId = job.id;
    renderResults();
    setMobileView("map");
    window.requestAnimationFrame(() => {
      map.invalidateSize();
      map.flyTo([Number(job.lat), Number(job.lon)], Math.max(map.getZoom(), 8), { duration: 0.45 });
      const marker = markersByCity.get(job.city);
      if (marker) window.setTimeout(() => marker.openPopup(), 480);
    });
  }

  function toggleSaved(jobId) {
    if (savedJobIds.has(jobId)) savedJobIds.delete(jobId);
    else savedJobIds.add(jobId);
    persistSavedIds();
    applyFilters();
  }

  function setMobileView(view) {
    const layout = document.querySelector(".discovery-layout");
    layout.dataset.mobileView = view;
    const listButton = $("show-list");
    const mapButton = $("show-map");
    listButton.classList.toggle("active", view === "list");
    mapButton.classList.toggle("active", view === "map");
    listButton.setAttribute("aria-pressed", String(view === "list"));
    mapButton.setAttribute("aria-pressed", String(view === "map"));
    if (view === "map" && map) window.requestAnimationFrame(() => {
      map.invalidateSize();
      if (!mapWasFit) fitAllLocations();
    });
  }

  function resetFilters() {
    $("q").value = "";
    $("where").value = "";
    $("country").value = "";
    $("role").value = "";
    $("level").value = "";
    $("remote").checked = false;
    $("salary").checked = false;
    $("fresh").checked = false;
    $("sort").value = profile ? "match" : "new";
    cityFilter = null;
    selectedJobId = null;
    onlySaved = false;
    updateSavedCount();
    applyFilters();
  }

  function fillControls() {
    const counts = new Map();
    jobs.forEach((job) => {
      if (job.country) counts.set(job.country, (counts.get(job.country) || 0) + 1);
    });
    $("country").replaceChildren(element("option", { value: "", text: `All Europe (${jobs.length})` }));
    [...counts.keys()].sort((a, b) => countryName(a).localeCompare(countryName(b))).forEach((code) => {
      $("country").append(element("option", { value: code, text: `${countryName(code)} (${counts.get(code)})` }));
    });
    for (const role of [...ROLES, "other"]) {
      $("role").append(element("option", { value: role, text: ROLE_LABEL[role] || "Other" }));
    }
    for (const role of ROLES) {
      const label = element("label", {}, element("input", { type: "checkbox", value: role }), ` ${ROLE_LABEL[role]}`);
      $("wantroles").append(label);
    }
  }

  function updateMeta() {
    const companyCount = new Set(jobs.map((job) => job.company).filter(Boolean)).size;
    const dateText = formattedDate(generatedAt);
    $("meta").textContent = jobs.length
      ? `${jobs.length.toLocaleString()} live roles · ${companyCount} employers${dateText ? ` · refreshed ${dateText}` : ""}`
      : (dataLoadError ? "Live job data unavailable" : (dateText ? "No current matching openings" : "Waiting for first live crawl"));

    const banner = $("demo-banner");
    if (dataLoadError) {
      banner.textContent = "Live job data could not be loaded. The website will show real employer-feed results after the crawler completes successfully.";
      banner.hidden = false;
    } else if (!jobs.length) {
      banner.textContent = generatedAt
        ? "No current vacancies passed Euro-Hire's Europe, role and source checks in the latest successful crawl. Check again after the next refresh."
        : "The first live employer crawl runs automatically after GitHub setup. No sample or fictional vacancies are shown.";
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }
  }

  function initialiseMap() {
    if (!window.L) {
      const fallback = element("div", { class: "empty-state", text: "The map could not load. Use the job list to explore available roles." });
      $("map").replaceWith(fallback);
      return;
    }
    map = L.map("map", { worldCopyJump: false, scrollWheelZoom: false, preferCanvas: true });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 18,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors',
    }).addTo(map);
    markerLayer = L.layerGroup().addTo(map);
    map.setView([50.85, 4.35], 5);
    window.addEventListener("resize", () => map.invalidateSize());
  }

  function fitAllLocations() {
    if (!map || map.getSize().x === 0 || map.getSize().y === 0) return;
    const coords = jobs.filter(hasCoordinates)
      .map((job) => [Number(job.lat), Number(job.lon)]);
    if (!coords.length) {
      map.setView([50.85, 4.35], 5);
    } else if (coords.length === 1) {
      map.setView(coords[0], 7);
    } else {
      map.fitBounds(L.latLngBounds(coords), { padding: [26, 26], maxZoom: 6 });
    }
    mapWasFit = true;
  }

  function wireEvents() {
    ["q", "where"].forEach((id) => $(id).addEventListener("input", () => { cityFilter = null; applyFilters(); }));
    ["country", "role", "level", "remote", "salary", "fresh"].forEach((id) => {
      $(id).addEventListener("change", () => { if (id === "country") cityFilter = null; applyFilters(); });
    });
    $("search-form").addEventListener("submit", (event) => { event.preventDefault(); cityFilter = null; applyFilters(); });
    $("sort").addEventListener("change", () => {
      if ($("sort").value === "match" && !profile) {
        $("sort").value = "new";
        $("match").showModal();
        return;
      }
      applyFilters();
    });
    $("reset").addEventListener("click", resetFilters);
    $("map-reset").addEventListener("click", () => { cityFilter = null; selectedJobId = null; applyFilters(); fitAllLocations(); });
    $("more").addEventListener("click", () => { visibleCount += PAGE_SIZE; renderResults(); });
    $("saved-toggle").addEventListener("click", () => {
      onlySaved = !onlySaved;
      cityFilter = null;
      setMobileView("list");
      updateSavedCount();
      applyFilters();
      document.querySelector("#results-heading").scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
    $("show-list").addEventListener("click", () => setMobileView("list"));
    $("show-map").addEventListener("click", () => setMobileView("map"));

    const dialog = $("match");
    $("match-open").addEventListener("click", () => dialog.showModal());
    dialog.querySelector("form").addEventListener("submit", (event) => {
      if (event.submitter?.id !== "match-go") return;
      const resumeText = $("resume").value.trim();
      if (!resumeText) {
        event.preventDefault();
        $("resume").focus();
        return;
      }
      profile = {
        tokens: new Set(tokens(resumeText)),
        level: levelFromYears(Number($("years").value) || 0),
        roles: [...dialog.querySelectorAll("#wantroles input:checked")].map((input) => input.value),
      };
      $("sort").value = "match";
    });
    dialog.addEventListener("close", () => {
      if (dialog.returnValue === "ok" && profile) applyFilters();
    });
  }

  async function initialise() {
    initialiseMap();
    try {
      const response = await fetch("data/jobs.json", { cache: "no-cache" });
      if (!response.ok) throw new Error(`Job data request failed (${response.status})`);
      const data = await response.json();
      generatedAt = data.generated_at || null;
      // Never render placeholder/demo rows, even if an old data file is accidentally uploaded.
      jobs = data.demo === true ? [] : (Array.isArray(data.jobs) ? data.jobs.filter((job) =>
        job && typeof job.country === "string" && /^[A-Z]{2}$/.test(job.country) && job.ats !== "demo" &&
        !String(job.id || "").startsWith("demo:") &&
        typeof job.url === "string" && job.url.startsWith("https://")
      ) : []);
    } catch (error) {
      console.error("Could not load job data:", error);
      dataLoadError = true;
      jobs = [];
    }

    fillControls();
    updateMeta();
    updateSavedCount();
    wireEvents();
    applyFilters();
    fitAllLocations();
  }

  initialise();
})();
