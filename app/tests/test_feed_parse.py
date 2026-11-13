"""Tests for reading a feed's items (services/feeds/parse.py): what is kept of each, and what is skipped."""

import feedparser

from services.feeds.parse import discussion_link, feed_text, parse


def test_a_feed_item_without_a_title_or_link_is_skipped():
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>
        <item><title>Bridge approved</title><link>https://pub.example/1</link></item>
        <item><link>https://pub.example/2</link><description>No title</description></item>
        <item><title>No link</title></item>
        </channel></rss>"""
    assert [h["title"] for h in parse(feed, "Feed")] == ["Bridge approved"]


def test_a_photo_credit_in_an_items_source_is_not_taken_for_its_publisher():
    # France 24 puts each photo's credit in <source>, pointing at the photo; Google News puts the publisher there
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>
        <item><title>Credit</title><link>https://pub.example/1</link>
            <source url="https://img.example/display/1.jpg">\xc2\xa9 Fred Tanneau, AFP</source></item>
        <item><title>Credit without the sign</title><link>https://pub.example/2</link>
            <source url="https://img.example/w:1024/2.JPEG">Stringer, Reuters</source></item>
        <item><title>Publisher</title><link>https://pub.example/3</link>
            <source url="https://www.bbc.co.uk">BBC</source></item>
        </channel></rss>"""
    assert [h["source"] for h in parse(feed, "France 24 Europe")] == ["France 24 Europe", "France 24 Europe", "BBC"]


def test_comments_link_to_the_article_itself_is_dropped():
    article = "https://mexiconewsdaily.com/news/story/"
    assert discussion_link(article, article) is None
    assert discussion_link(article, article + "#comments") is None
    assert discussion_link(article, None) is None
    assert discussion_link("https://hn.example/a", "https://news.ycombinator.com/item?id=1") == "https://news.ycombinator.com/item?id=1"


def test_feed_text_prefers_the_full_content_as_plain_text():
    xml = """<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>
      <title>T</title><link>https://pub.example/1</link><description>Short teaser</description>
      <content:encoded><![CDATA[<p>First   paragraph.</p><p>Second &amp; last.</p>]]></content:encoded>
    </item><item><title>U</title><link>https://pub.example/2</link></item></channel></rss>"""
    entries = feedparser.parse(xml).entries
    assert feed_text(entries[0]) == "First paragraph.Second & last."
    assert feed_text(entries[1]) == ""
