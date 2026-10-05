// Branch state for this browser: "My branch" (chosen once per computer, kept in
// localStorage) and the branch being viewed (the sidebar switcher: one branch,
// or "All branches"). Everything that lists or books appointments asks this
// module which branch to use, and redraws when it changes.
//
// The server never trusts this: every route re-checks that the branch exists.
// Pure helpers are at the top so tests/ptt_client.test.js can run them in node.

window.branchLabel = function (branch) {
  return branch ? branch.name : "";
};

// Which branch a stored value means: a valid branch id, else the fallback.
window.pickBranchId = function (stored, branches, fallbackId) {
  var id = parseInt(stored, 10);
  for (var i = 0; i < branches.length; i++) {
    if (branches[i].id === id) return id;
  }
  return fallbackId;
};

// "all" or a valid branch id, else the fallback (which is My branch).
window.pickViewBranch = function (stored, branches, fallbackId) {
  if (stored === "all" && branches.length > 1) return "all";
  return window.pickBranchId(stored, branches, fallbackId);
};

window.Branches = (function () {
  var KEY_MINE = "clinic.myBranch";
  var KEY_VIEW = "clinic.viewBranch";
  var node = document.getElementById ? document.getElementById("branches-data") : null;
  var data = { branches: [], default_branch_id: 1, multi_branch: false, doctors: [], schedules: [] };
  try {
    var parsed = JSON.parse(node ? node.textContent : "{}") || {};
    if (Array.isArray(parsed.branches)) data = parsed;   // no page data (or bad data): keep the safe defaults
  } catch (e) {}
  var storage = (typeof window !== "undefined" && window.localStorage) ? window.localStorage : null;

  function read(key) {
    try { return storage ? storage.getItem(key) : null; } catch (e) { return null; }
  }
  function write(key, value) {
    try { if (storage) storage.setItem(key, String(value)); } catch (e) {}
  }

  function mine() {
    return window.pickBranchId(read(KEY_MINE), data.branches, data.default_branch_id);
  }
  function view() {
    return window.pickViewBranch(read(KEY_VIEW), data.branches, mine());
  }
  function emit() {
    document.dispatchEvent(new CustomEvent("branchchange", { detail: { mine: mine(), view: view() } }));
  }

  return {
    data: function () { return data; },
    list: function () { return data.branches; },
    multi: function () { return data.branches.length > 1; },
    get: function (id) {
      for (var i = 0; i < data.branches.length; i++) if (data.branches[i].id === parseInt(id, 10)) return data.branches[i];
      return null;
    },
    mine: mine,
    view: view,
    // The branch to book into: the one being viewed, or My branch when viewing "all".
    bookingBranch: function () { var v = view(); return v === "all" ? mine() : v; },
    setMine: function (id) { write(KEY_MINE, id); if (read(KEY_VIEW) === null) write(KEY_VIEW, id); emit(); },
    setView: function (value) { write(KEY_VIEW, value); emit(); },
    // Replace the page's branch data (after Settings changes it) and redraw.
    update: function (fresh) { data = fresh || data; emit(); document.dispatchEvent(new CustomEvent("branchdata")); },
    // "?branch=2" style query for the views that filter by the viewed branch.
    viewQuery: function () { return "branch=" + encodeURIComponent(view()); },
  };
})();

// The sidebar switcher: only shown when there is more than one branch.
document.addEventListener("DOMContentLoaded", function () {
  var box = document.getElementById("branch-switcher-box");
  var select = document.getElementById("branch-switcher");
  if (!box || !select) return;

  function draw() {
    var multi = Branches.multi();
    box.hidden = !multi;
    select.innerHTML = "";
    var all = document.createElement("option");
    all.value = "all";
    all.textContent = "All branches";
    select.appendChild(all);
    Branches.list().forEach(function (b) {
      var o = document.createElement("option");
      o.value = b.id;
      o.textContent = b.name + (b.id === Branches.mine() ? " \u2605" : "") + (b.status === "closed" ? " (closed)" : "");   // star = my branch
      select.appendChild(o);
    });
    select.value = String(Branches.view());
  }

  select.addEventListener("change", function () { Branches.setView(select.value); });
  document.addEventListener("branchchange", draw);
  document.addEventListener("branchdata", draw);
  draw();
});

if (typeof module !== "undefined" && module.exports) {
  module.exports = { pickBranchId: window.pickBranchId, pickViewBranch: window.pickViewBranch };
}
