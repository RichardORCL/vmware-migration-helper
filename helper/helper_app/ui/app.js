/* vCenter to OCI migration - helper web UI (no framework, hash routing). */
(function () {
  "use strict";

  const app = document.getElementById("app");
  const nav = document.getElementById("nav");
  const userBox = document.getElementById("user");
  const state = { me: null, config: null, jobsByVm: {} };
  let activePoll = null;

  // ---------------------------------------------------------------------- api
  class ApiError extends Error {
    constructor(message, status) { super(message); this.status = status; }
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
      throw new ApiError(detail, resp.status);
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
      if (k === "class") e.className = v; else if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else if (v !== null && v !== undefined) e.setAttribute(k, v);
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
  const TERMINAL = ["COMPLETED", "FAILED", "CANCELLED"];
  const STEP_LABELS = { seed_image: "Seed image import", launch_instance: "Instance launch" };

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
    if (form.elements.vcenter.value) form.elements.username.focus();
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      err.textContent = ""; btn.disabled = true;
      const vcenter = form.elements.vcenter.value.trim();
      try {
        const me = await api("POST", "/auth/login", { username: form.elements.username.value, password: form.elements.password.value, vcenter_host: vcenter });
        rememberVcenter(vcenter);
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
    const rows = [
      ["Source VM", `${job.vm.name} (${job.vm.moid})`],
      ["Step", job.step || "-"],
      ["Disk download", job.nfc_host ? `${job.nfc_host}${job.target.nfc_direct_to_esxi ? " (ESXi host, direct)" : ""}${job.target.pipelined_decode ? ", pipelined decode/write" : ""}` : "-"],
      ["Instance", job.instance_id || "-"],
      ["Seed image", job.seed_image_id || "-"],
      ["Launch options", job.launch_options ? `${job.launch_options.firmware}${job.launch_options.secure_boot ? " + Secure Boot (shielded instance, with Measured Boot + vTPM on VM shapes)" : ""}, boot ${job.launch_options.boot_volume_type}, nic ${job.launch_options.network_type}` : "-"],
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
    kv(root.querySelector("[data-oci]"), rows);

    const cancelBtn = root.querySelector("[data-cancel]");
    cancelBtn.hidden = job.phase === "COMPLETED" || job.phase === "CANCELLED";
    cancelBtn.textContent = job.phase === "FAILED" ? "Clean up OCI resources" : "Cancel";
    cancelBtn.onclick = async () => {
      if (!confirm("Cancel this migration? The OCI instance and volumes created so far will be deleted.")) return;
      cancelBtn.disabled = true;
      try { await api("POST", `/jobs/${job.id}/cancel`); } catch (e) { alert(e.message); }
      finally { cancelBtn.disabled = false; }
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
    const licSel = root.querySelector("[data-license]"); const licBtn = root.querySelector("[data-license-btn]");
    const showLic = isWindows(job.vm) && job.instance_id && (job.phase === "COMPLETED" || job.phase === "FINALIZING");
    licSel.hidden = licBtn.hidden = !showLic;
    if (showLic) {
      if (job.target.windows_license_type && !licSel.dataset.touched) licSel.value = job.target.windows_license_type;
      licSel.onchange = () => { licSel.dataset.touched = "1"; };
      licBtn.onclick = async () => {
        licBtn.disabled = true;
        try { await api("POST", `/jobs/${job.id}/licensing`, { license_type: licSel.value }); alert("License type updated."); }
        catch (e) { alert(e.message); } finally { licBtn.disabled = false; }
      };
    }
    if (opts.onTerminal && terminal) opts.onTerminal(job);
    return job.phase === "COMPLETED" || job.phase === "CANCELLED";
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
    const count = document.getElementById("vm-count");
    const err = document.getElementById("vm-error");
    let vms = [];

    const render = () => {
      const q = filter.value.trim().toLowerCase();
      rows.innerHTML = "";
      let shown = 0;
      for (const vm of vms) {
        if (offOnly.checked && vm.power_state !== "poweredOff") continue;
        if (q && !`${vm.name} ${vm.folder} ${vm.guest_full_name}`.toLowerCase().includes(q)) continue;
        shown++;
        const job = state.jobsByVm[vm.moid];
        const off = vm.power_state === "poweredOff";
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
            : el("a", { href: `#/export/${vm.moid}`, class: "button primary small" + (off ? "" : " disabled"), title: off ? "" : "Power off the VM first" }, "Migrate"))));
      }
      count.textContent = `${shown} of ${vms.length} virtual machines`;
    };

    const load = async (refresh) => {
      err.textContent = ""; count.textContent = "Loading inventory...";
      try {
        const [list, jobs] = await Promise.all([api("GET", "/vms" + (refresh ? "?refresh=true" : "")), api("GET", "/jobs")]);
        vms = list;
        state.jobsByVm = {};
        for (const j of jobs) if (!state.jobsByVm[j.vm.moid]) state.jobsByVm[j.vm.moid] = j; // jobs are newest first
        render();
      } catch (e) { if (e.status !== 401) err.textContent = e.message; count.textContent = ""; }
    };
    filter.addEventListener("input", render);
    offOnly.addEventListener("change", render);
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
      ["Network", vm.nics.map((n) => `${n.label}: ${n.adapter_type}`).join("; ") || "-"],
    ]);
    const problems = document.getElementById("vm-problems");
    for (const p of inspection.problems) problems.append(el("li", {}, p));
    const warnings = document.getElementById("vm-warnings");
    for (const w of inspection.warnings) warnings.append(el("li", {}, w));

    // populate the target form
    const sel = (name) => form.elements[name];
    for (const c of options.compartments) sel("compartment_id").append(el("option", { value: c.id }, c.path || c.name));
    for (const ad of options.availability_domains) sel("availability_domain").append(el("option", { value: ad }, ad + (ad === options.helper_availability_domain ? " (helper)" : "")));
    sel("availability_domain").value = options.helper_availability_domain;
    // VCN -> subnet: the subnet list is filtered by the selected VCN
    let netOptions = options;
    const fillSubnets = () => {
      const vcnId = sel("vcn_id").value;
      const subnets = netOptions.subnets.filter((s) => s.vcn_id === vcnId);
      sel("subnet_id").innerHTML = "";
      for (const s of subnets) sel("subnet_id").append(el("option", { value: s.id }, `${s.name} (${s.cidr_block})${s.prohibit_public_ip ? ", private" : ""}${s.availability_domain ? ", " + s.availability_domain : ""}`));
      document.getElementById("subnet-hint").textContent = subnets.length ? "" : (vcnId ? "No subnets in this VCN within the selected compartment." : "Select a VCN first.");
    };
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
    sel("shape").append(el("option", { value: "" }, `${options.default_shape} (default)`));
    for (const s of options.shapes) if (s.name !== options.default_shape) sel("shape").append(el("option", { value: s.name }, s.name));
    document.getElementById("shape-hint").textContent = `Sized to ${Math.max(1, Math.ceil(vm.num_cpu / 2))} OCPU / ${Math.max(1, Math.ceil(vm.memory_mb / 1024))} GB from the source VM.`;
    sel("display_name").value = vm.name;
    const isWin = isWindows(vm);
    document.getElementById("windows-fieldset").hidden = !isWin;
    document.getElementById("esxi-host-hint").textContent = vm.host_name ? `(${vm.host_name})` : "";
    sel("nfc_direct_to_esxi").disabled = !vm.host_name;
    if (options.compartments.some((c) => c.id === options.helper_compartment_id)) sel("compartment_id").value = options.helper_compartment_id;
    else if (options.compartments.length) sel("compartment_id").selectedIndex = 0;
    sel("compartment_id").addEventListener("change", async () => {
      formError.textContent = "";
      try { fillNetworks(await api("GET", `/oci/options?compartment_id=${encodeURIComponent(sel("compartment_id").value)}`)); }
      catch (e) { formError.textContent = e.message; }
    });

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

    // resume display of an active job for this VM, if any
    const jobCard = document.getElementById("job-card"); const jobView = document.getElementById("job-view");
    try {
      const jobs = await api("GET", `/jobs?vm_moid=${encodeURIComponent(moid)}`);
      const active = jobs.find((j) => !TERMINAL.includes(j.phase));
      if (active) { jobCard.hidden = false; submit.disabled = true; activePoll = pollJob(active.id, jobView, { onTerminal: () => { submit.disabled = !inspection.can_export; } }); }
    } catch (_) { /* ignore */ }

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      formError.textContent = "";
      const fd = new FormData(form);
      const target = {
        compartment_id: fd.get("compartment_id"),
        availability_domain: fd.get("availability_domain"),
        subnet_id: fd.get("subnet_id"),
        shape: fd.get("shape") || null,
        display_name: fd.get("display_name") || null,
        assign_public_ip: fd.get("assign_public_ip") === "on",
        start_after_migration: fd.get("start_after_migration") === "on",
        windows_license_type: isWin ? fd.get("windows_license_type") : null,
        compatibility_mode: fd.get("compatibility_mode") === "on",
        boot_volume_type_override: fd.get("boot_volume_type_override") || null,
        network_type_override: fd.get("network_type_override") || null,
        nfc_direct_to_esxi: fd.get("nfc_direct_to_esxi") === "on",
        pipelined_decode: fd.get("pipelined_decode") === "on",
        volume_vpus_per_gb: Number(fd.get("volume_vpus_per_gb") || 10),
      };
      if (target.availability_domain !== options.helper_availability_domain) {
        formError.textContent = "The availability domain must match the helper VM's AD."; return;
      }
      submit.disabled = true;
      try {
        const job = await api("POST", "/jobs", { vm_moid: moid, target });
        jobCard.hidden = false;
        stopPolling();
        activePoll = pollJob(job.id, jobView, { onTerminal: () => { submit.disabled = !inspection.can_export; } });
      } catch (e) { formError.textContent = e.message; submit.disabled = false; }
    });
  }

  // ---------------------------------------------------------------- jobs view
  async function jobsView() {
    let jobs = [];
    try { jobs = await api("GET", "/jobs"); } catch (e) { if (e.status !== 401) showError(e.message); return; }
    app.innerHTML = "";
    const table = el("table", { class: "jobs" },
      el("thead", {}, el("tr", {}, ...["VM", "Phase", "Message", "OCI instance", "Started", "By", ""].map((h) => el("th", {}, h)))),
      el("tbody", {}, ...jobs.map((j) => el("tr", {},
        el("td", { class: "name" }, j.vm.name), el("td", {}, el("span", { class: "phase " + j.phase }, j.phase)),
        el("td", {}, (j.message || "") + (j.phase === "EXPORTING" && j.transfer && j.transfer.started_at
          ? ` - ${j.transfer.percent || 0}%${j.transfer.throughput_bps ? ", " + fmtRate(j.transfer.throughput_bps) : ""}`
          : !TERMINAL.includes(j.phase) && j.step_percent !== null && j.step_percent !== undefined && !/\d+%/.test(j.message || "")
            ? ` - ${j.step_percent}%` : "")),
        el("td", { class: "ocid" }, j.instance_id || "-"),
        el("td", {}, new Date(j.created_at).toLocaleString()), el("td", {}, j.created_by || "-"),
        el("td", {}, el("a", { class: "button secondary small", href: `#/jobs/${j.id}` }, "Details"))))));
    app.append(el("div", { class: "card" }, el("h2", {}, "Migration jobs"),
      jobs.length ? table : el("div", { class: "muted" }, "No jobs yet. Pick a powered-off VM under Virtual machines to start one.")));
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

    document.getElementById("seed-cleanup").addEventListener("click", async (ev) => {
      const out = document.getElementById("seed-result");
      if (!confirm("Delete all seed images and their staging objects?")) return;
      ev.target.disabled = true; out.textContent = "Deleting...";
      try { const r = await api("DELETE", "/seed-images"); out.textContent = `Deleted ${r.deleted.length} object(s).`; }
      catch (e) { out.textContent = e.message; } finally { ev.target.disabled = false; }
    });

    try {
      const [info, lg] = await Promise.all([api("GET", "/setup/info"), api("GET", "/setup/logging")]);
      renderLogging(lg);
      kv(document.getElementById("setup-kv"), [
        ["Version", info.version + (info.commit ? ` (${short(info.commit)})` : "")],
        ["Region / AD", `${info.region} / ${info.availability_domain}`],
        ["Instance", info.instance_id], ["Compartment", info.compartment_id],
        ["Default vCenter", info.default_vcenter || "(none)"], ["Verify vCenter TLS", info.vcenter_verify_ssl ? "yes" : "no"],
        ["Seed image bucket", info.seed_bucket], ["Default shape", info.default_shape],
        ["Concurrent migrations", String(info.max_concurrent_jobs)], ["Session idle timeout", `${Math.round(info.session_ttl_s / 3600)} h`],
        ["Logged-in sessions", String(info.sessions)], ["Running migrations", String(info.active_jobs)],
      ]);
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
    const hash = location.hash || "#/vms";
    for (const a of nav.querySelectorAll("a")) a.classList.toggle("active", hash.startsWith(a.getAttribute("href")));
    let m;
    if ((m = /^#\/export\/(.+)$/.exec(hash))) return exportView(decodeURIComponent(m[1]));
    if ((m = /^#\/jobs\/(.+)$/.exec(hash))) return jobDetailView(decodeURIComponent(m[1]));
    if (hash === "#/jobs") return jobsView();
    if (hash === "#/setup") return setupView();
    return vmsView();
  }

  window.addEventListener("hashchange", () => { route().catch((e) => showError(e.message)); });
  route().catch((e) => showError(e.message));
})();
