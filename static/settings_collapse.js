// Settings tab: every configuration card collapses under its heading, so the page reads as a short list of
// headings that open on demand. Click (or Enter / Space) on a heading toggles its card; "Expand all" and
// "Collapse all" sit at the top. Which cards are open is remembered in this browser (localStorage) by heading
// text, and the cards the other Settings scripts draw later, or draw again after a save, are picked up
// automatically. A card starts collapsed until it has been opened once. Text only, never HTML.
(function () {
  var STORAGE_KEY = "clinic.settingsCards";

  // The key a card is remembered under: its heading text, trimmed, lower-cased, spaces collapsed.
  function cardKey(headingText) {
    return String(headingText || "").replace(/\s+/g, " ").trim().toLowerCase();
  }
  // The remembered {key: true|false} map from the stored text; anything unreadable is an empty map.
  function readState(stored) {
    try {
      var parsed = JSON.parse(stored || "{}");
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
    } catch (e) { return {}; }
  }
  // A card is open only when it was opened on purpose.
  function isOpen(state, key) { return !!(state && state[key] === true); }
  // A copy of the map with one card's state set.
  function withOpen(state, key, open) {
    var next = {};
    Object.keys(state || {}).forEach(function (k) { next[k] = state[k]; });
    next[key] = !!open;
    return next;
  }
  var api = { cardKey: cardKey, readState: readState, isOpen: isOpen, withOpen: withOpen, STORAGE_KEY: STORAGE_KEY };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof window !== "undefined") window.SettingsCollapse = api;
  if (typeof document === "undefined" || !document.addEventListener) return;

  document.addEventListener("DOMContentLoaded", function () {
    var panel = document.querySelector('.tab-panel[data-tab="settings"]');
    if (!panel) return;

    function load() {
      try { return readState(window.localStorage.getItem(STORAGE_KEY)); } catch (e) { return {}; }
    }
    function save(state) {
      try { window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch (e) { /* not remembered */ }
    }
    var state = load();

    function heading(card) {
      for (var i = 0; i < card.children.length; i++) if (card.children[i].tagName === "H2") return card.children[i];
      return null;
    }
    function body(card) {
      for (var i = 0; i < card.children.length; i++) {
        if (card.children[i].classList.contains("card-collapse-body")) return card.children[i];
      }
      return null;
    }

    function apply(card, open) {
      var h = heading(card), b = body(card);
      if (!h || !b) return;
      b.hidden = !open;
      card.classList.toggle("collapsed", !open);
      h.setAttribute("aria-expanded", open ? "true" : "false");
    }

    function toggle(card) {
      var h = heading(card);
      var key = cardKey(h && h.textContent);
      var open = !!card.classList.contains("collapsed");
      state = withOpen(state, key, open);
      save(state);
      apply(card, open);
    }

    // Wrap everything under the heading in one body element and make the heading the toggle.
    function enhance(card) {
      var h = heading(card);
      if (!h || body(card)) return;
      var wrapper = document.createElement("div");
      wrapper.className = "card-collapse-body";
      var rest = [];
      for (var i = 0; i < card.children.length; i++) if (card.children[i] !== h) rest.push(card.children[i]);
      rest.forEach(function (child) { wrapper.appendChild(child); });
      card.appendChild(wrapper);
      if (!h.querySelector(".card-chevron")) {
        var chevron = document.createElement("span");
        chevron.className = "card-chevron";
        chevron.setAttribute("aria-hidden", "true");
        h.insertBefore(chevron, h.firstChild);
      }
      card.classList.add("collapsible");
      h.setAttribute("role", "button");
      h.setAttribute("tabindex", "0");
      if (!wrapper.id) {
        wrapper.id = "card-body-" + cardKey(h.textContent).replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
        h.setAttribute("aria-controls", wrapper.id);
      }
      h.addEventListener("click", function () { toggle(card); });
      h.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(card); }
      });
      apply(card, isOpen(state, cardKey(h.textContent)));
    }

    function cards() { return [].slice.call(panel.querySelectorAll(".card")); }
    function scan() {
      observer.disconnect();
      cards().forEach(enhance);
      observer.observe(panel, { childList: true, subtree: true });
    }
    var pending = false;
    var observer = new MutationObserver(function () {
      if (pending) return;
      pending = true;
      (window.requestAnimationFrame || window.setTimeout)(function () { pending = false; scan(); });
    });

    function setAll(open) {
      cards().forEach(function (card) {
        var h = heading(card);
        if (!h || !body(card)) return;
        state = withOpen(state, cardKey(h.textContent), open);
        apply(card, open);
      });
      save(state);
    }

    // "Expand all" / "Collapse all", right under the Settings flash line.
    var bar = document.createElement("div");
    bar.className = "settings-collapse-bar";
    [["Expand all", true], ["Collapse all", false]].forEach(function (pair) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "btn-link";
      button.textContent = pair[0];
      button.addEventListener("click", function () { setAll(pair[1]); });
      bar.appendChild(button);
    });
    var flash = panel.querySelector("#settings-flash");
    if (flash && flash.nextSibling) panel.insertBefore(bar, flash.nextSibling); else panel.insertBefore(bar, panel.firstChild);

    scan();
  });
})();
