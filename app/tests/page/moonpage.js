// Opens the moon page in jsdom: the HTML the server renders (the file MOON_HTML names, written by
// tests/test_page_script.py) with the repo's sky.js, moonart.js, earth.js and moon.js. Canvases record what's drawn on them,
// and animation frames and timers wait until a test runs them.
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const HTML = fs.readFileSync(process.env.MOON_HTML, "utf8");
const SCRIPTS = ["sky.js", "moonart.js", "earth.js", "moon.js"];

function pageWith(names) {
    // The scripts are deferred: they run in order once the page is parsed, so here they go at the end of the body
    return SCRIPTS.reduce((page, name) => {
        const tag = new RegExp('<script src="/static/' + name.replace(".", "\\.") + '[^>]*></script>');
        if (!tag.test(page)) throw new Error("The moon page doesn't load /static/" + name);
        page = page.replace(tag, "");
        if (!names.includes(name)) return page;
        const script = fs.readFileSync(path.join(__dirname, "../../static", name), "utf8");
        return page.split("</body>").join("<script>" + script.split("</script").join("<\\/script") + "</script></body>");
    }, HTML);
}

// options: now (UTC string), moon ({south, position}), reducedMotion, stored (localStorage), noSky (only moon.js: no sky.js,
// moonart.js or earth.js), setup(window) before the page runs
async function open(options) {
    options = options || {};
    let html = pageWith(options.noSky ? ["moon.js"] : SCRIPTS);
    if (options.moon) html = html.replace(/(<script id="moon-data" type="application\/json">)[^<]*/, (_, tag) => tag + JSON.stringify(options.moon));
    const page = { errors: [], frameQueue: [], intervals: [], timeouts: [], clock: 0, now: Date.parse(options.now || "2026-11-19T00:00:00Z") };
    const virtualConsole = new VirtualConsole();
    virtualConsole.on("jsdomError", (error) => page.errors.push(error));
    const dom = new JSDOM(html, { runScripts: "dangerously", pretendToBeVisual: true, url: "https://testserver/moon", virtualConsole: virtualConsole, beforeParse(window) {
        Object.entries(options.stored || {}).forEach(([key, value]) => window.localStorage.setItem(key, value));
        window.Date.now = () => page.now;
        window.matchMedia = (query) => ({ matches: Boolean(options.reducedMotion) && /reduce/.test(query), addEventListener() {}, addListener() {} });
        window.requestAnimationFrame = (callback) => { page.frameQueue.push(callback); return page.frameQueue.length; };
        window.cancelAnimationFrame = () => { page.frameQueue = []; };
        window.setInterval = (callback, ms) => { page.intervals.push({ callback: callback, ms: ms }); return page.intervals.length; };
        // Timeouts wait until a test fires them (page.fireTimeouts)
        let timeoutId = 0;
        window.setTimeout = (callback, ms) => { page.timeouts.push({ id: ++timeoutId, callback: callback, ms: ms }); return timeoutId; };
        window.clearTimeout = (id) => { page.timeouts = page.timeouts.filter((t) => t.id !== id); };
        window.HTMLCanvasElement.prototype.getContext = function () {
            const canvas = this;
            canvas.ops = canvas.ops || [];
            const target = {
                createImageData: (w, h) => ({ width: w, height: h, data: new Uint8ClampedArray(w * h * 4) }),
                putImageData: (image) => { canvas.ops.push(["putImageData", null, image]); canvas.image = Buffer.from(image.data).toString("base64"); }
            };
            return new Proxy(target, {
                get: (target, name) => (name in target ? target[name] : (...args) => { canvas.ops.push([name, target.fillStyle, ...args]); }),
                set: (target, name, value) => { target[name] = value; return true; }
            });
        };
        window.addEventListener("error", (event) => page.errors.push(event.error || event.message));
        if (options.setup) options.setup(window);
    } });
    const window = dom.window, document = window.document;
    page.window = window;
    page.$ = (id) => document.getElementById(id);
    page.settle = (ms) => new Promise((resolve) => setTimeout(resolve, ms || 10));
    page.key = async (key) => { document.dispatchEvent(new window.KeyboardEvent("keydown", { key: key, bubbles: true })); await page.settle(); };
    page.click = async (element) => { element.dispatchEvent(new window.MouseEvent("click", { bubbles: true, cancelable: true, button: 0 })); await page.settle(); };
    // Runs the animation frames waiting, `ms` after the last ones
    page.frame = (ms) => { page.clock += ms; const queued = page.frameQueue; page.frameQueue = []; queued.forEach((callback) => callback(page.clock)); };
    page.frames = () => page.frameQueue.length;
    page.draws = (id) => (page.$(id).ops || []).length;
    page.text = (id) => page.$(id).textContent.replace(/\s+/g, " ").trim();
    page.setNow = (utc) => { page.now = Date.parse(utc); };
    page.tick = () => page.intervals.forEach((interval) => interval.callback());  // Every timer fires once
    page.fireTimeouts = () => { const due = page.timeouts; page.timeouts = []; due.forEach((t) => t.callback()); };
    page.image = (id) => page.$(id).image;  // What a canvas drawn with putImageData shows now
    // A pointer drag on an element: from x, y by dx, dy in `steps` moves; `holdMs` still before letting go
    page.drag = async (id, dx, dy, options) => {
        const o = Object.assign({ x: 100, y: 100, steps: 5, holdMs: 0, pointerType: "mouse" }, options || {}), el = page.$(id);
        const fire = (type, x, y) => el.dispatchEvent(new window.MouseEvent(type, { bubbles: true, clientX: x, clientY: y, button: 0 }));
        fire("pointerdown", o.x, o.y);
        for (let i = 1; i <= o.steps; i++) fire("pointermove", o.x + dx * i / o.steps, o.y + dy * i / o.steps);
        if (o.holdMs) await page.settle(o.holdMs);
        fire("pointerup", o.x + dx, o.y + dy);
        await page.settle();
    };
    page.close = () => window.close();
    await page.settle();
    page.frame(0);  // The first frame, which sets the animation's starting time
    return page;
}

module.exports = { open };
