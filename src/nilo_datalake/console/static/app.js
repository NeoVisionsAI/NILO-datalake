const app = document.querySelector("#app");
const sections = [
  ["overview", "Overview"],
  ["general", "General"],
  ["schedule", "Schedule"],
  ["storage", "Storage"],
  ["minio", "MinIO"],
  ["ingress", "Data ingress"],
  ["api", "Test API"],
  ["account", "Account"],
];

let state = null;
let dashboard = null;
let section = "overview";
let message = null;
let connectionTests = { minio: null, pull: null, ingest: null, direct: null };
let storageBrowse = { open: false, path: "/", volume: null };
let pathChecks = {};
let openApiDoc = null;
let openApiLoading = false;
let loadingCount = 0;
let testIngestKey = "";
let testApiPanel = { open: false, ok: null, text: "" };

const ARCHIVE_TRY = {
  "get /v1/health": "health",
  "get /v1/status": "status",
  "post /v1/batches": "open_batch",
};

ensureChrome();
boot();

function ensureChrome() {
  if (!document.getElementById("top-loader")) {
    const bar = el("div", { className: "top-loader", id: "top-loader" });
    bar.append(el("div", { className: "top-loader-bar" }));
    document.body.prepend(bar);
  }
  if (!document.getElementById("toast-host")) {
    document.body.append(el("div", { className: "toast-host", id: "toast-host" }));
  }
}

function beginLoading() {
  loadingCount += 1;
  document.getElementById("top-loader")?.classList.add("active");
}

function endLoading() {
  loadingCount = Math.max(0, loadingCount - 1);
  if (loadingCount === 0) {
    document.getElementById("top-loader")?.classList.remove("active");
  }
}

async function withLoading(fn) {
  beginLoading();
  try {
    return await fn();
  } finally {
    endLoading();
  }
}

function toast(kind, text) {
  ensureChrome();
  const host = document.getElementById("toast-host");
  const node = el("div", { className: `toast toast-${kind}` });
  node.textContent = text;
  host.append(node);
  requestAnimationFrame(() => node.classList.add("show"));
  setTimeout(() => {
    node.classList.remove("show");
    setTimeout(() => node.remove(), 280);
  }, 5000);
}

async function boot() {
  await withLoading(async () => {
    const response = await fetch("/console/api/settings");
    if (response.status === 401) {
      renderLogin();
      return;
    }
    if (!response.ok) {
      renderLogin(await errorText(response));
      return;
    }
    state = await response.json();
    await refreshDashboard();
    renderApp();
  });
}

async function refreshDashboard() {
  const response = await fetch("/console/api/dashboard");
  if (response.status === 401) {
    renderLogin();
    return false;
  }
  if (response.ok) {
    dashboard = await response.json();
  }
  return true;
}

function renderLogin(error) {
  app.innerHTML = "";
  const wrap = el("div", { className: "login-wrap" });
  const card = el("form", { className: "card login" });
  const logo = el("img", {
    className: "login-logo",
    src: "/console/static/logo.svg",
    alt: "NILO",
  });
  card.append(
    logo,
    el("h1", { textContent: "NILO archive" }),
    el("p", { className: "lede", textContent: "Sign in to manage backups and storage." }),
  );
  const user = input("text", "admin");
  const password = input("password", "");
  card.append(field("Username", user), field("Password", password));
  if (error) card.append(el("p", { className: "error", textContent: error }));
  const button = el("button", { className: "primary", type: "submit", textContent: "Sign in" });
  card.append(button);
  card.addEventListener("submit", async (event) => {
    event.preventDefault();
    button.disabled = true;
    await withLoading(async () => {
      const response = await fetch("/console/api/login", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ username: user.value, password: password.value }),
      });
      if (!response.ok) {
        const err = await errorText(response);
        toast("error", err);
        renderLogin(err);
        return;
      }
      toast("ok", "Signed in.");
      section = "overview";
      await boot();
    });
    button.disabled = false;
  });
  wrap.append(card);
  app.append(wrap);
}

function renderApp() {
  app.innerHTML = "";
  const shell = el("div", { className: "shell" });
  const nav = el("nav", { className: "nav" });
  const brand = el("div", { className: "nav-brand" });
  brand.append(
    el("img", { src: "/console/static/logo.svg", alt: "" }),
    el("span", { textContent: "NILO" }),
  );
  nav.append(brand, el("p", { className: "hint", textContent: state.settings.site_id }));
  for (const [id, label] of sections) {
    const button = el("button", { type: "button", textContent: label });
    if (id === section) button.className = "active";
    button.addEventListener("click", () => {
      section = id;
      renderApp();
    });
    nav.append(button);
  }
  const main = el("main", { className: "main" });
  const top = el("div", { className: "top" });
  const title = sections.find((item) => item[0] === section)[1];
  top.append(
    el("div", {}, [
      el("h1", { textContent: title }),
      el("p", {
        className: "lede",
        textContent: section === "overview"
          ? "Status of local storage and the latest archived copies from MinIO."
          : "Saved values replace the settings file. A blank secret keeps the one already stored.",
      }),
    ]),
  );
  const actions = el("div", { className: "row" });
  actions.append(
    button("Save", "primary", save),
    button("Restart service", "ghost", restart),
    button("Sign out", "ghost", logout),
  );
  top.append(actions);
  main.append(top);
  const panel = el("section", { className: "panel" });
  panel.append(renderSection());
  main.append(panel);
  shell.append(nav, main);
  app.append(shell);
}

function renderSection() {
  const settings = state.settings;
  const root = el("div");
  if (section === "overview") {
    root.append(renderOverview());
    return root;
  }
  if (section === "general") {
    root.append(
      field("Site id", text(settings.site_id, (value) => { settings.site_id = value; })),
      field("Log level", select(settings.log_level, ["DEBUG", "INFO", "WARNING", "ERROR"], (value) => { settings.log_level = value; })),
      field("Trace directory (empty uses the catalog directory)", text(settings.trace.directory || "", (value) => { settings.trace.directory = value || null; })),
    );
  }
  if (section === "schedule") {
    const cron = text(settings.pull.schedule, (value) => { settings.pull.schedule = value; });
    const cronHelp = el("p", { className: "hint compact-grid-wide" });
    cronHelp.append(
      "Build or check cron expressions at ",
      el("a", { className: "ext-link", href: "https://crontab.guru/", target: "_blank", rel: "noopener", textContent: "crontab.guru" }),
      ".",
    );
    const sched = el("div", { className: "compact-grid" });
    sched.append(
      el("div", { className: "compact-grid-wide" }, check("Pull from MinIO and other backends on a schedule", settings.pull.enabled, (value) => { settings.pull.enabled = value; })),
      compactField("Cron", cron),
      compactField("Timezone", text(settings.pull.timezone, (value) => { settings.pull.timezone = value; })),
      compactField("Safety delay (s)", number(settings.pull.safety_delay_seconds, (value) => { settings.pull.safety_delay_seconds = value; })),
      el("div", { className: "compact-grid-wide" }, check("Watch the rsync/scp inbox", settings.inbox.enabled, (value) => { settings.inbox.enabled = value; })),
      compactField("Inbox poll (s)", number(settings.inbox.poll_seconds, (value) => { settings.inbox.poll_seconds = value; })),
      compactField("Abandon after (h)", number(settings.inbox.abandon_after_hours, (value) => { settings.inbox.abandon_after_hours = value; })),
    );
    root.append(
      sched,
      cronHelp,
      el("div", { className: "row" }, [
        button("Daily at 02:00", "ghost", () => { settings.pull.schedule = "0 2 * * *"; renderApp(); }),
        button("Hourly", "ghost", () => { settings.pull.schedule = "0 * * * *"; renderApp(); }),
        button("Every 15 minutes", "ghost", () => { settings.pull.schedule = "*/15 * * * *"; renderApp(); }),
      ]),
      button("Run sync now", "ghost", runSync),
    );
  }
  if (section === "storage") {
    root.append(
      el("p", { className: "hint", textContent: "The first enabled disk is filled first. Check that each path exists before saving." }),
      el("p", {
        className: "hint",
        textContent:
          "Path checks run inside the datalake container. /data is often a Docker volume (not /data on the NAS host over SSH). Use a host bind mount in compose if you need a specific NAS folder.",
      }),
    );
    root.append(el("div", { className: "storage-hint-box" }, [
      el("strong", { textContent: "Reserve and min free" }),
      el("p", {
        className: "hint",
        textContent:
          "Reserve is space never used for archives (headroom for the OS and Docker). "
          + "0.0625 GiB = 64 MiB is the small Docker default; on a hospital disk set Reserve to tens of GiB. "
          + "Min free blocks new batches when the volume has less than that much free space.",
      }),
    ]));
    for (const volume of settings.storage.volumes) {
      const row = el("div", { className: "list-row storage-vol" });
      const pathInput = text(volume.root, (value) => {
        volume.root = value;
        delete pathChecks[volume.id];
      });
      row.append(
        labeled("Id", text(volume.id, (value) => { volume.id = value; })),
        labeled("Path", pathInput),
        el("span", { className: pathStatusClass(volume.id), textContent: pathStatusText(volume.id) }),
        button("Check", "ghost", () => checkVolumePath(volume)),
        button("Browse…", "ghost", () => openStorageBrowse(volume)),
        labeled("On", checkbox(volume.enabled, (value) => { volume.enabled = value; })),
        button("Remove", "danger", () => {
          settings.storage.volumes = settings.storage.volumes.filter((item) => item !== volume);
          renderApp();
        }),
      );
      root.append(row);
      const scopeNote = pathChecks[volume.id]?.note;
      if (scopeNote) {
        root.append(el("p", { className: "hint", style: "margin:-6px 0 12px 0", textContent: scopeNote }));
      }
    }
    if (storageBrowse.open) {
      root.append(renderFolderBrowser());
    }
    root.append(button("Add disk", "ghost", () => {
      settings.storage.volumes.push({ id: "disk" + (settings.storage.volumes.length + 1), root: "", enabled: false });
      renderApp();
    }));
    const storGrid = el("div", { className: "compact-grid" });
    storGrid.append(
      compactField(`Reserve GiB (${formatSizeHint(settings.storage.reserve_bytes)})`, text(toGiB(settings.storage.reserve_bytes), (value) => { settings.storage.reserve_bytes = fromGiB(value); renderApp(); })),
      compactField(`Min free GiB (${formatSizeHint(settings.storage.min_free_bytes)})`, text(toGiB(settings.storage.min_free_bytes), (value) => { settings.storage.min_free_bytes = fromGiB(value); renderApp(); })),
      el("div", { className: "compact-grid-wide" }, field("Catalog path", text(settings.storage.catalog_path, (value) => { settings.storage.catalog_path = value; }))),
    );
    root.append(storGrid);
  }
  if (section === "minio") {
    const minio = settings.pull.minio;
    root.append(
      el("p", { className: "lede", textContent: "Session videos and MongoDB dump files are copied from MinIO buckets into local archive disks. This service does not connect to MongoDB directly." }),
      el("div", { className: "compact-grid-wide" }, check("Pull from MinIO", minio.enabled, (value) => { minio.enabled = value; })),
    );
    const minioGrid = el("div", { className: "compact-grid" });
    minioGrid.append(
      compactField("Endpoint", text(minio.endpoint, (value) => { minio.endpoint = value; })),
      compactField("Access key", text(minio.access_key, (value) => { minio.access_key = value; })),
      el("div", { className: "compact-grid-wide" }, secretField("Secret key", minio.secret_key, state.secrets_set.includes("pull.minio.secret_key"), (value) => { minio.secret_key = value; })),
      el("div", { className: "compact-grid-wide" }, check("TLS", minio.secure, (value) => { minio.secure = value; })),
      compactField("Session prefix", text(minio.session_prefix, (value) => { minio.session_prefix = value; })),
    );
    root.append(minioGrid);
    root.append(renderConnectionTest("minio", "Test MinIO connection", "Checks endpoint, credentials, and lists buckets."));
    for (const bucket of minio.buckets) {
      const row = el("div", { className: "list-row bucket" });
      row.append(
        labeled("Bucket", text(bucket.name, (value) => { bucket.name = value; })),
        labeled("Prefix", text(bucket.prefix, (value) => { bucket.prefix = value; })),
        button("Remove", "danger", () => {
          minio.buckets = minio.buckets.filter((item) => item !== bucket);
          renderApp();
        }),
      );
      root.append(row);
    }
    root.append(button("Add bucket", "ghost", () => {
      minio.buckets.push({ name: "", prefix: "" });
      renderApp();
    }));
  }
  if (section === "ingress") {
    root.append(renderIngress());
  }
  if (section === "api") {
    if (!openApiDoc && !openApiLoading) {
      openApiLoading = true;
      loadOpenApiDoc().finally(() => { openApiLoading = false; });
    }
    root.append(renderApiDoc());
  }
  if (section === "account") {
    root.append(
      field("Username", text(settings.console.username, (value) => { settings.console.username = value; })),
      secretField("Password", settings.console.password, state.secrets_set.includes("console.password"), (value) => { settings.console.password = value; }),
    );
  }
  return root;
}

function renderOverview() {
  const wrap = el("div");
  if (!dashboard) {
    wrap.append(el("p", { className: "hint", textContent: "Loading dashboard…" }));
    return wrap;
  }
  const grid = el("div", { className: "stats-grid" });
  const disk = dashboard.disks[0];
  if (disk && !disk.error) {
    grid.append(renderDiskGauge(disk));
  } else if (disk && disk.error) {
    grid.append(statCard("Archive disk", "Unavailable", disk.error));
  }
  grid.append(
    statCard(
      "Indexed archive",
      formatBytes(dashboard.archive_bytes),
      `${dashboard.archive_objects} object(s) in the catalog`,
    ),
  );
  grid.append(statCard("Session backup", backupTitle(dashboard.last_session_backup), backupDetail(dashboard.last_session_backup)));
  grid.append(statCard("Database backup", backupTitle(dashboard.last_database_backup), backupDetail(dashboard.last_database_backup)));
  wrap.append(grid);

  const syncLine = el("p", { className: "hint" });
  if (dashboard.sync_running) {
    syncLine.textContent = "A sync is running now.";
  } else if (dashboard.last_sync && dashboard.last_sync.finished_at) {
    syncLine.textContent = `Last sync finished at ${formatWhen(dashboard.last_sync.finished_at)}${dashboard.last_sync.errors?.length ? " (with errors)" : ""}.`;
  } else {
    syncLine.textContent = "No sync has completed yet in this process. Use Schedule → Run sync now.";
  }
  wrap.append(syncLine);
  wrap.append(button("Refresh summary", "ghost", async () => {
    await withLoading(async () => {
      await refreshDashboard();
      toast("ok", "Summary refreshed.");
      renderApp();
    });
  }));
  return wrap;
}

function renderDiskGauge(disk) {
  const usedPct = disk.total_bytes ? Math.min(100, Math.round((disk.used_bytes / disk.total_bytes) * 100)) : 0;
  const card = el("div", { className: "stat-card gauge-card" });
  const ring = el("div", { className: "gauge-ring", style: `--pct:${usedPct}` });
  const inner = el("div", { className: "gauge-inner" });
  inner.append(
    el("div", { className: "gauge-pct", textContent: `${usedPct}%` }),
    el("div", { className: "gauge-label", textContent: "used" }),
  );
  ring.append(inner);
  const meta = el("div", { className: "gauge-meta" });
  meta.append(
    el("h2", { textContent: "Archive disk" }),
    el("p", {
      className: "stat-detail",
      textContent: `${formatBytes(disk.used_bytes)} of ${formatBytes(disk.total_bytes)} · ${disk.free_bytes !== undefined ? formatBytes(disk.free_bytes) + " free" : ""} (${disk.id})`,
    }),
  );
  card.append(ring, meta);
  return card;
}

function statCard(title, value, detail) {
  const card = el("div", { className: "stat-card" });
  card.append(
    el("h2", { textContent: title }),
    el("p", { className: "stat-value", textContent: value }),
    el("p", { className: "stat-detail", textContent: detail }),
  );
  return card;
}

function backupTitle(row) {
  if (!row || !row.at) return "None yet";
  return formatWhen(row.at);
}

function backupDetail(row) {
  if (!row || !row.at) return "Run a MinIO pull to archive sessions and dump files.";
  const parts = [];
  if (row.label) parts.push(row.label);
  if (row.size_bytes) parts.push(formatBytes(row.size_bytes));
  if (row.path && row.path !== row.label) parts.push(row.path);
  return parts.join(" · ") || "Archived from MinIO";
}

function renderIngress() {
  const settings = state.settings;
  const http = settings.pull.http;
  const ingest = settings.ingest;
  const ssh = settings.pull.ssh;
  settings.pull.ssh.enabled = true;
  const grid = el("div", { className: "ingress-grid" });

  const pullCard = el("div", { className: "mode-card" });
  pullCard.append(
    el("span", { className: "tag", textContent: "Option A" }),
    el("h2", { textContent: "Pull from a remote server" }),
    el("p", { className: "lede", textContent: "This archive calls a remote HTTP API and copies the changes it returns." }),
    check("Enabled", http.enabled, (value) => { http.enabled = value; }),
    field("Remote base URL", text(http.base_url, (value) => { http.base_url = value; })),
    secretField("Remote API key", http.api_key, state.secrets_set.includes("pull.http.api_key"), (value) => { http.api_key = value; }),
    el("div", { className: "compact-grid" }, [
      compactField("Page size", number(http.page_limit, (value) => { http.page_limit = value; })),
      compactField("Timeout (s)", number(http.timeout_seconds, (value) => { http.timeout_seconds = value; })),
    ]),
    renderConnectionTest("pull", "Test remote API", "Calls the remote /changes endpoint."),
  );

  const pushCard = el("div", { className: "mode-card" });
  pushCard.append(
    el("span", { className: "tag", textContent: "Option B" }),
    el("h2", { textContent: "Push into this datalake" }),
    el("p", { className: "lede", textContent: "Another system uploads batches to the ingest API on this host (Ethernet or routed TCP)." }),
    check("Accept pushes", ingest.enabled, (value) => { ingest.enabled = value; }),
    el("div", { className: "compact-grid" }, [
      compactField("Bind host", text(ingest.host, (value) => { ingest.host = value; })),
      compactField("Port", number(ingest.port, (value) => { ingest.port = value; })),
    ]),
    secretField("Ingest API key", ingest.api_key, state.secrets_set.includes("ingest.api_key"), (value) => { ingest.api_key = value; }),
    el("p", { className: "hint", textContent: "Host/port changes need Restart service. See Archive API for the full contract." }),
    renderConnectionTest("ingest", "Test ingest API", "Checks /v1/health and /v1/status on this machine."),
  );

  const directCard = el("div", { className: "mode-card" });
  const guide = el("div", { className: "ingress-guide" });
  guide.append(
    el("p", {}, [
      document.createTextNode("With USB–Ethernet between a MiniPC and this NAS, we recommend "),
      el("strong", { textContent: "Option B (HTTP push)" }),
      document.createTextNode(": point the MiniPC at this host’s ingest URL (e.g. http://<NAS-on-link>:8088) and upload batches with the ingest API key. No extra service on the NAS; same contract as a normal network push, only over the direct cable."),
    ]),
    el("p", {}, [
      document.createTextNode("Alternative: copy files with "),
      el("strong", { textContent: "rsync" }),
      document.createTextNode(" or "),
      el("strong", { textContent: "scp" }),
      document.createTextNode(" into the rsync/scp inbox folders on this machine; the scheduled job imports them. "),
      el("strong", { textContent: "SSH sources" }),
      document.createTextNode(" below mean the archive SSHs into the MiniPC and pulls paths with rsync (you need SSH on the MiniPC). FTP is not supported. A private HTTP API on the NAS is unnecessary if you use the built-in ingest API (Option B)."),
    ]),
  );
  directCard.append(
    el("span", { className: "tag", textContent: "Option C" }),
    el("h2", { textContent: "Direct link (MiniPC / cable)" }),
    el("p", { className: "lede", textContent: "Same ingest paths as B, plus file drops and optional rsync-over-SSH pulls from the linked node." }),
    guide,
    check("Watch rsync/scp inbox folders", settings.inbox.enabled, (value) => { settings.inbox.enabled = value; }),
    el("div", { className: "compact-grid" }, [
      compactField("rsync binary", text(ssh.binary, (value) => { ssh.binary = value; })),
      compactField("SSH command", text(ssh.ssh_command, (value) => { ssh.ssh_command = value; })),
    ]),
    renderConnectionTest("direct", "Test direct paths", "Lists active network links, inbox folders, and rsync sources."),
  );
  for (const source of ssh.sources) {
    const row = el("div", { className: "list-row ssh" });
    row.append(
      labeled("Name", text(source.name, (value) => { source.name = value; })),
      labeled("Remote path", text(source.remote, (value) => { source.remote = value; })),
      labeled("Delete after copy", checkbox(source.delete_after, (value) => { source.delete_after = value; })),
      button("Remove", "danger", () => {
        ssh.sources = ssh.sources.filter((item) => item !== source);
        renderApp();
      }),
    );
    directCard.append(row);
  }
  directCard.append(button("Add rsync/SSH source", "ghost", () => {
    ssh.sources.push({ name: "", remote: "", delete_after: false });
    renderApp();
  }));

  grid.append(pullCard, pushCard, directCard);
  return grid;
}

function renderApiDoc() {
  const wrap = el("div", { className: "api-doc" });
  wrap.append(
    el("p", { className: "lede", textContent: "Try the ingest API on this server. OpenAPI lists the same routes as Swagger." }),
    renderTestIngestKeyField(),
    el("div", { className: "row" }, [
      button("Load / refresh spec", "ghost", loadOpenApiDoc),
      button("Try health", "primary", () => tryArchiveAction("health")),
      button("Try status", "primary", () => tryArchiveAction("status")),
      button("Try open batch", "primary", () => tryArchiveAction("open_batch")),
    ]),
    renderTestApiPanel(),
  );
  if (!openApiDoc) {
    wrap.append(el("p", { className: "hint", textContent: openApiLoading ? "Loading OpenAPI spec…" : "Waiting for spec…" }));
    return wrap;
  }
  wrap.append(
    el("p", { className: "hint", textContent: "Try it sends a request with saved settings; use the key field above to override the ingest API key without saving." }),
  );
  const paths = openApiDoc.paths || {};
  for (const [path, methods] of Object.entries(paths)) {
    for (const [method, detail] of Object.entries(methods)) {
      if (method === "parameters") continue;
      const block = el("div", { className: "api-endpoint" });
      const key = `${method.toLowerCase()} ${path}`;
      const tryAction = ARCHIVE_TRY[key];
      const head = el("div", { className: "api-endpoint-head" });
      head.append(
        el("h3", {}, [
          el("span", { className: "api-method", textContent: method.toUpperCase() }),
          document.createTextNode(" " + path),
        ]),
      );
      if (tryAction) {
        head.append(button("Try it", "primary", () => tryArchiveAction(tryAction)));
      } else {
        head.append(el("span", { className: "hint", textContent: "Try via client with ingest API key" }));
      }
      block.append(head);
      if (detail.summary) block.append(el("p", { textContent: detail.summary }));
      if (detail.description) block.append(el("p", { textContent: detail.description }));
      if (detail.requestBody) block.append(el("p", { textContent: "Request body: JSON (see schema in OpenAPI)." }));
      wrap.append(block);
    }
  }
  return wrap;
}

async function loadOpenApiDoc() {
  await withLoading(async () => {
    const response = await fetch("/console/api/openapi");
    if (response.status === 401) return renderLogin();
    if (!response.ok) {
      toast("error", await errorText(response));
      return;
    }
    openApiDoc = await response.json();
    toast("ok", "API spec loaded.");
    renderApp();
  });
}

async function tryArchiveAction(action) {
  await withLoading(async () => {
    const response = await fetch("/console/api/archive/try/" + action, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload()),
    });
    if (response.status === 401) return renderLogin();
    const body = await response.json();
    const detail = body.detail;
    const textOut = detail == null
      ? JSON.stringify(body, null, 2)
      : (typeof detail === "string" ? detail : JSON.stringify(detail, null, 2));
    testApiPanel = { open: true, ok: !!body.ok, text: textOut || "Request finished." };
    renderApp();
  });
}

function renderTestIngestKeyField() {
  const box = el("div", { className: "field field-compact" });
  box.style.maxWidth = "28rem";
  const node = el("input", {
    type: "password",
    value: testIngestKey,
    placeholder: "Uses saved key if empty",
    autocomplete: "off",
  });
  node.addEventListener("input", () => { testIngestKey = node.value; });
  box.append(
    el("label", { textContent: "Ingest API key (for tests only)" }),
    node,
    el("span", { className: "hint", textContent: "Not stored when you Save — only sent with Try it / Try health / status / open batch." }),
  );
  return box;
}

function renderTestApiPanel() {
  if (!testApiPanel.open) return el("div");
  const box = el("div", { className: "test-api-output" });
  const head = el("div", { className: "test-api-output-head" });
  head.append(
    el("span", { textContent: testApiPanel.ok ? "Response" : "Error" }),
    button("Hide", "ghost", () => { testApiPanel.open = false; renderApp(); }),
  );
  const body = el("pre", {
    className: "test-api-output-body " + (testApiPanel.ok ? "ok" : "error"),
    textContent: testApiPanel.text,
  });
  box.append(head, body);
  return box;
}

function pathStatusClass(volumeId) {
  const check = pathChecks[volumeId];
  if (!check) return "path-status";
  return "path-status " + (check.ok ? "ok" : "bad");
}

function pathStatusText(volumeId) {
  const check = pathChecks[volumeId];
  if (!check) return "unchecked";
  if (check.ok && check.scope === "datalake_container") return "exists (container)";
  return check.detail || (check.ok ? "exists" : "missing");
}

async function checkVolumePath(volume) {
  await withLoading(async () => {
    const path = volume.root || "/";
    const response = await fetch("/console/api/storage/check?path=" + encodeURIComponent(path));
    if (response.status === 401) return renderLogin();
    const body = await response.json();
    pathChecks[volume.id] = body;
    renderApp();
  });
}

function openStorageBrowse(volume) {
  storageBrowse.open = true;
  storageBrowse.volume = volume;
  storageBrowse.path = volume.root || "/";
  loadBrowse(storageBrowse.path);
}

async function loadBrowse(path) {
  await withLoading(async () => {
    const response = await fetch("/console/api/storage/browse?path=" + encodeURIComponent(path || "/"));
    if (response.status === 401) return renderLogin();
    if (!response.ok) {
      toast("error", await errorText(response));
      return;
    }
    const body = await response.json();
    storageBrowse.path = body.path;
    storageBrowse.entries = body.directories;
    storageBrowse.parent = body.parent;
    storageBrowse.roots = body.roots;
    renderApp();
  });
}

function renderFolderBrowser() {
  const box = el("div", { className: "folder-browser" });
  const head = el("div", { className: "folder-browser-head" });
  head.append(
    el("strong", { textContent: "Choose folder" }),
    el("span", { className: "folder-browser-path", textContent: storageBrowse.path }),
  );
  if (storageBrowse.parent) {
    head.append(button("Up", "ghost", () => loadBrowse(storageBrowse.parent)));
  }
  head.append(
    button("Select this folder", "primary", () => {
      if (storageBrowse.volume) {
        storageBrowse.volume.root = storageBrowse.path;
        delete pathChecks[storageBrowse.volume.id];
      }
      storageBrowse.open = false;
      renderApp();
    }),
    button("Close", "ghost", () => {
      storageBrowse.open = false;
      renderApp();
    }),
  );
  box.append(head);
  const list = el("div", { className: "folder-list" });
  for (const entry of storageBrowse.entries || []) {
    const item = el("button", { type: "button", textContent: "📁 " + entry.name });
    item.addEventListener("click", () => loadBrowse(entry.path));
    list.append(item);
  }
  if (!(storageBrowse.entries || []).length) {
    list.append(el("p", { className: "hint", textContent: "No subfolders (or cannot read this directory)." }));
  }
  box.append(list);
  return box;
}

function renderConnectionTest(kind, label, description) {
  const card = el("div", { className: "connection-card" });
  card.append(
    el("h2", { textContent: "Connection test" }),
    el("p", { className: "lede", textContent: description }),
  );
  const row = el("div", { className: "test-row" });
  const testButton = el("button", { type: "button", className: "primary", textContent: label });
  testButton.addEventListener("click", () => testKind(kind));
  const result = el("div", { className: "test-result", textContent: "Not tested in this session." });
  const saved = connectionTests[kind];
  if (saved) {
    result.textContent = saved.text;
    result.className = "test-result " + (saved.ok ? "ok" : "error");
  }
  row.append(testButton, result);
  card.append(row);
  return card;
}

async function save() {
  await withLoading(async () => {
    const response = await fetch("/console/api/settings", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload()),
    });
    if (response.status === 401) return renderLogin();
    if (!response.ok) {
      toast("error", await errorText(response));
      return;
    }
    const body = await response.json();
    state = body;
    toast(
      body.restart_required ? "warn" : "ok",
      body.restart_required
        ? "Saved. Restart the service to bind the new host or port."
        : "Saved. Schedule and credentials are in use.",
    );
    await refreshDashboard();
    renderApp();
  });
}

async function runSync() {
  await withLoading(async () => {
    const response = await fetch("/console/api/sync", { method: "POST" });
    if (response.status === 401) return renderLogin();
    const body = await response.json();
    toast(body.started ? "ok" : "warn", body.started ? "Sync started." : (body.reason || "Sync did not start"));
    renderApp();
  });
}

async function testKind(kind) {
  await withLoading(async () => {
    const response = await fetch("/console/api/test/" + kind, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload()),
    });
    if (response.status === 401) return renderLogin();
    const body = await response.json();
    connectionTests[kind] = { ok: body.ok, text: body.detail || body.error || "Test failed" };
    toast(body.ok ? "ok" : "error", connectionTests[kind].text);
    renderApp();
  });
}

async function restart() {
  toast("warn", "Restarting service…");
  renderApp();
  await withLoading(async () => {
    await fetch("/console/api/restart", { method: "POST" });
    for (let attempt = 0; attempt < 20; attempt += 1) {
      await sleep(1000);
      try {
        const health = await fetch("/v1/health");
        if (health.ok) {
          toast("ok", "Service is back.");
          await boot();
          return;
        }
      } catch (_error) {
        // The process is still down.
      }
    }
    toast("error", "Service did not come back. Check container logs.");
    renderApp();
  });
}

async function logout() {
  await fetch("/console/api/logout", { method: "POST" });
  state = null;
  dashboard = null;
  connectionTests = { minio: null, pull: null, ingest: null, direct: null };
  renderLogin();
}

function payload() {
  const body = { settings: state.settings };
  if (state.mongo_login) body.mongo_login = state.mongo_login;
  if (testIngestKey.trim()) body.test_ingest_api_key = testIngestKey.trim();
  return body;
}

function secretField(label, value, stored, setter) {
  const hint = stored ? "A value is stored. Leave this blank to keep it." : "No value stored yet.";
  const box = el("div", { className: "field" });
  box.append(el("label", { textContent: label }), input("password", value, setter), el("span", { className: "hint", textContent: hint }));
  return box;
}

function field(label, control) {
  return el("div", { className: "field" }, [el("label", { textContent: label }), control]);
}

function compactField(label, control) {
  return el("div", { className: "field field-compact" }, [el("label", { textContent: label }), control]);
}

function labeled(label, control) {
  return el("div", { className: "field" }, [el("label", { textContent: label }), control]);
}

function check(label, value, setter) {
  const box = el("div", { className: "field inline" });
  box.append(checkbox(value, setter), el("label", { textContent: label }));
  return box;
}

function text(value, setter) {
  return input("text", value ?? "", setter);
}

function number(value, setter) {
  return input("number", value ?? 0, (raw) => setter(Number(raw)));
}

function input(type, value, setter) {
  const node = el("input", { type, value: value ?? "" });
  if (setter) node.addEventListener("input", () => setter(node.value));
  return node;
}

function checkbox(value, setter) {
  const node = el("input", { type: "checkbox" });
  node.checked = Boolean(value);
  node.addEventListener("change", () => setter(node.checked));
  return node;
}

function select(value, options, setter) {
  const node = el("select");
  for (const option of options) {
    node.append(el("option", { value: option, textContent: option }));
  }
  node.value = value;
  node.addEventListener("change", () => setter(node.value));
  return node;
}

function button(label, className, onClick) {
  const node = el("button", { type: "button", className, textContent: label });
  node.addEventListener("click", onClick);
  return node;
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "style") node.style.cssText = value;
    else node[key] = value;
  }
  for (const child of children) node.append(child);
  return node;
}

function toGiB(bytes) {
  return String(Number((Number(bytes) / 1073741824).toFixed(4)));
}

function fromGiB(text) {
  const value = Number(text);
  if (!Number.isFinite(value) || value < 0) return 0;
  return Math.round(value * 1073741824);
}

function formatSizeHint(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n <= 0) return "GiB";
  if (n < 1073741824) return `${Math.round(n / 1048576)} MiB`;
  return `${(n / 1073741824).toFixed(2)} GiB`;
}

function formatBytes(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let size = n;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size >= 10 || unit === 0 ? size.toFixed(unit === 0 ? 0 : 1) : size.toFixed(2)} ${units[unit]}`;
}

function formatWhen(iso) {
  if (!iso) return "—";
  try {
    const date = new Date(iso);
    return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  } catch (_error) {
    return iso;
  }
}

async function errorText(response) {
  try {
    const body = await response.json();
    return body.detail || JSON.stringify(body);
  } catch (_error) {
    return response.statusText || "Request failed";
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
