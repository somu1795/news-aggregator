// What a reader sees and can do on the page (static/retro.js): tabs, order, search, theme, failures.
const test = require("node:test");
const assert = require("node:assert/strict");
const page_ = require("./page");
const { item, REGIONS, TOPICS } = page_;

// Every page a test opens is closed when the test ends, passed or failed: its timers would keep node running
const opened = [];
async function open(views, options) {
    const page = await page_.open(views, options);
    opened.push(page);
    return page;
}
test.afterEach(() => { opened.splice(0).forEach((page) => page.window.close()); });

const [ALL, REGION] = REGIONS, [EVERYTHING, TOPIC] = TOPICS;
const MIX = [item("Storm warning issued", 30), item("Markets close higher", 5), item("Old bridge reopens", 90)];
const VIEWS = {
    [ALL + "/" + EVERYTHING]: MIX,
    [REGION + "/" + EVERYTHING]: [item("Regional election called", 10), MIX[0]],
    [REGION + "/" + TOPIC]: [item("A regional topic story", 20, { summary: "Rain delayed the storm cleanup." })],
};

test("the starting tab lists its headlines in the server's order, numbered", async () => {
    const page = await open(VIEWS);
    assert.deepEqual(page.titles(), ["Storm warning issued", "Markets close higher", "Old bridge reopens"]);
    assert.deepEqual([...page.window.document.querySelectorAll("#list .num")].map((n) => n.textContent), ["1.", "2.", "3."]);
    assert.equal(page.tab("regions", null).getAttribute("aria-current"), "page");
});

test("s goes through newest, oldest, random and back to the mix, and the choice is remembered", async () => {
    const page = await open(VIEWS);
    await page.key("s");
    assert.deepEqual(page.titles(), ["Markets close higher", "Storm warning issued", "Old bridge reopens"]);
    assert.equal(page.$("sort-mode").textContent, "newest");
    await page.key("s");
    assert.deepEqual(page.titles(), ["Old bridge reopens", "Storm warning issued", "Markets close higher"]);
    await page.key("s");
    assert.equal(page.$("sort-mode").textContent, "random");
    assert.deepEqual(page.titles().sort(), MIX.map((i) => i.title).sort());
    await page.key("s");
    assert.deepEqual(page.titles(), MIX.map((i) => i.title));

    const again = await open(VIEWS, { stored: { retroSort: "oldest" } });
    assert.deepEqual(again.titles(), ["Old bridge reopens", "Storm warning issued", "Markets close higher"]);
});

test("a region tab and a topic key each open their own headlines, and the tab is remembered", async () => {
    const page = await open(VIEWS);
    await page.click(page.tab("regions", REGION));
    assert.deepEqual(page.titles(), ["Regional election called", "Storm warning issued"]);
    assert.equal(page.requests.at(-1), "/api/headlines?region=" + REGION);
    await page.key("t");
    assert.deepEqual(page.titles(), ["A regional topic story"]);
    assert.equal(page.requests.at(-1), "/api/headlines?region=" + REGION + "&topic=" + TOPIC);
    assert.equal(page.tab("topics", TOPIC).getAttribute("aria-current"), "page");
    assert.equal(page.window.location.search, "");  // The address doesn't name the tab

    const again = await open(VIEWS, { stored: { retroRegion: REGION, retroTopic: TOPIC } });
    assert.deepEqual(again.titles(), ["A regional topic story"]);
});

test("a number key opens that region, and a shared link opens its tab", async () => {
    const page = await open(VIEWS);
    await page.key("2");
    assert.deepEqual(page.titles(), ["Regional election called", "Storm warning issued"]);

    const linked = await open(VIEWS, { query: "?region=" + REGION + "&topic=" + TOPIC, stored: { retroRegion: ALL } });
    assert.deepEqual(linked.titles(), ["A regional topic story"]);
});

test("a visitor who hasn't picked a tab starts on the region the server guessed", async () => {
    const guessed = require("fs").readFileSync(process.env.PAGE_HTML, "utf8").split('<script id="home-data" type="application/json">')[1].split("</script>")[0];
    const page = await open(VIEWS);
    assert.equal(page.requests[0], "/api/headlines?region=" + JSON.parse(guessed));
});

test("d switches the theme and remembers it", async () => {
    const page = await open(VIEWS);
    const before = page.window.document.documentElement.getAttribute("data-theme");
    await page.key("d");
    const after = page.window.document.documentElement.getAttribute("data-theme");
    assert.deepEqual([before, after].sort(), ["dark", "light"]);
    assert.equal(page.window.localStorage.getItem("retroTheme"), after);
});

test("search finds a word in every tab's headlines and summaries, each headline once, saying where", async () => {
    const page = await open(VIEWS);
    await page.key("/");
    await page.type("storm");
    // The headline match first (it is in two tabs, listed once), then the one found in a summary
    assert.deepEqual(page.titles(), ["Storm warning issued", "A regional topic story"]);
    const where = [...page.window.document.querySelectorAll("#list .where")].map((w) => w.textContent);
    assert.equal(where.length, 2);
    assert.ok(where[1].includes(" / "), where[1]);  // A region and its topic
    assert.ok(page.window.document.querySelector("#list .snippet").textContent.includes("storm cleanup"));
    const marked = (selector) => [...page.window.document.querySelectorAll("#list " + selector + " mark")].map((m) => m.textContent);
    assert.deepEqual([marked(".title"), marked(".snippet")], [["Storm"], ["storm"]]);
    assert.equal(page.requests.filter((url) => url.startsWith("/api/search-index")).length, 1);
    await page.type("warn");
    assert.deepEqual(page.titles(), ["Storm warning issued"]);  // The start of a word
    await page.type("arning");
    assert.deepEqual(page.titles(), []);  // Not the middle of one
});

test("a link that isn't a web address is never listed", async () => {
    const page = await open({ [ALL + "/" + EVERYTHING]: [item("Bad link", 1, { link: "javascript:alert(1)" }), item("Good link", 2)] });
    assert.deepEqual(page.titles(), ["Good link"]);
    assert.equal(page.window.document.querySelector("#list a.title").protocol, "https:");
});

test("a headline's text is shown as text, whatever it holds", async () => {
    const page = await open({ [ALL + "/" + EVERYTHING]: [item("<img src=x onerror=alert(1)> & <b>bold</b>", 1, { link: "https://pub.example/x" })] });
    assert.deepEqual(page.titles(), ["<img src=x onerror=alert(1)> & <b>bold</b>"]);
    assert.equal(page.window.document.querySelectorAll("#list img:not(.site-icon), #list b").length, 0);
});

test("a tab that can't be loaded says so, and r loads it once the server answers", async () => {
    const page = await open(VIEWS, { failing: true });
    assert.deepEqual(page.titles(), []);
    assert.equal(page.$("notice").hidden, false);
    assert.match(page.$("notice").textContent, /could not load .*Headlines are not available yet/);
    page.failing = false;
    await page.key("r");
    assert.deepEqual(page.titles(), MIX.map((i) => i.title));
    assert.equal(page.$("notice").hidden, true);
});

test("an empty tab says there are no headlines", async () => {
    const page = await open({});
    assert.equal(page.$("notice").hidden, false);
    assert.match(page.$("notice").textContent, /no headlines for /);
});
