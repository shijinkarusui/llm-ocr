/* 预绘制主题：在样式与模块脚本之前同步执行，避免深色用户先看到一帧浅色。
   读的是 src/stores/session.ts 里的同一个 localStorage key。
   放在 public/ 走外链而非内联，是为了不必给 CSP 加 hash ——
   tauri.conf.json 的 script-src 'self' 直接放行同源脚本，维护成本更低。 */
(function () {
  try {
    var mode = localStorage.getItem("llm-ocr:theme") || "system";
    var dark =
      mode === "dark" ||
      (mode === "system" &&
        window.matchMedia("(prefers-color-scheme: dark)").matches);
    if (dark) document.documentElement.classList.add("dark");
  } catch (e) {
    /* 隐私模式下 localStorage 不可用，交由后续 effect 兜底 */
  }
})();
