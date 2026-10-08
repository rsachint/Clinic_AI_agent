// The "saved" signal for every Save button: a small check mark beside the button, no text.
//
//   SaveTick.show(button)   put a check mark right after `button`
//   SaveTick.clear(node)    remove any check mark inside `node`
//
// It goes away by itself after a few seconds, or as soon as the person changes something in the
// same card, so it never claims more than it should. Errors are NOT shown with it: a failed save
// keeps its message (nobody should have to guess why).

window.SaveTick = (function () {
  var SHOW_MS = 5000;

  function clear(root) {
    if (!root || !root.querySelectorAll) return;
    Array.prototype.slice.call(root.querySelectorAll(".save-tick")).forEach(function (tick) { tick.remove(); });
  }

  function show(anchor) {
    if (!anchor || !anchor.parentNode) return null;
    clear(anchor.parentNode);
    var tick = document.createElement("span");
    tick.className = "save-tick";
    tick.setAttribute("role", "status");
    tick.setAttribute("aria-label", "Saved");
    tick.textContent = "\u2713";
    anchor.parentNode.insertBefore(tick, anchor.nextSibling);

    var scope = (anchor.closest && anchor.closest(".card, form, tr")) || anchor.parentNode;
    var timer = null;
    function remove() {
      clearTimeout(timer);
      scope.removeEventListener("input", remove);
      scope.removeEventListener("change", remove);
      if (tick.parentNode) tick.remove();
    }
    timer = setTimeout(remove, SHOW_MS);
    scope.addEventListener("input", remove);
    scope.addEventListener("change", remove);
    return remove;
  }

  return { show: show, clear: clear };
})();
