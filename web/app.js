// council-brief: the video player, the timeline playhead, and both search boxes.
// Plain JavaScript, no libraries. Every link still works without it.
(function () {
  "use strict";
  var player = null;
  var playerEl = document.getElementById("player");

  // ---------- video: timestamps play the embedded video instead of leaving the page ----------
  window.onYouTubeIframeAPIReady = function () {
    if (!playerEl) return;
    player = new YT.Player("player", {
      videoId: playerEl.dataset.video,
      host: "https://www.youtube-nocookie.com",
      playerVars: { rel: 0, playsinline: 1 },
      events: { onReady: function () { seekToHash(); trackPlayhead(); } }
    });
  };

  function playAt(seconds) {
    if (!player || !player.seekTo) return false;
    player.seekTo(seconds, true);
    player.playVideo();
    var box = document.querySelector(".player");
    var r = box.getBoundingClientRect();
    if (r.top < 0 || r.bottom > window.innerHeight) box.scrollIntoView({ behavior: "smooth", block: "start" });
    return true;
  }

  document.addEventListener("click", function (ev) {
    var link = ev.target.closest("[data-t]");
    if (!link || link.tagName !== "A" || !playerEl) return;
    if (playAt(Number(link.dataset.t))) ev.preventDefault();   // otherwise the link opens YouTube
  });

  // A link like m/<id>.html#t=1745 (from search) highlights that line now,
  // and moves the video there once the player is ready.
  function hashTime() {
    var m = location.hash.match(/^#t=(\d+)/);
    return m ? Number(m[1]) : null;
  }
  function seekToHash() {
    var t = hashTime();
    if (t !== null && player && player.seekTo) player.seekTo(t, true);
  }
  (function showHashLine() {
    var t = hashTime();
    var line = t !== null && document.getElementById("t" + t);
    if (line) { line.classList.add("here"); line.scrollIntoView({ block: "center" }); }
  })();

  // A thin line on the timeline shows where the video is.
  function trackPlayhead() {
    var bar = document.querySelector(".strip:not(.compact) .strip-bar");
    if (!bar || !player.getDuration) return;
    var head = document.createElement("span");
    head.className = "playhead";
    bar.appendChild(head);
    setInterval(function () {
      var d = player.getDuration(), t = player.getCurrentTime();
      if (!d) return;
      head.style.display = "block";
      head.style.left = (t / d * 100) + "%";
    }, 500);
  }

  // ---------- highlighting ----------
  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function highlight(text, q) {
    var i = text.toLowerCase().indexOf(q);
    if (i < 0) return escapeHtml(text);
    return escapeHtml(text.slice(0, i)) + "<mark>" + escapeHtml(text.slice(i, i + q.length)) +
      "</mark>" + escapeHtml(text.slice(i + q.length));
  }
  function clock(s) {
    var h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
    var mm = h ? String(m).padStart(2, "0") : String(m);
    return (h ? h + ":" : "") + mm + ":" + String(sec).padStart(2, "0");
  }

  // ---------- find in this transcript ----------
  var find = document.getElementById("find");
  if (find) {
    var lines = Array.prototype.slice.call(document.querySelectorAll(".lines p"));
    var originals = lines.map(function (p) { return p.querySelector(".said").innerHTML; });
    var count = document.querySelector(".find-count");
    find.addEventListener("input", function () {
      var q = find.value.trim().toLowerCase(), shown = 0;
      lines.forEach(function (p, i) {
        var said = p.querySelector(".said");
        said.innerHTML = originals[i];
        if (!q) { p.classList.remove("hidden"); return; }
        var hit = said.textContent.toLowerCase().indexOf(q) >= 0;
        p.classList.toggle("hidden", !hit);
        if (hit) {
          shown++;
          // Highlight inside text only, leaving speaker names alone.
          Array.prototype.forEach.call(said.childNodes, function (node) {
            if (node.nodeType === 3 && node.textContent.toLowerCase().indexOf(q) >= 0) {
              var span = document.createElement("span");
              span.innerHTML = highlight(node.textContent, q);
              node.replaceWith(span);
            }
          });
        }
      });
      count.textContent = q ? (shown ? shown + (shown === 1 ? " line" : " lines") + " mention it" : "Not mentioned in this transcript.") : "";
    });
  }

  // ---------- search every transcript (home page) ----------
  var q = document.getElementById("q");
  if (q) {
    var index = null, results = document.querySelector(".results"), status = document.querySelector(".search-count");
    function load() {
      if (index) return Promise.resolve(index);
      return fetch("search.json").then(function (r) { return r.json(); }).then(function (d) { index = d; return d; });
    }
    q.addEventListener("focus", load, { once: true });
    q.addEventListener("input", function () {
      var term = q.value.trim().toLowerCase();
      if (term.length < 3) { results.innerHTML = ""; status.textContent = term ? "Type at least 3 letters." : ""; return; }
      load().then(function (data) {
        var hits = data.lines.filter(function (l) { return l[2].toLowerCase().indexOf(term) >= 0; });
        var meetings = {};
        hits.forEach(function (h) { meetings[h[0]] = true; });
        status.textContent = hits.length
          ? hits.length + (hits.length === 1 ? " mention" : " mentions") + " in " + Object.keys(meetings).length +
            (Object.keys(meetings).length === 1 ? " meeting" : " meetings") + (hits.length > 50 ? ". Showing the first 50." : ".")
          : "No meeting mentions that yet.";
        results.innerHTML = hits.slice(0, 50).map(function (h) {
          var m = data.meetings[h[0]];
          return '<li><a href="m/' + h[0] + ".html#t=" + h[1] + '">' + highlight(h[2], term) + "</a>" +
            '<span class="where">' + escapeHtml(m.board + ", " + m.date) + ", at " + clock(h[1]) + "</span></li>";
        }).join("");
      }).catch(function () { status.textContent = "Search couldn't load. Try reloading the page."; });
    });
  }
})();
