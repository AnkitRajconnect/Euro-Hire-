(() => {
  "use strict";

  const API_BASE = "https://api.gleif.org/api/v1";
  const PAGE_SIZE = 20;
  const STORAGE_KEY = "gullyhire.company-shortlist.v1";
  const $ = (id) => document.getElementById(id);

  let currentQuery = "";
  let currentPage = 1;
  let totalPages = 1;
  let activeController = null;
  let currentRecords = [];
  let shortlist = loadShortlist();

  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined && text !== null) item.textContent = String(text);
    return item;
  }

  function loadShortlist() {
    try {
      const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
      return Array.isArray(raw) ? raw.filter((item) => item && typeof item.lei === "string") : [];
    } catch {
      return [];
    }
  }

  function persistShortlist() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(shortlist));
    } catch {
      showStatus("Browser storage is unavailable. Your saved list will remain only until this page is closed.", "error");
    }
    renderShortlist();
    renderResults();
  }

  function valueAt(object, path, fallback = "") {
    const result = path.split(".").reduce((value, key) => value == null ? undefined : value[key], object);
    return result == null ? fallback : result;
  }

  function arrayText(value) {
    if (!Array.isArray(value)) return "";
    return value.filter(Boolean).join(", ");
  }

  function normaliseRecord(record) {
    const attributes = record?.attributes || record || {};
    const entity = attributes.entity || {};
    const registration = attributes.registration || {};
    const address = entity.legalAddress || {};
    const legalName = entity.legalName;
    const legalForm = entity.legalForm || {};
    const authority = entity.registrationAuthority || {};
    const registeredAt = entity.registeredAt || {};
    const lei = attributes.lei || record?.id || "";
    return {
      lei,
      name: typeof legalName === "string" ? legalName : valueAt(legalName, "name", "Name not supplied"),
      entityStatus: String(entity.status || "UNKNOWN").toUpperCase(),
      registrationStatus: String(registration.status || "UNKNOWN").toUpperCase(),
      country: String(address.country || "").toUpperCase(),
      city: address.city || "",
      region: address.region || "",
      postalCode: address.postalCode || "",
      addressLines: arrayText(address.addressLines),
      legalForm: legalForm.other || legalForm.id || "Not supplied",
      registrationAuthority: registeredAt.id || authority.registrationAuthorityID || "",
      registrationNumber: entity.registeredAs || authority.registrationAuthorityEntityID || "",
      initialRegistrationDate: registration.initialRegistrationDate || "",
      lastUpdateDate: registration.lastUpdateDate || "",
      nextRenewalDate: registration.nextRenewalDate || "",
    };
  }

  function safeDate(value) {
    if (!value) return "Not supplied";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }).format(date);
  }

  function countryName(code) {
    if (!code) return "Country not supplied";
    try { return new Intl.DisplayNames(["en"], { type: "region" }).of(code) || code; }
    catch { return code; }
  }

  function showStatus(message, variant = "") {
    const status = $("search-status");
    status.textContent = message;
    status.className = `search-status${variant ? ` ${variant}` : ""}`;
    status.hidden = !message;
  }

  function makeFact(label, value) {
    const wrap = node("div", "company-fact");
    wrap.append(node("dt", "", label), node("dd", "", value || "Not supplied"));
    return wrap;
  }

  function renderRecord(record) {
    const card = node("article", "company-card");
    const top = node("div", "company-card-top");
    const nameWrap = node("div", "company-name-wrap");
    nameWrap.append(node("h3", "", record.name), node("p", "company-lei", `LEI ${record.lei}`));
    const pills = node("div", "company-state-pills");
    const entityPill = node("span", `state-pill ${record.entityStatus.toLowerCase()}`, `Entity ${record.entityStatus}`);
    const registrationPill = node("span", `state-pill ${record.registrationStatus.toLowerCase()}`, `LEI ${record.registrationStatus}`);
    pills.append(entityPill, registrationPill);
    top.append(nameWrap, pills);

    const facts = node("dl", "company-facts");
    const address = [record.addressLines, record.city, record.region, record.postalCode, record.country].filter(Boolean).join(", ");
    facts.append(
      makeFact("Registered address", address || "Not supplied"),
      makeFact("Legal form", record.legalForm),
      makeFact("Registration authority", record.registrationAuthority || "Not supplied"),
      makeFact("National registration number", record.registrationNumber || "Not supplied"),
      makeFact("Initial LEI registration", safeDate(record.initialRegistrationDate)),
      makeFact("LEI record last updated", safeDate(record.lastUpdateDate)),
      makeFact("Next renewal date", safeDate(record.nextRenewalDate)),
      makeFact("Country", countryName(record.country)),
    );

    const foot = node("div", "company-card-foot");
    const recordLink = node("a", "", "Open GLEIF JSON record ↗");
    recordLink.href = `${API_BASE}/lei-records/${encodeURIComponent(record.lei)}`;
    recordLink.target = "_blank";
    recordLink.rel = "noopener noreferrer";
    const saved = shortlist.some((item) => item.lei === record.lei);
    const saveButton = node("button", "add-candidate", saved ? "Saved to shortlist" : "＋ Save candidate");
    saveButton.type = "button";
    saveButton.disabled = saved;
    saveButton.addEventListener("click", () => addToShortlist(record));
    foot.append(recordLink, saveButton);
    card.append(top, facts, foot);
    return card;
  }

  function filterRecords(records) {
    const country = "FR";
    const status = $("entity-status-filter").value;
    return records.filter((record) => (!country || record.country === country) && (!status || record.entityStatus === status));
  }

  function renderResults() {
    const container = $("company-results");
    container.replaceChildren();
    const visible = filterRecords(currentRecords);
    if (!currentRecords.length) {
      container.append(emptyState("No results on this page", "Try a shorter company name or another spelling. If the company has no LEI, it may not appear in this database."));
      return;
    }
    if (!visible.length) {
      container.append(emptyState("No matches for these filters", "Change the country or entity-status filter to see the returned records."));
      return;
    }
    visible.forEach((record) => container.append(renderRecord(record)));
  }

  function emptyState(title, description) {
    const wrap = node("div", "empty-directory-state");
    wrap.append(node("div", "empty-mark", "G"), node("h3", "", title), node("p", "", description));
    return wrap;
  }

  function renderShortlist() {
    const container = $("shortlist-items");
    container.replaceChildren();
    $("shortlist-count").textContent = String(shortlist.length);
    $("export-shortlist").disabled = shortlist.length === 0;
    $("clear-shortlist").disabled = shortlist.length === 0;
    if (!shortlist.length) {
      container.append(node("p", "shortlist-empty", "No saved companies yet."));
      return;
    }
    shortlist.forEach((record) => {
      const item = node("div", "shortlist-item");
      const details = node("div", "");
      details.append(node("strong", "", record.name), node("span", "", `${record.lei} · ${countryName(record.country)}`));
      const remove = node("button", "remove-shortlist", "×");
      remove.type = "button";
      remove.setAttribute("aria-label", `Remove ${record.name} from shortlist`);
      remove.addEventListener("click", () => {
        shortlist = shortlist.filter((saved) => saved.lei !== record.lei);
        persistShortlist();
      });
      item.append(details, remove);
      container.append(item);
    });
  }

  function addToShortlist(record) {
    if (shortlist.some((item) => item.lei === record.lei)) return;
    shortlist.push({
      lei: record.lei, name: record.name, country: record.country,
      entityStatus: record.entityStatus, registrationStatus: record.registrationStatus,
      legalForm: record.legalForm, city: record.city, address: record.addressLines,
      registrationNumber: record.registrationNumber, registrationAuthority: record.registrationAuthority,
      initialRegistrationDate: record.initialRegistrationDate, lastUpdateDate: record.lastUpdateDate,
      source: "GLEIF Global LEI Index", savedAt: new Date().toISOString(),
    });
    persistShortlist();
    showStatus(`${record.name} was added to your browser shortlist. It has not been verified or approved as an employer.`, "");
  }

  function csvCell(value) {
    let text = String(value ?? "");
    if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`;
    return `"${text.replace(/"/g, '""')}"`;
  }

  function exportCsv() {
    const columns = ["name", "lei", "country", "city", "address", "entityStatus", "registrationStatus", "legalForm", "registrationAuthority", "registrationNumber", "initialRegistrationDate", "lastUpdateDate", "source", "savedAt"];
    const rows = [columns.join(","), ...shortlist.map((record) => columns.map((key) => csvCell(record[key])).join(","))];
    const blob = new Blob(["\uFEFF", rows.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = node("a");
    link.href = url;
    link.download = "gullyhire-company-shortlist.csv";
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  function updatePagination() {
    $("page-label").textContent = `Page ${currentPage}${totalPages > 1 ? ` of ${totalPages}` : ""}`;
    $("previous-page").disabled = currentPage <= 1;
    $("next-page").disabled = currentPage >= totalPages;
  }

  async function searchCompanies(page = 1) {
    const query = $("company-query").value.trim();
    if (query.length < 2) {
      showStatus("Enter at least two characters to search.", "error");
      $("company-query").focus();
      return;
    }
    currentQuery = query;
    currentPage = page;
    if (activeController) activeController.abort();
    const controller = new AbortController();
    activeController = controller;
    const timeout = window.setTimeout(() => controller.abort(), 20000);
    $("result-summary").textContent = "Searching the GLEIF public database…";
    showStatus("Connecting directly to the public GLEIF API. No API key is being used.", "loading");
    $("company-results").replaceChildren();
    $("company-results").append(emptyState("Searching registry records…", "The first search may take a few seconds."));

    try {
      const isLei = /^[0-9A-Z]{20}$/i.test(query);
      let url;
      if (isLei) {
        url = `${API_BASE}/lei-records/${encodeURIComponent(query.toUpperCase())}`;
      } else {
        const params = new URLSearchParams();
        params.set("filter[entity.legalName]", query);
        const country = "FR";
        const entityStatus = $("entity-status-filter").value;
        if (country) params.set("filter[entity.legalAddress.country]", country);
        if (entityStatus) params.set("filter[entity.status]", entityStatus);
        params.set("page[size]", String(PAGE_SIZE));
        params.set("page[number]", String(page));
        url = `${API_BASE}/lei-records?${params.toString()}`;
      }
      const response = await fetch(url, { method: "GET", headers: { Accept: "application/vnd.api+json, application/json" }, mode: "cors", cache: "no-store", signal: controller.signal });
      if (!response.ok) {
        if (response.status === 404 && isLei) throw new Error("That LEI was not found in the GLEIF index.");
        if (response.status === 429) throw new Error("The public data provider is receiving too many requests. Please wait a little and try again.");
        throw new Error(`GLEIF returned HTTP ${response.status}. Please try again.`);
      }
      const data = await response.json();
      const rawRecords = Array.isArray(data.data) ? data.data : (data.data ? [data.data] : []);
      currentRecords = rawRecords.map(normaliseRecord).filter((record) => record.lei);
      const total = Number(data.meta?.pagination?.total || (isLei ? currentRecords.length : currentRecords.length));
      totalPages = isLei ? 1 : Math.max(1, Math.ceil(total / PAGE_SIZE));
      const filteredCount = filterRecords(currentRecords).length;
      $("result-summary").textContent = `${total.toLocaleString()} registry match${total === 1 ? "" : "es"} · ${filteredCount} shown on this page`;
      showStatus(currentRecords.length ? `Source: GLEIF Global LEI Index · ${new Date().toLocaleString()}` : "No matching entity was returned. Try a different spelling or check the company in GLEIF's own search tool.");
      updatePagination();
      renderResults();
    } catch (error) {
      if (error.name === "AbortError") {
        if (controller !== activeController) return;
        showStatus("The search timed out. Check your connection and try again.", "error");
      } else {
        console.error("GLEIF search failed:", error);
        showStatus(`${error.message} The public directory needs an internet connection. You can also search directly on GLEIF's website.`, "error");
      }
      currentRecords = [];
      $("result-summary").textContent = "Could not complete the search.";
      $("company-results").replaceChildren(emptyState("Company search unavailable", "The GullyHire page is static; company data is requested live from GLEIF. Check your internet connection or use the official GLEIF search link below."));
      const link = node("a", "", "Open GLEIF search ↗");
      link.href = "https://www.gleif.org/en/lei-data/lei-search";
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      $("company-results").append(link);
      totalPages = 1;
      updatePagination();
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function wireEvents() {
    $("company-search").addEventListener("submit", (event) => {
      event.preventDefault();
      searchCompanies(1);
    });
    document.querySelectorAll("[data-query]").forEach((button) => {
      button.addEventListener("click", () => {
        $("company-query").value = button.dataset.query;
        searchCompanies(1);
      });
    });
    $("country-filter").addEventListener("change", () => {
      if ($("company-query").value.trim().length >= 2) searchCompanies(1);
      else renderResults();
    });
    $("entity-status-filter").addEventListener("change", () => {
      if ($("company-query").value.trim().length >= 2) searchCompanies(1);
      else renderResults();
    });
    $("previous-page").addEventListener("click", () => { if (currentPage > 1) searchCompanies(currentPage - 1); });
    $("next-page").addEventListener("click", () => { if (currentPage < totalPages) searchCompanies(currentPage + 1); });
    $("export-shortlist").addEventListener("click", exportCsv);
    $("clear-shortlist").addEventListener("click", () => {
      if (!shortlist.length || !window.confirm("Clear all saved company candidates from this browser?")) return;
      shortlist = [];
      persistShortlist();
    });
  }

  wireEvents();
  renderShortlist();
  updatePagination();
  // Run one real, no-key example search so the database page is useful immediately.
  searchCompanies(1);
})();
