/* 展示会・商談会 公募案件ダッシュボード
 * data/listings.json, data/scrape_log.json を読み込んで表示するだけの
 * 静的サイト用スクリプト。ビルド工程なし・依存ライブラリなし。
 */

(function () {
  "use strict";

  const state = {
    items: [],
    orgs: [],
    log: null,
  };

  const els = {
    lastUpdated: document.getElementById("lastUpdated"),
    tableBody: document.getElementById("listingTableBody"),
    resultCount: document.getElementById("resultCount"),
    emptyState: document.getElementById("emptyState"),
    searchInput: document.getElementById("searchInput"),
    categoryFilter: document.getElementById("categoryFilter"),
    periodFilter: document.getElementById("periodFilter"),
    sortSelect: document.getElementById("sortSelect"),
    orgFilter: document.getElementById("orgFilter"),

    statusToggleBtn: document.getElementById("statusToggleBtn"),
    closeStatusBtn: document.getElementById("closeStatusBtn"),
    statusPanel: document.getElementById("statusPanel"),
    statusSummary: document.getElementById("statusSummary"),
    statusTableBody: document.getElementById("statusTableBody"),

    addOrgBtn: document.getElementById("addOrgBtn"),
    closeModalBtn: document.getElementById("closeModalBtn"),
    addOrgModal: document.getElementById("addOrgModal"),
    addOrgForm: document.getElementById("addOrgForm"),
    addOrgResult: document.getElementById("addOrgResult"),

    ghOwner: document.getElementById("ghOwner"),
    ghRepo: document.getElementById("ghRepo"),
    ghBranch: document.getElementById("ghBranch"),
    ghToken: document.getElementById("ghToken"),
    saveGhSettingsBtn: document.getElementById("saveGhSettingsBtn"),
    ghSettingsStatus: document.getElementById("ghSettingsStatus"),
    repoLink: document.getElementById("repoLink"),
  };

  const GH_STORAGE_KEY = "koboDashboardGhSettings";

  // ---------- data loading ----------

  async function loadJson(path) {
    const res = await fetch(path + "?t=" + Date.now(), { cache: "no-store" });
    if (!res.ok) throw new Error(`${path} の取得に失敗しました (${res.status})`);
    return res.json();
  }

  async function init() {
    detectRepoFromLocation();
    loadGhSettingsIntoForm();
    bindEvents();

    try {
      const [listings, orgConfig, log] = await Promise.all([
        loadJson("data/listings.json"),
        loadJson("config/organizations.json"),
        loadJson("data/scrape_log.json").catch(() => null),
      ]);
      state.items = listings.items || [];
      state.orgs = (orgConfig.organizations || []);
      state.log = log;

      renderLastUpdated(listings.generated_at);
      populateOrgFilter();
      renderStatusPanel();
      applyFiltersAndRender();
    } catch (err) {
      els.tableBody.innerHTML = "";
      els.resultCount.textContent = "";
      els.emptyState.classList.remove("hidden");
      els.emptyState.textContent =
        "データの読み込みに失敗しました: " + err.message +
        "（GitHub Actions が未実行の場合、data/listings.json はまだ空です）";
    }
  }

  function detectRepoFromLocation() {
    // https://<owner>.github.io/<repo>/... 形式のプロジェクトページを想定した自動検出。
    // カスタムドメインや別ホスティングの場合は「GitHub連携設定」で手動入力する。
    const host = location.hostname; // e.g. your-org.github.io
    const pathParts = location.pathname.split("/").filter(Boolean);
    if (host.endsWith(".github.io") && pathParts.length > 0) {
      const owner = host.replace(".github.io", "");
      const repo = pathParts[0];
      const saved = getGhSettings();
      if (!saved.owner) saved.owner = owner;
      if (!saved.repo) saved.repo = repo;
      saveGhSettings(saved);
      els.repoLink.href = `https://github.com/${owner}/${repo}`;
    }
  }

  // ---------- rendering ----------

  function renderLastUpdated(generatedAt) {
    if (!generatedAt) {
      els.lastUpdated.textContent = "最終更新: 未実行（GitHub Actions の初回実行待ち）";
      return;
    }
    const d = new Date(generatedAt);
    els.lastUpdated.textContent = "最終更新: " + d.toLocaleString("ja-JP", {
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit",
    });
  }

  function populateOrgFilter() {
    const names = Array.from(new Set(state.items.map((i) => i.org_name))).sort();
    for (const name of names) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      els.orgFilter.appendChild(opt);
    }
  }

  function applyFiltersAndRender() {
    const q = els.searchInput.value.trim();
    const category = els.categoryFilter.value;
    const period = els.periodFilter.value;
    const org = els.orgFilter.value;
    const sort = els.sortSelect.value;

    let items = state.items.slice();

    if (q) {
      const terms = q.split(/\s+/).filter(Boolean);
      items = items.filter((i) => {
        const hay = (i.title + " " + (i.context || "") + " " + i.org_name).toLowerCase();
        return terms.every((t) => hay.includes(t.toLowerCase()));
      });
    }

    if (category !== "all") {
      items = items.filter((i) => i.org_category === category);
    }

    if (org !== "all") {
      items = items.filter((i) => i.org_name === org);
    }

    if (period !== "all") {
      const days = parseInt(period, 10);
      const cutoff = Date.now() - days * 24 * 60 * 60 * 1000;
      items = items.filter((i) => new Date(i.last_seen).getTime() >= cutoff);
    }

    items.sort((a, b) => {
      if (sort === "new") return b.first_seen.localeCompare(a.first_seen);
      if (sort === "recent") return b.last_seen.localeCompare(a.last_seen);
      if (sort === "org") return a.org_name.localeCompare(b.org_name, "ja");
      return 0;
    });

    renderTable(items);
  }

  function renderTable(items) {
    els.resultCount.textContent = `${items.length} 件表示中（全 ${state.items.length} 件）`;
    els.tableBody.innerHTML = "";

    if (items.length === 0) {
      els.emptyState.classList.remove("hidden");
      return;
    }
    els.emptyState.classList.add("hidden");

    const frag = document.createDocumentFragment();
    for (const item of items) {
      const tr = document.createElement("tr");

      const orgTd = document.createElement("td");
      const badge = document.createElement("span");
      badge.className = "badge " + (item.org_category === "prefecture" ? "badge-prefecture" : "badge-partner");
      badge.textContent = item.org_category === "prefecture" ? "都道府県" : "連携団体";
      orgTd.appendChild(badge);
      const orgNameSpan = document.createElement("div");
      orgNameSpan.textContent = item.org_name;
      orgNameSpan.style.marginTop = "4px";
      orgTd.appendChild(orgNameSpan);
      tr.appendChild(orgTd);

      const titleTd = document.createElement("td");
      titleTd.className = "item-title";
      const a = document.createElement("a");
      a.href = item.url;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = item.title;
      titleTd.appendChild(a);
      if (item.context && item.context !== item.title) {
        const ctx = document.createElement("div");
        ctx.className = "item-context";
        ctx.textContent = item.context;
        titleTd.appendChild(ctx);
      }
      tr.appendChild(titleTd);

      const kwTd = document.createElement("td");
      (item.matched_keywords || []).forEach((k) => {
        const chip = document.createElement("span");
        chip.className = "kw-chip";
        chip.textContent = k;
        kwTd.appendChild(chip);
      });
      tr.appendChild(kwTd);

      const firstTd = document.createElement("td");
      firstTd.textContent = item.first_seen;
      tr.appendChild(firstTd);

      const lastTd = document.createElement("td");
      lastTd.textContent = item.last_seen;
      tr.appendChild(lastTd);

      const sourceTd = document.createElement("td");
      const sa = document.createElement("a");
      sa.href = item.source_page;
      sa.target = "_blank";
      sa.rel = "noopener";
      sa.textContent = "一覧ページ";
      sourceTd.appendChild(sa);
      tr.appendChild(sourceTd);

      frag.appendChild(tr);
    }
    els.tableBody.appendChild(frag);
  }

  function renderStatusPanel() {
    if (!state.log || !state.log.entries) {
      els.statusSummary.textContent = "収集ログがまだありません。";
      return;
    }
    const { org_count, error_count, matched_today, new_items_today, generated_at } = state.log;
    els.statusSummary.innerHTML = `
      <span>対象団体数: <strong>${org_count}</strong></span>
      <span class="${error_count > 0 ? "status-error" : "status-ok"}">エラー: <strong>${error_count}</strong> 件</span>
      <span>本日のマッチ件数: <strong>${matched_today}</strong></span>
      <span>新規案件: <strong>${new_items_today}</strong></span>
      <span>実行日時(UTC): ${generated_at || "-"}</span>
    `;

    els.statusTableBody.innerHTML = "";
    const frag = document.createDocumentFragment();
    for (const e of state.log.entries) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(e.org_name)}</td>
        <td class="${e.status === "ok" ? "status-ok" : "status-error"}">${e.status}</td>
        <td>${e.matched_count}</td>
        <td>${escapeHtml(e.error || "")}</td>
        <td>${escapeHtml(e.checked_at || "")}</td>
      `;
      frag.appendChild(tr);
    }
    els.statusTableBody.appendChild(frag);
  }

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ---------- GitHub settings ----------

  function getGhSettings() {
    try {
      return JSON.parse(localStorage.getItem(GH_STORAGE_KEY) || "{}");
    } catch {
      return {};
    }
  }

  function saveGhSettings(settings) {
    localStorage.setItem(GH_STORAGE_KEY, JSON.stringify(settings));
  }

  function loadGhSettingsIntoForm() {
    const s = getGhSettings();
    els.ghOwner.value = s.owner || "";
    els.ghRepo.value = s.repo || "";
    els.ghBranch.value = s.branch || "main";
    els.ghToken.value = s.token || "";
  }

  // ---------- events ----------

  function bindEvents() {
    [els.searchInput, els.categoryFilter, els.periodFilter, els.orgFilter, els.sortSelect].forEach((el) => {
      el.addEventListener("input", applyFiltersAndRender);
      el.addEventListener("change", applyFiltersAndRender);
    });

    els.statusToggleBtn.addEventListener("click", () => {
      els.statusPanel.classList.toggle("hidden");
    });
    els.closeStatusBtn.addEventListener("click", () => {
      els.statusPanel.classList.add("hidden");
    });

    els.addOrgBtn.addEventListener("click", () => {
      els.addOrgModal.classList.remove("hidden");
      els.addOrgResult.textContent = "";
    });
    els.closeModalBtn.addEventListener("click", () => {
      els.addOrgModal.classList.add("hidden");
    });
    els.addOrgModal.addEventListener("click", (e) => {
      if (e.target === els.addOrgModal) els.addOrgModal.classList.add("hidden");
    });

    els.saveGhSettingsBtn.addEventListener("click", () => {
      saveGhSettings({
        owner: els.ghOwner.value.trim(),
        repo: els.ghRepo.value.trim(),
        branch: els.ghBranch.value.trim() || "main",
        token: els.ghToken.value.trim(),
      });
      els.ghSettingsStatus.textContent = "保存しました";
      els.ghSettingsStatus.style.color = "var(--color-ok)";
      setTimeout(() => (els.ghSettingsStatus.textContent = ""), 3000);
    });

    els.addOrgForm.addEventListener("submit", handleAddOrgSubmit);
  }

  // ---------- add organization via GitHub Contents API ----------

  async function handleAddOrgSubmit(e) {
    e.preventDefault();
    const settings = getGhSettings();
    if (!settings.owner || !settings.repo || !settings.token) {
      els.addOrgResult.textContent =
        "先に「GitHub連携設定」で Owner / Repo / Token を保存してください。";
      els.addOrgResult.style.color = "var(--color-danger)";
      els.ghSettingsDetails ? (document.getElementById("ghSettingsDetails").open = true) : null;
      return;
    }

    const name = document.getElementById("orgName").value.trim();
    const category = document.getElementById("orgCategory").value;
    const url = document.getElementById("orgUrl").value.trim();
    const note = document.getElementById("orgNote").value.trim();

    if (!name || !url) return;

    const id = slugify(name);
    const submitBtn = els.addOrgForm.querySelector("button[type=submit]");
    submitBtn.disabled = true;
    els.addOrgResult.style.color = "var(--color-text-muted)";
    els.addOrgResult.textContent = "GitHubへコミット中...";

    try {
      const apiBase = `https://api.github.com/repos/${settings.owner}/${settings.repo}`;
      const path = "config/organizations.json";
      const branch = settings.branch || "main";

      const getRes = await fetch(`${apiBase}/contents/${path}?ref=${branch}`, {
        headers: ghHeaders(settings.token),
      });
      if (!getRes.ok) throw new Error(`ファイル取得に失敗 (${getRes.status})`);
      const fileData = await getRes.json();
      const currentContent = JSON.parse(decodeBase64Utf8(fileData.content));

      if (currentContent.organizations.some((o) => o.id === id)) {
        throw new Error("同名のidが既に存在します。団体名を変えてください。");
      }

      currentContent.organizations.push({
        id,
        name,
        category,
        url,
        format: "html_list",
        note: note || "ダッシュボードから追加",
        active: true,
        added_by: "dashboard",
        added_at: new Date().toISOString().slice(0, 10),
      });

      const newContentB64 = encodeBase64Utf8(JSON.stringify(currentContent, null, 2) + "\n");

      const putRes = await fetch(`${apiBase}/contents/${path}`, {
        method: "PUT",
        headers: ghHeaders(settings.token),
        body: JSON.stringify({
          message: `chore: add organization "${name}" via dashboard`,
          content: newContentB64,
          sha: fileData.sha,
          branch,
        }),
      });
      if (!putRes.ok) {
        const errBody = await putRes.json().catch(() => ({}));
        throw new Error(`コミットに失敗 (${putRes.status}) ${errBody.message || ""}`);
      }

      els.addOrgResult.style.color = "var(--color-ok)";
      els.addOrgResult.textContent =
        "追加しました。次回の日次実行（または手動実行）で収集対象になります。";
      els.addOrgForm.reset();
    } catch (err) {
      els.addOrgResult.style.color = "var(--color-danger)";
      els.addOrgResult.textContent = "エラー: " + err.message;
    } finally {
      submitBtn.disabled = false;
    }
  }

  function ghHeaders(token) {
    return {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
      "X-GitHub-Api-Version": "2022-11-28",
    };
  }

  function slugify(name) {
    const base = name
      .normalize("NFKC")
      .replace(/[^\p{L}\p{N}]+/gu, "_")
      .replace(/^_+|_+$/g, "");
    return (base || "org") + "_" + Math.random().toString(36).slice(2, 7);
  }

  function decodeBase64Utf8(b64) {
    const binary = atob(b64.replace(/\n/g, ""));
    const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
    return new TextDecoder("utf-8").decode(bytes);
  }

  function encodeBase64Utf8(str) {
    const bytes = new TextEncoder().encode(str);
    let binary = "";
    bytes.forEach((b) => (binary += String.fromCharCode(b)));
    return btoa(binary);
  }

  init();
})();
