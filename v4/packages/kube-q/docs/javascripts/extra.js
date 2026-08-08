/* kube-q docs — subtle landing-page enhancements
 *
 * 1. Fade-in on scroll for `.kq-reveal` blocks.
 * 2. Animated number counters for `.kq-stat-number` (reads
 *    `data-target` numeric attribute; falls back to its text).
 * 3. Respects `prefers-reduced-motion`.
 */

(function () {
  "use strict";

  const prefersReduced = window.matchMedia(
    "(prefers-reduced-motion: reduce)"
  ).matches;

  // ── Reveal on scroll ────────────────────────────────────────────────
  function setupReveal() {
    const targets = document.querySelectorAll(".kq-reveal");
    if (!targets.length) return;

    if (prefersReduced || !("IntersectionObserver" in window)) {
      targets.forEach((el) => el.classList.add("is-visible"));
      return;
    }

    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add("is-visible");
            io.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.12, rootMargin: "0px 0px -40px 0px" }
    );

    targets.forEach((el) => io.observe(el));
  }

  // ── Count-up for stat numbers ───────────────────────────────────────
  function animateCount(el) {
    const raw = el.dataset.target || el.textContent.trim();
    const match = raw.match(/([<>~]?)(-?\d+(?:\.\d+)?)(.*)/);
    if (!match) return;

    const prefix = match[1] || "";
    const target = parseFloat(match[2]);
    const suffix = match[3] || "";

    if (!isFinite(target) || prefersReduced) {
      el.textContent = prefix + target + suffix;
      return;
    }

    const duration = 1400;
    const start = performance.now();
    const isFloat = !Number.isInteger(target);

    function frame(now) {
      const p = Math.min(1, (now - start) / duration);
      // easeOutCubic
      const eased = 1 - Math.pow(1 - p, 3);
      const current = target * eased;
      el.textContent =
        prefix +
        (isFloat ? current.toFixed(1) : Math.round(current).toLocaleString()) +
        suffix;
      if (p < 1) requestAnimationFrame(frame);
    }

    requestAnimationFrame(frame);
  }

  function setupStats() {
    const stats = document.querySelectorAll(".kq-stat-number");
    if (!stats.length) return;

    if (!("IntersectionObserver" in window)) {
      stats.forEach(animateCount);
      return;
    }

    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            animateCount(entry.target);
            io.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.4 }
    );

    stats.forEach((el) => io.observe(el));
  }

  function init() {
    setupReveal();
    setupStats();
  }

  // Initial load + re-init on Material's instant navigation
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  if (window.document$ && typeof window.document$.subscribe === "function") {
    window.document$.subscribe(init);
  }
})();
