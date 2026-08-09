/* Vectra-180 -- hero demo.
 *
 * A hand-written WebGL1 shader, no library, nothing fetched. It renders a
 * synthetic driving scene twice -- once through each lens of a stereo pair --
 * and lets you scrub between the raw equidistant fisheye projection the camera
 * produces and the rectified view the dewarper produces from it.
 *
 * The scene is invented. The projection is not: r = f * theta is the same
 * equidistant model imaging/dewarp.py inverts.
 *
 * If anything here fails -- no WebGL, no context, a shader that will not
 * compile -- the function returns quietly and the still poster underneath
 * stays visible. The page never depends on this file.
 */
(function () {
	"use strict";

	var VERT = [
		"attribute vec2 aPos;",
		"void main() { gl_Position = vec4(aPos, 0.0, 1.0); }"
	].join("\n");

	var FRAG = [
		"precision highp float;",
		"uniform vec2  uRes;",
		"uniform float uTime;",
		"uniform float uMorph;",   // 0 = raw fisheye, 1 = rectified
		"uniform float uFov;",     // lens field of view, radians
		"uniform vec3  uCyan;",

		/* Ground plane at y = -1.6, receding in +z. */
		"vec3 scene(vec3 ro, vec3 rd) {",
		"  vec3 sky = mix(vec3(0.055,0.105,0.165), vec3(0.035,0.06,0.11), clamp(rd.y*1.6,0.0,1.0));",
		"  float horizon = exp(-abs(rd.y)*22.0);",
		"  sky += uCyan * horizon * 0.16;",
		"  if (rd.y > -0.004) return sky;",

		"  float t = (-1.6 - ro.y) / rd.y;",
		"  vec3 h = ro + rd * t;",
		"  float x = h.x;",
		"  float z = h.z + uTime * 9.0;",

		"  vec3 col;",
		"  float road = step(abs(x), 4.0);",
		/* verge: a coarse grid so motion is legible off the tarmac */
		"  float gx = smoothstep(0.94, 0.99, abs(fract(x*0.25)*2.0-1.0));",
		"  float gz = smoothstep(0.94, 0.99, abs(fract(z*0.16)*2.0-1.0));",
		"  vec3 verge = mix(vec3(0.035,0.062,0.055), uCyan*0.20, max(gx,gz)*0.55);",
		/* tarmac, dashed centre line, solid edge lines */
		"  vec3 tar = vec3(0.055,0.062,0.078);",
		"  float dash = step(abs(x),0.16) * step(fract(z*0.09), 0.5);",
		"  float edge = step(3.72, abs(x)) * step(abs(x), 3.96);",
		"  tar = mix(tar, vec3(0.80,0.84,0.88), max(dash, edge*0.75));",
		"  col = mix(verge, tar, road);",

		/* distance fog toward the sky colour, so the horizon closes cleanly */
		"  float fog = 1.0 - exp(-t * 0.018);",
		"  return mix(col, sky, clamp(fog, 0.0, 1.0));",
		"}",

		"void main() {",
		"  vec2 uv = (gl_FragCoord.xy / uRes) * 2.0 - 1.0;",
		"  float aspect = uRes.x / uRes.y;",
		"  float side = uv.x < 0.0 ? -1.0 : 1.0;",

		/* Recentre on this eye's half of the frame. Each half is one lens. */
		"  vec2 q = vec2(uv.x * aspect - side * aspect * 0.5, uv.y) / 0.94;",
		"  float r = length(q);",
		"  float ang = atan(q.y, q.x);",

		/* Equidistant fisheye: image radius is linear in incident angle. */
		"  float theta = r * uFov * 0.5;",
		"  vec3 dFish = vec3(sin(theta)*cos(ang), sin(theta)*sin(ang), cos(theta));",

		/* Rectified: the same solid angle laid out on a regular grid. */
		"  float lon = q.x * uFov * 0.5;",
		"  float lat = q.y * uFov * 0.5;",
		"  vec3 dFlat = vec3(sin(lon)*cos(lat), sin(lat), cos(lon)*cos(lat));",

		"  vec3 rd = normalize(mix(dFish, dFlat, uMorph));",
		/* The two lenses sit a baseline apart -- that offset is the whole */
		/* reason a depth map is possible at all. */
		"  vec3 ro = vec3(side * 0.075, 0.0, 0.0);",

		"  vec3 col = scene(ro, rd);",

		/* Outside the image circle a fisheye records nothing. That black */
		/* corner is real, and it retreats as the frame is rectified. */
		"  float disc = 1.0 - smoothstep(0.985, 1.005, r);",
		"  float ring = smoothstep(0.975, 0.995, r) * (1.0 - smoothstep(1.0, 1.02, r));",
		"  col = mix(col * disc, col, uMorph);",
		"  col += uCyan * ring * 0.55 * (1.0 - uMorph);",

		/* Seam between the two lenses. */
		"  float seam = 1.0 - smoothstep(0.0, 1.6 / uRes.x, abs(uv.x));",
		"  col = mix(col, uCyan * 0.5, seam * 0.35);",

		/* Vignette, then a faint scanline so it reads as a sensor feed. */
		"  col *= 1.0 - 0.22 * dot(uv, uv) * 0.5;",
		"  col += 0.012 * sin(gl_FragCoord.y * 3.14159);",
		"  gl_FragColor = vec4(col, 1.0);",
		"}"
	].join("\n");

	function compile(gl, type, src) {
		var s = gl.createShader(type);
		gl.shaderSource(s, src);
		gl.compileShader(s);
		if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) return null;
		return s;
	}

	window.vectraDemo = function (canvas, opts) {
		var gl;
		try {
			gl = canvas.getContext("webgl", { antialias: false, alpha: false, depth: false }) ||
				canvas.getContext("experimental-webgl", { antialias: false, alpha: false, depth: false });
		} catch (e) {
			return null;
		}
		if (!gl) return null;

		var vs = compile(gl, gl.VERTEX_SHADER, VERT);
		var fs = compile(gl, gl.FRAGMENT_SHADER, FRAG);
		if (!vs || !fs) return null;

		var prog = gl.createProgram();
		gl.attachShader(prog, vs);
		gl.attachShader(prog, fs);
		gl.linkProgram(prog);
		if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return null;
		gl.useProgram(prog);

		var buf = gl.createBuffer();
		gl.bindBuffer(gl.ARRAY_BUFFER, buf);
		gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
		var loc = gl.getAttribLocation(prog, "aPos");
		gl.enableVertexAttribArray(loc);
		gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);

		var uRes = gl.getUniformLocation(prog, "uRes");
		var uTime = gl.getUniformLocation(prog, "uTime");
		var uMorph = gl.getUniformLocation(prog, "uMorph");
		var uFov = gl.getUniformLocation(prog, "uFov");
		gl.uniform3f(gl.getUniformLocation(prog, "uCyan"), 0.0, 0.72, 0.85);

		var state = { morph: 0, target: 0, running: false, raf: 0, t0: 0 };
		var still = !!(opts && opts.still);

		function resize() {
			// Cap the backing store: this is decoration, and a phone GPU has
			// better things to do than shade a 3x device-pixel-ratio canvas.
			var dpr = Math.min(window.devicePixelRatio || 1, 1.75);
			var w = Math.max(1, Math.round(canvas.clientWidth * dpr));
			var h = Math.max(1, Math.round(canvas.clientHeight * dpr));
			if (canvas.width !== w || canvas.height !== h) {
				canvas.width = w;
				canvas.height = h;
				gl.viewport(0, 0, w, h);
			}
		}

		function draw(seconds) {
			resize();
			state.morph += (state.target - state.morph) * 0.12;
			gl.uniform2f(uRes, canvas.width, canvas.height);
			gl.uniform1f(uTime, seconds);
			gl.uniform1f(uMorph, state.morph);
			gl.uniform1f(uFov, Math.PI);
			gl.drawArrays(gl.TRIANGLES, 0, 3);
		}

		function frame(now) {
			if (!state.running) return;
			if (!state.t0) state.t0 = now;
			draw((now - state.t0) / 1000);
			state.raf = window.requestAnimationFrame(frame);
		}

		return {
			start: function () {
				if (still) { draw(4.2); return; }   // one frame, then nothing
				if (state.running) return;
				state.running = true;
				state.raf = window.requestAnimationFrame(frame);
			},
			stop: function () {
				state.running = false;
				if (state.raf) window.cancelAnimationFrame(state.raf);
				state.raf = 0;
			},
			setMorph: function (v) {
				state.target = v;
				if (still) { state.morph = v; draw(4.2); }
			}
		};
	};
})();
