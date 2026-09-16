/* vCenter to OCI migration - helper web UI (no framework, hash routing). */
(function () {
  "use strict";

  const app = document.getElementById("app");
  const nav = document.getElementById("nav");
  const userBox = document.getElementById("user");
  const state = { me: null, config: null, jobsByVm: {}, region: "" };
  let activePoll = null;

  // ---------------------------------------------------------------------- api
  class ApiError extends Error {
    constructor(message, status, detail) { super(message); this.status = status; this.detail = detail; }
  }

  async function api(method, path, body) {
    const headers = { "Accept": "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const resp = await fetch("../api" + path, {
      method, headers, credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await resp.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (_) { data = { detail: text }; }
    if (!resp.ok) {
      const detail = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : resp.statusText;
      if (resp.status === 401 && !path.startsWith("/auth/login")) { state.me = null; showLogin(); }
      throw new ApiError(detail, resp.status, data && data.detail);
    }
    return data;
  }

  // -------------------------------------------------------------------- utils
  const fmtBytes = (n) => {
    if (n === null || n === undefined) return "-";
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; let v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v : v.toFixed(v >= 100 ? 0 : 1)) + " " + u[i];
  };
  const fmtRate = (bps) => (bps === null || bps === undefined) ? "-" : `${fmtBytes(bps)}/s (${(bps * 8 / 1e6).toFixed(bps * 8 >= 1e8 ? 0 : 1)} Mbit/s)`;
  const fmtDuration = (s) => {
    if (s === null || s === undefined) return "-";
    s = Math.round(s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return h ? `${h}h ${m}m ${sec}s` : m ? `${m}m ${sec}s` : `${sec}s`;
  };
  const el = (tag, attrs, ...children) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined) continue;
      if (k === "class") e.className = v; else if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else e.setAttribute(k, v);
    }
    for (const c of children) if (c !== null && c !== undefined) e.append(c.nodeType ? c : document.createTextNode(String(c)));
    return e;
  };
  const kv = (container, pairs) => {
    container.innerHTML = "";
    for (const [k, v] of pairs) { container.append(el("span", { class: "k" }, k), el("span", { class: "v" }, v)); }
  };
  const showError = (msg) => { app.innerHTML = ""; app.append(el("div", { class: "card" }, el("span", { class: "error" }, msg))); };
  const tpl = (id) => document.getElementById(id).content.cloneNode(true);
  const stopPolling = () => { if (activePoll) { activePoll(); activePoll = null; } };
  const isWindows = (vm) => /windows/i.test((vm.guest_id || "") + " " + (vm.guest_full_name || ""));
  // client editions (mirrors mapping.map_guest_os): "Microsoft Windows 10/11 (64-bit)", or windows9/11/12_64Guest
  // without a "Server" release in the display name
  const isWindowsClient = (vm) => /windows\s+(10|11)\b/i.test(vm.guest_full_name || "")
    || (!/server/i.test(vm.guest_full_name || "") && /^windows(9|1[12])_64/i.test(vm.guest_id || ""));
  const TERMINAL = ["COMPLETED", "FAILED", "CANCELLED"];
  // remote console: completed migrations with an OCI instance (mirrors routes_console._console_job)
  const hasConsole = (job) => job.phase === "COMPLETED" && !!job.instance_id;
  const STEP_LABELS = { seed_image: "Seed image import", launch_instance: "Instance launch" };
  // OCI console deep link for an instance OCID; the region query parameter makes the console switch to
  // the helper's region instead of the user's last one
  const consoleUrl = (kind, ocid) => `https://cloud.oracle.com/compute/${kind}/${encodeURIComponent(ocid)}${state.region ? `?region=${encodeURIComponent(state.region)}` : ""}`;
  const ocidLink = (kind, ocid, cls) => ocid
    ? el("a", { href: consoleUrl(kind, ocid), target: "_blank", rel: "noopener", class: cls || null, title: "Open in the OCI console" }, ocid)
    : "-";

  // -------------------------------------------------------------------- auth
  function setUser(me) {
    state.me = me;
    nav.hidden = !me;
    userBox.hidden = !me;
    if (me) userBox.querySelector("[data-username]").textContent = `${me.username} @ ${me.vcenter_host}${me.vcenter_port && me.vcenter_port !== 443 ? ":" + me.vcenter_port : ""}`;
  }

  async function showLogin() {
    stopPolling();
    setUser(null);
    app.innerHTML = "";
    app.append(tpl("tpl-login"));
    const form = document.getElementById("login-form");
    const err = document.getElementById("login-error");
    const btn = document.getElementById("login-btn");
    // recently used vCenters live in this browser only; the configured one is the default
    const recent = recentVcenters();
    const datalist = document.getElementById("vcenter-recent");
    for (const h of recent) datalist.append(el("option", { value: h }));
    try {
      state.config = state.config || await api("GET", "/auth/config");
      const configured = state.config.vcenter_host ? state.config.vcenter_host + (state.config.vcenter_port !== 443 ? ":" + state.config.vcenter_port : "") : "";
      if (configured && !recent.includes(configured)) datalist.append(el("option", { value: configured }));
      form.elements.vcenter.value = recent[0] || configured;
    } catch (e) { err.textContent = e.message; }
    // the last user name that logged in to the selected vCenter is remembered in this browser
    const prefillUser = () => {
      const u = lastUsername(form.elements.vcenter.value.trim());
      if (u && !form.elements.username.value) form.elements.username.value = u;
    };
    prefillUser();
    form.elements.vcenter.addEventListener("change", () => { form.elements.username.value = ""; prefillUser(); });
    if (form.elements.vcenter.value) (form.elements.username.value ? form.elements.password : form.elements.username).focus();
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      err.textContent = ""; btn.disabled = true;
      const vcenter = form.elements.vcenter.value.trim();
      const username = form.elements.username.value.trim();
      try {
        const me = await api("POST", "/auth/login", { username, password: form.elements.password.value, vcenter_host: vcenter });
        rememberVcenter(vcenter);
        rememberUsername(vcenter, username);
        setUser(me);
        route();
      } catch (e) { err.textContent = e.message; }
      finally { btn.disabled = false; }
    });
  }

  function recentVcenters() {
    try { return JSON.parse(localStorage.getItem("vcoci.recentVcenters") || "[]"); } catch (_) { return []; }
  }
  function rememberVcenter(host) {
    if (!host) return;
    const list = [host, ...recentVcenters().filter((h) => h !== host)].slice(0, 8);
    try { localStorage.setItem("vcoci.recentVcenters", JSON.stringify(list)); } catch (_) { /* private mode */ }
  }
  // user names only (never passwords), keyed by vCenter host; "" holds the last one used anywhere
  function lastUsernames() {
    try { return JSON.parse(localStorage.getItem("vcoci.lastUsernames") || "{}") || {}; } catch (_) { return {}; }
  }
  function lastUsername(host) {
    const map = lastUsernames();
    return map[host] || map[""] || "";
  }
  function rememberUsername(host, username) {
    if (!username) return;
    const map = lastUsernames();
    map[host] = username; map[""] = username;
    try { localStorage.setItem("vcoci.lastUsernames", JSON.stringify(map)); } catch (_) { /* private mode */ }
  }

  document.getElementById("logout-btn").addEventListener("click", async () => {
    try { await api("POST", "/auth/logout"); } catch (_) { /* ignore */ }
    location.hash = "#/vms";
    showLogin();
  });

  // ------------------------------------------------------------- job rendering
  function renderJob(container, job, opts) {
    opts = opts || {};
    let root = container.querySelector(".job");
    if (!root) { root = tpl("tpl-job").firstElementChild; container.innerHTML = ""; container.append(root); }
    const phase = root.querySelector("[data-phase]");
    phase.textContent = job.phase; phase.className = "phase " + job.phase;
    root.querySelector("[data-message]").textContent = job.message || "";
    root.querySelector("[data-error]").textContent = job.error || "";

    // export-phase line: the percentage vCenter shows on its "Export OVF template" task + the last-minute speed
    const tr = job.transfer || {};
    const transfer = root.querySelector("[data-transfer]");
    transfer.innerHTML = "";
    if (job.phase === "EXPORTING" && tr.started_at) {
      transfer.hidden = false;
      transfer.textContent = `Export OVF template: ${tr.percent || 0}% - ${fmtBytes(tr.bytes_received)} received` +
        (tr.throughput_bps ? ` at ${fmtRate(tr.throughput_bps)} (last minute)` : "") +
        ` - running ${fmtDuration((Date.now() - new Date(tr.started_at)) / 1000)}`;
    } else if (!TERMINAL.includes(job.phase) && job.step_percent !== null && job.step_percent !== undefined) {
      // an OCI work request (e.g. the seed image import) reports how far the current step is
      transfer.hidden = false;
      transfer.append(el("div", { class: "meta" }, el("span", {}, `${STEP_LABELS[job.step] || job.step}: ${job.step_percent}%`)),
        el("div", { class: "bar" }, el("div", { style: `width:${job.step_percent}%` })));
    } else transfer.hidden = true;

    const disks = root.querySelector("[data-disks]");
    disks.innerHTML = "";
    for (const d of job.disks) {
      // percent of this disk's stream: exact when the lease reported the stream size, else bounded by capacity
      const pct = d.status === "COPIED" ? 100 : d.percent || Math.min(99, Math.round(100 * (d.bytes_received || 0) / Math.max(1, d.stream_bytes || d.capacity_bytes)));
      const barClass = "bar" + (d.status === "COPIED" ? " done" : d.status === "FAILED" ? " failed" : "");
      const of = d.stream_bytes ? ` of ${fmtBytes(d.stream_bytes)}` : "";
      const detail = d.status === "COPIED" ? `copied, ${fmtBytes(d.bytes_received)} received, ${fmtBytes(d.bytes_written)} written` :
        d.status === "COPYING" ? `${pct}% - ${fmtBytes(d.bytes_received)}${of} received` +
          (d.throughput_bps ? ` at ${fmtRate(d.throughput_bps)}` : "") + (d.attempts > 1 ? ` (attempt ${d.attempts})` : "") :
        d.status === "FAILED" ? (d.error || "failed") : d.status.toLowerCase();
      disks.append(el("div", { class: "disk" },
        el("div", { class: "meta" },
          el("span", {}, `${d.label || "disk " + d.index} (${fmtBytes(d.capacity_bytes)})${d.device ? " -> " + d.device : ""}`),
          el("span", {}, detail)),
        el("div", { class: barClass }, el("div", { style: `width:${pct}%` }))));
    }

    const terminal = TERMINAL.includes(job.phase);
    const sm = job.summary || {};
    // left panel: the instance that is (being) created in OCI
    kv(root.querySelector("[data-target]"), [
      ["Name", job.instance_id ? el("strong", {}, job.instance_display_name || job.target.display_name || job.vm.name)
        : `${job.target.display_name || job.vm.name} (not launched yet)`],
      ["Instance", ocidLink("instances", job.instance_id)],
      ["State in OCI", ociStateEl(root, job)],
      ["Source VM", `${job.vm.name} (${job.vm.moid})${job.vcenter_host ? " on " + job.vcenter_host : ""} - ${job.vm.num_cpu} vCPU, ${fmtBytes(job.vm.memory_mb * 1024 * 1024)} RAM, ${job.vm.disks.length} disk(s)`],
      ["Guest OS", `${job.vm.guest_full_name || job.vm.guest_id}${job.target.operating_system_version ? ` - release ${job.target.operating_system_version} (selected)` : ""}`],
      ["Shape", `${job.target.shape || "(helper default)"}${job.target.ocpus || job.target.memory_gb ? ` - ${job.target.ocpus ?? "auto"} OCPU / ${job.target.memory_gb ?? "auto"} GB (custom)` : " - sized from the source VM"}`],
      ["IP addresses", ociIpsEl(root, job)],
      ["Launch options", job.launch_options ? `${job.launch_options.firmware}${job.launch_options.secure_boot ? " + Secure Boot (shielded instance, with Measured Boot + vTPM on VM shapes)" : ""}, boot ${job.launch_options.boot_volume_type}, nic ${job.launch_options.network_type}` : "-"],
      ...(job.target.windows_license_type ? [["Windows license", job.target.windows_license_type === "OCI_PROVIDED"
        ? "OCI provided (change it in the OCI console if needed)" : "Bring your own license (change it in the OCI console if needed)"]] : []),
      ["Seed image", job.seed_image_id || "-"],
    ]);
    // right panel: the migration job itself
    const rows = [
      ["Step", job.step || "-"],
      ...(job.power_off_source ? [["Source power-off", { already_off: "was already powered off when the export started",
        guest_shutdown: "shut down cleanly through VMware Tools before the export",
        powered_off: "powered off hard before the export (VMware Tools not running or guest did not stop in time)" }[job.power_off_result]
        || "the VM was powered on when the job was created; it is shut down right before the export"]] : []),
      ...(job.guest_fixup ? [["Guest fix-up", el("span", {},
        el("span", { class: "badge " + ({ done: "ok", not_needed: "ok", failed: "warn" }[job.guest_fixup.status] || "") },
          { done: "done", not_needed: "not needed", skipped: "skipped", failed: "failed" }[job.guest_fixup.status] || job.guest_fixup.status),
        " ", job.guest_fixup.detail,
        job.guest_fixup.status === "failed" ? el("div", { class: "muted" },
          "The instance may stop in the dracut emergency shell; rebuild the initramfs with virtio drivers inside the guest (dracut -f --add-drivers \"virtio_blk virtio_scsi virtio_pci virtio_net\") and migrate again, or check Copy diagnostics for the details.") : null)]] : []),
      ["Disk download", job.nfc_host ? `${job.nfc_host}${job.target.nfc_direct_to_esxi ? " (ESXi host, direct)" : ""}${job.target.pipelined_decode ? ", pipelined decode/write" : ""}` : "-"],
      ["Started by", `${job.created_by || "-"} at ${new Date(job.created_at).toLocaleString()}`],
    ];
    if (terminal) {
      rows.push(["Finished", job.finished_at ? new Date(job.finished_at).toLocaleString() : "-"]);
      rows.push(["Duration", fmtDuration(sm.duration_s) + (sm.transfer_duration_s ? ` (export ${fmtDuration(sm.transfer_duration_s)})` : "")]);
      if (tr.started_at) {
        rows.push(["Data transferred", `${fmtBytes(sm.bytes_received)} received from vCenter, ${fmtBytes(sm.bytes_written)} written to OCI volumes`]);
        rows.push(["Average bandwidth", sm.average_bps ? fmtRate(sm.average_bps) : "-"]);
      }
    }
    rows.push(["Job id", job.id]);
    kv(root.querySelector("[data-migration]"), rows);

    const cancelBtn = root.querySelector("[data-cancel]");
    cancelBtn.hidden = job.phase === "COMPLETED" || job.phase === "CANCELLED";
    cancelBtn.textContent = job.phase === "FAILED" ? "Clean up OCI resources" : "Cancel";
    cancelBtn.onclick = async () => {
      if (!confirm("Cancel this migration? The OCI instance and volumes created so far will be deleted.")) return;
      cancelBtn.disabled = true;
      try { await api("POST", `/jobs/${job.id}/cancel`); } catch (e) { alert(e.message); }
      finally { cancelBtn.disabled = false; }
    };
    // a job that failed after all disks were copied (attach / start rejected by OCI) can resume finalizing
    const resumeBtn = root.querySelector("[data-resume]");
    resumeBtn.hidden = !(job.phase === "FAILED" && job.instance_id && job.disks.length && job.disks.every((d) => d.status === "COPIED"));
    resumeBtn.onclick = async () => {
      resumeBtn.disabled = true;
      try { await api("POST", `/jobs/${job.id}/finalize`); route(); }  // polling stopped at FAILED; restart the view
      catch (e) { alert(e.message); }
      finally { resumeBtn.disabled = false; }
    };
    const copyBtn = root.querySelector("[data-copy]"); const copyState = root.querySelector("[data-copy-state]");
    if (!copyBtn.onclick) copyBtn.onclick = async () => {
      copyBtn.disabled = true; copyState.textContent = "Collecting...";
      try {
        const resp = await fetch(`../api/jobs/${encodeURIComponent(job.id)}/diagnostics`, { credentials: "same-origin", cache: "no-store" });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const text = await resp.text();
        const diag = root.querySelector("[data-diag]"); const area = diag.querySelector("textarea");
        area.value = text;
        try {
          if (!navigator.clipboard) throw new Error("clipboard API not available");
          await navigator.clipboard.writeText(text);
          copyState.textContent = `Copied ${text.split("\n").length} lines to the clipboard.`;
        } catch (_) {
          // insecure context or permission denied: show the text for manual copying
          diag.hidden = false; diag.open = true; area.focus(); area.select();
          copyState.textContent = "Clipboard not available; the text is selected below, press Ctrl+C.";
        }
      } catch (e) { copyState.textContent = "Cannot collect diagnostics: " + e.message; }
      finally { copyBtn.disabled = false; setTimeout(() => { if (copyState.textContent.startsWith("Copied")) copyState.textContent = ""; }, 6000); }
    };
    // the VNC console of the migrated instance (OCI console connection through the helper)
    const consoleBtn = root.querySelector("[data-console]");
    consoleBtn.hidden = !hasConsole(job);
    consoleBtn.href = `#/jobs/${job.id}/console`;
    if (opts.onTerminal && terminal) opts.onTerminal(job);
    return job.phase === "COMPLETED" || job.phase === "CANCELLED";
  }

  // Live state of the target instance (lifecycle state + addresses of its primary VNIC), asked from OCI
  // through the helper.  While the job runs its record is re-rendered every few seconds and the state is
  // refreshed at most every OCI_STATE_TTL ms; once the job is finished (job polling stops) a timer keeps
  // the state refreshing on its own for as long as the page is open.
  const OCI_STATE_TTL = 15000;
  const OCI_STATE_CLASS = { RUNNING: "ok", PROVISIONING: "", STARTING: "", STOPPING: "warn", STOPPED: "warn",
    CREATING_IMAGE: "", MOVING: "", TERMINATING: "bad", TERMINATED: "bad", NOT_FOUND: "bad" };
  function ociStateEl(root, job) {
    const span = el("span", { "data-oci-state": "" });
    if (!job.instance_id) { span.textContent = "-"; return span; }
    const c = ociCached(root, job);
    if (c) fillOciState(span, c); else span.textContent = "checking...";
    ensureOciRefresh(root, job);
    return span;
  }
  function ociIpsEl(root, job) {
    const span = el("span", { "data-oci-ips": "" });
    fillOciIps(span, job, ociCached(root, job));
    return span;
  }
  const ociCached = (root, job) => (root._ociState && root._ociState.id === job.instance_id) ? root._ociState : null;
  function ensureOciRefresh(root, job) {
    const c = ociCached(root, job);
    const age = c ? Date.now() - c.at : Infinity;
    if (age > OCI_STATE_TTL) refreshOciState(root, job);
    else if (TERMINAL.includes(job.phase)) scheduleOciRefresh(root, job, OCI_STATE_TTL - age);
  }
  function scheduleOciRefresh(root, job, delayMs) {
    clearTimeout(root._ociTimer);
    root._ociTimer = setTimeout(() => { if (root.isConnected) refreshOciState(root, job); }, Math.max(1000, delayMs));
  }
  function fillOciState(span, c) {
    span.innerHTML = "";
    if (c.error) { span.append(el("span", { class: "badge bad" }, "unknown"), ` ${c.error}`); return; }
    const cls = OCI_STATE_CLASS[c.state];
    span.append(el("span", { class: "badge" + (cls ? " " + cls : "") }, c.state),
      el("span", { class: "muted" }, ` checked ${new Date(c.at).toLocaleTimeString()}`));
  }
  function fillOciIps(span, job, c) {
    // what was configured, then what OCI actually assigned once the VNIC exists
    const wanted = `${job.target.private_ip ? "private IP " + job.target.private_ip + " (fixed)" : "private IP assigned by OCI (DHCP)"}${job.target.assign_public_ip ? ", public IP" : ""}`;
    span.innerHTML = "";
    if (c && !c.error && c.private_ip) {
      span.append(el("strong", {}, `private ${c.private_ip}`), c.public_ip ? el("strong", {}, `, public ${c.public_ip}`) : "",
        el("div", { class: "muted" }, `requested: ${wanted}`));
    } else if (job.instance_id && (!c || c.error || !["TERMINATING", "TERMINATED", "NOT_FOUND"].includes(c.state))) {
      span.append(wanted, el("span", { class: "muted" }, c && !c.error ? " - address not assigned yet" : " - checking..."));
    } else span.textContent = wanted;
  }
  async function refreshOciState(root, job) {
    root._ociJob = job;  // newest record: the phase may turn terminal while a request is in flight
    if (root._ociPending) return;
    root._ociPending = true;
    const cache = { id: job.instance_id, at: Date.now() };
    try {
      const st = await api("GET", `/jobs/${job.id}/instance`);
      cache.state = st.lifecycle_state; cache.at = new Date(st.checked_at).getTime() || cache.at;
      cache.private_ip = st.private_ip || null; cache.public_ip = st.public_ip || null;
    } catch (e) { if (e.status === 401) return; cache.error = e.message; }
    finally { root._ociPending = false; }
    root._ociState = cache;
    job = root._ociJob;
    root.querySelectorAll("[data-oci-state]").forEach((s) => fillOciState(s, cache));
    root.querySelectorAll("[data-oci-ips]").forEach((s) => fillOciIps(s, job, cache));
    if (TERMINAL.includes(job.phase)) scheduleOciRefresh(root, job, OCI_STATE_TTL);  // job polling has stopped
  }

  function pollJob(jobId, container, opts) {
    let timer = null; let stopped = false;
    const tick = async () => {
      if (stopped) return;
      try {
        const job = await api("GET", `/jobs/${jobId}`);
        if (renderJob(container, job, opts)) return;
      } catch (e) { if (e.status === 401) return; console.warn(e); }
      timer = setTimeout(tick, 3000);
    };
    tick();
    return () => { stopped = true; clearTimeout(timer); };
  }

  // ------------------------------------------------------------------ VM list
  async function vmsView() {
    app.innerHTML = "";
    app.append(tpl("tpl-vms"));
    const rows = document.getElementById("vm-rows");
    const filter = document.getElementById("vm-filter");
    const offOnly = document.getElementById("vm-off-only");
    const folderSel = document.getElementById("vm-folder");
    const osSel = document.getElementById("vm-os");
    const count = document.getElementById("vm-count");
    const err = document.getElementById("vm-error");
    const pager = document.getElementById("vm-pager");
    const pageInfo = document.getElementById("vm-page-info");
    const prevBtn = document.getElementById("vm-prev"), nextBtn = document.getElementById("vm-next");
    const pageSizeSel = document.getElementById("vm-page-size");
    let vms = [];
    let page = 0;
    const pageSize = () => Number(pageSizeSel.value) || 50;
    const osOf = (vm) => vm.guest_full_name || vm.guest_id || "(unknown)";

    // folder / guest OS dropdowns are built from the inventory; the current choice survives a refresh
    const fillFilters = () => {
      const fill = (sel, values, all) => {
        const previous = sel.value;
        sel.innerHTML = "";
        sel.append(el("option", { value: "" }, all));
        for (const v of values) sel.append(el("option", { value: v }, v));
        sel.value = values.includes(previous) ? previous : "";
      };
      const uniq = (list) => [...new Set(list)].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
      fill(folderSel, uniq(vms.map((vm) => vm.folder || "(no folder)")), `All folders (${new Set(vms.map((vm) => vm.folder || "(no folder)")).size})`);
      fill(osSel, uniq(vms.map(osOf)), `All guest OSes (${new Set(vms.map(osOf)).size})`);
    };

    const matches = (vm) => {
      if (offOnly.checked && vm.power_state !== "poweredOff") return false;
      if (folderSel.value && (vm.folder || "(no folder)") !== folderSel.value) return false;
      if (osSel.value && osOf(vm) !== osSel.value) return false;
      const q = filter.value.trim().toLowerCase();
      return !q || `${vm.name} ${vm.folder} ${vm.guest_full_name}`.toLowerCase().includes(q);
    };

    const render = () => {
      const filtered = vms.filter(matches);
      const size = pageSize(), pages = Math.max(1, Math.ceil(filtered.length / size));
      page = Math.min(page, pages - 1);
      const start = page * size, visible = filtered.slice(start, start + size);
      rows.innerHTML = "";
      for (const vm of visible) {
        const job = state.jobsByVm[vm.moid];
        const off = vm.power_state === "poweredOff";
        const on = vm.power_state === "poweredOn";  // migratable: the helper shuts it down before the export
        const active = job && !TERMINAL.includes(job.phase);
        rows.append(el("tr", {},
          el("td", { class: "name" }, vm.name),
          el("td", { class: "muted" }, vm.folder || "-"),
          el("td", {}, el("span", { class: "power " + vm.power_state }, vm.power_state.replace("powered", "").toLowerCase())),
          el("td", {}, vm.guest_full_name || vm.guest_id || "-"),
          el("td", {}, `${vm.num_cpu} / ${fmtBytes(vm.memory_mb * 1024 * 1024)}`),
          el("td", {}, `${vm.num_disks} (${fmtBytes(vm.disk_capacity_bytes)})`),
          el("td", {}, job ? el("a", { href: `#/jobs/${job.id}`, class: "phase " + job.phase }, job.phase) : el("span", { class: "muted" }, "-")),
          el("td", {}, active
            ? el("a", { href: `#/jobs/${job.id}`, class: "button secondary small" }, "View job")
            : el("a", { href: `#/export/${vm.moid}`, class: "button primary small" + (off || on ? "" : " disabled"),
              title: off ? "" : on ? "The VM is powered on: it will be shut down just before the disk export" : "Resume and shut down, or power off the VM first" }, "Migrate"))));
      }
      if (!visible.length) rows.append(el("tr", {}, el("td", { colspan: 8, class: "muted" }, vms.length ? "No virtual machines match the filters." : "No virtual machines found in this inventory.")));
      count.textContent = filtered.length === vms.length ? `${vms.length} virtual machines` : `${filtered.length} of ${vms.length} virtual machines`;
      // pagination: only when the filtered list does not fit on one page
      pager.hidden = filtered.length <= size && page === 0;
      pageInfo.textContent = filtered.length ? `${start + 1}-${Math.min(start + size, filtered.length)} of ${filtered.length} (page ${page + 1} of ${pages})` : "";
      prevBtn.disabled = page === 0;
      nextBtn.disabled = page >= pages - 1;
    };
    const resetPage = () => { page = 0; render(); };

    const load = async (refresh) => {
      err.textContent = ""; count.textContent = "Loading inventory...";
      try {
        const [list, jobs] = await Promise.all([api("GET", "/vms" + (refresh ? "?refresh=true" : "")), api("GET", "/jobs")]);
        vms = list;
        state.jobsByVm = {};
        for (const j of jobs) if (!state.jobsByVm[j.vm.moid]) state.jobsByVm[j.vm.moid] = j; // jobs are newest first
        fillFilters();
        render();
      } catch (e) { if (e.status !== 401) err.textContent = e.message; count.textContent = ""; }
    };
    filter.addEventListener("input", resetPage);
    offOnly.addEventListener("change", resetPage);
    folderSel.addEventListener("change", resetPage);
    osSel.addEventListener("change", resetPage);
    pageSizeSel.addEventListener("change", resetPage);
    prevBtn.addEventListener("click", () => { page = Math.max(0, page - 1); render(); rows.closest("table").scrollIntoView({ block: "start" }); });
    nextBtn.addEventListener("click", () => { page += 1; render(); rows.closest("table").scrollIntoView({ block: "start" }); });
    document.getElementById("vm-refresh").addEventListener("click", () => load(true));
    await load(false);
  }

  // -------------------------------------------------------------- export view
  async function exportView(moid) {
    app.innerHTML = "";
    app.append(tpl("tpl-export"));
    const form = document.getElementById("target-form");
    const formError = document.getElementById("form-error");
    const submit = document.getElementById("submit-btn");

    let inspection, options;
    try {
      [inspection, options] = await Promise.all([api("GET", `/vms/${encodeURIComponent(moid)}`), api("GET", "/oci/options")]);
    } catch (e) { if (e.status !== 401) showError("Cannot load VM or OCI information: " + e.message); return; }
    const vm = inspection.vm;

    kv(document.getElementById("vm-details"), [
      ["Name", vm.name], ["Guest OS", vm.guest_full_name || vm.guest_id],
      ["Power state", vm.power_state], ["ESXi host", vm.host_name || "-"],
      ["CPU / memory", `${vm.num_cpu} vCPU / ${fmtBytes(vm.memory_mb * 1024 * 1024)}`],
      ["Firmware", vm.firmware.toUpperCase() + (vm.secure_boot ? " (secure boot)" : "")],
      ["Disks", vm.disks.map((d) => `${d.label}: ${fmtBytes(d.capacity_bytes)} on ${d.controller_type}`).join("; ")],
      // one line per adapter: type, port group and the last addresses VMware Tools reported (when vCenter knows them)
      ["Network", vm.nics.length ? el("span", {}, ...vm.nics.map((n) => el("div", {},
        `${n.label}: ${n.adapter_type}${n.network ? " on " + n.network : ""}`,
        n.ip_addresses && n.ip_addresses.length ? el("span", {}, " - ", el("strong", {}, n.ip_addresses.join(", ")))
          : el("span", { class: "muted" }, " - IP address unknown")))) : "-"],
    ]);
    const problems = document.getElementById("vm-problems");
    for (const p of inspection.problems) problems.append(el("li", {}, p));
    const warnings = document.getElementById("vm-warnings");
    for (const w of inspection.warnings) warnings.append(el("li", {}, w));
    document.getElementById("power-off-note").hidden = !inspection.needs_power_off;

    // populate the target form
    const sel = (name) => form.elements[name];
    for (const c of options.compartments) {
      sel("compartment_id").append(el("option", { value: c.id }, c.path || c.name));
      sel("network_compartment_id").append(el("option", { value: c.id }, c.path || c.name));
    }
    // not a choice: the helper writes the volumes itself and boot volumes are AD-local, so the target
    // always lands in the helper's AD (the API enforces it as well)
    sel("availability_domain").value = options.helper_availability_domain;
    // VCN -> subnet: the subnet list is filtered by the selected VCN
    let netOptions = options;
    const fillSubnets = () => {
      const vcnId = sel("vcn_id").value;
      const subnets = netOptions.subnets.filter((s) => s.vcn_id === vcnId);
      sel("subnet_id").innerHTML = "";
      for (const s of subnets) sel("subnet_id").append(el("option", { value: s.id }, `${s.name} (${s.cidr_block})${s.prohibit_public_ip ? ", private" : ""}${s.availability_domain ? ", " + s.availability_domain : ""}`));
      document.getElementById("subnet-hint").textContent = subnets.length ? "" : (vcnId ? "No subnets in this VCN within the network compartment." : "Select a VCN first.");
      checkPrivateIp();
    };
    // fixed private IP: instant feedback that it fits the selected subnet's CIDR (the API additionally asks
    // OCI whether the address is free; the first two and the last address of a CIDR are reserved by OCI)
    const ipInput = sel("private_ip"); const ipHint = document.getElementById("private-ip-hint");
    const ipCheckBtn = document.getElementById("private-ip-check");
    const ipHintDefault = ipHint.textContent;
    const ipToInt = (ip) => { const m = /^\s*(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\s*$/.exec(ip); if (!m) return null; const p = m.slice(1).map(Number); return p.some((x) => x > 255) ? null : ((p[0] << 24) | (p[1] << 16) | (p[2] << 8) | p[3]) >>> 0; };
    const setIpHint = (text, cls) => { ipHint.textContent = text; ipHint.classList.remove("error", "ok"); if (cls) ipHint.classList.add(cls); };
    // local (instant) part: syntax and CIDR fit; returns the problem text or "" when OCI may be asked
    const localIpProblem = () => {
      const raw = ipInput.value.trim(); const subnet = netOptions.subnets.find((s) => s.id === sel("subnet_id").value);
      const ip = ipToInt(raw); const [base, bits] = (subnet && subnet.cidr_block || "").split("/");
      if (ip === null) return "Enter an IPv4 address such as 10.0.1.25.";
      if (subnet && bits !== undefined) {
        const mask = bits === "0" ? 0 : (~0 << (32 - Number(bits))) >>> 0; const net = (ipToInt(base) & mask) >>> 0; const bcast = (net | (~mask >>> 0)) >>> 0;
        if ((ip & mask) >>> 0 !== net) return `${raw} is outside the subnet ${subnet.name} (${subnet.cidr_block}).`;
        if (ip === net || ip === net + 1 || ip === bcast) return `${raw} is reserved by OCI in ${subnet.cidr_block} (network address, gateway or broadcast).`;
      }
      return "";
    };
    // remote part: ask OCI (GET /api/oci/private-ip-check) whether the address is allocated; the answer is
    // tied to the exact ip+subnet it was given for, so any later edit invalidates it
    let ipChecked = null;  // { key, available }
    let ipCheckTimer = null; let ipCheckSeq = 0;
    const ipKey = () => `${ipInput.value.trim()}@${sel("subnet_id").value}`;
    const remoteIpCheck = async () => {
      const key = ipKey(); const [ip, subnetId] = key.split("@");
      if (!ip || !subnetId || localIpProblem()) return;
      const seq = ++ipCheckSeq;
      ipCheckBtn.disabled = true; setIpHint(`Checking with OCI whether ${ip} is free...`);
      try {
        const res = await api("GET", `/oci/private-ip-check?subnet_id=${encodeURIComponent(subnetId)}&ip=${encodeURIComponent(ip)}`);
        if (seq !== ipCheckSeq || key !== ipKey()) return;  // user typed on meanwhile
        ipChecked = { key, available: res.available };
        ipInput.setCustomValidity(res.available ? "" : res.message);
        setIpHint(res.message, res.available ? "ok" : "error");
      } catch (e) {
        if (seq !== ipCheckSeq || key !== ipKey()) return;
        ipChecked = null;
        setIpHint(`Could not check ${ip} with OCI: ${e.message}. You can retry with Check; the migration verifies it again.`, "error");
      } finally { if (key === ipKey()) ipCheckBtn.disabled = false; }
    };
    const checkPrivateIp = () => {
      clearTimeout(ipCheckTimer); ipCheckSeq++;
      const raw = ipInput.value.trim();
      ipInput.setCustomValidity("");
      if (!raw) { ipChecked = null; ipCheckBtn.disabled = true; setIpHint(ipHintDefault); return; }
      const problem = localIpProblem();
      if (problem) { ipChecked = null; ipCheckBtn.disabled = true; ipInput.setCustomValidity(problem); setIpHint(problem, "error"); return; }
      ipCheckBtn.disabled = false;
      if (ipChecked && ipChecked.key === ipKey()) {  // unchanged since the last answer
        if (!ipChecked.available) ipInput.setCustomValidity(ipHint.textContent);
        return;
      }
      const subnet = netOptions.subnets.find((s) => s.id === sel("subnet_id").value);
      setIpHint(`${raw} lies in ${subnet ? subnet.cidr_block : "the subnet"}; checking with OCI whether it is free...`);
      ipCheckTimer = setTimeout(remoteIpCheck, 700);  // a complete address usually means a typing pause
    };
    ipInput.addEventListener("input", checkPrivateIp);
    ipInput.addEventListener("blur", () => { if (ipCheckTimer && !localIpProblem() && ipInput.value.trim()) { clearTimeout(ipCheckTimer); remoteIpCheck(); } });
    ipCheckBtn.addEventListener("click", () => { clearTimeout(ipCheckTimer); ipChecked = null; remoteIpCheck(); });
    sel("subnet_id").addEventListener("change", checkPrivateIp);
    const fillNetworks = (o) => {
      netOptions = o;
      const vcnSel = sel("vcn_id");
      const previous = vcnSel.value;
      vcnSel.innerHTML = "";
      for (const v of o.vcns) vcnSel.append(el("option", { value: v.id }, `${v.name}${v.cidr_blocks.length ? " (" + v.cidr_blocks.join(", ") + ")" : ""}`));
      // VCNs that only show up through their subnets (VCN in another compartment)
      for (const s of o.subnets) if (!o.vcns.some((v) => v.id === s.vcn_id) && ![...vcnSel.options].some((op) => op.value === s.vcn_id)) vcnSel.append(el("option", { value: s.vcn_id }, s.vcn_name || s.vcn_id));
      if ([...vcnSel.options].some((op) => op.value === previous)) vcnSel.value = previous;
      fillSubnets();
    };
    sel("vcn_id").addEventListener("change", fillSubnets);
    fillNetworks(options);
    // shapes: x86 flex shapes only (the API already drops Ampere/ARM shapes)
    let shapes = options.shapes;
    const fillShapes = (o) => {
      shapes = o.shapes;
      const shapeSel = sel("shape"); const previous = shapeSel.value;
      shapeSel.innerHTML = "";
      shapeSel.append(el("option", { value: "" }, `${o.default_shape} (default)`));
      for (const s of o.shapes) if (s.name !== o.default_shape) shapeSel.append(el("option", { value: s.name }, s.name));
      if ([...shapeSel.options].some((op) => op.value === previous)) shapeSel.value = previous;
      renderSizing();
    };
    // sizing mirrors mapping.map_shape: 2 vCPU = 1 OCPU, RAM rounded up to whole GB; both can be overridden
    const autoOcpus = Math.max(1, Math.ceil(vm.num_cpu / 2));
    const autoMemoryGb = Math.max(1, Math.ceil(vm.memory_mb / 1024));
    const currentShape = () => shapes.find((s) => s.name === (sel("shape").value || options.default_shape));
    const renderSizing = () => {
      const shape = currentShape();
      const ocpusIn = sel("ocpus"), memIn = sel("memory_gb");
      ocpusIn.placeholder = `${autoOcpus} (auto)`; memIn.placeholder = `${autoMemoryGb} (auto)`;
      if (shape && shape.is_flex) {
        if (shape.min_ocpus != null) ocpusIn.min = shape.min_ocpus;
        if (shape.max_ocpus != null) ocpusIn.max = shape.max_ocpus;
        if (shape.min_memory_gb != null) memIn.min = shape.min_memory_gb;
        if (shape.max_memory_gb != null) memIn.max = shape.max_memory_gb;
      } else { ocpusIn.removeAttribute("max"); memIn.removeAttribute("max"); }
      const ocpus = Number(ocpusIn.value) || autoOcpus, mem = Number(memIn.value) || autoMemoryGb;
      const overridden = ocpusIn.value !== "" || memIn.value !== "";
      const range = shape && shape.is_flex && shape.max_ocpus != null
        ? ` ${shape.name} allows ${shape.min_ocpus ?? 1}-${shape.max_ocpus} OCPU and ${shape.min_memory_gb ?? 1}-${shape.max_memory_gb} GB.` : "";
      document.getElementById("shape-hint").textContent = `Source VM: ${vm.num_cpu} vCPU / ${(vm.memory_mb / 1024).toFixed(vm.memory_mb % 1024 ? 1 : 0)} GB.${range}`;
      document.getElementById("sizing-hint").textContent = overridden
        ? `Instance will be launched with ${ocpus} OCPU / ${mem} GB (custom). Leave both fields empty to size from the source VM.`
        : `Instance will be launched with ${autoOcpus} OCPU / ${autoMemoryGb} GB, derived from the source VM. Enter values to override.`;
    };
    sel("shape").addEventListener("change", renderSizing);
    sel("ocpus").addEventListener("input", renderSizing);
    sel("memory_gb").addEventListener("input", renderSizing);
    fillShapes(options);
    sel("display_name").value = vm.name;
    // guest OS release recorded on the OCI image: vSphere encodes it for most guests, but not for e.g.
    // ubuntu64Guest ("Ubuntu Linux (64-bit)"), where the user has to pick it from OCI's list
    const osInfo = inspection.os;
    const osLabel = document.getElementById("os-version-label"), osSel = sel("operating_system_version");
    if (osInfo && osInfo.version_choices.length) {
      osLabel.hidden = false;
      osSel.innerHTML = "";
      if (!osInfo.version_detected) osSel.append(el("option", { value: "" }, `Select the ${osInfo.operating_system} release...`));
      for (const v of osInfo.version_choices) osSel.append(el("option", { value: v }, `${osInfo.operating_system} ${v}`));
      osSel.value = osInfo.version_detected ? osInfo.operating_system_version : "";
      osSel.required = !osInfo.version_detected;
      osLabel.classList.toggle("attention", !osInfo.version_detected);
      osSel.addEventListener("change", () => osLabel.classList.toggle("attention", !osSel.value));
      document.getElementById("os-version-hint").textContent = osInfo.version_detected
        ? `Detected from vCenter (${vm.guest_full_name || vm.guest_id}); change it if the guest runs another release.`
        : `vCenter only reports "${vm.guest_full_name || vm.guest_id}" without the release. Select the one installed in the guest; OCI records it on the image and uses it for OS-specific defaults.`;
    } else {
      osLabel.hidden = true; osSel.required = false;
    }
    const isWin = isWindows(vm);
    document.getElementById("windows-fieldset").hidden = !isWin;
    document.getElementById("windows-driver-note").hidden = !isWin;
    // the initramfs fix-up is a Linux thing (Windows gets its VirtIO drivers installed inside the guest)
    document.getElementById("rebuild-initramfs-label").hidden = isWin;
    document.getElementById("rebuild-initramfs-hint").hidden = isWin;
    if (isWin && isWindowsClient(vm)) {
      // OCI has no licenses for client editions; the API refuses OCI_PROVIDED for them
      const ociLic = form.querySelector('input[name="windows_license_type"][value="OCI_PROVIDED"]');
      ociLic.disabled = true; ociLic.checked = false;
      form.querySelector('input[name="windows_license_type"][value="BRING_YOUR_OWN_LICENSE"]').checked = true;
      document.getElementById("windows-license-hint").textContent = "Windows 10/11: OCI does not provide licenses for client editions, so the instance is registered as BYOL (check your Microsoft license terms for running the desktop OS in a cloud).";
    }
    document.getElementById("esxi-host-hint").textContent = vm.host_name ? `(${vm.host_name})` : "";
    sel("nfc_direct_to_esxi").disabled = !vm.host_name;
    // both compartment pickers start at the helper's compartment; the instance compartment drives the shape
    // list, the network compartment the VCN/subnet list
    for (const name of ["compartment_id", "network_compartment_id"]) {
      if (options.compartments.some((c) => c.id === options.helper_compartment_id)) sel(name).value = options.helper_compartment_id;
      else if (options.compartments.length) sel(name).selectedIndex = 0;
    }
    const reloadOptions = async () => {
      formError.textContent = "";
      const q = new URLSearchParams({ compartment_id: sel("compartment_id").value, network_compartment_id: sel("network_compartment_id").value });
      try {
        const o = await api("GET", `/oci/options?${q}`);
        fillNetworks(o); fillShapes(o);
      } catch (e) { formError.textContent = e.message; }
    };
    sel("compartment_id").addEventListener("change", reloadOptions);
    sel("network_compartment_id").addEventListener("change", reloadOptions);

    // mirrors mapping.map_launch_options: paravirtualized unless "Maximum compatibility" or an override is chosen
    const renderPreview = () => {
      const compat = form.elements.compatibility_mode.checked;
      const bootOverride = form.elements.boot_volume_type_override.value, netOverride = form.elements.network_type_override.value;
      kv(document.getElementById("launch-preview"), [
        ["Firmware", (vm.firmware === "efi" ? "UEFI_64" : "BIOS") + (vm.secure_boot ? " + Secure Boot" : "")],
        ["Boot volume type", bootOverride || (compat ? "IDE" : "PARAVIRTUALIZED")],
        ["Network type", netOverride || (compat ? "E1000" : "PARAVIRTUALIZED")],
      ]);
    };
    renderPreview();
    for (const name of ["compatibility_mode", "boot_volume_type_override", "network_type_override"]) {
      form.elements[name].addEventListener("change", renderPreview);
    }

    submit.disabled = !inspection.can_export;

    // a migration of this VM is already running: nothing to configure here, show the job instead
    try {
      const jobs = await api("GET", `/jobs?vm_moid=${encodeURIComponent(moid)}`);
      const active = jobs.find((j) => !TERMINAL.includes(j.phase));
      if (active) { location.hash = `#/jobs/${active.id}`; return; }
    } catch (_) { /* ignore */ }

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      formError.textContent = "";
      const fd = new FormData(form);
      const target = {
        compartment_id: fd.get("compartment_id"),
        availability_domain: options.helper_availability_domain,
        subnet_id: fd.get("subnet_id"),
        private_ip: (fd.get("private_ip") || "").trim() || null,
        shape: fd.get("shape") || null,
        ocpus: fd.get("ocpus") ? Number(fd.get("ocpus")) : null,
        memory_gb: fd.get("memory_gb") ? Number(fd.get("memory_gb")) : null,
        display_name: fd.get("display_name") || null,
        operating_system_version: osLabel.hidden ? null : (fd.get("operating_system_version") || null),
        assign_public_ip: fd.get("assign_public_ip") === "on",
        start_after_migration: fd.get("start_after_migration") === "on",
        windows_license_type: isWin ? fd.get("windows_license_type") : null,
        compatibility_mode: fd.get("compatibility_mode") === "on",
        boot_volume_type_override: fd.get("boot_volume_type_override") || null,
        network_type_override: fd.get("network_type_override") || null,
        nfc_direct_to_esxi: fd.get("nfc_direct_to_esxi") === "on",
        pipelined_decode: fd.get("pipelined_decode") === "on",
        rebuild_initramfs: !isWin && fd.get("rebuild_initramfs") === "on",
        volume_vpus_per_gb: Number(fd.get("volume_vpus_per_gb") || 10),
      };
      // a running VM is shut down by the migration: make the operator confirm it, naming the VM
      if (inspection.needs_power_off) {
        const how = inspection.tools_running
          ? "It will be shut down through VMware Tools (guest OS shutdown); if it does not stop in time it is powered off hard."
          : "VMware Tools is NOT running, so it will be POWERED OFF HARD (like pulling the plug).";
        const ok = confirm(`WARNING: "${vm.name}" is powered on.\n\n` +
          `Starting this migration will POWER OFF the VM "${vm.name}" right before the disk export ` +
          `(after the OCI instance and volumes are prepared). ${how}\n\n` +
          "The VM stays powered off in vSphere afterwards.\n\n" +
          `Power off "${vm.name}" and migrate it?`);
        if (!ok) return;
      }
      submit.disabled = true;
      try {
        const job = await api("POST", "/jobs", { vm_moid: moid, target, power_off_source: inspection.needs_power_off });
        location.hash = `#/jobs/${job.id}`;  // follow the migration on its own page
      } catch (e) { formError.textContent = e.message; submit.disabled = false; }
    });
  }

  // ---------------------------------------------------------------- jobs view
  async function jobsView() {
    let jobs = [];
    try { jobs = await api("GET", "/jobs"); } catch (e) { if (e.status !== 401) showError(e.message); return; }
    app.innerHTML = "";
    // fixed layout (see style.css): the message column takes what the others leave
    const columns = [["VM", "15%"], ["Phase", "112px"], ["Message", null], ["OCI instance", "17%"], ["Started", "11%"], ["By", "13%", "by"], ["", "150px"]];
    const table = el("table", { class: "jobs" },
      el("colgroup", {}, ...columns.map(([, w, cls]) => el("col", { style: w ? `width:${w}` : null, class: cls || null }))),
      el("thead", {}, el("tr", {}, ...columns.map(([h, , cls]) => el("th", { class: cls || null }, h)))),
      el("tbody", {}, ...jobs.map((j) => el("tr", {},
        el("td", { class: "name" }, j.vm.name), el("td", {}, el("span", { class: "phase " + j.phase }, j.phase)),
        el("td", {}, (j.message || "") + (j.phase === "EXPORTING" && j.transfer && j.transfer.started_at
          ? ` - ${j.transfer.percent || 0}%${j.transfer.throughput_bps ? ", " + fmtRate(j.transfer.throughput_bps) : ""}`
          : !TERMINAL.includes(j.phase) && j.step_percent !== null && j.step_percent !== undefined && !/\d+%/.test(j.message || "")
            ? ` - ${j.step_percent}%` : "")),
        el("td", { class: "ocid", title: j.instance_id || "" }, ocidLink("instances", j.instance_id)),
        el("td", {}, new Date(j.created_at).toLocaleString()), el("td", { class: "by" }, j.created_by || "-"),
        el("td", { class: "row-actions" },
          hasConsole(j) ? el("a", { class: "button secondary small", href: `#/jobs/${j.id}/console`, title: "Open the VNC console of the instance" }, "Console") : null,
          el("a", { class: "button secondary small", href: `#/jobs/${j.id}` }, "Details"))))));
    app.append(el("div", { class: "card" }, el("h2", {}, "Migration jobs"),
      jobs.length ? table : el("div", { class: "muted" }, "No jobs yet. Pick a powered-off VM under Source VMs to start one.")));
    // refresh the table while jobs are active
    if (jobs.some((j) => !TERMINAL.includes(j.phase))) {
      const t = setTimeout(() => { if (location.hash === "#/jobs") jobsView(); }, 5000);
      activePoll = () => clearTimeout(t);
    }
  }

  async function jobDetailView(jobId) {
    app.innerHTML = "";
    const c = el("div", { class: "card" });
    app.append(el("div", { class: "toolbar" }, el("a", { href: "#/jobs", class: "muted" }, "\u2190 all jobs")), c);
    activePoll = pollJob(jobId, c);
  }

  // ------------------------------------------------------------ remote console
  // OCI instance console connection (created by the helper with a temporary key) -> SSH tunnel on the helper
  // -> WebSocket on this origin -> noVNC in this page.  The connection is deleted on Close or when idle.
  async function consoleView(jobId) {
    app.innerHTML = "";
    app.append(tpl("tpl-console"));
    const status = document.getElementById("console-status"), errBox = document.getElementById("console-error");
    const screen = document.getElementById("vnc-screen");
    const cadBtn = document.getElementById("console-cad"), reconnectBtn = document.getElementById("console-reconnect");
    const closeBtn = document.getElementById("console-close"), back = document.getElementById("console-back");
    back.href = `#/jobs/${jobId}`;
    let rfb = null; let stopped = false; let closing = false;
    const setStatus = (text, ok) => { status.textContent = text; status.className = "console-status" + (ok ? " ok" : " muted"); };
    const setError = (text) => { errBox.textContent = text || ""; };

    let job;
    try { job = await api("GET", `/jobs/${encodeURIComponent(jobId)}`); }
    catch (e) { if (e.status !== 401) showError(e.message); return; }
    document.getElementById("console-title").textContent = `- ${job.instance_display_name || job.vm.name}`;
    if (!hasConsole(job)) { setStatus("", false); setError("The remote console is available for completed migrations with an OCI instance."); closeBtn.hidden = true; return; }

    // 1. console connection on the OCI side (idempotent while one is active)
    const openConnection = async () => {
      setStatus("Creating the OCI console connection...", false);
      let st;
      try { st = await api("POST", `/jobs/${encodeURIComponent(jobId)}/console`); }
      catch (e) {
        if (e.status === 409 && e.detail && e.detail.code === "foreign_connection") {
          if (!confirm(`${e.detail.message}\n\nReplace the existing console connection ${e.detail.connection_id}?`)) throw new Error("An existing console connection is in the way; nothing was changed.");
          st = await api("POST", `/jobs/${encodeURIComponent(jobId)}/console?replace=true`);
        } else throw e;
      }
      while (st.state === "CREATING" && !stopped) {
        await new Promise((r) => setTimeout(r, 2000));
        st = await api("GET", `/jobs/${encodeURIComponent(jobId)}/console`);
      }
      if (st.state !== "ACTIVE") throw new Error(st.error || `console connection is ${st.state}`);
      return st;
    };

    // 2. noVNC over the helper's WebSocket bridge
    const connectVnc = async () => {
      const { default: RFB } = await import("./vendor/novnc/core/rfb.js");
      const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/jobs/${encodeURIComponent(jobId)}/console/vnc`;
      setStatus("Connecting to the instance console...", false);
      screen.innerHTML = "";
      rfb = new RFB(screen, url, {});
      rfb.scaleViewport = true;
      rfb.resizeSession = false;
      rfb.background = "#000";
      rfb.addEventListener("connect", () => { setStatus("Connected", true); setError(""); cadBtn.disabled = false; reconnectBtn.disabled = true; rfb.focus(); });
      rfb.addEventListener("disconnect", (ev) => {
        cadBtn.disabled = true; reconnectBtn.disabled = stopped;
        if (closing) return;
        setStatus(ev.detail.clean ? "Disconnected" : "Connection lost", false);
        if (!ev.detail.clean) setError("The console connection dropped (the helper logs the reason; the instance may be rebooting or the tunnel was refused). Use Reconnect to try again.");
      });
      rfb.addEventListener("securityfailure", (ev) => setError(`VNC security failure: ${ev.detail.reason || ev.detail.status}`));
      rfb.addEventListener("credentialsrequired", () => setError("The VNC server asked for credentials; the OCI console does not normally do this."));
    };

    const start = async () => {
      setError(""); reconnectBtn.disabled = true;
      try { await openConnection(); if (!stopped) await connectVnc(); }
      catch (e) { if (e.status === 401) return; setStatus("Not connected", false); setError(e.message); reconnectBtn.disabled = false; }
    };
    cadBtn.onclick = () => { if (rfb) rfb.sendCtrlAltDel(); };
    reconnectBtn.onclick = () => { if (rfb) { try { rfb.disconnect(); } catch (_) { /* already gone */ } rfb = null; } start(); };
    closeBtn.onclick = async () => {
      if (!confirm("Close the remote console and delete the OCI console connection?")) return;
      closing = true; closeBtn.disabled = true; setStatus("Closing...", false);
      if (rfb) { try { rfb.disconnect(); } catch (_) { /* ignore */ } rfb = null; }
      try { await api("DELETE", `/jobs/${encodeURIComponent(jobId)}/console`); } catch (e) { alert(e.message); }
      location.hash = `#/jobs/${jobId}`;
    };
    // leaving the view (hash change) disconnects the VNC session; the console connection stays for a quick
    // return and is removed by the helper's idle timeout
    activePoll = () => { stopped = true; if (rfb) { try { rfb.disconnect(); } catch (_) { /* ignore */ } rfb = null; } };
    await start();
  }

  // --------------------------------------------------------------- setup view
  async function setupView() {
    app.innerHTML = "";
    app.append(tpl("tpl-setup"));
    const swKv = document.getElementById("sw-kv"); const swState = document.getElementById("sw-state");
    const swErr = document.getElementById("sw-error"); const swBtn = document.getElementById("sw-update");
    const swLog = document.getElementById("sw-log"); const swLogDetails = document.getElementById("sw-log-details");
    const short = (sha) => (sha || "").slice(0, 10);
    const when = (iso) => (iso ? new Date(iso).toLocaleString() : "");
    let timer = null;
    let watching = false; // update triggered: keep the page locked until the helper comes back
    let lastRemote = {};  // latest_* fields survive refreshes done with check=false

    const renderSoftware = (sw) => {
      if (sw.latest_commit) lastRemote = { latest_commit: sw.latest_commit, latest_date: sw.latest_date, latest_subject: sw.latest_subject, update_available: sw.update_available };
      else if (!sw.check_error) sw = { ...lastRemote, ...sw, latest_commit: lastRemote.latest_commit || "", latest_date: lastRemote.latest_date || "", latest_subject: lastRemote.latest_subject || "", update_available: lastRemote.update_available ?? null };
      if (watching) sw = { ...sw, update_running: true, can_update: false, reason: "" };
      const badge = sw.update_running ? el("span", { class: "badge warn" }, "update running")
        : sw.update_available === true ? el("span", { class: "badge warn" }, "update available")
        : sw.update_available === false ? el("span", { class: "badge ok" }, "up to date")
        : el("span", { class: "badge" }, sw.install_method === "source" ? "unknown" : sw.install_method);
      const rows = [["Installed version", `${sw.version}`], ["Status", badge]];
      if (sw.install_method === "source") {
        rows.push(["Installed commit", `${short(sw.commit)}${sw.commit_date ? " (" + when(sw.commit_date) + ")" : ""}${sw.commit_subject ? " - " + sw.commit_subject : ""}`]);
        rows.push(["Latest on " + (sw.branch || "remote"), sw.latest_commit ? `${short(sw.latest_commit)}${sw.latest_date ? " (" + when(sw.latest_date) + ")" : ""}${sw.latest_subject ? " - " + sw.latest_subject : ""}` : (sw.check_error || "-")]);
        rows.push(["Repository", sw.repo_url ? el("a", { href: sw.repo_url, target: "_blank", rel: "noopener" }, sw.repo_url) : sw.source_dir]);
      }
      kv(swKv, rows);
      swErr.textContent = sw.can_update ? "" : (sw.reason || "");
      swBtn.hidden = sw.install_method !== "source";
      swBtn.disabled = !sw.can_update;
      swBtn.textContent = sw.update_available === false ? "Reinstall current version" : "Update now";
      swLogDetails.hidden = !sw.log;
      swLog.textContent = sw.log || "";
      if (sw.update_running) { swLogDetails.open = true; swLog.scrollTop = swLog.scrollHeight; }
      return sw;
    };

    const loadSoftware = async (check) => {
      swState.textContent = check ? "Checking GitHub..." : "";
      try { const sw = renderSoftware(await api("GET", "/setup/software?check=" + (check ? "true" : "false"))); swState.textContent = ""; return sw; }
      catch (e) { if (e.status !== 401) swErr.textContent = e.message; swState.textContent = ""; return null; }
    };

    // after the update is triggered the service restarts: watch /api/health until the commit changes,
    // then send the user back to the login page (sessions do not survive a restart)
    const watchRestart = (oldCommit) => {
      const started = Date.now();
      const tick = async () => {
        try {
          const r = await fetch("../api/health", { cache: "no-store" });
          if (r.ok) {
            const h = await r.json();
            if (h.commit && h.commit !== oldCommit) { swState.textContent = `Updated to ${short(h.commit)}; please log in again.`; setTimeout(showLogin, 1500); return; }
          }
        } catch (_) { swState.textContent = "Helper is restarting..."; }
        if (Date.now() - started > 15 * 60 * 1000) { swState.textContent = "The update is taking unusually long; check the update log or the service journal."; return; }
        const sw = await loadSoftware(false).catch(() => null);
        if (sw) swState.textContent = "Update running; waiting for the helper to restart...";
        if (sw && sw.log && /UPDATE FAILED/.test(sw.log.split("update started").pop())) { watching = false; renderSoftware(sw); swState.textContent = "The update failed; see the log."; return; }
        timer = setTimeout(tick, 3000);
      };
      timer = setTimeout(tick, 3000);
    };

    swBtn.addEventListener("click", async () => {
      const sw = await loadSoftware(false);
      if (!sw) return;
      const msg = sw.update_available ? "Update the helper to the latest version from GitHub and restart the service?" : "Reinstall the current version and restart the service?";
      if (!confirm(msg + "\n\nAll users will have to log in again.")) return;
      swBtn.disabled = true; swErr.textContent = "";
      try { const r = await api("POST", "/setup/software/update", {}); watching = true; renderSoftware(r); swState.textContent = "Update started..."; watchRestart(sw.commit); }
      catch (e) { swErr.textContent = e.message; swBtn.disabled = false; }
    });
    document.getElementById("sw-check").addEventListener("click", () => loadSoftware(true));

    // logging: applies immediately, persisted on the helper
    const logForm = document.getElementById("log-form"); const logResult = document.getElementById("log-result");
    const renderLogging = (lg) => {
      const sel = logForm.elements.log_level;
      sel.innerHTML = "";
      for (const l of lg.levels) sel.append(el("option", { value: l }, l));
      sel.value = lg.log_level;
      logForm.elements.oci_log_requests.checked = lg.oci_log_requests;
      logResult.textContent = lg.warning || (lg.persisted ? "" : "Defaults from the environment; not changed yet.");
      logResult.className = lg.warning ? "error" : "muted";
    };
    logForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const btn = document.getElementById("log-save"); btn.disabled = true;
      try {
        const lg = await api("PUT", "/setup/logging", { log_level: logForm.elements.log_level.value, oci_log_requests: logForm.elements.oci_log_requests.checked });
        renderLogging(lg);
        if (!lg.warning) logResult.textContent = "Saved and applied.";
      } catch (e) { logResult.textContent = e.message; logResult.className = "error"; }
      finally { btn.disabled = false; }
    });

    // helper identity; the operation values are taken from the form so a save is reflected at once
    let info = null;
    const fmtTtl = (s) => { const h = s / 3600; return h >= 1 ? `${Math.round(h * 10) / 10} h` : `${Math.round(s / 60)} min`; };
    const renderHelperInfo = () => {
      if (!info) return;
      const f = document.getElementById("op-form").elements;
      state.region = state.region || info.region;
      kv(document.getElementById("setup-kv"), [
        ["Version", info.version + (info.commit ? ` (${short(info.commit)})` : "")],
        ["Region / AD", `${info.region} / ${info.availability_domain}`],
        ["Instance", ocidLink("instances", info.instance_id)], ["Compartment", info.compartment_id],
        ["Default vCenter", info.default_vcenter || "(none)"], ["Verify vCenter TLS", info.vcenter_verify_ssl ? "yes" : "no"],
        ["Seed image bucket", info.seed_bucket], ["Default shape", info.default_shape],
        ["Concurrent migrations", f.max_concurrent_jobs.value || String(info.max_concurrent_jobs)],
        ["Session idle timeout", fmtTtl(f.session_ttl_h.value ? Number(f.session_ttl_h.value) * 3600 : info.session_ttl_s)],
        ["Logged-in sessions", String(info.sessions)], ["Running migrations", String(info.active_jobs)],
      ]);
    };

    // operation limits: concurrency and session idle timeout; applied immediately, persisted on the helper
    const opForm = document.getElementById("op-form"); const opResult = document.getElementById("op-result");
    const renderOperation = (op) => {
      const conc = opForm.elements.max_concurrent_jobs, ttl = opForm.elements.session_ttl_h;
      conc.max = op.max_concurrent_jobs_limit; conc.value = op.max_concurrent_jobs;
      ttl.min = (op.session_ttl_min_s / 3600).toFixed(1); ttl.max = Math.round(op.session_ttl_max_s / 3600);
      ttl.value = String(Math.round(op.session_ttl_s / 360) / 10);
      document.getElementById("op-concurrency-hint").textContent = `Migrations copying disks at the same time (1-${op.max_concurrent_jobs_limit}); further jobs wait in the queue. Lowering it never interrupts a running migration.`;
      opResult.textContent = op.warning || (op.persisted ? "" : "Defaults from the environment; not changed yet.");
      opResult.className = op.warning ? "error" : "muted";
      renderHelperInfo();
    };
    opForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const btn = document.getElementById("op-save"); btn.disabled = true;
      try {
        const op = await api("PUT", "/setup/operation", {
          max_concurrent_jobs: Number(opForm.elements.max_concurrent_jobs.value),
          session_ttl_s: Math.round(Number(opForm.elements.session_ttl_h.value) * 3600),
        });
        renderOperation(op);
        if (!op.warning) opResult.textContent = "Saved and applied.";
      } catch (e) { opResult.textContent = e.message; opResult.className = "error"; }
      finally { btn.disabled = false; }
    });

    document.getElementById("seed-cleanup").addEventListener("click", async (ev) => {
      const out = document.getElementById("seed-result");
      if (!confirm("Delete all seed images and their staging objects?")) return;
      ev.target.disabled = true; out.textContent = "Deleting...";
      try { const r = await api("DELETE", "/seed-images"); out.textContent = `Deleted ${r.deleted.length} object(s).`; }
      catch (e) { out.textContent = e.message; } finally { ev.target.disabled = false; }
    });

    // job history: delete the records of failed (FAILED + CANCELLED) or of all finished jobs
    const purgeBtns = ["jobs-purge-failed", "jobs-purge-all"].map((id) => document.getElementById(id));
    const purge = async (scope) => {
      const out = document.getElementById("jobs-purge-result");
      const what = scope === "failed" ? "all FAILED and CANCELLED jobs" : "ALL finished jobs (completed, failed and cancelled)";
      if (!confirm(`Delete the records of ${what} from the helper?\n\n` +
        "Running or queued jobs are kept. This only removes the job history: OCI resources a failed job may have " +
        "left behind (instance, volumes) are NOT cleaned up - use 'Clean up OCI resources' on such jobs first if needed.\n\n" +
        "This cannot be undone.")) return;
      purgeBtns.forEach((b) => { b.disabled = true; }); out.textContent = "Deleting...";
      try {
        const r = await api("DELETE", `/setup/jobs?scope=${scope}`);
        out.textContent = `Deleted ${r.deleted} job record(s)${r.kept_active ? `; ${r.kept_active} active job(s) kept` : ""}.`;
      } catch (e) { out.textContent = e.message; } finally { purgeBtns.forEach((b) => { b.disabled = false; }); }
    };
    purgeBtns[0].addEventListener("click", () => purge("failed"));
    purgeBtns[1].addEventListener("click", () => purge("all"));

    try {
      const [inf, lg, op] = await Promise.all([api("GET", "/setup/info"), api("GET", "/setup/logging"), api("GET", "/setup/operation")]);
      info = inf;
      renderLogging(lg);
      renderOperation(op);
    } catch (e) { if (e.status !== 401) showError(e.message); return; }
    await loadSoftware(true);
    activePoll = () => clearTimeout(timer);
  }

  // ------------------------------------------------------------------- routing
  async function route() {
    stopPolling();
    if (!state.me) {
      try { setUser(await api("GET", "/auth/me")); } catch (e) { return; /* api() showed the login view */ }
    }
    if (!state.region) {
      try { state.region = (await api("GET", "/health")).region || ""; } catch (_) { /* links work without it */ }
    }
    const hash = location.hash || "#/vms";
    for (const a of nav.querySelectorAll("a")) a.classList.toggle("active", hash.startsWith(a.getAttribute("href")));
    let m;
    if ((m = /^#\/export\/(.+)$/.exec(hash))) return exportView(decodeURIComponent(m[1]));
    if ((m = /^#\/jobs\/([^/]+)\/console$/.exec(hash))) return consoleView(decodeURIComponent(m[1]));
    if ((m = /^#\/jobs\/(.+)$/.exec(hash))) return jobDetailView(decodeURIComponent(m[1]));
    if (hash === "#/jobs") return jobsView();
    if (hash === "#/setup") return setupView();
    return vmsView();
  }

  window.addEventListener("hashchange", () => { route().catch((e) => showError(e.message)); });
  route().catch((e) => showError(e.message));
})();
