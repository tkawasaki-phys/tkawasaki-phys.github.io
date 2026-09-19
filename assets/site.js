/* 言語切替のみ。既定は英語で、HTML には英語が直接書いてあるので
   JavaScript が無効でも英語版がそのまま読める。 */
(function () {
  var root = document.documentElement;

  function setLang(l) {
    root.setAttribute("lang", l === "ja" ? "ja" : "en");
    root.setAttribute("data-lang", l);
    var nodes = document.querySelectorAll("[data-en][data-ja]");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      el.textContent = (l === "ja") ? el.dataset.ja : el.dataset.en;
    }
    var btns = document.querySelectorAll(".langbtn");
    for (var j = 0; j < btns.length; j++) {
      btns[j].setAttribute("aria-pressed", String(btns[j].dataset.lang === l));
    }
    try { localStorage.setItem("site-lang", l); } catch (e) {}
  }

  document.addEventListener("click", function (ev) {
    var b = ev.target.closest && ev.target.closest(".langbtn");
    if (b) setLang(b.dataset.lang);
  });

  var saved = null;
  try { saved = localStorage.getItem("site-lang"); } catch (e) {}
  if (saved !== "ja" && saved !== "en") {
    saved = (navigator.language || "").toLowerCase().indexOf("ja") === 0 ? "ja" : "en";
  }
  setLang(saved);
})();

/* 業績ページの目次で、いま読んでいる節に印を付ける。
   JavaScript が無くてもリンクとしては動くので、これは飾りの範囲。

   「画面に入っている節」で選ぶと、前の節が画面下に残っている間ずっと
   そちらが選ばれてしまう。見出しが上端を通過した最後の節＝いま中にいる節、
   という判定にする。 */
(function () {
  var map = document.querySelector(".pubmap");
  if (!map) return;
  var links = [].slice.call(map.querySelectorAll("a"));
  var targets = links.map(function (a) {
    return document.querySelector(a.getAttribute("href"));
  });
  var LINE = 140;               // ヘッダの下あたりを基準線にする
  var queued = false;

  function update() {
    queued = false;
    var cur = -1;
    for (var i = 0; i < targets.length; i++) {
      if (targets[i] && targets[i].getBoundingClientRect().top <= LINE) cur = i;
    }
    // 最下部まで来たら最後の節を選ぶ（短い節が選ばれないままにならないように）
    if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 4) {
      cur = targets.length - 1;
    }
    for (var k = 0; k < links.length; k++) {
      if (k === cur) links[k].setAttribute("aria-current", "true");
      else links[k].removeAttribute("aria-current");
    }
  }
  function onScroll() {
    if (queued) return;
    queued = true;
    window.requestAnimationFrame(update);
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onScroll);
  update();
})();
