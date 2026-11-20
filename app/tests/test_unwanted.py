"""Headlines that aren't news to read (services/feeds/unwanted.py): video and podcast pages, horoscopes, puzzle
answers, lottery numbers. Examples from the feeds of Nov 13-20, 2026."""

import pytest

from services.feeds.parse import parse
from services.feeds.unwanted import drop_unwanted, unwanted


@pytest.mark.parametrize("title, link", [
    # Video pages: nothing to read (and nothing to summarize: a playlist of clip lengths)
    ("Police confront protesters in clashes across France and Germany", "https://www.aljazeera.com/video/newsfeed/2026/11/15/police-confront-protesters"),
    ("Will the Eurovision Song Contest succeed in Asia?", "https://www.bbc.co.uk/news/videos/cxvg23n59y1ro?at_medium=RSS"),
    ("Have the Maple Leafs hit rock bottom?", "https://www.tsn.ca/nhl/video/2026/11/18/have-the-maple-leafs-hit-rock-bottom/"),
    # Podcast episodes: something to listen to
    ("When EU foreign policy jobs collide", "https://www.politico.eu/podcast/brussels-playbook-podcast/when-eu-foreign-policy"),
    ("The Ultraviolent, Wildly Successful, Uniquely American Phenomenon of Grand Theft Auto", "https://www.nytimes.com/2026/11/15/podcasts/the-daily/the-ultraviolent"),
    # Horoscopes
    ("Cancer Horoscope Today: November 14, 2026", "https://www.vogue.in/content/cancer-horoscope-today-november-14-2026"),
    ("Your Love Horoscope For November 13: A Relationship Rebuilding Phase Begins", "https://www.yourtango.com/zodiac/love-horoscope-friday"),
    ("Horoscopes for the week ahead", "https://pub.example/1"),
    # Puzzle hints and answers
    ("Today’s NYT Connections Hints And Answers For Saturday, November 14", "https://www.forbes.com/sites/paultassi/2026/11/13/x"),
    ("NYT Strands Hints for November 13, 2026", "https://www.nytimes.com/2026/11/12/crosswords/strands-sidekick-985.html"),
    ("Wordle today: Answer and hints for November 15", "https://pub.example/2"),
    ("Hints and answers for the NYT Mini crossword, Nov 16", "https://pub.example/3"),
    # Lottery numbers
    ("Lotto results: Five players have won Lotto First Division tonight", "https://www.nzherald.co.nz/nz/lotto-results-five-players"),
    ("Powerball winning numbers for Saturday, November 15", "https://pub.example/4"),
])
def test_not_news_to_read(title, link):
    assert unwanted(title, link)


@pytest.mark.parametrize("title, link", [
    # Articles with a clip in them, or about videos and podcasts
    ("Watch: Warner addresses fresh sandpaper drama", "https://www.couriermail.com.au/sport/cricket/david-warner-live"),
    ("Darren Lim and Evelyn Tan open up about marriage", "https://www.asiaone.com/entertainment/darren-lim-evelyn-tan-podcast-marriage"),
    ("Heidi Gardner's Late Boyfriend Ray Tintori Recorded Podcast Days Before Death", "https://www.tmz.com/2026/11/17/ray-tintori-recorded-podcast-before-death/"),
    ("YouTube changes how video creators are paid", "https://pub.example/youtube-video-creators"),
    # Not horoscopes or puzzles
    ("I got a tarot reading from Google’s smart glasses at Gentle Monster’s pop-up", "https://www.tomsguide.com/computing/smart-glasses/x"),
    ("Leo Varadkar steps down as Fine Gael leader", "https://pub.example/5"),
    ("Investigators look into the minister's connections to the firm", "https://pub.example/6"),
    ("Crossword setter Araucaria's papers go to the British Library", "https://pub.example/7"),
    ("Minister answers questions over his connections to the firm", "https://pub.example/8"),
    # Lottery news
    ("Toronto Tempo win WNBA draft lottery, earn No. 1 pick", "https://www.nbcsports.com/wnba/news/toronto-tempo-win-wnba-draft-lottery"),
    ("Husband guessed $61 - Dunedin woman’s Lotto prize was a lot bigger than that", "https://www.odt.co.nz/news/dunedin/husband-guessed"),
])
def test_news(title, link):
    assert not unwanted(title, link)


def test_a_feed_leaves_them_out():
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>
        <item><title>Bridge approved</title><link>https://pub.example/1</link></item>
        <item><title>Aries Horoscope Today: November 15, 2026</title><link>https://pub.example/2</link></item>
        <item><title>Police confront protesters</title><link>https://www.aljazeera.com/video/newsfeed/2026/11/15/x</link></item>
        </channel></rss>"""
    assert [h["title"] for h in parse(feed, "Feed")] == ["Bridge approved"]


def test_views_lose_them_once_links_are_resolved():
    # A Google News item's real address is only known once resolved
    news = {"title": "Bridge approved", "link": "https://pub.example/1"}
    clip = {"title": "Hyman scores his 500th point", "link": "https://www.tsn.ca/nhl/video/2026/11/18/hyman-500th"}
    views = {"all": [news, clip], "asia": [clip]}
    assert drop_unwanted(views) == {"all": [news], "asia": []}
