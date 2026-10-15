(function () {
  // tasdiqlash oynasi: <button data-confirm="Aniqmi?">
  document.addEventListener("click", function (e) {
    var el = e.target.closest("[data-confirm]");
    if (el && !window.confirm(el.getAttribute("data-confirm"))) {
      e.preventDefault();
    }
  });

  // qayta sanash
  var cd = document.querySelector(".countdown[data-start]");
  if (cd) {
    var target = new Date(cd.getAttribute("data-start")).getTime();
    var parts = {
      d: cd.querySelector('[data-u="d"]'),
      h: cd.querySelector('[data-u="h"]'),
      m: cd.querySelector('[data-u="m"]'),
      s: cd.querySelector('[data-u="s"]'),
    };
    var tick = function () {
      var diff = Math.max(0, Math.floor((target - Date.now()) / 1000));
      parts.d.textContent = Math.floor(diff / 86400);
      parts.h.textContent = Math.floor((diff % 86400) / 3600);
      parts.m.textContent = Math.floor((diff % 3600) / 60);
      parts.s.textContent = diff % 60;
    };
    tick();
    setInterval(tick, 1000);
  }

  // mukofot qatorlari (admin)
  var box = document.getElementById("prizes");
  if (box) {
    var unit = box.getAttribute("data-unit") || "";
    var add = function (val) {
      var n = box.querySelectorAll(".prize-row").length + 1;
      var row = document.createElement("div");
      row.className = "prize-row";
      var b = document.createElement("b");
      b.textContent = n + "-o'rin";
      var inp = document.createElement("input");
      inp.type = "text";
      inp.inputMode = "numeric";
      inp.name = "prize_amount";
      inp.placeholder = "Summa";
      inp.autocomplete = "off";
      if (val) inp.value = val;
      row.appendChild(b);
      row.appendChild(inp);
      box.appendChild(row);
    };
    var existing = box.querySelectorAll(".prize-row").length;
    for (var i = existing; i < 3; i++) add("");
    document.getElementById("add-prize").addEventListener("click", function () { add(""); });
    document.getElementById("del-prize").addEventListener("click", function () {
      var rows = box.querySelectorAll(".prize-row");
      if (rows.length > 1) rows[rows.length - 1].remove();
    });
  }

  // admin: o'yin/tur tanlanganda sig'im izohi
  var game = document.getElementById("game"), mode = document.getElementById("mode"), hint = document.getElementById("cap-hint");
  if (game && mode && hint) {
    var upd = function () {
      if (mode.value === "pvp") {
        hint.textContent = "Ishtirokchilar soni (2–50).";
      } else if (game.value === "mlbb") {
        hint.textContent = "Jamoalar soni (2–30). Har jamoa 5 kishi, random tuziladi.";
      } else {
        hint.textContent = "Jamoalar soni (2–20). Har jamoa 4 kishi (80 o'yinchigacha), random tuziladi.";
      }
    };
    game.addEventListener("change", upd);
    mode.addEventListener("change", upd);
    upd();
  }
})();
