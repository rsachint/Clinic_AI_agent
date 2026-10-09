// Settings tab: "Command understanding" -- the switch between Classic (rules first), New (model first,
// experimental) and Model does all read operations. Read from GET /settings/intent-architecture, saved with POST (JSON {mode}).
// An option can carry one sentence of help (shown under it) from the server.
// The server reads the setting at every command, so a change applies to the next sentence: no restart.
// A successful save shows only the check mark beside the button (static/save_tick.js); a failure keeps a
// text message. Everything is inserted as text, never as HTML.
(function () {
  var ONE_SENTENCE = "The new mode sends each command and the conversation so far to the Sarvam model, and code then checks what it says; switching back to Classic is instant and takes effect on your next command.";

  // The line "Current mode: ..." for a reply of GET /settings/intent-architecture.
  function currentLine(data) {
    var label = (data && data.label) || "Classic (rules first)";
    return "Current mode: " + label;
  }
  // The mode to save from the radio buttons' state: [{value, checked}].
  function chosenMode(radios) {
    for (var i = 0; i < (radios || []).length; i++) if (radios[i].checked) return radios[i].value;
    return null;
  }
  // The help sentence under an option, or "" when it has none.
  function optionHelp(option) {
    return option && typeof option.help === "string" ? option.help : "";
  }
  var api = { currentLine: currentLine, chosenMode: chosenMode, optionHelp: optionHelp, sentence: ONE_SENTENCE };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof window !== "undefined") window.IntentArchitecture = api;
  if (typeof document === "undefined" || !document.addEventListener) return;

  document.addEventListener("DOMContentLoaded", function () {
    var root = document.getElementById("settings-architecture");
    if (!root) return;

    function el(tag, cls, text) {
      var node = document.createElement(tag);
      if (cls) node.className = cls;
      if (text !== undefined && text !== null) node.textContent = text;
      return node;
    }
    var tickAfterDraw = false;

    function draw(data) {
      while (root.firstChild) root.removeChild(root.firstChild);
      var tick = tickAfterDraw;
      tickAfterDraw = false;
      var card = el("div", "card");
      card.id = "settings-architecture-card";
      card.appendChild(el("h2", null, "Command understanding"));
      card.appendChild(el("p", "muted auto-help", ONE_SENTENCE));
      var current = el("p", "architecture-current", currentLine(data));
      current.id = "architecture-current";
      card.appendChild(current);

      var form = el("form", "appt-form");
      form.autocomplete = "off";
      var radios = [];
      (data.options || []).forEach(function (option) {
        var row = el("label", "architecture-option");
        var radio = el("input");
        radio.type = "radio";
        radio.name = "intent-architecture";
        radio.value = option.value;
        radio.checked = option.value === data.mode;
        radios.push(radio);
        row.appendChild(radio);
        row.appendChild(el("span", null, " " + option.label));
        form.appendChild(row);
        if (optionHelp(option)) form.appendChild(el("div", "muted architecture-help", optionHelp(option)));
      });
      var actions = el("div", "appt-form-actions");
      var save = el("button", "btn-confirm", "Save");
      save.type = "submit";
      actions.appendChild(save);
      var error = el("span", "save-status error");
      error.setAttribute("role", "status");
      error.hidden = true;
      actions.appendChild(error);
      form.appendChild(actions);
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        var mode = chosenMode(radios);
        if (!mode) { error.hidden = false; error.textContent = "Choose one."; return; }
        save.disabled = true;
        save.textContent = "Saving...";
        fetch("/settings/intent-architecture", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode: mode })
        }).then(function (r) { return r.json(); }).then(function (r) {
          save.disabled = false;
          save.textContent = "Save";
          if (!r.ok) { error.hidden = false; error.textContent = r.error || "Could not save."; return; }
          tickAfterDraw = true;
          draw(r.data);
        }).catch(function (err) {
          save.disabled = false;
          save.textContent = "Save";
          error.hidden = false;
          error.textContent = "Could not save: " + err.message;
        });
      });
      card.appendChild(form);
      root.appendChild(card);
      if (tick && window.SaveTick) window.SaveTick.show(save);
    }

    function load() {
      return fetch("/settings/intent-architecture").then(function (r) { return r.json(); })
        .then(function (r) { if (r.ok) draw(r.data); })
        .catch(function () { /* nothing to show */ });
    }
    document.addEventListener("tabchange", function (event) { if (event.detail && event.detail.id === "settings") load(); });
    load();
  });
})();
