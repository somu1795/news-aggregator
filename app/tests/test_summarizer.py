"""
Tests for the summarizer (which articles it summarizes, what it stores) and for
the refresher adding stored summaries to published items.
"""

import asyncio
import json

import httpx
import pytest
import respx

import catalog as catalog_module
import summarizer
from services.summaries import reading
from catalog import Catalog
from config import settings
from keys import article_text_key, browser_host_key, feed_text_key, full_page_host_key, snapshot_key, summary_key
from refresher import publish
from summarizer import LLMUnavailable, read_ahead, summarize_ready, top_items

pytestmark = pytest.mark.asyncio

CATALOG = Catalog(
    regions=[{"id": "asia", "label": "Asia", "sources": [{"name": "A", "url": "https://a.example/rss", "weight": 1.0}]}],
    extras=[],
    topics=[{"id": "sport", "label": "Sport"}],
)
LLM = "http://llm.example:11434"
ARTICLE = "<html><body><article>" + "".join(
    f"<p>Paragraph {i}: the council debated the bridge plan, its cost and its route for hours on end.</p>" for i in range(20)
) + "</article></body></html>"  # Distinct paragraphs: the extractor drops repeated ones


def item(n):
    return {"title": f"Story {n}", "link": f"https://pub.example/{n}", "source": "Pub", "published": 0, "last_updated": 0}


async def put_view(redis, view, numbers):
    await redis.set(snapshot_key(view), json.dumps({"data": [item(n) for n in numbers]}))


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """Every host is public, except *.internal.example (a LAN address); IP literals are themselves."""
    import ipaddress
    import services.safe_fetch as safe_fetch

    async def resolve(host, port):
        try:
            return [str(ipaddress.ip_address(host))]
        except ValueError:
            return ["192.168.1.1"] if host.endswith("internal.example") else ["93.184.216.34"]
    monkeypatch.setattr(safe_fetch, "_resolve", resolve)


@pytest.fixture(autouse=True)
def summarizer_settings(monkeypatch):
    monkeypatch.setattr(catalog_module, "_catalog", CATALOG)
    monkeypatch.setattr(settings, "SUMMARIZER_LLM_URL", LLM)
    monkeypatch.setattr(settings, "SUMMARY_TOP_N", 2)


def llm_reply(text="The council approved a new bridge."):
    return httpx.Response(200, json={"message": {"role": "assistant", "content": text}})


def llm_up(text="The council approved a new bridge."):
    """Mocks a running LLM server; returns the chat route."""
    respx.get(f"{LLM}/api/tags").mock(return_value=httpx.Response(200, json={"models": []}))
    return respx.post(f"{LLM}/api/chat").mock(return_value=llm_reply(text))


async def summarize_new(client, redis):
    """A read pass, then a summary pass: what the summarizer's two loops do between them."""
    await read_ahead(client, redis)
    return await summarize_ready(client, redis)


async def test_top_items_main_views_first_by_position(mock_redis):
    await put_view(mock_redis, "all", [1, 2, 3])
    await put_view(mock_redis, "asia", [4, 1, 5])
    await put_view(mock_redis, "all:sport", [6, 7])
    links = [i["link"].rsplit("/", 1)[1] for i in await top_items(mock_redis)]
    # Rank 1 of each main view, then rank 2, then the topic views; only the top 2; no duplicates
    assert links == ["1", "4", "2", "6", "7"]


@respx.mock
async def test_summarizes_new_articles_and_marks_unusable_ones(mock_redis):
    await put_view(mock_redis, "all", [1, 2])
    await put_view(mock_redis, "asia", [3])
    await mock_redis.set(summary_key("https://pub.example/3"), json.dumps({"text": "Already done."}))
    respx.get("https://pub.example/1").mock(return_value=httpx.Response(200, html=ARTICLE))
    respx.get("https://pub.example/2").mock(return_value=httpx.Response(200, html="<p>Subscribe to read</p>"))
    respx.get(reading.ARCHIVE + "https://pub.example/2").mock(return_value=httpx.Response(404))
    llm = llm_up("<think></think>The council approved a new bridge.")

    async with httpx.AsyncClient() as client:
        assert await summarize_new(client, mock_redis) == 1

    stored = json.loads(await mock_redis.get(summary_key("https://pub.example/1")))
    assert stored == {"text": "The council approved a new bridge.", "model": settings.SUMMARIZER_MODEL, "via": "direct"}
    # Story 2 also had no feed text and no archive snapshot
    assert json.loads(await mock_redis.get(summary_key("https://pub.example/2"))) == {
        "failed": "too little text; feed: none; archive: HTTP 404"}
    assert llm.call_count == 1  # Story 3 already had a summary
    request = json.loads(llm.calls.last.request.content)
    assert request["think"] is False and "Story 1" in request["messages"][1]["content"]


@respx.mock
async def test_articles_are_read_while_the_llm_is_down(mock_redis):
    await put_view(mock_redis, "all", [1])
    respx.get("https://pub.example/1").mock(return_value=httpx.Response(200, html=ARTICLE))
    respx.get(f"{LLM}/api/tags").mock(side_effect=httpx.ConnectError("unreachable"))

    async with httpx.AsyncClient() as client:
        assert await read_ahead(client, mock_redis) == 1
        with pytest.raises(LLMUnavailable):
            await summarize_ready(client, mock_redis)
    assert await mock_redis.get(summary_key("https://pub.example/1")) is None  # Summarized once the LLM is back
    read = json.loads(await mock_redis.get(article_text_key("https://pub.example/1")))
    assert read["via"] == "direct" and "the bridge plan" in read["text"]
    assert 0 < await mock_redis.ttl(article_text_key("https://pub.example/1")) <= settings.ARTICLE_TEXT_TTL_SECONDS


@respx.mock
async def test_read_articles_are_summarized_without_reading_them_again(mock_redis):
    await put_view(mock_redis, "all", [1])
    link = "https://pub.example/1"
    await mock_redis.set(article_text_key(link), json.dumps({"text": "The council met. " * 50, "via": "browser"}))
    page = respx.get(link).mock(return_value=httpx.Response(200, html=ARTICLE))
    llm = llm_up()

    async with httpx.AsyncClient() as client:
        assert await read_ahead(client, mock_redis) == 0  # Already read
        assert await summarize_ready(client, mock_redis) == 1

    assert page.call_count == 0
    assert "The council met." in json.loads(llm.calls.last.request.content)["messages"][1]["content"]
    assert json.loads(await mock_redis.get(summary_key(link)))["via"] == "browser"
    assert await mock_redis.get(article_text_key(link)) is None  # Not needed once summarized


async def test_unread_articles_wait_for_their_text(mock_redis):
    await put_view(mock_redis, "all", [1])
    async with httpx.AsyncClient() as client:
        assert await summarize_ready(client, mock_redis) == 0  # Nothing read: the LLM isn't even asked


@respx.mock
async def test_summaries_go_by_the_tabs_order_a_batch_at_a_time(mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "SUMMARY_TOP_N", 3)
    monkeypatch.setattr(settings, "SUMMARY_BATCH", 2)
    await put_view(mock_redis, "all", [1, 2, 3])
    for n in (3, 1, 2):  # Read in any order
        await mock_redis.set(article_text_key(f"https://pub.example/{n}"), json.dumps({"text": f"Text {n}. " * 100, "via": "direct"}))
    llm = llm_up()

    async with httpx.AsyncClient() as client:
        assert await summarize_ready(client, mock_redis) == 2
        await put_view(mock_redis, "all", [4, 3])  # The tabs changed; story 4 isn't read yet
        assert await summarize_ready(client, mock_redis) == 1

    asked = [json.loads(call.request.content)["messages"][1]["content"].split("\n")[0] for call in llm.calls]
    assert asked == ["Headline: Story 1", "Headline: Story 2", "Headline: Story 3"]


def slow_page(active, peak):
    """An article page that takes a moment, counting how many are being read at once."""
    async def respond(request):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0.05)
        active[0] -= 1
        return httpx.Response(200, html=ARTICLE)
    return respond


@respx.mock
async def test_several_sites_are_read_at_once(mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "SUMMARY_TOP_N", 4)
    monkeypatch.setattr(settings, "SUMMARY_READ_CONCURRENCY", 3)
    links = [f"https://site{n}.example/a" for n in range(4)]
    await mock_redis.set(snapshot_key("all"), json.dumps({"data": [{**item(n), "link": link} for n, link in enumerate(links)]}))
    active, peak = [0], [0]
    respx.get(url__regex=r"https://site\d\.example/a").mock(side_effect=slow_page(active, peak))

    async with httpx.AsyncClient() as client:
        assert await read_ahead(client, mock_redis) == 4
    assert peak[0] == 3  # At most SUMMARY_READ_CONCURRENCY


@respx.mock
async def test_one_site_is_read_one_article_at_a_time(mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "SUMMARY_TOP_N", 3)
    await put_view(mock_redis, "all", [1, 2, 3])
    active, peak = [0], [0]
    respx.get(url__regex=r"https://pub\.example/\d").mock(side_effect=slow_page(active, peak))

    async with httpx.AsyncClient() as client:
        assert await read_ahead(client, mock_redis) == 3
    assert peak[0] == 1


async def test_published_items_carry_their_summary(mock_redis):
    await mock_redis.set(summary_key("https://pub.example/1"), json.dumps({"text": "A summary.", "model": "m"}))
    await mock_redis.set(summary_key("https://pub.example/2"), json.dumps({"failed": "HTTP 403"}))
    await publish(mock_redis, {"all": [item(1), item(2), item(3)]}, 0.0)
    data = json.loads(await mock_redis.get(snapshot_key("all")))["data"]
    assert [i["summary"] for i in data] == ["A summary.", None, None]


@respx.mock
async def test_paywalled_articles_are_read_from_the_archive(mock_redis):
    paid, missing = "https://www.nytimes.com/2026/a.html", "https://www.ft.com/content/b"
    await mock_redis.set(snapshot_key("all"), json.dumps({"data": [
        {**item(1), "link": paid}, {**item(2), "link": missing}]}))
    respx.get(paid).mock(return_value=httpx.Response(403))
    respx.get(reading.ARCHIVE + paid).mock(return_value=httpx.Response(200, html=ARTICLE))
    respx.get(missing).mock(return_value=httpx.Response(403))
    respx.get(reading.ARCHIVE + missing).mock(return_value=httpx.Response(404))  # No snapshot yet
    llm_up()

    async with httpx.AsyncClient() as client:
        assert await summarize_new(client, mock_redis) == 1

    assert json.loads(await mock_redis.get(summary_key(paid)))["text"] == "The council approved a new bridge."
    assert json.loads(await mock_redis.get(summary_key(paid)))["via"] == "archive"
    assert json.loads(await mock_redis.get(summary_key(missing))) == {"failed": "HTTP 403; archive: HTTP 404; feed: none"}
    assert 0 < await mock_redis.ttl(summary_key(missing)) <= settings.SUMMARY_RETRY_SECONDS  # Retried sooner


@respx.mock
async def test_links_to_private_addresses_are_never_fetched(mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "SUMMARY_TOP_N", 3)
    # A public page redirecting into the LAN, a tailnet address, and a host that resolves to the LAN
    links = ["https://evil.example/r", "http://100.100.1.2:11434/", "http://router.internal.example/"]
    await mock_redis.set(snapshot_key("all"), json.dumps({"data": [{**item(i), "link": link} for i, link in enumerate(links)]}))
    respx.get("https://evil.example/r").mock(return_value=httpx.Response(302, headers={"location": "http://192.168.1.1/admin"}))
    lan = respx.get(url__regex=r"http://(192\.168|100\.100|router)").mock(return_value=httpx.Response(200, html=ARTICLE))
    llm = llm_up()

    async with httpx.AsyncClient() as client:
        assert await summarize_new(client, mock_redis) == 0

    assert lan.call_count == 0 and llm.call_count == 0
    for link in links:
        assert json.loads(await mock_redis.get(summary_key(link))) == {"failed": "not a public address"}


@respx.mock
async def test_a_redirect_without_a_location_fails_only_its_article(mock_redis):
    await put_view(mock_redis, "all", [1, 2])
    respx.get("https://pub.example/1").mock(return_value=httpx.Response(302))
    respx.get(reading.ARCHIVE + "https://pub.example/1").mock(return_value=httpx.Response(404))
    respx.get("https://pub.example/2").mock(return_value=httpx.Response(200, html=ARTICLE))
    llm_up()

    async with httpx.AsyncClient() as client:
        assert await summarize_new(client, mock_redis) == 1

    assert json.loads(await mock_redis.get(summary_key("https://pub.example/1"))) == {"failed": "HTTP 302; feed: none; archive: HTTP 404"}
    assert json.loads(await mock_redis.get(summary_key("https://pub.example/2")))["via"] == "direct"


@respx.mock
async def test_summaries_with_links_are_rejected(mock_redis):
    await put_view(mock_redis, "all", [1])
    respx.get("https://pub.example/1").mock(return_value=httpx.Response(200, html=ARTICLE))
    llm_up("Great deals at https://scam.example now.")

    async with httpx.AsyncClient() as client:
        assert await summarize_new(client, mock_redis) == 0
    assert json.loads(await mock_redis.get(summary_key("https://pub.example/1"))) == {"failed": "summary contains a link"}


BROWSER, PROXY = "http://browser.example:8191", "http://proxy.example:4750"


@pytest.fixture
def with_browser(monkeypatch):
    monkeypatch.setattr(settings, "SUMMARIZER_BROWSER_URL", BROWSER)
    monkeypatch.setattr(settings, "SUMMARIZER_BROWSER_PROXY", PROXY)


def browser_page(html=ARTICLE, status=200):
    return httpx.Response(200, json={"status": "ok", "message": "", "solution": {"status": status, "response": html}})


@respx.mock
async def test_blocking_sites_are_read_with_the_browser_and_remembered(mock_redis, with_browser):
    await put_view(mock_redis, "all", [1, 2])
    # AWS WAF's bot check answers 202 with an empty challenge page
    first = respx.get("https://pub.example/1").mock(return_value=httpx.Response(202, headers={"x-amzn-waf-action": "challenge"}))
    second = respx.get("https://pub.example/2").mock(return_value=httpx.Response(200, html=ARTICLE))
    browser = respx.post(f"{BROWSER}/v1").mock(return_value=browser_page())
    llm_up()

    async with httpx.AsyncClient() as client:
        assert await summarize_new(client, mock_redis) == 2

    assert json.loads(await mock_redis.get(summary_key("https://pub.example/1")))["via"] == "browser"
    # Story 2 is on the same (now remembered) site: straight to the browser, no plain request
    assert json.loads(await mock_redis.get(summary_key("https://pub.example/2")))["via"] == "browser"
    assert first.call_count == 1 and second.call_count == 0 and browser.call_count == 2
    sent = json.loads(browser.calls[0].request.content)
    assert sent["cmd"] == "request.get" and sent["url"] == "https://pub.example/1" and sent["proxy"] == {"url": PROXY}
    assert 0 < await mock_redis.ttl(browser_host_key("pub.example")) <= settings.BROWSER_HOST_TTL_SECONDS


@respx.mock
async def test_feed_text_when_neither_the_page_nor_the_browser_can_be_read(mock_redis, with_browser):
    await put_view(mock_redis, "all", [1])
    await mock_redis.set(feed_text_key("https://pub.example/1"), "The council met. " * 50)
    respx.get("https://pub.example/1").mock(return_value=httpx.Response(403))
    respx.post(f"{BROWSER}/v1").mock(return_value=httpx.Response(
        200, json={"status": "error", "message": "Error solving the challenge. Timeout after 60.0 seconds."}))
    llm = llm_up()

    async with httpx.AsyncClient() as client:
        assert await summarize_new(client, mock_redis) == 1

    assert json.loads(await mock_redis.get(summary_key("https://pub.example/1")))["via"] == "feed"
    assert "The council met." in json.loads(llm.calls.last.request.content)["messages"][1]["content"]


@respx.mock
async def test_the_browser_never_gets_private_addresses(mock_redis, with_browser):
    link = "http://router.internal.example/admin"
    await mock_redis.set(snapshot_key("all"), json.dumps({"data": [{**item(1), "link": link}]}))
    await mock_redis.set(browser_host_key("router.internal.example"), 1)  # Even a remembered "blocking" site
    browser = respx.post(f"{BROWSER}/v1").mock(return_value=browser_page())
    llm_up()

    async with httpx.AsyncClient() as client:
        assert await summarize_new(client, mock_redis) == 0

    assert browser.call_count == 0
    assert json.loads(await mock_redis.get(summary_key(link))) == {"failed": "not a public address"}


async def test_refresher_keeps_feed_texts_long_enough_to_summarize(mock_redis):
    long_text, short_text = "Long article text. " * 40, "A teaser."
    items = [{**item(1), "feed_text": long_text}, {**item(2), "feed_text": short_text}, item(3)]
    await publish(mock_redis, {"all": items}, 0.0)
    assert await mock_redis.get(feed_text_key("https://pub.example/1")) == long_text
    assert 0 < await mock_redis.ttl(feed_text_key("https://pub.example/1")) <= settings.FEED_TEXT_TTL_SECONDS
    assert await mock_redis.get(feed_text_key("https://pub.example/2")) is None
    published = json.loads(await mock_redis.get(snapshot_key("all")))["data"]
    assert all("feed_text" not in i for i in published)  # Internal: never sent to readers


def test_feed_text_prefers_the_full_content_as_plain_text():
    import feedparser
    from services.feeds.parse import feed_text
    xml = """<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>
      <title>T</title><link>https://pub.example/1</link><description>Short teaser</description>
      <content:encoded><![CDATA[<p>First   paragraph.</p><p>Second &amp; last.</p>]]></content:encoded>
    </item><item><title>U</title><link>https://pub.example/2</link></item></channel></rss>"""
    entries = feedparser.parse(xml).entries
    assert feed_text(entries[0]) == "First paragraph.Second & last."
    assert feed_text(entries[1]) == ""


@respx.mock
async def test_the_second_browser_tries_when_the_first_fails(mock_redis, with_browser, monkeypatch):
    browser2_url = "http://browser2.example:8191"
    monkeypatch.setattr(settings, "SUMMARIZER_BROWSER2_URL", browser2_url)
    await put_view(mock_redis, "all", [1])
    await mock_redis.set(feed_text_key("https://pub.example/1"), "The council met. " * 50)  # Not needed: browser2 wins
    respx.get("https://pub.example/1").mock(return_value=httpx.Response(403))
    first = respx.post(f"{BROWSER}/v1").mock(return_value=browser_page("<p>Checking your connection</p>"))
    second = respx.post(f"{browser2_url}/v1").mock(return_value=browser_page())
    llm_up()

    async with httpx.AsyncClient() as client:
        assert await summarize_new(client, mock_redis) == 1

    assert first.call_count == 1 and second.call_count == 1
    assert json.loads(second.calls[0].request.content)["proxy"] == {"url": PROXY}
    assert json.loads(await mock_redis.get(summary_key("https://pub.example/1")))["via"] == "browser2"


FILTERED_PROXY = "http://filtered-proxy.example:4750"


def browser_by_proxy(pages):
    """A browser whose page depends on the proxy each request names."""
    return lambda request: browser_page(pages[json.loads(request.content)["proxy"]["url"]])


@respx.mock
async def test_the_browser_loads_pages_without_ads_first(mock_redis, with_browser, monkeypatch):
    monkeypatch.setattr(settings, "SUMMARIZER_BROWSER_FILTERED_PROXY", FILTERED_PROXY)
    await put_view(mock_redis, "all", [1])
    respx.get("https://pub.example/1").mock(return_value=httpx.Response(403))
    browser = respx.post(f"{BROWSER}/v1").mock(side_effect=browser_by_proxy({FILTERED_PROXY: ARTICLE, PROXY: ARTICLE}))
    llm_up()

    async with httpx.AsyncClient() as client:
        assert await summarize_new(client, mock_redis) == 1

    assert [json.loads(call.request.content)["proxy"]["url"] for call in browser.calls] == [FILTERED_PROXY]
    assert json.loads(await mock_redis.get(summary_key("https://pub.example/1")))["via"] == "browser"
    assert not await mock_redis.exists(full_page_host_key("pub.example"))


@respx.mock
async def test_a_site_that_needs_its_ads_gets_the_whole_page_and_is_remembered(mock_redis, with_browser, monkeypatch):
    monkeypatch.setattr(settings, "SUMMARIZER_BROWSER_FILTERED_PROXY", FILTERED_PROXY)
    await put_view(mock_redis, "all", [1, 2])
    await mock_redis.set(browser_host_key("pub.example"), 1)  # Straight to the browser
    # Without what the proxy refuses, the article never appears
    browser = respx.post(f"{BROWSER}/v1").mock(
        side_effect=browser_by_proxy({FILTERED_PROXY: "<p>Loading…</p>", PROXY: ARTICLE}))
    llm_up()

    async with httpx.AsyncClient() as client:
        assert await summarize_new(client, mock_redis) == 2

    # Story 1: without ads, then the whole page; story 2, same site: the whole page at once
    proxies = [json.loads(call.request.content)["proxy"]["url"] for call in browser.calls]
    assert proxies == [FILTERED_PROXY, PROXY, PROXY]
    for n in (1, 2):
        assert json.loads(await mock_redis.get(summary_key(f"https://pub.example/{n}")))["via"] == "browser-full"
    assert 0 < await mock_redis.ttl(full_page_host_key("pub.example")) <= settings.BROWSER_HOST_TTL_SECONDS


@respx.mock
async def test_a_site_is_not_remembered_when_the_whole_page_fails_too(mock_redis, with_browser, monkeypatch):
    monkeypatch.setattr(settings, "SUMMARIZER_BROWSER_FILTERED_PROXY", FILTERED_PROXY)
    await put_view(mock_redis, "all", [1])
    await mock_redis.set(browser_host_key("pub.example"), 1)
    await mock_redis.set(feed_text_key("https://pub.example/1"), "The council met. " * 50)
    respx.post(f"{BROWSER}/v1").mock(return_value=browser_page("<p>Checking your connection</p>"))
    llm_up()

    async with httpx.AsyncClient() as client:
        assert await summarize_new(client, mock_redis) == 1

    assert json.loads(await mock_redis.get(summary_key("https://pub.example/1")))["via"] == "feed"
    assert not await mock_redis.exists(full_page_host_key("pub.example"))

@respx.mock
async def test_each_browser_reads_one_page_at_a_time(mock_redis, with_browser, monkeypatch):
    monkeypatch.setattr(settings, "SUMMARY_TOP_N", 3)
    links = [f"https://site{n}.example/a" for n in range(3)]
    await mock_redis.set(snapshot_key("all"), json.dumps({"data": [{**item(n), "link": link} for n, link in enumerate(links)]}))
    for n in range(3):
        await mock_redis.set(browser_host_key(f"site{n}.example"), 1)  # All three need the browser
    active, peak = [0], [0]

    async def page(request):  # A page can take most of the browser's memory
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0.05)
        active[0] -= 1
        return browser_page()
    respx.post(f"{BROWSER}/v1").mock(side_effect=page)

    async with httpx.AsyncClient() as client:
        assert await read_ahead(client, mock_redis) == 3
    assert peak[0] == 1


TAGGER = "http://tagger.test"


@respx.mock
async def test_unsure_story_pairs_are_judged_and_sent_back(monkeypatch):
    monkeypatch.setattr(settings, "TAGGER_URL", TAGGER)
    pairs = [{"a": "Big dogs age faster", "b": "Why small dogs live longer", "similarity": 0.9},
             {"a": "City v PSG preview", "b": "City v Ipswich preview", "similarity": 0.91}]
    respx.get(f"{TAGGER}/story-candidates").mock(side_effect=[httpx.Response(200, json={"pairs": pairs}),
                                                              httpx.Response(200, json={"pairs": []})])
    respx.post(f"{LLM}/api/chat").mock(side_effect=[llm_reply("Yes"), llm_reply("no")])
    stored = respx.post(f"{TAGGER}/story-verdicts").mock(return_value=httpx.Response(200, json={"stored": 2}))
    async with httpx.AsyncClient() as client:
        assert await summarizer.judge_stories(client) == 2
    verdicts = json.loads(stored.calls.last.request.content)["verdicts"]
    assert [(v["a"], v["same"]) for v in verdicts] == [("Big dogs age faster", True), ("City v PSG preview", False)]


@respx.mock
async def test_story_judging_stops_quietly_without_the_tagger(monkeypatch):
    monkeypatch.setattr(settings, "TAGGER_URL", TAGGER)
    respx.get(f"{TAGGER}/story-candidates").mock(side_effect=httpx.ConnectError("unreachable"))
    async with httpx.AsyncClient() as client:
        assert await summarizer.judge_stories(client) == 0


@respx.mock
async def test_story_judging_reports_the_llm_down(monkeypatch):
    monkeypatch.setattr(settings, "TAGGER_URL", TAGGER)
    respx.get(f"{TAGGER}/story-candidates").mock(
        return_value=httpx.Response(200, json={"pairs": [{"a": "x", "b": "y", "similarity": 0.9}]}))
    respx.post(f"{LLM}/api/chat").mock(side_effect=httpx.ConnectError("unreachable"))
    verdicts = respx.post(f"{TAGGER}/story-verdicts")
    async with httpx.AsyncClient() as client:
        with pytest.raises(LLMUnavailable):
            await summarizer.judge_stories(client)
    assert not verdicts.called


class Stop(BaseException):  # Not an Exception: the loop logs those and carries on
    pass


async def run_summary_loop(monkeypatch, summarized, judged):
    """Runs summary_loop over the given results of each pass; returns its pauses."""
    passes, judgings, pauses = iter(summarized), iter(judged), []

    async def summarize_ready(client, redis):
        try:
            return next(passes)
        except StopIteration:
            raise Stop

    async def judge_stories(client):
        return next(judgings)

    async def sleep(seconds):
        pauses.append(seconds)
    monkeypatch.setattr(summarizer, "summarize_ready", summarize_ready)
    monkeypatch.setattr(summarizer, "judge_stories", judge_stories)
    monkeypatch.setattr(summarizer.asyncio, "sleep", sleep)
    with pytest.raises(Stop):
        await summarizer.summary_loop(None, None)
    return pauses


async def test_after_judging_story_pairs_it_looks_for_articles_at_once(monkeypatch):
    # Nothing to summarize, so pairs are judged; articles read meanwhile are next, without a pause
    assert await run_summary_loop(monkeypatch, summarized=[0, 3], judged=[12]) == []


async def test_it_pauses_only_when_there_was_nothing_to_do(monkeypatch):
    assert await run_summary_loop(monkeypatch, summarized=[0], judged=[0]) == [settings.SUMMARY_INTERVAL_SECONDS]
