/* Vectra-180 -- landing site behaviour.
 *
 * Everything here is an enhancement of markup that already works: the page is
 * complete in the first HTTP response, and this file only makes it nicer to
 * move around in. If it fails to load, nothing is lost but the polish.
 *
 * No dependencies, no build step. Runs deferred, after the document is parsed.
 */
(function () {
	"use strict";

	var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

	/* ------------------------------------------------------------- theme */

	var toggle = document.getElementById("theme");
	if (toggle) {
		toggle.addEventListener("click", function () {
			var root = document.documentElement;
			// Whatever is on screen right now, not what was stored -- with no
			// stored choice the first click has to flip away from the OS
			// preference, not land on it and appear to do nothing.
			var current = root.dataset.theme ||
				(window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
			var next = current === "dark" ? "light" : "dark";
			root.dataset.theme = next;
			try { localStorage.setItem("vectra-theme", next); } catch (e) { /* private mode */ }
			toggle.setAttribute("aria-label",
				next === "dark" ? "Switch to the light theme" : "Switch to the dark theme");
		});
	}

	/* --------------------------------------------------------------- nav */

	var nav = document.getElementById("nav");
	if (nav) {
		var onScroll = function () {
			nav.classList.toggle("stuck", window.scrollY > 8);
		};
		window.addEventListener("scroll", onScroll, { passive: true });
		onScroll();
	}

	/* ----------------------------------------------------------- reveals */

	var revealed = document.querySelectorAll(".reveal, .reveal-stagger");
	if (!("IntersectionObserver" in window) || reduced) {
		// No observer, or motion is unwelcome: show everything immediately.
		Array.prototype.forEach.call(revealed, function (el) { el.classList.add("in"); });
	} else {
		var io = new IntersectionObserver(function (entries) {
			entries.forEach(function (entry) {
				if (!entry.isIntersecting) return;
				entry.target.classList.add("in");
				io.unobserve(entry.target);
			});
		}, { rootMargin: "0px 0px -12% 0px", threshold: 0.06 });
		Array.prototype.forEach.call(revealed, function (el) { io.observe(el); });
	}

	/* -------------------------------------------------------------- tabs */

	var tablist = document.querySelector(".tabs");
	if (tablist) {
		var tabs = Array.prototype.slice.call(tablist.querySelectorAll("[role=tab]"));
		var panels = tabs.map(function (t) {
			return document.getElementById(t.getAttribute("aria-controls"));
		});

		var select = function (i, focus) {
			tabs.forEach(function (tab, j) {
				tab.setAttribute("aria-selected", j === i ? "true" : "false");
				tab.tabIndex = j === i ? 0 : -1;
				if (panels[j]) panels[j].hidden = j !== i;
			});
			if (focus) tabs[i].focus();
		};

		tabs.forEach(function (tab, i) {
			tab.addEventListener("click", function () { select(i, false); });
			tab.addEventListener("keydown", function (ev) {
				var d = ev.key === "ArrowRight" ? 1 : ev.key === "ArrowLeft" ? -1 : 0;
				if (d) {
					ev.preventDefault();
					select((i + d + tabs.length) % tabs.length, true);
				} else if (ev.key === "Home") {
					ev.preventDefault();
					select(0, true);
				} else if (ev.key === "End") {
					ev.preventDefault();
					select(tabs.length - 1, true);
				}
			});
		});

		select(0, false);
	}

	/* -------------------------------------------------------------- copy */

	Array.prototype.forEach.call(document.querySelectorAll(".copy"), function (btn) {
		btn.addEventListener("click", function () {
			var block = btn.closest(".code");
			var code = block && block.querySelector("pre");
			if (!code) return;
			// Comment lines are explanation, not instruction -- pasting them
			// into a shell is harmless but noisy, so they are stripped.
			var text = code.innerText
				.split("\n")
				.filter(function (line) { return line.trim() && line.trim()[0] !== "#"; })
				.join("\n");
			var done = function (ok) {
				btn.textContent = ok ? "copied" : "select it";
				btn.classList.toggle("done", ok);
				setTimeout(function () {
					btn.textContent = "copy";
					btn.classList.remove("done");
				}, 1600);
			};
			if (navigator.clipboard && navigator.clipboard.writeText) {
				navigator.clipboard.writeText(text).then(function () { done(true); },
					function () { done(false); });
			} else {
				done(false);
			}
		});
	});

	/* --------------------------------------------------------- hero demo */

	var canvas = document.getElementById("demo-canvas");
	var slider = document.getElementById("morph");
	var label = document.getElementById("morph-val");
	var hud = document.getElementById("hud-right");

	var describe = function (v) {
		if (v <= 0.005) return "raw frame";
		if (v >= 0.995) return "rectified";
		return "dewarp " + Math.round(v * 100) + "%";
	};

	if (canvas && window.vectraDemo) {
		var demo = window.vectraDemo(canvas, { still: reduced });

		if (demo) {
			// Only reveal the canvas once it has actually drawn something --
			// until then the still poster underneath is the better picture.
			demo.start();
			requestAnimationFrame(function () {
				requestAnimationFrame(function () { canvas.classList.add("ready"); });
			});

			if (!reduced && "IntersectionObserver" in window) {
				new IntersectionObserver(function (entries) {
					entries.forEach(function (e) {
						if (e.isIntersecting) { demo.start(); } else { demo.stop(); }
					});
				}, { threshold: 0.01 }).observe(canvas);

				// A backgrounded tab has no business shading anything.
				document.addEventListener("visibilitychange", function () {
					if (document.hidden) { demo.stop(); } else { demo.start(); }
				});
			}

			if (slider) {
				var apply = function () {
					var v = Number(slider.value) / 100;
					demo.setMorph(v);
					if (label) label.textContent = describe(v);
					if (hud) hud.textContent = "FOV 180° · DEWARP " + Math.round(v * 100) + "%";
				};
				slider.addEventListener("input", apply);
				apply();
			}
		} else if (slider) {
			// No WebGL. The poster stands on its own, so retire the control
			// rather than leave a slider that does nothing.
			var row = slider.closest(".demo-controls");
			if (row) row.hidden = true;
		}
	}
})();
