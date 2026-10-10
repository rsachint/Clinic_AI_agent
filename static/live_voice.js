// Hold-to-talk live voice UI over a Socket.IO connection to
// clinic/realtime_voice.py. The microphone is OFF unless a talk key (Enter or
// F1) or the on-screen orb is being held: nothing is captured, streamed or
// sent to the speech service otherwise, and when idle the browser releases the
// microphone entirely (every track stopped, capture graph closed).
//
// What this file does NOT do, on purpose: it never decides what a write
// intent should save (that's a `review_card` payload rendered by the
// existing, unmodified ReviewCard.build(), approved via the existing
// /approve HTTP POST), and it never speaks a reply back -- no echo-back by
// design; read answers and save confirmations are shown as text only.
//
// The key decision logic (pttShouldStart) and the held-key state machine
// (createHoldMachine) are pure and DOM-free so tests/ptt_client.test.js can
// exercise them under node.

var PTT_MIN_HOLD_MS = 300;     // shorter presses are accidental taps: cancelled
var PTT_MAX_HOLD_MS = 30000;   // auto-release a stuck key

// Roles whose own Enter behaviour must keep working (activate / type).
var PTT_ENTER_ROLES = {
  button: 1, link: 1, menuitem: 1, option: 1, tab: 1, checkbox: 1, radio: 1,
  switch: 1, textbox: 1, searchbox: 1, combobox: 1, listbox: 1,
};
var PTT_ENTER_TAGS = { input: 1, textarea: 1, select: 1, button: 1, a: 1, summary: 1 };

window.pttIsTalkKey = function (key) {
  return key === "Enter" || key === "F1";
};

// A plain-object snapshot of the focused element, so pttShouldStart stays pure.
window.pttElementInfo = function (el) {
  if (!el || !el.tagName) return { tag: "body", role: "", contentEditable: false, isNavItem: false };
  return {
    tag: String(el.tagName).toLowerCase(),
    role: (el.getAttribute && el.getAttribute("role")) || "",
    contentEditable: !!el.isContentEditable,
    isNavItem: !!(el.closest && el.closest(".nav-link")),
  };
};

// Should this keydown be handled as a talk key?  Returns
//   { start: bool,           begin a listen (false for auto-repeats)
//     preventDefault: bool } the page owns the key, so the browser must not act on it
// Enter is only claimed when focus is not somewhere Enter already means
// something (typing, activating a button/link); the left-nav items are the
// one exception. F1 is always claimed (so the browser's Help shortcut doesn't fire).
// Held Cmd/Ctrl/Alt/Meta means a browser shortcut: never ours.
window.pttShouldStart = function (event, info) {
  var no = { start: false, preventDefault: false };
  if (!event || !window.pttIsTalkKey(event.key)) return no;
  if (event.ctrlKey || event.metaKey || event.altKey) return no;
  if (event.isComposing) return no;
  if (event.key === "Enter") {
    info = info || {};
    var tag = String(info.tag || "").toLowerCase();
    var role = String(info.role || "").toLowerCase();
    var ownsEnter = !!(PTT_ENTER_TAGS[tag] || info.contentEditable || PTT_ENTER_ROLES[role]);
    if (ownsEnter && !info.isNavItem) return no;
  }
  return { start: !event.repeat, preventDefault: true };
};

// One held "talk" source at a time (a key name, or "pointer"). Pure state
// machine: the clock and timers are injectable.
//   press(id)    -> true if a listen started (false: another is held, or onStart refused)
//   release(id)  -> ends the listen only if `id` is the one held (a different key's keyup is ignored)
//   end(reason)  -> end whatever is held (blur, hidden, pagehide ...)
//   cancel(reason) -> discard (Escape, errors)
// Ending after less than minHoldMs calls onTooShort instead of onStop, except
// for the max-hold auto-release.
window.createHoldMachine = function (cb, opts) {
  opts = opts || {};
  var minMs = opts.minHoldMs != null ? opts.minHoldMs : PTT_MIN_HOLD_MS;
  var maxMs = opts.maxHoldMs != null ? opts.maxHoldMs : PTT_MAX_HOLD_MS;
  var now = opts.now || Date.now;
  var setT = opts.setTimeout || setTimeout;
  var clearT = opts.clearTimeout || clearTimeout;
  var holder = null;

  function take() {
    var h = holder;
    holder = null;
    if (h) clearT(h.timer);
    return h;
  }
  function end(reason) {
    var h = take();
    if (!h) return false;
    var held = now() - h.t;
    if (held < minMs && reason !== "max") {
      if (cb.onTooShort) cb.onTooShort(held, h.id);
    } else if (cb.onStop) {
      cb.onStop(reason, held, h.id);
    }
    return true;
  }
  return {
    press: function (id) {
      if (holder) return false;
      holder = { id: id, t: now(), timer: null };
      var mine = holder;
      if (cb.onStart && cb.onStart(id) === false) {
        if (holder === mine) take();
        return false;
      }
      if (holder === mine) mine.timer = setT(function () { end("max"); }, maxMs);
      return true;
    },
    release: function (id) {
      if (!holder || holder.id !== id) return false;
      return end("release");
    },
    end: end,
    cancel: function (reason) {
      var h = take();
      if (!h) return false;
      if (cb.onCancel) cb.onCancel(reason, h.id);
      return true;
    },
    isHeld: function () { return !!holder; },
    heldId: function () { return holder ? holder.id : null; },
  };
};

// Reads whose table already says everything the sentence would ("1 appointment
// on Mon 5 Oct", "2 follow-ups pending"): the sentence is hidden when there are
// rows to show. With no rows the sentence is kept, as it is the only answer.
var SENTENCE_REDUNDANT_WITH_TABLE = {
  patient_lookup: true, list_appointments: true, missed_followups: true, next_appointment: true,
};

// Columns never shown: every appointment is 30 minutes, so the length says nothing;
// the branch / doctor ids and the branch code duplicate the names beside them.
var HIDDEN_COLUMNS = { duration_minutes: true, branch_id: true, doctor_id: true, branch_code: true };
// Only worth a column when the clinic has more than one branch.
var BRANCH_COLUMNS = { branch: true, doctor: true };

// Table columns in display order: hidden ones dropped, free-text Notes always last.
window.orderColumns = function (keys, multiBranch) {
  keys = keys.filter(function (k) {
    var name = String(k).toLowerCase();
    return !HIDDEN_COLUMNS[name] && (multiBranch || !BRANCH_COLUMNS[name]);
  });
  var rest = keys.filter(function (k) { return String(k).toLowerCase() !== "notes"; });
  var notes = keys.filter(function (k) { return String(k).toLowerCase() === "notes"; });
  return rest.concat(notes);
};

// A heard transcript the person can correct before it is processed. Returns
// { element, textarea }. Enter sends (Shift+Enter is a new line); Send and
// Discard buttons do the same by mouse. handlers.onSend(text, edited) gets the
// trimmed text and whether it differs from what was heard; nothing is sent
// when the box is emptied. Pure DOM, no socket: the caller does the sending.
window.buildTranscriptReview = function (data, handlers) {
  function node(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text) n.textContent = text;
    return n;
  }
  var wrap = node("div", "transcript-review");
  wrap.appendChild(node("div", "bubble-label", "Heard \u2014 fix any mistakes, then send"));
  var textarea = node("textarea", "transcript-edit");
  textarea.rows = 2;
  textarea.value = data.text;
  textarea.setAttribute("aria-label", "Transcript of what was heard (editable)");
  wrap.appendChild(textarea);
  var actions = node("div", "transcript-actions");
  var send = node("button", "btn-confirm transcript-send", "Send");
  send.type = "button";
  var discard = node("button", "btn-reject transcript-discard", "Discard");
  discard.type = "button";
  actions.appendChild(send);
  actions.appendChild(discard);
  wrap.appendChild(actions);

  var done = false;
  function submit() {
    var text = textarea.value.trim();
    if (done || !text) return;
    done = true;
    handlers.onSend(text, text !== String(data.text).trim());
  }
  send.addEventListener("click", submit);
  discard.addEventListener("click", function () {
    if (done) return;
    done = true;
    handlers.onDiscard();
  });
  textarea.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      submit();
    }
  });
  return { element: wrap, textarea: textarea };
};

// The planner (a local model) can take several seconds. While it works the page
// shows this line in the assistant's bubble, so the wait never looks like a hang.
// `data` is the server's "thinking" event ({transcript, stage}). Pure: no DOM.
window.thinkingNote = function (data) {
  if (!data || typeof data !== "object") return "";
  return data.stage === "planner" ? "Working out what you meant\u2026" : "Thinking\u2026";
};

// How long the page waits on "Thinking..." before giving up on a silent server. It has to
// outlast the planner's own time limit (12 s) plus the one-word fallback behind it.
window.THINKING_SAFETY_MS = 45000;

window.readAnswerSentence = function (data) {
  var rows = Array.isArray(data.data) ? data.data : (data.data ? [data.data] : []);
  var hasTable = rows.length > 0 && typeof rows[0] === "object";
  if (SENTENCE_REDUNDANT_WITH_TABLE[data.intent] && hasTable) return "";
  return String(data.answer_text || "").replace(/\s*Source:.*$/i, "").trim();
};

document.addEventListener("DOMContentLoaded", function () {
  var stage = document.getElementById("call-stage");
  var orb = document.getElementById("call-orb");
  var statusEl = document.getElementById("call-status");
  var captionEl = document.getElementById("call-caption");
  var feed = document.getElementById("conversation-feed");
  var emptyState = document.getElementById("conversation-empty");
  if (!stage || !window.io) return;

  var uiState = "idle";
  var connected = false;
  var thinkingTimer = null;
  var captionTimer = null;

  function setState(state) {
    uiState = state;
    orb.setAttribute("data-state", state);
    stage.setAttribute("data-active", state === "listening" ? "true" : "false");
    var labels = {
      idle: connected ? "Hold 'Enter' or 'F1' key to speak and release to end" : "Connecting…",
      listening: "Listening… release to send",
      thinking: "Thinking…",
      error: "Something went wrong",
    };
    statusEl.textContent = labels[state] || state;
    clearTimeout(thinkingTimer);
    thinkingTimer = null;
    if (state === "thinking") {
      // Safety net: never stay on "Thinking..." forever if the server goes quiet.
      thinkingTimer = setTimeout(function () {
        cancelAllListens();
        setState("idle");
        flashCaption("No response from the assistant. Try again.");
      }, window.THINKING_SAFETY_MS);
    }
  }

  // A short-lived hint line (also used for live partial captions).
  function flashCaption(text, ms) {
    captionEl.textContent = text;
    clearTimeout(captionTimer);
    if (ms !== 0) captionTimer = setTimeout(function () { captionEl.textContent = ""; }, ms || 4000);
  }

  // ---- conversation feed (same DOM shape/classes as the old speak.js, so
  // static/style.css's .turn/.bubble-* rules need no changes) -----------
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") node.textContent = attrs[k];
      else node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { node.appendChild(c); });
    return node;
  }

  function appendTurn(transcript) {
    if (emptyState) { emptyState.remove(); emptyState = null; }
    var turn = el("div", { class: "turn" });
    if (transcript) {
      turn.appendChild(el("div", { class: "bubble bubble-user" }, [
        el("div", { class: "bubble-label", text: "You said" }),
        el("div", { class: "bubble-text", text: '"' + transcript + '"' }),
      ]));
    }
    var assistantBubble = el("div", { class: "bubble bubble-assistant" });
    turn.appendChild(assistantBubble);
    feed.appendChild(turn);
    feed.scrollTop = feed.scrollHeight;
    return assistantBubble;
  }

  // Assistant bubbles waiting for their result, matched by the transcript they
  // were sent with (several commands can be in flight at once).
  var waitingBubbles = [];
  function takeBubble(transcript) {
    for (var i = 0; i < waitingBubbles.length; i++) {
      if (waitingBubbles[i].text === transcript) {
        var bubble = waitingBubbles.splice(i, 1)[0].bubble;
        clearThinking(bubble);
        return bubble;
      }
    }
    return appendTurn(transcript);
  }

  // The "working out what you meant" line shown while the planner runs.
  function showThinking(transcript, note) {
    for (var i = 0; i < waitingBubbles.length; i++) {
      var bubble = waitingBubbles[i].bubble;
      if (waitingBubbles[i].text === transcript && !bubble.querySelector(".assistant-thinking")) {
        bubble.appendChild(el("div", { class: "assistant-thinking", text: note }));
        feed.scrollTop = feed.scrollHeight;
        return;
      }
    }
  }

  function clearThinking(bubble) {
    var line = bubble.querySelector(".assistant-thinking");
    if (line) line.remove();
  }

  // Cards still waiting for a decision, by id, so a voice edit can find its card.
  var openCards = {};

  function cardClosed(cardId, outcome) {
    if (!cardId) return;
    delete openCards[cardId];
    socket.emit("card_closed", { card_id: cardId, outcome: outcome || null });
  }

  function showReviewTurn(bubble, data) {
    var cardId = data.card_id || null;
    var card = ReviewCard.build(
      { intent: data.intent, slots: data.slots, resolved: data.resolved },
      {
        cardId: cardId,
        approveUrl: "/approve",
        // resolved.followups / resolved.appointments (see clinic/pipeline.py's
        // defer_intents branch) carry that specific best-effort-resolved
        // patient's own pending follow-ups / upcoming appointments, for the
        // "followup"/"appointment" review-card dropdowns -- mirroring how
        // static/wa_inbox.js already passes context.followups from its own
        // per-item pending_followups.
        context: {
          followups: (data.resolved && data.resolved.followups) || [],
          appointments: (data.resolved && data.resolved.appointments) || [],
        },
        // resolved.note: why the best-guess dropdown was left blank, e.g.
        // "Token 9 is not waiting in today's queue."
        notes: [].concat(data.resolved && data.resolved.note ? [data.resolved.note] : [],
                         (data.resolved && data.resolved.notes) || []),
        buildApprovePayload: function (slots) {
          return { intent: data.intent, slots: slots, transcript: data.transcript, language: data.language };
        },
        onApproved: function (result) {
          if (!result.ok) {
            bubble.appendChild(el("div", { class: "flash error", text: "Could not save: " + result.error }));
            return;
          }
          ReviewCard.showOutcome(card, "✅ " + result.message, true);
          cardClosed(cardId, "approved");
          if (window.DashboardRefresh) DashboardRefresh.refresh();
        },
        onRejected: function () {
          ReviewCard.showOutcome(card, "Rejected -- nothing was saved.", false);
          cardClosed(cardId, "rejected");
        },
      }
    );
    card.classList.remove("card");
    if (cardId) openCards[cardId] = card;
    bubble.appendChild(card);
  }

  // The assistant needs one more detail before it can build the card. Options
  // (e.g. two patients called Mohan) are buttons: tapping one is the same as
  // saying it. Nothing is saved by answering; the card still needs Approve.
  function showQuestionTurn(bubble, data) {
    bubble.appendChild(el("div", { class: "assistant-question", text: data.question }));
    appendOptions(bubble, data.options);
  }

  // The answer buttons of an open question; tapping one is the same as saying it.
  function appendOptions(bubble, options) {
    options = options || [];
    if (options.length) {
      var row = el("div", { class: "option-row" });
      options.forEach(function (option, index) {
        var button = el("button", { type: "button", class: "option-btn", text: option.label });
        button.addEventListener("click", function () {
          Array.prototype.forEach.call(row.querySelectorAll("button"), function (b) { b.disabled = true; });
          socket.emit("pick_option", { index: index });
        });
        row.appendChild(button);
      });
      bubble.appendChild(row);
    }
  }

  // "Make it 6 pm instead": change only those fields on the card already on screen.
  function showCardUpdateTurn(bubble, data) {
    var card = openCards[data.card_id];
    if (card && document.body.contains(card)) {
      ReviewCard.applyChanges(card, data.changes);
      bubble.appendChild(el("div", { class: "assistant-note", text: "Updated the open card: " + data.summary }));
    } else {
      delete openCards[data.card_id];
      bubble.appendChild(el("div", { class: "assistant-note", text: "That card is no longer open." }));
    }
  }

  function showReadTurn(bubble, data) {
    var sentence = window.readAnswerSentence(data);
    if (sentence) bubble.appendChild(el("div", { style: "font-size:16px;margin-bottom:10px;", text: sentence }));
    // What this list covers ("Appointments for Amit, all dates"), when the table cannot say.
    if (data.scope_caption) bubble.appendChild(el("div", { class: "list-caption", text: data.scope_caption }));
    if (data.data) {
      var rows = Array.isArray(data.data) ? data.data : [data.data];
      if (rows.length && typeof rows[0] === "object") {
        var table = el("table");
        bubble.classList.add("bubble-wide");      // a table gets the whole row, not 92% of it
        var headerRow = el("tr");
        var columns = window.orderColumns(Object.keys(rows[0]), !!(window.Branches && Branches.multi()));
        var fmt = window.ReadFormat;                // money as Rs, numbers on the right (static/read_format.js)
        columns.forEach(function (k) {
          headerRow.appendChild(el("th", fmt && fmt.isNumericColumn(k, rows) ? { text: k, class: "cell-num" } : { text: k }));
        });
        table.appendChild(headerRow);
        rows.forEach(function (row) {
          var tr = el("tr");
          columns.forEach(function (k) {
            var shown = fmt ? fmt.cell(k, row[k]) : { text: row[k] === null || row[k] === undefined ? "-" : row[k], numeric: false };
            var cell = el("td", shown.chips ? {} : { text: shown.text });
            if (shown.chips) {
              // a list (the free slots of a day) wraps as pills inside the cell instead of one clipped line
              cell.className = "cell-chips";
              shown.chips.forEach(function (piece) { cell.appendChild(el("span", { class: "slot-chip", text: piece })); });
            }
            if (shown.numeric) cell.className = "cell-num";
            if (String(k).toLowerCase() === "notes") cell.className = "cell-notes";   // the one column allowed to wrap
            tr.appendChild(cell);
          });
          table.appendChild(tr);
        });
        bubble.appendChild(table);
      }
    }
    bubble.appendChild(el("div", {
      class: "muted", style: "font-size:12px;margin-top:10px;",
      text: "Source: " + data.citation.source + ", " + data.citation.as_of,
    }));
    appendOptions(bubble, data.options);       // a booking offer that goes with the answer ("Book Neha ...?")
  }

  // "Open the calendar" and friends: pure navigation, nothing to approve and
  // nothing written. The server has already decided the tab and view.
  function showNavigateTurn(bubble, data) {
    bubble.appendChild(el("div", { style: "font-size:16px;", text: data.answer_text }));
    if (window.CalendarTab) {
      CalendarTab.show(data.mode);
    } else if (window.ClinicNav) {
      ClinicNav.select(data.tab || "appointments");
    }
  }

  function showError(bubble, text) {
    bubble.appendChild(el("div", { class: "flash error", text: text }));
  }

  // ---- mic capture ---------------------------------------------------
  // Web Audio API capture at 16kHz mono PCM16 (what the realtime STT
  // endpoint expects, see clinic/realtime_voice.py's encoding="linear16",
  // sample_rate="16000"), chunked via a ScriptProcessorNode and sent to the
  // backend as base64 over Socket.IO -- but ONLY between a talk-key press and
  // its release. Each press opens the mic (getUserMedia); each release stops
  // every track and closes the capture graph, so the OS mic indicator goes off.
  //
  // `listen` objects (one per press):
  //   { id, ready, buffer[], over, stopRequested, chunks, stream, ctx, ... }
  // Chunks captured before the server says `session_ready` (its STT connection
  // takes a moment to open) are buffered here and flushed in order, so the first
  // words are not lost.
  var socket = io();
  window.clinicSocket = socket;   // the page's one connection: static/network_chip.js listens on it too
  var seq = 0;
  var current = null;   // the listen for the most recent press
  var listens = {};     // id -> listen, until the server reports it ended

  function pcm16Base64FromFloat32(float32) {
    var int16 = new Int16Array(float32.length);
    for (var i = 0; i < float32.length; i++) {
      var v = Math.max(-1, Math.min(1, float32[i]));
      int16[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
    }
    var bytes = new Uint8Array(int16.buffer);
    var binary = "";
    var chunkSize = 0x8000;
    for (var j = 0; j < bytes.length; j += chunkSize) {
      binary += String.fromCharCode.apply(null, bytes.subarray(j, j + chunkSize));
    }
    return btoa(binary);
  }

  function setLevel(level) {
    stage.style.setProperty("--level", level.toFixed(3));
  }

  function startLevelMeter(listen) {
    var data = new Uint8Array(listen.analyser.fftSize);
    (function tick() {
      if (!listen.analyser) return;
      listen.analyser.getByteTimeDomainData(data);
      var sum = 0;
      for (var i = 0; i < data.length; i++) {
        var d = (data[i] - 128) / 128;
        sum += d * d;
      }
      setLevel(Math.min(1, Math.sqrt(sum / data.length) * 5));
      listen.raf = requestAnimationFrame(tick);
    })();
  }

  // Stop capturing and give the microphone back. Safe to call repeatedly.
  function releaseCapture(listen) {
    if (!listen) return;
    listen.over = true;
    if (listen.raf) cancelAnimationFrame(listen.raf);
    listen.raf = null;
    setLevel(0);
    try { if (listen.processor) { listen.processor.onaudioprocess = null; listen.processor.disconnect(); } } catch (e) {}
    try { if (listen.source) listen.source.disconnect(); } catch (e) {}
    try { if (listen.sink) listen.sink.disconnect(); } catch (e) {}
    listen.analyser = null;
    if (listen.stream) {
      listen.stream.getTracks().forEach(function (track) { track.stop(); });
      listen.stream = null;
    }
    if (listen.ctx) {
      try { listen.ctx.close(); } catch (e) {}
      listen.ctx = null;
    }
    listen.processor = listen.source = listen.sink = null;
  }

  function startCapture(listen) {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return Promise.reject(new Error("this browser cannot use the microphone here (it needs https or localhost)"));
    }
    return navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } })
      .then(function (stream) {
        if (listen.over) {
          // Released (or cancelled) while the permission prompt / device was opening.
          stream.getTracks().forEach(function (track) { track.stop(); });
          return;
        }
        listen.stream = stream;
        listen.ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
        listen.source = listen.ctx.createMediaStreamSource(stream);

        listen.analyser = listen.ctx.createAnalyser();
        listen.analyser.fftSize = 512;
        listen.source.connect(listen.analyser);
        startLevelMeter(listen);

        // 1024 samples @ 16kHz -> ~64ms per chunk. Small on purpose: on release
        // the partly-filled buffer is dropped, so a short buffer loses little.
        // ScriptProcessorNode is deprecated but needs no separate worklet module
        // file -- simplest option for this no-build-step project.
        listen.processor = listen.ctx.createScriptProcessor(1024, 1, 1);
        listen.processor.onaudioprocess = function (event) {
          if (listen.over) return;
          var audio = pcm16Base64FromFloat32(event.inputBuffer.getChannelData(0));
          listen.chunks += 1;
          if (listen.ready && socket.connected) socket.emit("audio_chunk", { audio: audio });
          else listen.buffer.push(audio);
        };
        listen.source.connect(listen.processor);
        // Some browsers only fire onaudioprocess if the node reaches a
        // destination; a silent gain keeps it running without audibly looping
        // the mic back to the speakers.
        listen.sink = listen.ctx.createGain();
        listen.sink.gain.value = 0;
        listen.processor.connect(listen.sink);
        listen.sink.connect(listen.ctx.destination);
      });
  }

  // Throw away a listen's audio and tell the server to close its STT session.
  function discardListen(listen, tellServer) {
    if (!listen) return;
    releaseCapture(listen);
    listen.buffer = [];
    listen.stopRequested = false;
    if (tellServer) socket.emit("listen_cancel", { id: listen.id });
    delete listens[listen.id];
  }

  function cancelAllListens() {
    Object.keys(listens).forEach(function (id) { releaseCapture(listens[id]); });
    listens = {};
    current = null;
    socket.emit("listen_cancel");
  }

  // ---- hold state machine wiring -----------------------------------------
  var machine = window.createHoldMachine({
    onStart: function () {
      if (!socket.connected) {
        setState("error");
        flashCaption("Not connected to the assistant. Wait a moment, or reload the page.", 0);
        return false;
      }
      seq += 1;
      var listen = { id: seq, ready: false, buffer: [], over: false, stopRequested: false, chunks: 0 };
      current = listen;
      listens[listen.id] = listen;
      setState("listening");
      flashCaption("", 0);
      socket.emit("listen_start", { id: listen.id });
      startCapture(listen).catch(function (err) {
        if (listen.over) return;           // already released/cancelled: nothing to report
        if (current === listen && machine.isHeld()) machine.cancel("mic-error");   // -> onCancel discards
        else discardListen(listen, true);
        setState("error");
        flashCaption("Microphone access failed: " + err.message + ". Allow microphone access and try again.", 0);
      });
      return true;
    },
    onStop: function (reason) {
      var listen = current;
      if (!listen) { setState("idle"); return; }
      releaseCapture(listen);              // mic off immediately
      if (listen.chunks === 0) {
        // Nothing was captured (e.g. the mic was still opening): nothing to send.
        discardListen(listen, true);
        setState("idle");
        flashCaption("The microphone was not ready. Hold the key and try again.");
        return;
      }
      listen.stopRequested = true;
      setState("thinking");
      if (reason === "max") flashCaption("Auto-released after 30 seconds.", 3000);
      if (listen.ready) socket.emit("listen_stop", { id: listen.id });
      // else: sent right after the buffered audio is flushed, on session_ready
    },
    onTooShort: function () {
      discardListen(current, true);
      setState("idle");
      flashCaption("Hold the key while you speak.");
    },
    onCancel: function (reason) {
      // (the server already closed its side on "error"; a dead socket on "disconnect")
      discardListen(current, reason !== "error" && reason !== "disconnect");
      if (reason === "mic-error" || reason === "error") return;   // keep the error visible
      setState("idle");
      if (reason === "escape") flashCaption("Cancelled.", 2000);
    },
  });

  // ---- keyboard ------------------------------------------------------------
  window.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      if (machine.isHeld() || uiState === "thinking") {
        event.preventDefault();
        if (!machine.cancel("escape")) {
          cancelAllListens();
          setState("idle");
          flashCaption("Cancelled.", 2000);
        }
      }
      return;
    }
    var decision = window.pttShouldStart(event, window.pttElementInfo(document.activeElement));
    if (decision.preventDefault) event.preventDefault();
    if (decision.start) machine.press(event.key);
  }, true);

  window.addEventListener("keyup", function (event) {
    if (window.pttIsTalkKey(event.key) && machine.release(event.key)) event.preventDefault();
  }, true);

  // ---- on-screen orb: press and hold (trackpad / touch / accessibility) -----
  orb.addEventListener("pointerdown", function (event) {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    event.preventDefault();
    try { orb.setPointerCapture(event.pointerId); } catch (e) {}
    machine.press("pointer");
  });
  ["pointerup", "pointercancel", "lostpointercapture"].forEach(function (name) {
    orb.addEventListener(name, function () { machine.release("pointer"); });
  });
  orb.addEventListener("contextmenu", function (event) { event.preventDefault(); });

  // ---- safety nets: never leave the mic open ---------------------------------
  window.addEventListener("blur", function () { machine.end("blur"); });
  document.addEventListener("visibilitychange", function () { if (document.hidden) machine.end("hidden"); });
  window.addEventListener("pagehide", function () { machine.end("pagehide"); });

  // ---- socket wiring ---------------------------------------------------------
  function afterResult() {
    // A result for a finished listen: back to idle unless a new press is under way.
    if (!machine.isHeld()) setState("idle");
  }

  // Tell the server which branch this computer works for (My branch) and which one
  // the switcher shows, so "book Amit tomorrow" lands in the right branch.
  function syncBranch() {
    if (!window.Branches || !socket.connected) return;
    socket.emit("set_branch", { mine: Branches.mine(), view: Branches.view() });
  }
  document.addEventListener("branchchange", syncBranch);

  socket.on("connect", function () {
    syncBranch();
    connected = true;
    if (!machine.isHeld()) setState("idle");
  });

  socket.on("disconnect", function () {
    connected = false;
    Object.keys(listens).forEach(function (id) { releaseCapture(listens[id]); });
    listens = {};
    machine.cancel("disconnect");
    current = null;
    setState("idle");
  });

  socket.on("session_ready", function (data) {
    var listen = listens[data && data.id];
    if (!listen || listen.ready) return;
    listen.ready = true;
    // Flush what was captured while the STT connection opened, in order.
    listen.buffer.forEach(function (audio) { socket.emit("audio_chunk", { audio: audio }); });
    listen.buffer = [];
    if (listen.stopRequested) socket.emit("listen_stop", { id: listen.id });
  });

  socket.on("vad_speech_start", function () {
    if (uiState === "listening") captionEl.textContent = "";
  });

  socket.on("transcript_partial", function (data) {
    if (uiState === "listening" || uiState === "thinking") flashCaption(data.text, 0);
  });

  socket.on("transcript_final", function (data) {
    captionEl.textContent = "";
    waitingBubbles.push({ text: data.text, bubble: appendTurn(data.text) });
    if (!machine.isHeld()) setState("thinking");
  });

  // The server holds what was heard until the person has had a chance to fix it.
  socket.on("transcript_review", function (data) {
    captionEl.textContent = "";
    if (emptyState) { emptyState.remove(); emptyState = null; }
    var turn = el("div", { class: "turn" });
    var review = window.buildTranscriptReview(data, {
      onSend: function (text, edited) {
        var said = el("div", { class: "bubble bubble-user" }, [
          el("div", { class: "bubble-label", text: edited ? "You said (corrected)" : "You said" }),
          el("div", { class: "bubble-text", text: '"' + text + '"' }),
        ]);
        turn.replaceChild(said, review.element);
        var assistantBubble = el("div", { class: "bubble bubble-assistant" });
        turn.appendChild(assistantBubble);
        waitingBubbles.push({ text: text, bubble: assistantBubble });
        socket.emit("submit_transcript", { id: data.id, text: text });
        if (!machine.isHeld()) setState("thinking");
      },
      onDiscard: function () {
        turn.remove();
        socket.emit("discard_transcript", { id: data.id });
      },
    });
    turn.appendChild(review.element);
    feed.appendChild(turn);
    feed.scrollTop = feed.scrollHeight;
    if (!machine.isHeld()) setState("idle");
  });

  // The planner is working on this command: say so, and keep the safety timer from
  // firing while it does (a slow model is not a silent server).
  socket.on("thinking", function (data) {
    showThinking(data.transcript, window.thinkingNote(data));
    if (!machine.isHeld()) setState("thinking");
  });

  socket.on("review_card", function (data) {
    var bubble = takeBubble(data.transcript);
    showReviewTurn(bubble, data);
    afterResult();
  });

  socket.on("read_answer", function (data) {
    var bubble = takeBubble(data.transcript);
    showReadTurn(bubble, data);
    afterResult();
  });

  socket.on("assistant_question", function (data) {
    var bubble = takeBubble(data.transcript);
    showQuestionTurn(bubble, data);
    afterResult();
  });

  socket.on("card_update", function (data) {
    var bubble = takeBubble(data.transcript);
    showCardUpdateTurn(bubble, data);
    afterResult();
  });

  socket.on("assistant_note", function (data) {
    var bubble = takeBubble(data.transcript);
    bubble.appendChild(el("div", { class: "assistant-note", text: data.message }));
    afterResult();
  });

  // "Switch to Branch C": this computer's My branch (and what it views) changes.
  socket.on("switch_branch", function (data) {
    var bubble = takeBubble(data.transcript);
    if (window.Branches) {
      Branches.setMine(data.branch_id);
      Branches.setView(data.branch_id);
    }
    bubble.appendChild(el("div", { style: "font-size:16px;", text: data.answer_text }));
    afterResult();
  });

  // "Close Branch A tomorrow": the batch review card, a plan only. The Apply button
  // on it (a person's tap) is what closes the branch and moves anyone.
  socket.on("closure_plan", function (data) {
    var bubble = takeBubble(data.transcript);
    bubble.classList.add("bubble-wide");
    bubble.appendChild(el("div", { style: "font-size:16px;margin-bottom:10px;", text: data.answer_text }));
    if (window.ClosureCard) bubble.appendChild(ClosureCard.build(data.plan, { reason: data.reason }));
    afterResult();
  });

  socket.on("navigate", function (data) {
    var bubble = takeBubble(data.transcript);
    showNavigateTurn(bubble, data);
    afterResult();
  });

  socket.on("pipeline_error", function (data) {
    var bubble = takeBubble(data.transcript);
    showError(bubble, data.error);
    afterResult();
  });

  // The server closed a listen's STT session (done, nothing heard, error or cap).
  socket.on("listen_end", function (data) {
    var id = data && data.id;
    var listen = listens[id];
    if (listen) { releaseCapture(listen); delete listens[id]; }
    if (machine.isHeld()) {
      if (current && current.id === id) {
        // The server gave up on the listen we are still holding: let go.
        machine.cancel("error");
        if (uiState !== "error") { setState("idle"); flashCaption("The speech service closed. Try again."); }
      }
      return;                              // an older listen ending; a new one is in progress
    }
    if (uiState === "error") return;       // keep the voice_error message visible
    setState("idle");
    if (!data.heard) flashCaption("Didn't catch that. Hold the key and speak again.");
  });

  socket.on("voice_error", function (data) {
    if (machine.isHeld()) machine.cancel("error");
    setState("error");
    flashCaption(data.message, 0);
  });

  // ---- dictation (the "Need help" box, static/help.js) --------------------------------------
  // Click to start, click to stop. The same microphone capture (startCapture / releaseCapture above) and the
  // same speech connection as hold-to-talk, but on its OWN socket events, so nothing here touches the
  // assistant's conversation, its talk key or its status line. The recognised text is only handed to the page
  // that asked for it, through the handlers given to window.Dictation.start; the server never treats it as a
  // command (clinic/realtime_voice.py, dictation mode).
  //
  // handlers: onState("starting"|"listening"|"finishing"|"idle"), onText(text), onPartial(text),
  //           onError(message), onEnd({ heard, reason }).
  var DICTATION_MAX_MS = 120000;       // the server stops it at 120 s too
  var DICTATION_END_WAIT_MS = 9000;    // after stop: give up waiting for the server's closing event
  var dictation = null;                // { id, serverReady, buffer[], over, stopRequested, chunks, handlers, ... }

  function dictationFlush(d) {
    var chunks = d.buffer;
    d.buffer = [];
    chunks.forEach(function (audio) { socket.emit("dictation_audio", { id: d.id, audio: audio }); });
  }

  function dictationFinish(d, state) {
    if (!d || d.done) return;
    d.done = true;
    clearTimeout(d.cap);
    clearTimeout(d.endWait);
    clearInterval(d.pump);
    releaseCapture(d);                 // the microphone is released whatever way this ends
    if (dictation === d) dictation = null;
    if (d.handlers.onState) d.handlers.onState(state || "idle");
  }

  // Something went wrong: tell the server to drop it, give the microphone back, tell the page.
  function dictationFail(d, message) {
    if (!d || d.done) return;
    socket.emit("dictation_cancel", { id: d.id });
    dictationFinish(d, "idle");
    if (d.handlers.onError) d.handlers.onError(message);
  }

  function dictationStop() {
    var d = dictation;
    if (!d || d.stopRequested || d.done) return false;
    d.stopRequested = true;
    releaseCapture(d);                 // microphone off immediately
    if (d.chunks === 0) {
      // Nothing was captured (the microphone was still opening): nothing to send.
      socket.emit("dictation_cancel", { id: d.id });
      dictationFinish(d, "idle");
      if (d.handlers.onError) d.handlers.onError("The microphone was not ready. Try again.");
      return true;
    }
    if (d.handlers.onState) d.handlers.onState("finishing");
    if (d.serverReady) {
      dictationFlush(d);
      socket.emit("dictation_stop", { id: d.id });
    }                                  // else: sent right after the buffered audio, when the server is ready
    d.endWait = setTimeout(function () { dictationFinish(d, "idle"); }, DICTATION_END_WAIT_MS);
    return true;
  }

  function dictationCancel() {
    var d = dictation;
    if (!d) return false;
    socket.emit("dictation_cancel", { id: d.id });
    dictationFinish(d, "idle");
    return true;
  }

  function dictationStart(handlers) {
    if (dictation) return false;
    var h = handlers || {};
    if (!socket.connected) {
      if (h.onError) h.onError("Not connected to the server. Wait a moment, or reload the page.");
      return false;
    }
    seq += 1;
    var d = { id: seq, ready: false, serverReady: false, buffer: [], over: false, stopRequested: false, done: false,
              chunks: 0, handlers: h };
    dictation = d;
    if (h.onState) h.onState("starting");
    // d.ready stays false on purpose: startCapture then keeps every chunk in d.buffer, and the pump below
    // sends them on the dictation event once the server has opened the speech connection.
    d.pump = setInterval(function () { if (d.serverReady && !d.done) dictationFlush(d); }, 100);
    d.cap = setTimeout(dictationStop, DICTATION_MAX_MS);
    socket.emit("dictation_start", { id: d.id });
    startCapture(d).then(function () {
      if (!d.over && !d.done && h.onState) h.onState("listening");
    }).catch(function (err) {
      if (d.over || d.done) return;
      dictationFail(d, "Microphone access failed: " + err.message + ". Allow microphone access and try again.");
    });
    return true;
  }

  window.Dictation = {
    supported: function () { return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia); },
    start: dictationStart,
    stop: dictationStop,
    cancel: dictationCancel,
    active: function () { return !!dictation; },
  };

  socket.on("dictation_ready", function (data) {
    var d = dictation;
    if (!d || !data || d.id !== data.id || d.serverReady) return;
    d.serverReady = true;
    dictationFlush(d);
    if (d.stopRequested) socket.emit("dictation_stop", { id: d.id });
  });
  socket.on("dictation_partial", function (data) {
    var d = dictation;
    if (d && data && d.id === data.id && d.handlers.onPartial) d.handlers.onPartial(String(data.text || ""));
  });
  socket.on("dictation_text", function (data) {
    var d = dictation;
    if (d && data && d.id === data.id && d.handlers.onText) d.handlers.onText(String(data.text || ""));
  });
  socket.on("dictation_end", function (data) {
    var d = dictation;
    if (!d || !data || d.id !== data.id) return;
    var handlers = d.handlers;
    dictationFinish(d, "idle");
    if (handlers.onEnd) handlers.onEnd({ heard: !!data.heard, reason: data.reason || "stopped" });
  });
  socket.on("dictation_error", function (data) {
    dictationFail(dictation, (data && data.message) || "Dictation stopped.");
  });
  socket.on("disconnect", function () {
    dictationFail(dictation, "Lost the connection. Dictation stopped.");
  });
  window.addEventListener("pagehide", function () { dictationCancel(); });
});

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    pttShouldStart: window.pttShouldStart,
    pttIsTalkKey: window.pttIsTalkKey,
    pttElementInfo: window.pttElementInfo,
    createHoldMachine: window.createHoldMachine,
    readAnswerSentence: window.readAnswerSentence,
    thinkingNote: window.thinkingNote,
    orderColumns: window.orderColumns,
    buildTranscriptReview: window.buildTranscriptReview,
  };
}
