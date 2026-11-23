(() => {
  "use strict";

  const API_BASE = "https://recherche-entreprises.api.gouv.fr/search";
  const API_DOCS = "https://recherche-entreprises.api.gouv.fr/docs/";
  const PAGE_SIZE = 25;
  const STORAGE_KEY = "eurohire.fr-company-shortlist.v1";
  const $ = (id) => document.getElementById(id);

  let currentPage = 1;
  let totalPages = 1;
  let activeController = null;
  let currentRecords = [];
  let shortlist = loadShortlist();
  let verifiedWebsiteRecords = [];

  const EMPLOYEE_BANDS = {
    NN: "Not reported", "00": "0 employees", "01": "1–2 employees", "02": "3–5 employees",
    "03": "6–9 employees", "11": "10–19 employees", "12": "20–49 employees",
    "21": "50–99 employees", "22": "100–199 employees", "31": "200–249 employees",
    "32": "250–499 employees", "41": "500–999 employees", "42": "1,000–1,999 employees",
    "51": "2,000–4,999 employees", "52": "5,000–9,999 employees", "53": "10,000+ employees"
  };

  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined && text !== null) item.textContent = String(text);
    return item;
  }

  function loadShortlist() {
    try {
      const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
      return Array.isArray(raw) ? raw.filter((item) => item && typeof item.siren === "string") : [];
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

  function normaliseRecord(item) {
    const head = item.siege || {};
    const siren = String(item.siren || "");
    const employeeFlag = String(item.caractere_employeur || head.caractere_employeur || "").trim().toUpperCase();
    const employeeBand = String(head.tranche_effectif_salarie || item.tranche_effectif_salarie || "").trim().toUpperCase();
    let employerStatus = "unknown";
    if (employeeFlag === "O" || employeeFlag === "OUI") employerStatus = "employer";
    else if (employeeFlag === "N" || employeeFlag === "NON") employerStatus = "non_employer";
    else if (employeeBand === "NN" || employeeBand === "00") employerStatus = "non_employer";
    else if (/^(0[1-3]|1[12]|2[12]|3[12]|4[12]|5[123])$/.test(employeeBand)) employerStatus = "employer";

    const activity = String(item.activite_principale || head.activite_principale || "");
    return {
      siren,
      name: item.nom_raison_sociale || item.nom_complet || "Name not supplied",
      entityStatus: String(item.etat_administratif || "A").toUpperCase(),
      employerStatus,
      employerFlag: employeeFlag || "Not supplied",
      employeeBand,
      employeeBandLabel: EMPLOYEE_BANDS[employeeBand] || "Not reported",
      legalForm: String(item.nature_juridique || "Not supplied"),
      category: String(item.categorie_entreprise || "Not supplied"),
      activity,
      activityLabel: String(item.section_activite_principale || ""),
      address: String(head.adresse || ""),
      city: String(head.libelle_commune || head.commune || ""),
      postalCode: String(head.code_postal || ""),
      department: String(head.departement || ""),
      region: String(head.region || ""),
      openEstablishments: Number(item.nombre_etablissements_ouverts || 0),
      establishments: Number(item.nombre_etablissements || 0),
      registryUrl: "https://annuaire-entreprises.data.gouv.fr/entreprise/" + encodeURIComponent(siren),
      source: "API Recherche d'entreprises (RNE/SIRENE)"
    };
  }

  function showStatus(message, variant) {
    const status = $("search-status");
    status.textContent = message;
    status.className = "search-status" + (variant ? " " + variant : "");
    status.hidden = !message;
  }

  function makeFact(label, value) {
    const wrap = node("div", "company-fact");
    wrap.append(node("dt", "", label), node("dd", "", value || "Not supplied"));
    return wrap;
  }

  function employerLabel(status) {
    if (status === "employer") return "Employer indicated";
    if (status === "non_employer") return "No employees recorded";
    return "Employer status unknown";
  }

  function renderRecord(record) {
    const card = node("article", "company-card");
    const top = node("div", "company-card-top");
    const nameWrap = node("div", "company-name-wrap");
    nameWrap.append(node("h3", "", record.name), node("p", "company-lei", "SIREN " + record.siren));
    const pills = node("div", "company-state-pills");
    pills.append(
      node("span", "state-pill " + (record.entityStatus === "A" ? "active" : "inactive"), record.entityStatus === "A" ? "Active legal entity" : "Not active"),
      node("span", "state-pill " + (record.employerStatus === "employer" ? "active" : ""), employerLabel(record.employerStatus))
    );
    top.append(nameWrap, pills);

    const facts = node("dl", "company-facts");
    facts.append(
      makeFact("Registered head office", [record.address, record.postalCode, record.city].filter(Boolean).join(", ")),
      makeFact("Region / department", [record.region, record.department].filter(Boolean).join(" / ")),
      makeFact("Employee range", record.employeeBandLabel),
      makeFact("Employer indicator", record.employerFlag),
      makeFact("Main activity code", record.activity),
      makeFact("Legal form code", record.legalForm),
      makeFact("Company category", record.category),
      makeFact("Open establishments", record.openEstablishments.toLocaleString("en-GB") + " of " + record.establishments.toLocaleString("en-GB"))
    );

    const foot = node("div", "company-card-foot");
    const recordLink = node("a", "", "Open official company record ↗");
    recordLink.href = record.registryUrl;
    recordLink.target = "_blank";
    recordLink.rel = "noopener noreferrer";
    const saved = shortlist.some((item) => item.siren === record.siren);
    const saveButton = node("button", "add-candidate", saved ? "Saved to shortlist" : "＋ Save candidate");
    saveButton.type = "button";
    saveButton.disabled = saved;
    saveButton.addEventListener("click", () => addToShortlist(record));
    foot.append(recordLink, saveButton);
    card.append(top, facts, foot);
    return card;
  }

  function filterRecords(records) {
    const employer = $("employer-filter").value;
    return records.filter((record) => employer === "all" || record.employerStatus === employer);
  }

  function renderResults() {
    const container = $("company-results");
    container.replaceChildren();
    const visible = filterRecords(currentRecords);
    if (!currentRecords.length) {
      container.append(emptyState("No registry matches on this page", "Try another company name, SIREN or SIRET, or remove the region and sector filters."));
      return;
    }
    if (!visible.length) {
      container.append(emptyState("No results for this employer filter", "Choose All registry results or move to another results page. The official register did not mark the entities on this page as matching that employer status."));
      return;
    }
    visible.forEach((record) => container.append(renderRecord(record)));
  }

  function emptyState(title, description) {
    const wrap = node("div", "empty-directory-state");
    wrap.append(node("div", "empty-mark", "F"), node("h3", "", title), node("p", "", description));
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
      details.append(node("strong", "", record.name), node("span", "", record.siren + " · " + record.employerLabel));
      const remove = node("button", "remove-shortlist", "×");
      remove.type = "button";
      remove.setAttribute("aria-label", "Remove " + record.name + " from shortlist");
      remove.addEventListener("click", () => {
        shortlist = shortlist.filter((saved) => saved.siren !== record.siren);
        persistShortlist();
      });
      item.append(details, remove);
      container.append(item);
    });
  }

  function addToShortlist(record) {
    if (shortlist.some((item) => item.siren === record.siren)) return;
    shortlist.push({
      siren: record.siren,
      name: record.name,
      city: record.city,
      department: record.department,
      region: record.region,
      employerLabel: employerLabel(record.employerStatus),
      employeeBand: record.employeeBand,
      activity: record.activity,
      legalForm: record.legalForm,
      registryUrl: record.registryUrl,
      source: record.source,
      savedAt: new Date().toISOString()
    });
    persistShortlist();
    showStatus(record.name + " was saved as a registry candidate. Its official website and hiring source are not yet verified.", "");
  }

  function csvCell(value) {
    let text = String(value == null ? "" : value);
    if (/^[=+\-@\t\r]/.test(text)) text = "'" + text;
    return '"' + text.replace(/"/g, '""') + '"';
  }

  function exportCsv() {
    const columns = ["name", "siren", "city", "department", "region", "employerLabel", "employeeBand", "activity", "legalForm", "registryUrl", "source", "savedAt"];
    const rows = [columns.join(","), ...shortlist.map((record) => columns.map((key) => csvCell(record[key])).join(","))];
    const blob = new Blob(["\uFEFF", rows.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = node("a");
    link.href = url;
    link.download = "euro-hire-france-company-shortlist.csv";
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  function updatePagination() {
    $("page-label").textContent = "Page " + currentPage + (totalPages > 1 ? " of " + totalPages : "");
    $("previous-page").disabled = currentPage <= 1;
    $("next-page").disabled = currentPage >= totalPages;
  }

  async function searchCompanies(page) {
    const query = $("company-query").value.trim();
    const region = $("region-filter").value;
    const sector = $("sector-filter").value;
    if (query && query.length < 3 && !/^\\d{9}$|^\\d{14}$/.test(query)) {
      showStatus("Enter at least three characters, a 9-digit SIREN or a 14-digit SIRET.", "error");
      $("company-query").focus();
      return;
    }
    if (!query && !region && !sector) {
      showStatus("Enter a company name, SIREN or SIRET, or choose a French region or activity sector to browse.", "error");
      return;
    }
    currentPage = page || 1;
    if (activeController) activeController.abort();
    const controller = new AbortController();
    activeController = controller;
    const timeout = window.setTimeout(() => controller.abort(), 25000);
    const params = new URLSearchParams();
    if (query) params.set("q", query);
    params.set("page", String(currentPage));
    params.set("per_page", String(PAGE_SIZE));
    params.set("minimal", "true");
    params.set("include", "siege");
    const status = $("entity-status-filter").value;
    if (status) params.set("etat_administratif", status);
    if (region) params.set("region", region);
    if (sector) params.set("section_activite_principale", sector);

    $("result-summary").textContent = "Searching French public registry records…";
    showStatus("Connecting directly to the official French company-search service.", "loading");
    $("company-results").replaceChildren(emptyState("Searching French registry records…", "The API searches public legal-entity records for the whole country."));
    try {
      const response = await fetch(API_BASE + "?" + params.toString(), {
        method: "GET",
        headers: { Accept: "application/json" },
        mode: "cors",
        cache: "no-store",
        signal: controller.signal
      });
      if (!response.ok) {
        if (response.status === 429) throw new Error("The official service is rate-limiting requests. Wait briefly and search again.");
        throw new Error("The French registry service returned HTTP " + response.status + ".");
      }
      const data = await response.json();
      const rawRecords = Array.isArray(data.results) ? data.results : [];
      const seen = new Set();
      currentRecords = rawRecords.map(normaliseRecord).filter((record) => {
        if (!record.siren || seen.has(record.siren)) return false;
        seen.add(record.siren);
        return true;
      });
      const total = Number(data.total_results || data.total || data.meta?.total_results || currentRecords.length);
      totalPages = Math.max(1, Math.min(400, Math.ceil(total / PAGE_SIZE)));
      const visible = filterRecords(currentRecords).length;
      $("result-summary").textContent = total.toLocaleString("en-GB") + " public registry matches · " + visible + " shown on this page";
      const coverageNote = total >= 10000
        ? " This filter has reached the API's 10,000-result ceiling; narrow by region or activity to continue exploring."
        : "";
      showStatus("Source: API Recherche d'entreprises (RNE / SIRENE). The service excludes non-diffusible records and is not a complete SIRENE data dump." + coverageNote);
      updatePagination();
      renderResults();
    } catch (error) {
      if (error.name === "AbortError") {
        if (controller !== activeController) return;
        showStatus("The search timed out. Check your connection and try again.", "error");
      } else {
        console.error("French company registry search failed:", error);
        showStatus(error.message + " You can still search the official Annuaire des Entreprises directly.", "error");
      }
      currentRecords = [];
      $("result-summary").textContent = "Could not complete the French registry search.";
      $("company-results").replaceChildren(emptyState("French company search unavailable", "This page requests public registry records live. Check your connection or open the official company directory."));
      const link = node("a", "", "Open official French company directory ↗");
      link.href = "https://annuaire-entreprises.data.gouv.fr/";
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      $("company-results").append(link);
      totalPages = 1;
      updatePagination();
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function safeHttpsUrl(value) {
    try {
      const parsed = new URL(String(value || ""));
      return parsed.protocol === "https:" ? parsed.href : "";
    } catch {
      return "";
    }
  }

  function renderVerifiedWebsiteCard(record) {
    const card = node("article", "verified-website-card");
    const heading = node("div", "verified-website-heading");
    const identity = node("div", "");
    identity.append(
      node("h3", "", record.name || record.legal_name || "Company name not supplied"),
      node("p", "company-lei", "SIREN " + String(record.siren || ""))
    );
    const verified = node("span", "state-pill active", "Legal notice matched");
    heading.append(identity, verified);

    const placeLocation = [
      record.website_place_address, record.website_place_postcode,
      record.website_place_city, record.website_place_region
    ].filter(Boolean);
    const locationText = placeLocation.length
      ? "Business location in discovery source: " + placeLocation.join(" · ")
      : "Registered head office: " + (record.head_office_address || "Not supplied");
    const details = node("p", "verified-website-location", locationText);
    const links = node("div", "verified-website-links");
    const websiteUrl = safeHttpsUrl(record.official_website);
    const registryUrl = safeHttpsUrl(record.registry_record);
    if (websiteUrl) {
      const website = node("a", "", "Open official website ↗");
      website.href = websiteUrl;
      website.target = "_blank";
      website.rel = "noopener noreferrer";
      links.append(website);
    }
    if (registryUrl) {
      const registry = node("a", "", "Check French registry record ↗");
      registry.href = registryUrl;
      registry.target = "_blank";
      registry.rel = "noopener noreferrer";
      links.append(registry);
    }
    const discoverySources = Array.from(new Set(
      (record.evidence && record.evidence.discovery_sources || [])
        .map((item) => item && (item.provider || item.dataset))
        .filter(Boolean)
    ));
    const proof = node("p", "verified-website-proof",
      "Verification: " + (record.verification_method || "SIREN/SIRET matched in first-party legal notice") +
      (record.verified_at ? " · Checked " + new Date(record.verified_at).toLocaleDateString("en-GB") : "") +
      (discoverySources.length ? " · Candidate source: " + discoverySources.join(", ") : "")
    );
    const legalNoticeUrl = safeHttpsUrl(record.evidence && record.evidence.legal_notice_url);
    if (legalNoticeUrl) {
      const evidenceLink = node("a", "verified-website-evidence", "View matching legal notice ↗");
      evidenceLink.href = legalNoticeUrl;
      evidenceLink.target = "_blank";
      evidenceLink.rel = "noopener noreferrer";
      links.append(evidenceLink);
    }
    card.append(heading, details, links, proof);
    return card;
  }

  function renderVerifiedWebsites() {
    const container = $("verified-websites-results");
    const query = $("verified-website-query").value.trim().toLocaleLowerCase("fr");
    container.replaceChildren();
    const visible = verifiedWebsiteRecords.filter((record) => {
      const haystack = [
        record.name, record.legal_name, record.siren, record.official_website,
        record.head_office_address, record.website_place_address,
        record.website_place_city, record.website_place_postcode, record.website_place_region,
        record.activity_code
      ].filter(Boolean).join(" ").toLocaleLowerCase("fr");
      return !query || haystack.includes(query);
    });
    if (!verifiedWebsiteRecords.length) {
      container.append(emptyState(
        "No company websites have passed verification yet",
        "This means the current discovery database has not found a matching SIREN/SIRET in an accessible first-party legal notice. It does not mean French employers have no websites."
      ));
      return;
    }
    if (!visible.length) {
      container.append(emptyState("No verified website matches", "Try a different company name, city, SIREN or domain."));
      return;
    }
    visible.slice(0, 100).forEach((record) => container.append(renderVerifiedWebsiteCard(record)));
    if (visible.length > 100) {
      container.append(node("p", "verified-website-proof",
        "Showing 100 of " + visible.length.toLocaleString("en-GB") + " matches. Narrow your search to see more."
      ));
    }
  }

  async function loadVerifiedWebsites() {
    const status = $("verified-websites-status");
    try {
      const response = await fetch("data/france-company-websites.json", {
        method: "GET", headers: { Accept: "application/json" }, cache: "no-store"
      });
      if (!response.ok) throw new Error("Dataset returned HTTP " + response.status);
      const payload = await response.json();
      verifiedWebsiteRecords = Array.isArray(payload.companies) ? payload.companies : [];
      status.textContent = verifiedWebsiteRecords.length.toLocaleString("en-GB") +
        " verified French company websites · Candidate discovery: Overture Maps and SIREN-linked Wikidata records · Approval requires a matching first-party legal notice";
      status.className = "search-status";
      renderVerifiedWebsites();
    } catch (error) {
      status.textContent = "The verified company website dataset is not available yet. " + error.message;
      status.className = "search-status error";
      $("verified-websites-results").replaceChildren(emptyState(
        "Verified website dataset unavailable",
        "The discovery workflow has not published its evidence-backed dataset yet."
      ));
    }
  }

  function wireEvents() {
    $("company-search").addEventListener("submit", (event) => {
      event.preventDefault();
      searchCompanies(1);
    });
    document.querySelectorAll("[data-sector]").forEach((button) => {
      button.addEventListener("click", () => {
        $("company-query").value = "";
        $("sector-filter").value = button.dataset.sector;
        searchCompanies(1);
      });
    });
    ["region-filter", "sector-filter", "entity-status-filter"].forEach((id) => {
      $(id).addEventListener("change", () => {
        const query = $("company-query").value.trim();
        if (query.length >= 3 || /^\\d{9}$|^\\d{14}$/.test(query) || $("region-filter").value || $("sector-filter").value) {
          searchCompanies(1);
        }
      });
    });
    $("employer-filter").addEventListener("change", renderResults);
    $("verified-website-query").addEventListener("input", renderVerifiedWebsites);
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
  loadVerifiedWebsites();
  // No hard-coded sample company is searched on load. The first result set
  // must come from the visitor's own query or a sector shortcut.
})();