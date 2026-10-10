// "Need help" tab: tell the team how the app feels to use (a confusing screen, a slow step, something you could
// not find, voice or language not understood), then follow what happened to it. Three sub-tabs: Report an issue,
// My requests, Team view (static/help_subtabs.js shows them). Technical errors are NOT reported here (the app
// records those itself), and the form says so first.
//
// A request is a FEEDBACK RECORD (clinic/help_requests.py): nothing here proposes, saves or changes clinic data.
// The username shown is always the server's (clinic/help_requests.py current_user), never typed in.
//
// Dictation: the mic button in the corner of the description box uses window.Dictation (static/live_voice.js): click to start, click to stop, the
// recognised text is appended to the description box and can be edited. It is never a voice command.
//
// Everything from the server is put in with textContent, never as HTML. The pure helpers at the top are exported
// for tests/help.test.js (no DOM, no network). The draft is kept in localStorage (always in try/catch: the page
// works without it) and cleared after a successful send.
(function () {
  var STATUS_LABELS = { "new": "New", acknowledged: "Acknowledged", in_progress: "In progress", resolved: "Resolved", closed: "Closed" };
  var SLA_LABELS = { on_track: "On track", due_soon: "Due soon", overdue: "Overdue", met: "Met", breached: "Breached" };
  var SOURCE_LABELS = { typed: "Typed", voice: "Dictated", mixed: "Typed and dictated" };
  var ACTOR_LABELS = { user: "You", team: "Team", "import": "Update from the developer" };
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var DRAFT_KEY = "clinic.helpDraft";
  var DEFAULT_LIMITS = {
    max_files: 5, max_file_bytes: 10 * 1024 * 1024, max_total_bytes: 25 * 1024 * 1024,
    image_extensions: ["png", "jpg", "jpeg", "webp", "gif", "heic"], document_extensions: ["pdf", "txt", "csv", "docx", "xlsx"],
    min_description: 10, max_description: 4000,
  };

  // ---- pure helpers ------------------------------------------------------------------------------

  function fileExtension(name) {
    var text = String(name || "");
    text = text.slice(Math.max(text.lastIndexOf("/"), text.lastIndexOf("\\")) + 1);
    var dot = text.lastIndexOf(".");
    return dot >= 0 ? text.slice(dot + 1).toLowerCase() : "";
  }

  function formatBytes(bytes) {
    var n = Number(bytes) || 0;
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(n < 10240 ? 1 : 0) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }

  function allowedExtensions(limits) {
    limits = limits || DEFAULT_LIMITS;
    return (limits.image_extensions || []).concat(limits.document_extensions || []);
  }

  // Which of the files `incoming` can be added to the `current` list, mirroring the server's limits (the server stays
  // the authority): type, not empty, size, how many, total size, not the same file twice. {accepted, rejected}.
  function validateFiles(current, incoming, limits) {
    limits = limits || DEFAULT_LIMITS;
    var allowed = allowedExtensions(limits);
    var accepted = [], rejected = [];
    var count = current.length;
    var total = current.reduce(function (sum, f) { return sum + (f.size || 0); }, 0);
    var seen = {};
    current.forEach(function (f) { seen[f.name + "|" + f.size] = true; });
    Array.prototype.forEach.call(incoming || [], function (file) {
      var reason = null;
      if (allowed.indexOf(fileExtension(file.name)) < 0) reason = "this type of file cannot be attached";
      else if (!file.size) reason = "the file is empty";
      else if (file.size > limits.max_file_bytes) reason = "it is larger than " + formatBytes(limits.max_file_bytes);
      else if (seen[file.name + "|" + file.size]) reason = "it is already added";
      else if (count >= limits.max_files) reason = "at most " + limits.max_files + " files can be attached";
      else if (total + file.size > limits.max_total_bytes) reason = "the files together would be larger than " + formatBytes(limits.max_total_bytes);
      if (reason) { rejected.push({ name: file.name, reason: reason }); return; }
      accepted.push(file);
      seen[file.name + "|" + file.size] = true;
      count += 1;
      total += file.size;
    });
    return { accepted: accepted, rejected: rejected };
  }

  // "2026-10-11 12:15:32" (the clinic's local time) -> "11 Oct 2026, 12:15"; anything else is shown as it came.
  function formatStamp(stamp) {
    var m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(String(stamp || ""));
    if (!m) return stamp ? String(stamp) : "-";
    return parseInt(m[3], 10) + " " + MONTHS[parseInt(m[2], 10) - 1] + " " + m[1] + ", " + m[4] + ":" + m[5];
  }

  function durationText(totalSeconds) {
    var minutes = Math.floor(Math.abs(Number(totalSeconds) || 0) / 60);
    var days = Math.floor(minutes / 1440), hours = Math.floor((minutes % 1440) / 60), mins = minutes % 60;
    if (days > 0) return days + " d " + hours + " h";
    if (hours > 0) return hours + " h " + mins + " min";
    if (minutes > 0) return minutes + " min";
    return "less than a minute";
  }

  // "23 h 10 min left" / "overdue by 2 h 5 min" for an open request (seconds left, negative when past); "" otherwise.
  function timeLeftText(seconds) {
    if (seconds === null || seconds === undefined) return "";
    return seconds < 0 ? "overdue by " + durationText(seconds) : durationText(seconds) + " left";
  }

  function slaLabel(state) { return SLA_LABELS[state] || String(state || ""); }
  function statusLabel(status) { return STATUS_LABELS[status] || String(status || ""); }

  // The one-line SLA text of a request: "Due soon, 3 h 10 min left" or "Met" / "Breached".
  function slaLine(item) {
    var label = slaLabel(item.sla_state);
    var left = timeLeftText(item.seconds_left);
    return left ? label + ", " + left : label;
  }

  // "24 hours, due 11 Oct 2026, 12:15"
  function slaDueText(hours, dueAt) {
    var h = Number(hours) || 0;
    return h + (h === 1 ? " hour" : " hours") + ", due " + formatStamp(dueAt);
  }

  function counterText(length, max) { return length + " / " + max; }

  // What to tell the person when dictation fails. Only our own plain messages (microphone, voice off, connection) are
  // shown as they are; anything else (a speech-service detail) becomes one friendly line, never the raw text.
  var FRIENDLY_DICTATION_ERROR = /microphone|voice is off|not connected|lost the connection|browser cannot|reload the page/i;
  function dictationErrorText(message) {
    var text = String(message || "").trim();
    if (text && FRIENDLY_DICTATION_ERROR.test(text) && text.length <= 160) return text + " You can keep typing.";
    return "Dictation stopped. You can keep typing.";
  }

  // Can this go? { ok, reason }. Words are what is left after the hidden-number placeholder; with a file attached a
  // few words are enough (the server checks again).
  function canSubmit(description, fileCount, limits) {
    limits = limits || DEFAULT_LIMITS;
    var text = String(description || "").replace(/\[number hidden\]/g, "").trim();
    if (!text) return { ok: false, reason: "Describe the problem in a few words." };
    if (String(description).length > limits.max_description) return { ok: false, reason: "Keep the description under " + limits.max_description + " characters." };
    if (!fileCount && text.length < limits.min_description) {
      return { ok: false, reason: "Please write at least " + limits.min_description + " characters, or attach a screenshot." };
    }
    return { ok: true, reason: "" };
  }

  // typed / voice / mixed from what the person did in the box.
  function sourceFor(flags) {
    var typed = !!(flags && flags.typed), dictated = !!(flags && flags.dictated);
    if (typed && dictated) return "mixed";
    return dictated ? "voice" : "typed";
  }

  // Dictated text added to what is already in the box: one space (or none after a new line), cut at the limit.
  function appendDictation(existing, addition, max) {
    var base = String(existing || ""), add = String(addition || "").trim();
    if (!add) return base;
    var joined = !base || /\s$/.test(base) ? base + add : base + " " + add;
    return max ? joined.slice(0, max) : joined;
  }

  // The draft kept between visits: only the three form fields, never a file.
  function draftToText(draft) {
    return JSON.stringify({ v: 1, category: String(draft.category || ""), severity: String(draft.severity || ""), description: String(draft.description || "") });
  }
  function draftFromText(text, max) {
    try {
      var d = JSON.parse(text || "null");
      if (!d || typeof d !== "object" || Array.isArray(d) || d.v !== 1) return null;
      var description = typeof d.description === "string" ? d.description.slice(0, max || 4000) : "";
      var draft = { category: typeof d.category === "string" ? d.category : "", severity: typeof d.severity === "string" ? d.severity : "", description: description };
      return draft.description || draft.category || draft.severity ? draft : null;
    } catch (e) { return null; }
  }

  // A line of a request's history, in the person's words.
  function historyText(event) {
    var who = ACTOR_LABELS[event.actor] || event.actor;
    var line;
    if (event.kind === "created") line = "Request sent";
    else if (event.kind === "status") line = "Status changed from " + statusLabel(event.from_status) + " to " + statusLabel(event.to_status);
    else if (event.kind === "note") line = "Note added";
    else if (event.kind === "exported") line = "Exported by the team";
    else line = String(event.kind || "");
    return formatStamp(event.at) + " — " + who + ": " + line + (event.note ? ". " + event.note : "");
  }

  // "12 requests: 3 new, ..." for the team view's summary line.
  function summaryText(summary) {
    if (!summary || !summary.total) return "No requests yet.";
    var parts = Object.keys(STATUS_LABELS).filter(function (s) { return summary.by_status[s]; })
      .map(function (s) { return summary.by_status[s] + " " + STATUS_LABELS[s].toLowerCase(); });
    var sla = ["overdue", "due_soon", "breached"].filter(function (s) { return summary.by_sla[s]; })
      .map(function (s) { return summary.by_sla[s] + " " + SLA_LABELS[s].toLowerCase(); });
    var text = summary.total + (summary.total === 1 ? " request: " : " requests: ") + parts.join(", ") + ".";
    if (sla.length) text += " SLA: " + sla.join(", ") + ".";
    return text;
  }

  // What the import said, as plain lines.
  function importSummaryLines(result) {
    var lines = [];
    lines.push("Read " + result.rows + (result.rows === 1 ? " row" : " rows") + ": " + result.updated + " updated, " + result.notes_added + " notes added, " + result.unchanged + " already up to date.");
    if (result.stale) lines.push(result.stale + " skipped because the request had already moved on (an older file?).");
    if (result.unknown_tickets && result.unknown_tickets.length) lines.push("Not found in this app: " + result.unknown_tickets.join(", ") + ".");
    (result.invalid || []).forEach(function (row) {
      lines.push("Row " + row.row + (row.ticket_no ? " (" + row.ticket_no + ")" : "") + " not used: " + row.reason + ".");
    });
    return lines;
  }

  var api = {
    fileExtension: fileExtension, formatBytes: formatBytes, validateFiles: validateFiles, formatStamp: formatStamp,
    durationText: durationText, timeLeftText: timeLeftText, slaLabel: slaLabel, statusLabel: statusLabel, slaLine: slaLine,
    slaDueText: slaDueText, counterText: counterText, dictationErrorText: dictationErrorText, canSubmit: canSubmit, sourceFor: sourceFor, appendDictation: appendDictation,
    draftToText: draftToText, draftFromText: draftFromText, historyText: historyText, summaryText: summaryText,
    importSummaryLines: importSummaryLines, DRAFT_KEY: DRAFT_KEY, DEFAULT_LIMITS: DEFAULT_LIMITS,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof window !== "undefined") window.HelpUi = api;
  if (typeof document === "undefined" || !document.addEventListener) return;

  // ---- the page ----------------------------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", function () {
    var reportRoot = document.getElementById("help-report");
    var mineRoot = document.getElementById("help-mine");
    var teamRoot = document.getElementById("help-team");
    if (!reportRoot || !mineRoot || !teamRoot) return;

    // The microphone glyph, drawn as SVG nodes (no HTML strings).
    function micIcon() {
      var ns = "http://www.w3.org/2000/svg";
      var svg = document.createElementNS(ns, "svg");
      svg.setAttribute("viewBox", "0 0 24 24");
      svg.setAttribute("width", "18");
      svg.setAttribute("height", "18");
      svg.setAttribute("fill", "none");
      svg.setAttribute("stroke", "currentColor");
      svg.setAttribute("stroke-width", "2");
      svg.setAttribute("stroke-linecap", "round");
      svg.setAttribute("stroke-linejoin", "round");
      svg.setAttribute("aria-hidden", "true");
      [["rect", { x: "9", y: "3", width: "6", height: "11", rx: "3" }],
       ["path", { d: "M5 11a7 7 0 0 0 14 0" }],
       ["line", { x1: "12", y1: "18", x2: "12", y2: "21" }]].forEach(function (part) {
        var node = document.createElementNS(ns, part[0]);
        Object.keys(part[1]).forEach(function (k) { node.setAttribute(k, part[1][k]); });
        svg.appendChild(node);
      });
      return svg;
    }
    function el(tag, cls, text) {
      var node = document.createElement(tag);
      if (cls) node.className = cls;
      if (text !== undefined && text !== null) node.textContent = text;
      return node;
    }
    function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
    function getJson(url) {
      return fetch(url).then(function (r) { return r.json(); });
    }
    function postJson(url, body) {
      return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
        .then(function (r) { return r.json(); });
    }
    // A response that is not JSON (a proxy's error page) must not look like a crash.
    function readJson(response) {
      return response.json().catch(function () { return { ok: false, error: "The server sent an answer the page could not read (" + response.status + ")." }; });
    }
    function chip(kind, key, text) {
      return el("span", "help-chip help-" + kind + " help-" + kind + "-" + key, text);
    }
    function kvRow(list, label, value) {
      list.appendChild(el("dt", null, label));
      var dd = el("dd");
      if (value && value.nodeType) dd.appendChild(value); else dd.textContent = value;
      list.appendChild(dd);
    }
    function storage(action, value) {
      try {
        if (!window.localStorage) return null;
        if (action === "get") return window.localStorage.getItem(DRAFT_KEY);
        if (action === "set") window.localStorage.setItem(DRAFT_KEY, value);
        if (action === "remove") window.localStorage.removeItem(DRAFT_KEY);
      } catch (e) { /* the page works without it */ }
      return null;
    }

    var config = null;
    var limits = DEFAULT_LIMITS;
    var previousTab = "";
    var currentTab = "";
    var currentSub = "report";
    // The open sub-tab: asked of static/help_subtabs.js when it is there (it may have restored one before this
    // script was listening), else the last one announced.
    function sub() {
      return (window.HelpSubtabs && window.HelpSubtabs.current && window.HelpSubtabs.current()) || currentSub;
    }

    // ================= Report an issue =================
    var form = null;           // the live form's parts, or null while the confirmation is shown

    function loadConfig() {
      return getJson("/help/config").then(function (r) {
        if (!r.ok) throw new Error(r.error || "failed");
        config = r.data;
        limits = Object.assign({}, DEFAULT_LIMITS, config.limits || {});
        drawReport();
      }).catch(function () {
        clear(reportRoot);
        var card = el("div", "card");
        card.appendChild(el("p", "flash error", "Could not load the form. Check the connection and try again."));
        var retry = el("button", "btn btn-confirm", "Try again");
        retry.type = "button";
        retry.addEventListener("click", loadConfig);
        card.appendChild(retry);
        reportRoot.appendChild(card);
      });
    }

    function drawReport() {
      clear(reportRoot);
      if (!config) { loadConfig(); return; }
      var state = { files: [], typed: false, dictated: false, submitting: false };

      var intro = el("div", "card help-disclaimer");
      intro.appendChild(el("h2", null, "Before you write"));
      intro.appendChild(el("p", null, config.disclaimer));
      reportRoot.appendChild(intro);

      var card = el("div", "card");
      card.appendChild(el("h2", null, "Report an issue"));
      var fields = el("form", "help-form");
      fields.autocomplete = "off";
      fields.noValidate = true;

      function field(label, control, hint) {
        var wrap = el("label", "help-field");
        wrap.appendChild(el("span", "help-label", label));
        wrap.appendChild(control);
        if (hint) wrap.appendChild(el("span", "muted help-hint", hint));
        return wrap;
      }
      var category = el("select");
      category.id = "help-category";
      var blank = el("option", null, "Choose...");
      blank.value = "";
      category.appendChild(blank);
      config.categories.forEach(function (c) {
        var o = el("option", null, c.label);
        o.value = c.key;
        category.appendChild(o);
      });
      var severity = el("select");
      severity.id = "help-severity";
      config.severities.forEach(function (s) {
        var o = el("option", null, s.label);
        o.value = s.key;
        severity.appendChild(o);
      });
      severity.value = config.default_severity || "minor";
      var grid = el("div", "help-grid");
      grid.appendChild(field("What is it about?", category));
      grid.appendChild(field("How much does it slow you down?", severity));
      fields.appendChild(grid);

      var description = el("textarea");
      description.id = "help-description";
      description.rows = 6;
      description.maxLength = limits.max_description;
      description.placeholder = "For example: I could not find where to add a new patient, it took me a minute.";
      var counter = el("span", "muted help-counter", counterText(0, limits.max_description));
      var descField = el("div", "help-field");
      var descLabel = el("label", "help-label", "What happened?");
      descLabel.htmlFor = "help-description";
      descField.appendChild(descLabel);
      // The mic and the character count sit inside the box, bottom left.
      var textbox = el("div", "help-textbox");
      textbox.appendChild(description);
      var bar = el("div", "help-textbox-bar");
      var dictate = el("button", "help-mic");
      dictate.type = "button";
      dictate.id = "help-dictate";
      dictate.title = "Dictate";
      dictate.setAttribute("aria-label", "Dictate");
      dictate.setAttribute("aria-pressed", "false");
      dictate.appendChild(micIcon());
      bar.appendChild(dictate);
      bar.appendChild(counter);
      textbox.appendChild(bar);
      descField.appendChild(textbox);
      var dictateStatus = el("div", "muted help-dictate-status");
      dictateStatus.setAttribute("role", "status");
      dictateStatus.setAttribute("aria-live", "polite");
      descField.appendChild(dictateStatus);
      var partial = el("div", "muted help-partial");
      descField.appendChild(partial);
      fields.appendChild(descField);

      // ---- attachments
      var filesBox = el("div", "help-field");
      filesBox.appendChild(el("span", "help-label", "Screenshots or documents (optional)"));
      var drop = el("div", "help-drop");
      drop.tabIndex = 0;
      drop.setAttribute("role", "button");
      drop.setAttribute("aria-label", "Choose files to attach, or drop them here");
      drop.appendChild(el("strong", null, "Drop files here, or choose files"));
      drop.appendChild(el("span", "muted", "Up to " + limits.max_files + " files, " + formatBytes(limits.max_file_bytes) + " each, " + formatBytes(limits.max_total_bytes) +
        " in all. Images (" + limits.image_extensions.join(", ") + ") and documents (" + limits.document_extensions.join(", ") + "). No videos."));
      var picker = el("input");
      picker.type = "file";
      picker.multiple = true;
      picker.hidden = true;
      picker.accept = allowedExtensions(limits).map(function (e) { return "." + e; }).join(",");
      var fileList = el("ul", "help-files");
      var fileProblems = el("div", "field-note field-error");
      fileProblems.hidden = true;
      filesBox.appendChild(drop);
      filesBox.appendChild(picker);
      filesBox.appendChild(fileProblems);
      filesBox.appendChild(fileList);
      fields.appendChild(filesBox);

      var error = el("div", "field-note field-error");
      error.hidden = true;
      error.setAttribute("role", "alert");
      var actions = el("div", "appt-form-actions");
      var submit = el("button", "btn-confirm", "Send request");
      submit.type = "submit";
      submit.id = "help-submit";
      actions.appendChild(submit);
      fields.appendChild(error);
      fields.appendChild(actions);
      card.appendChild(fields);
      reportRoot.appendChild(card);

      form = { dictate: dictate, description: description, category: category, severity: severity, state: state };

      function showError(text) { error.hidden = !text; error.textContent = text || ""; }
      function updateCounter() { counter.textContent = counterText(description.value.length, limits.max_description); }

      function drawFiles() {
        clear(fileList);
        state.files.forEach(function (file, index) {
          var li = el("li", "help-file");
          li.appendChild(el("span", "help-file-name", file.name));
          li.appendChild(el("span", "muted", formatBytes(file.size)));
          var remove = el("button", "btn-link", "Remove");
          remove.type = "button";
          remove.setAttribute("aria-label", "Remove " + file.name);
          remove.addEventListener("click", function () { state.files.splice(index, 1); drawFiles(); });
          li.appendChild(remove);
          fileList.appendChild(li);
        });
      }
      function addFiles(incoming) {
        var result = validateFiles(state.files, incoming, limits);
        state.files = state.files.concat(result.accepted);
        drawFiles();
        fileProblems.hidden = !result.rejected.length;
        fileProblems.textContent = result.rejected.map(function (r) { return "'" + r.name + "' was not added: " + r.reason + "."; }).join(" ");
      }
      drop.addEventListener("click", function () { picker.click(); });
      drop.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); picker.click(); }
      });
      picker.addEventListener("change", function () { addFiles(picker.files); picker.value = ""; });
      ["dragenter", "dragover"].forEach(function (name) {
        drop.addEventListener(name, function (event) { event.preventDefault(); drop.classList.add("dragging"); });
      });
      ["dragleave", "drop"].forEach(function (name) {
        drop.addEventListener(name, function (event) { event.preventDefault(); drop.classList.remove("dragging"); });
      });
      drop.addEventListener("drop", function (event) {
        if (event.dataTransfer && event.dataTransfer.files) addFiles(event.dataTransfer.files);
      });

      // ---- draft
      var draftTimer = null;
      function saveDraft() {
        clearTimeout(draftTimer);
        draftTimer = setTimeout(function () {
          var d = { category: category.value, severity: severity.value, description: description.value };
          if (!d.description && !d.category && d.severity === (config.default_severity || "minor")) storage("remove");
          else storage("set", draftToText(d));
        }, 400);
      }
      var saved = draftFromText(storage("get"), limits.max_description);
      if (saved) {
        description.value = saved.description;
        if (saved.category && config.categories.some(function (c) { return c.key === saved.category; })) category.value = saved.category;
        if (saved.severity && config.severities.some(function (s) { return s.key === saved.severity; })) severity.value = saved.severity;
        state.typed = !!saved.description;      // a restored draft was typed (or at least edited) by the person
        updateCounter();
      }
      description.addEventListener("input", function () { state.typed = true; updateCounter(); saveDraft(); showError(""); });
      category.addEventListener("change", function () { saveDraft(); showError(""); });
      severity.addEventListener("change", saveDraft);

      // ---- dictation
      var stopRequested = false;           // the person clicked the mic to stop: nothing technical is shown after that
      function dictationState(name) {
        var live = name === "listening" || name === "starting";
        dictate.disabled = name === "finishing";
        dictate.classList.toggle("is-listening", live);
        dictate.title = live ? "Stop dictating" : "Dictate";
        dictate.setAttribute("aria-label", live ? "Stop dictating" : "Dictate");
        dictate.setAttribute("aria-pressed", live ? "true" : "false");
        if (name === "listening") dictateStatus.textContent = "Listening... click the mic again when you are done.";
        else if (name === "starting") dictateStatus.textContent = "Opening the microphone...";
        else if (name === "finishing") dictateStatus.textContent = "Finishing...";
        else { dictateStatus.textContent = ""; dictateStatus.classList.remove("help-dictate-error"); }
        if (name === "idle") partial.textContent = "";
      }
      // A note after dictation ended (the state change to idle has already cleared the line).
      function dictationNote(text, isError) {
        dictateStatus.textContent = text;
        dictateStatus.classList.toggle("help-dictate-error", !!isError);
      }
      dictate.addEventListener("click", function () {
        showError("");
        if (!window.Dictation) { dictationNote("Dictation is not available on this page. You can type instead.", true); return; }
        if (window.Dictation.active()) { stopRequested = true; window.Dictation.stop(); return; }
        if (!window.Dictation.supported()) {
          dictationNote("This browser cannot use the microphone here (it needs https or localhost). You can type instead.", true);
          return;
        }
        stopRequested = false;
        dictateStatus.classList.remove("help-dictate-error");
        window.Dictation.start({
          onState: dictationState,
          onPartial: function (text) { partial.textContent = text; },
          onText: function (text) {
            partial.textContent = "";
            description.value = appendDictation(description.value, text, limits.max_description);
            state.dictated = true;
            updateCounter();
            saveDraft();
          },
          // Once the person has stopped, a late error is not their concern (the text they dictated is in the box).
          onError: function (message) { if (!stopRequested) dictationNote(dictationErrorText(message), true); },
          onEnd: function (info) {
            if (!info.heard && info.reason === "stopped") dictationNote("Did not catch anything. Try again, or type it.", false);
            else if (info.reason === "timeout") dictationNote("Dictation stopped after 2 minutes.", false);
            else if (info.reason === "cancelled" && !stopRequested) dictationNote("Dictation stopped.", false);
          },
        });
      });

      // ---- send
      fields.addEventListener("submit", function (event) {
        event.preventDefault();
        if (state.submitting) return;                     // a second click while sending does nothing
        if (window.Dictation && window.Dictation.active()) window.Dictation.cancel();
        if (!category.value) { showError("Choose what this is about from the list."); category.focus(); return; }
        var check = canSubmit(description.value, state.files.length, limits);
        if (!check.ok) { showError(check.reason); description.focus(); return; }
        state.submitting = true;
        submit.disabled = true;
        submit.textContent = "Sending...";
        showError("");
        var body = new FormData();
        body.append("category", category.value);
        body.append("severity", severity.value);
        body.append("description", description.value);
        body.append("source", sourceFor(state));
        var branch = window.Branches && window.Branches.view ? window.Branches.view() : null;
        body.append("context", JSON.stringify({
          tab: "help", screen: previousTab || "", language: navigator.language || "",
          branch: (typeof branch === "number" || branch === "all") ? String(branch) : "",
        }));
        state.files.forEach(function (file) { body.append("files", file, file.name); });
        fetch("/help/requests", { method: "POST", body: body }).then(readJson).then(function (r) {
          if (!r.ok) {
            state.submitting = false;
            submit.disabled = false;
            submit.textContent = "Send request";
            showError(r.error || "Could not send the request.");
            return;
          }
          storage("remove");
          clearTimeout(draftTimer);
          form = null;
          drawConfirmation(r.data);
          if (sub() === "mine") loadMine();
        }).catch(function () {
          state.submitting = false;
          submit.disabled = false;
          submit.textContent = "Send request";
          showError("Could not reach the server. Your text is still here: try again.");
        });
      });

      dictationState("idle");
      var support = window.Dictation && window.Dictation.supported ? window.Dictation.supported() : false;
      if (!support) {
        dictate.title = "The microphone needs https or localhost";
        dictate.classList.add("help-dictate-off");
      }
      drawFiles();
    }

    function drawConfirmation(data) {
      clear(reportRoot);
      var card = el("div", "card help-confirmation");
      card.setAttribute("role", "status");
      card.appendChild(el("h2", null, "Request sent"));
      card.appendChild(el("p", null, "Thank you. The team will look at it. You can follow it under My requests."));
      var list = el("dl", "help-kv");
      kvRow(list, "Ticket", data.ticket_no);
      kvRow(list, "Category", data.category_label);
      kvRow(list, "Reported", formatStamp(data.created_at));
      kvRow(list, "SLA", slaDueText(data.sla_hours, data.sla_due_at) + " (" + slaLabel(data.sla_state) + ")");
      kvRow(list, "Logged in as", data.username);
      kvRow(list, "Status", statusLabel(data.status));
      var names = (data.attachments || []).map(function (a) { return a.original_name; });
      kvRow(list, "Attachments", names.length ? names.join(", ") : "None");
      card.appendChild(list);
      if (data.masked) {
        card.appendChild(el("p", "field-note", "Some long numbers in your message were hidden for privacy and saved as [number hidden]."));
      }
      var again = el("button", "btn btn-confirm", "Send another request");
      again.type = "button";
      again.addEventListener("click", drawReport);
      card.appendChild(again);
      reportRoot.appendChild(card);
    }

    // ================= shared: a request's detail =================
    function attachmentLinks(item, base) {
      var wrap = el("div", "help-attach");
      (item.attachments || []).forEach(function (a) {
        var link = el("a", "help-attach-link", a.original_name + " (" + formatBytes(a.bytes) + ")");
        link.href = base + a.id;
        link.setAttribute("download", "");
        link.rel = "noopener";
        wrap.appendChild(link);
      });
      return wrap;
    }

    function detailBody(item, attachmentBase) {
      var box = el("div", "help-detail");
      box.appendChild(el("p", "help-description", item.description));
      var list = el("dl", "help-kv");
      kvRow(list, "How much it slows you down", item.severity_label);
      kvRow(list, "How it was written", SOURCE_LABELS[item.source] || item.source);
      kvRow(list, "SLA", slaDueText(item.sla_hours, item.sla_due_at) + ", " + slaLine(item).toLowerCase());
      if (item.resolved_at) kvRow(list, "Resolved", formatStamp(item.resolved_at));
      if (item.attachments && item.attachments.length) kvRow(list, "Attachments", attachmentLinks(item, attachmentBase));
      box.appendChild(list);
      var history = el("ul", "help-history");
      (item.events || []).forEach(function (event) { history.appendChild(el("li", null, historyText(event))); });
      box.appendChild(el("h3", null, "History"));
      box.appendChild(history);
      return box;
    }

    // ================= My requests =================
    var mineOpen = {};         // id -> true while a request's details are open

    function loadMine() {
      return getJson("/help/requests").then(function (r) {
        if (!r.ok) throw new Error("failed");
        drawMine(r.requests);
      }).catch(function () {
        clear(mineRoot);
        mineRoot.appendChild(el("div", "card")).appendChild(el("p", "flash error", "Could not load your requests."));
      });
    }

    function drawMine(items) {
      clear(mineRoot);
      var card = el("div", "card");
      var head = el("div", "help-head");
      head.appendChild(el("h2", null, "My requests"));
      var refresh = el("button", "btn", "Refresh");
      refresh.type = "button";
      refresh.addEventListener("click", loadMine);
      head.appendChild(refresh);
      card.appendChild(head);
      if (!items.length) card.appendChild(el("p", "muted", "You have not sent a request yet. Use Report an issue."));
      var list = el("div", "help-list");
      items.forEach(function (item) {
        var row = el("div", "help-item");
        var top = el("div", "help-item-top");
        top.appendChild(el("strong", "help-ticket", item.ticket_no));
        top.appendChild(chip("status", item.status, item.status_label));
        top.appendChild(chip("sla", item.sla_state, slaLabel(item.sla_state)));
        row.appendChild(top);
        row.appendChild(el("div", "help-item-line", item.category_label + " · reported " + formatStamp(item.created_at)));
        row.appendChild(el("div", "muted help-item-line", slaLine(item)));
        var toggle = el("button", "btn-link", mineOpen[item.id] ? "Hide details" : "Show details");
        toggle.type = "button";
        toggle.setAttribute("aria-expanded", mineOpen[item.id] ? "true" : "false");
        var slot = el("div", "help-slot");
        function showDetail() {
          clear(slot);
          slot.appendChild(el("p", "muted", "Loading..."));
          getJson("/help/requests/" + item.id).then(function (r) {
            clear(slot);
            if (!r.ok) { slot.appendChild(el("p", "flash error", r.error || "Could not load it.")); return; }
            slot.appendChild(detailBody(r.request, "/help/attachments/"));
          }).catch(function () { clear(slot); slot.appendChild(el("p", "flash error", "Could not load it.")); });
        }
        toggle.addEventListener("click", function () {
          mineOpen[item.id] = !mineOpen[item.id];
          toggle.textContent = mineOpen[item.id] ? "Hide details" : "Show details";
          toggle.setAttribute("aria-expanded", mineOpen[item.id] ? "true" : "false");
          if (mineOpen[item.id]) showDetail(); else clear(slot);
        });
        row.appendChild(toggle);
        row.appendChild(slot);
        if (mineOpen[item.id]) showDetail();
        list.appendChild(row);
      });
      card.appendChild(list);
      mineRoot.appendChild(card);
    }

    // ================= Team view =================
    var teamFilters = { status: "", category: "", overdue: false };
    var teamSelected = null;
    var teamTickFor = null;
    var teamImportResult = null;

    function loadTeam() {
      var query = [];
      if (teamFilters.status) query.push("status=" + encodeURIComponent(teamFilters.status));
      if (teamFilters.category) query.push("category=" + encodeURIComponent(teamFilters.category));
      if (teamFilters.overdue) query.push("overdue=1");
      return getJson("/help/team/requests" + (query.length ? "?" + query.join("&") : "")).then(function (r) {
        if (!r.ok) throw new Error("failed");
        drawTeam(r);
      }).catch(function () {
        clear(teamRoot);
        teamRoot.appendChild(el("div", "card")).appendChild(el("p", "flash error", "Could not load the team view."));
      });
    }

    function drawTeam(data) {
      clear(teamRoot);
      var note = el("div", "card help-disclaimer");
      note.appendChild(el("p", null, "This view has no access control yet: anyone who can open this page can see every request and change its status. It will be limited to the team once login exists."));
      teamRoot.appendChild(note);

      var card = el("div", "card");
      var head = el("div", "help-head");
      head.appendChild(el("h2", null, "All requests"));
      card.appendChild(head);
      card.appendChild(el("p", "help-summary", summaryText(data.summary)));

      var filters = el("div", "help-filters");
      function select(label, options, value, onChange) {
        var wrap = el("label", "help-field");
        wrap.appendChild(el("span", "help-label", label));
        var s = el("select");
        options.forEach(function (o) {
          var opt = el("option", null, o[1]);
          opt.value = o[0];
          s.appendChild(opt);
        });
        s.value = value;
        s.addEventListener("change", function () { onChange(s.value); });
        wrap.appendChild(s);
        return wrap;
      }
      var statusOptions = [["", "All"]].concat(Object.keys(STATUS_LABELS).map(function (k) { return [k, STATUS_LABELS[k]]; }));
      var categoryOptions = [["", "All"]].concat(data.summary.by_category.map(function (c) { return [c.key, c.label]; }));
      filters.appendChild(select("Status", statusOptions, teamFilters.status, function (v) { teamFilters.status = v; loadTeam(); }));
      filters.appendChild(select("Category", categoryOptions, teamFilters.category, function (v) { teamFilters.category = v; loadTeam(); }));
      var overdueWrap = el("label", "help-check");
      var overdue = el("input");
      overdue.type = "checkbox";
      overdue.checked = teamFilters.overdue;
      overdue.addEventListener("change", function () { teamFilters.overdue = overdue.checked; loadTeam(); });
      overdueWrap.appendChild(overdue);
      overdueWrap.appendChild(el("span", null, "Overdue only"));
      filters.appendChild(overdueWrap);
      var refresh = el("button", "btn", "Refresh");
      refresh.type = "button";
      refresh.addEventListener("click", loadTeam);
      filters.appendChild(refresh);
      card.appendChild(filters);

      if (!data.requests.length) card.appendChild(el("p", "muted", "No requests match."));
      else {
        var scroller = el("div", "help-table-wrap");
        var table = el("table", "help-table");
        var headRow = el("tr");
        ["Ticket", "Category", "Severity", "User", "Reported", "SLA", "Status", ""].forEach(function (h) { headRow.appendChild(el("th", null, h)); });
        table.appendChild(headRow);
        data.requests.forEach(function (item) {
          var tr = el("tr", teamSelected === item.id ? "help-row-selected" : "");
          tr.appendChild(el("td", "help-ticket", item.ticket_no));
          tr.appendChild(el("td", null, item.category_label));
          tr.appendChild(el("td", null, item.severity_label));
          tr.appendChild(el("td", null, item.username));
          tr.appendChild(el("td", null, formatStamp(item.created_at)));
          var sla = el("td");
          sla.appendChild(chip("sla", item.sla_state, slaLabel(item.sla_state)));
          tr.appendChild(sla);
          var st = el("td");
          st.appendChild(chip("status", item.status, item.status_label));
          tr.appendChild(st);
          var open = el("td");
          var button = el("button", "btn-queue", teamSelected === item.id ? "Close" : "Open");
          button.type = "button";
          button.addEventListener("click", function () {
            teamSelected = teamSelected === item.id ? null : item.id;
            loadTeam();
          });
          open.appendChild(button);
          tr.appendChild(open);
          table.appendChild(tr);
        });
        scroller.appendChild(table);
        card.appendChild(scroller);
      }
      teamRoot.appendChild(card);

      var detailSlot = el("div");
      teamRoot.appendChild(detailSlot);
      if (teamSelected) drawTeamDetail(detailSlot, teamSelected);

      // ---- export / import
      var tools = el("div", "card");
      tools.appendChild(el("h2", null, "Export and import"));
      tools.appendChild(el("p", "muted", "Export makes a ZIP (requests.csv, requests.json, the attached files and a README that explains the updates file) to send to the developer. The developer sends back a CSV or JSON file with ticket_no, status and note, and Import updates applies it. Importing the same file twice changes nothing."));
      var exportRow = el("div", "appt-form-actions");
      var exportNew = el("a", "btn btn-confirm", "Export new requests");
      exportNew.href = "/help/team/export?unexported=1&mark_exported=1";
      exportNew.setAttribute("download", "");
      exportNew.title = "Only requests not exported before. They are marked as exported.";
      exportNew.addEventListener("click", function () { setTimeout(loadTeam, 1500); });
      var exportAll = el("a", "btn", "Export all");
      exportAll.href = "/help/team/export";
      exportAll.setAttribute("download", "");
      exportAll.title = "Every request. Nothing is marked.";
      exportRow.appendChild(exportNew);
      exportRow.appendChild(exportAll);
      tools.appendChild(exportRow);

      var importRow = el("div", "appt-form-actions help-import");
      var fileInput = el("input");
      fileInput.type = "file";
      fileInput.accept = ".csv,.json,text/csv,application/json";
      fileInput.setAttribute("aria-label", "Updates file (CSV or JSON)");
      var importButton = el("button", "btn", "Import updates");
      importButton.type = "button";
      importButton.disabled = true;
      fileInput.addEventListener("change", function () { importButton.disabled = !fileInput.files.length; });
      var importOut = el("div", "help-import-result");
      importOut.setAttribute("role", "status");
      function showImport(lines, isError) {
        clear(importOut);
        lines.forEach(function (line) { importOut.appendChild(el("p", isError ? "field-note field-error" : "help-import-line", line)); });
      }
      importButton.addEventListener("click", function () {
        if (!fileInput.files.length) return;
        importButton.disabled = true;
        var body = new FormData();
        body.append("file", fileInput.files[0], fileInput.files[0].name);
        fetch("/help/team/import", { method: "POST", body: body }).then(readJson).then(function (r) {
          importButton.disabled = false;
          if (!r.ok) { showImport([r.error || "Could not import the file."], true); return; }
          teamImportResult = importSummaryLines(r.result);
          loadTeam();
        }).catch(function () { importButton.disabled = false; showImport(["Could not reach the server."], true); });
      });
      importRow.appendChild(fileInput);
      importRow.appendChild(importButton);
      tools.appendChild(importRow);
      tools.appendChild(importOut);
      if (teamImportResult) { showImport(teamImportResult, false); teamImportResult = null; }
      teamRoot.appendChild(tools);
    }

    function drawTeamDetail(slot, id) {
      var card = el("div", "card");
      card.appendChild(el("p", "muted", "Loading..."));
      slot.appendChild(card);
      getJson("/help/team/requests/" + id).then(function (r) {
        clear(card);
        if (!r.ok) { card.appendChild(el("p", "flash error", r.error || "Could not load it.")); return; }
        var item = r.request;
        var head = el("div", "help-head");
        head.appendChild(el("h2", null, item.ticket_no));
        head.appendChild(chip("status", item.status, item.status_label));
        head.appendChild(chip("sla", item.sla_state, slaLabel(item.sla_state)));
        card.appendChild(head);
        card.appendChild(el("p", "muted", item.category_label + " · " + item.username + " · reported " + formatStamp(item.created_at)));
        card.appendChild(detailBody(item, "/help/team/attachments/"));

        var change = el("form", "appt-form help-change");
        change.autocomplete = "off";
        var grid = el("div", "help-grid");
        var statusSel = el("select");
        var same = el("option", null, "Keep " + statusLabel(item.status).toLowerCase() + " (add a note only)");
        same.value = item.status;
        statusSel.appendChild(same);
        item.next_statuses.forEach(function (s) {
          var o = el("option", null, (item.status === "resolved" || item.status === "closed") && s === "in_progress" ? "Reopen as In progress" : "Move to " + statusLabel(s));
          o.value = s;
          statusSel.appendChild(o);
        });
        if (item.next_statuses.length) statusSel.selectedIndex = 1;
        var statusField = el("label", "help-field");
        statusField.appendChild(el("span", "help-label", "Status"));
        statusField.appendChild(statusSel);
        var noteInput = el("input");
        noteInput.type = "text";
        noteInput.maxLength = 500;
        noteInput.placeholder = "Optional note, shown in the history";
        noteInput.setAttribute("aria-label", "Note");
        var noteField = el("label", "help-field");
        noteField.appendChild(el("span", "help-label", "Note"));
        noteField.appendChild(noteInput);
        grid.appendChild(statusField);
        grid.appendChild(noteField);
        change.appendChild(grid);
        var message = el("span", "save-status error");
        message.setAttribute("role", "status");
        var actions = el("div", "appt-form-actions");
        var save = el("button", "btn-confirm", "Save status");
        save.type = "submit";
        actions.appendChild(save);
        actions.appendChild(message);
        change.appendChild(actions);
        change.addEventListener("submit", function (event) {
          event.preventDefault();
          save.disabled = true;
          message.textContent = "";
          postJson("/help/team/requests/" + item.id + "/status", { status: statusSel.value, note: noteInput.value }).then(function (res) {
            save.disabled = false;
            if (!res.ok) { message.textContent = res.error || "Could not save."; return; }
            teamTickFor = item.id;
            loadTeam();
          }).catch(function () { save.disabled = false; message.textContent = "Could not reach the server."; });
        });
        card.appendChild(change);
        if (teamTickFor === item.id) { teamTickFor = null; if (window.SaveTick) SaveTick.show(save); }
      }).catch(function () { clear(card); card.appendChild(el("p", "flash error", "Could not load it.")); });
    }

    // ================= wiring =================
    document.addEventListener("tabchange", function (event) {
      var id = event.detail && event.detail.id;
      if (id !== currentTab) {
        if (currentTab && currentTab !== "help") previousTab = currentTab;
        currentTab = id;
      }
      if (id !== "help") {
        if (window.Dictation && window.Dictation.active()) window.Dictation.cancel();      // never leave the microphone open
        return;
      }
      if (sub() === "mine") loadMine();
      else if (sub() === "team") loadTeam();
    });
    document.addEventListener("helpsubtabchange", function (event) {
      currentSub = event.detail && event.detail.id;
      if (currentSub !== "report" && window.Dictation && window.Dictation.active()) window.Dictation.cancel();
      if (currentTab !== "help") return;
      if (currentSub === "mine") loadMine();
      else if (currentSub === "team") loadTeam();
    });

    loadConfig();
  });
})();
