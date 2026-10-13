"""
Tests for the summarizer (which articles it summarizes, what it stores) and for
the refresher adding stored summaries to published items.
"""

import json

import httpx
import pytest
import respx

import summarizer
from config import Catalog, browser_host_key, feed_text_key, settings, snapshot_key, summary_key
from refresher import publish
from summarizer import LLMUnavailable, summarize_new, top_items

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
    monkeypatch.setattr(summarizer, "CATALOG", CATALOG)
    monkeypatch.setattr(settings, "SUMMARIZER_LLM_URL", LLM)
    monkeypatch.setattr(settings, "SUMMARY_TOP_N", 2)


def llm_reply(text="The council approved a new bridge."):
    return httpx.Response(200, json={"message": {"role": "assistant", "content": text}})


def llm_up(text="The council approved a new bridge."):
    """Mocks a running LLM server; returns the chat route."""
    respx.get(f"{LLM}/api/tags").mock(return_value=httpx.Response(200, json={"models": []}))
    return respx.post(f"{LLM}/api/chat").mock(return_value=llm_reply(text))


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
    respx.get(summarizer.ARCHIVE + "https://pub.example/2").mock(return_value=httpx.Response(404))
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
async def test_llm_down_leaves_articles_for_later(mock_redis):
    await put_view(mock_redis, "all", [1])
    article = respx.get("https://pub.example/1").mock(return_value=httpx.Response(200, html=ARTICLE))
    respx.get(f"{LLM}/api/tags").mock(side_effect=httpx.ConnectError("unreachable"))

    async with httpx.AsyncClient() as client:
        with pytest.raises(LLMUnavailable):
            await summarize_new(client, mock_redis)
    assert await mock_redis.get(summary_key("https://pub.example/1")) is None  # Retried next pass
    assert article.call_count == 0  # Nothing is fetched while the LLM is down


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
    respx.get(summarizer.ARCHIVE + paid).mock(return_value=httpx.Response(200, html=ARTICLE))
    respx.get(missing).mock(return_value=httpx.Response(403))
    respx.get(summarizer.ARCHIVE + missing).mock(return_value=httpx.Response(404))  # No snapshot yet
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
    from services.feed_service import feed_text
    xml = """<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>
      <title>T</title><link>https://pub.example/1</link><description>Short teaser</description>
      <content:encoded><![CDATA[<p>First   paragraph.</p><p>Second &amp; last.</p>]]></content:encoded>
    </item><item><title>U</title><link>https://pub.example/2</link></item></channel></rss>"""
    entries = feedparser.parse(xml).entries
    assert feed_text(entries[0]) == "First paragraph.Second & last."
    assert feed_text(entries[1]) == ""
