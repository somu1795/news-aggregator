// Opens the page in jsdom: the HTML the server renders (the file PAGE_HTML names, written by
// tests/test_page_script.py) with the repo's retro.js, and the API answered from a test's own views.
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const SCRIPT = fs.readFileSync(path.join(__dirname, "../../static/retro.js"), "utf8");
const SCRIPT_TAG = /<script src="\/static\/retro\.js[^>]*><\/script>/;
const HTML = fs.readFileSync(process.env.PAGE_HTML, "utf8");
if (!SCRIPT_TAG.test(HTML)) throw new Error("The page doesn't load /static/retro.js");
// The script is deferred: it runs once the page is parsed, so here it goes at the end of the body
const PAGE = HTML.replace(SCRIPT_TAG, "").split("</body>").join("<script>" + SCRIPT.split("</script").join("<\\/script") + "</script></body>");

function data(id) {
    return JSON.parse(HTML.split('<script id="' + id + '" type="application/json">')[1].split("</script>")[0]);
}
const REGIONS = data("views-data").map((v) => v.id);
const TOPICS = data("topics-data").map((t) => t.id);
const NOW = Date.now() / 1000;

// A headline published `minutes` ago
function item(title, minutes, extra) {
    return Object.assign({ title: title, link: "https://pub.example/" + title.toLowerCase().replace(/\W+/g, "-"), source: "Pub",
        published: NOW - minutes * 60, last_updated: NOW }, extra || {});
}

// What /api/search-index answers for these views: each link once, with the views it's in, in tab order
function searchIndex(views) {
    const items = new Map();
    for (const region of REGIONS) for (const topic of TOPICS) for (const one of views[region + "/" + topic] || []) {
        if (!items.has(one.link)) items.set(one.link, Object.assign({}, one, { views: [] }));
        items.get(one.link).views.push(topic === TOPICS[0] ? region : region + ":" + topic);
    }
    return { items: [...items.values()], fetched_at: NOW, expires_at: NOW + 300 };
}

// views: {"<region>/<topic>": [items]}; options: { stored: localStorage entries, country region the server guessed,
// failing: true while the API should answer 503 (page.failing can be changed later) }
async function open(views, options) {
    options = options || {};
    const page = { requests: [], failing: Boolean(options.failing) };
    const dom = new JSDOM(PAGE, { runScripts: "dangerously", url: "https://testserver/" + (options.query || ""), beforeParse(window) {
        Object.entries(options.stored || {}).forEach(([key, value]) => window.localStorage.setItem(key, value));
        window.matchMedia = () => ({ matches: false, addEventListener() {}, addListener() {} });
        window.scrollTo = () => {};
        window.fetch = (url) => {
            page.requests.push(url);
            const answer = (status, body) => Promise.resolve({ ok: status === 200, status: status, json: () => Promise.resolve(body) });
            if (page.failing) return answer(503, { detail: "Headlines are not available yet" });
            const asked = new URL(url, "https://testserver/");
            if (asked.pathname === "/api/search-index") return answer(200, searchIndex(views));
            if (asked.pathname === "/api/search") return answer(200, { items: [], words: [], corrected: [], next: null });
            const key = (asked.searchParams.get("region") || REGIONS[0]) + "/" + (asked.searchParams.get("topic") || TOPICS[0]);
            return answer(200, { fetched_at: NOW, data: views[key] || [] });
        };
    } });
    const window = dom.window, document = window.document;
    page.window = window;
    page.$ = (id) => document.getElementById(id);
    page.settle = (ms) => new Promise((resolve) => setTimeout(resolve, ms || 20));  // Lets the page's requests come back
    page.titles = () => [...document.querySelectorAll("#list .title")].map((a) => a.textContent);
    page.key = async (key) => { document.dispatchEvent(new window.KeyboardEvent("keydown", { key: key, bubbles: true })); await page.settle(); };
    page.click = async (element) => { element.dispatchEvent(new window.MouseEvent("click", { bubbles: true, cancelable: true, button: 0 })); await page.settle(); };
    page.type = async (text) => { page.$("search-input").value = text; page.$("search-input").dispatchEvent(new window.Event("input")); await page.settle(); };
    page.tab = (nav, id) => [...page.$(nav).querySelectorAll("a")].find((a) => new URL(a.href).searchParams.get(nav === "regions" ? "region" : "topic") === id);
    await page.settle();
    return page;
}

module.exports = { open, item, REGIONS, TOPICS };
