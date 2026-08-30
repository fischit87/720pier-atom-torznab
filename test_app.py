import sys
import types
import unittest
import xml.etree.ElementTree as ET

try:
    import fastapi  # noqa: F401
except ModuleNotFoundError:
    fastapi = types.ModuleType("fastapi")
    class F:
        def __init__(self, *a, **k): pass
        def get(self, *a, **k): return lambda fn: fn
    class H(Exception): pass
    fastapi.FastAPI, fastapi.HTTPException = F, H
    fastapi.Query = lambda default=None, **kwargs: default
    responses = types.ModuleType("fastapi.responses")
    responses.Response = object
    sys.modules["fastapi"] = fastapi
    sys.modules["fastapi.responses"] = responses

from app import FeedItem, _terms, parse_feed, parse_topic

FEED = b'''<feed xmlns="http://www.w3.org/2005/Atom">
<entry><published>2026-08-29T08:03:53+02:00</published><id>https://720pier.ru/viewtopic.php?p=10</id><link href="https://720pier.ru/viewtopic.php?p=10"/><title>NFL &#8226; NFL 2026 / Seattle Seahawks @ Kansas City Chiefs [1080p]</title><content type="html">&lt;img alt="game.mkv.torrent" /&gt;</content></entry>
<entry><published>2026-08-29T08:21:42+02:00</published><id>https://720pier.ru/viewtopic.php?p=11</id><link href="https://720pier.ru/viewtopic.php?p=11"/><title>NFL &#8226; NFL 2026 / Seattle Seahawks @ Kansas City Chiefs [1080p]</title><content type="html">Thank you</content></entry>
</feed>'''
TOPIC = b'''<html><table><tr><td>game.mkv.torrent</td><td><a href="/download/torrent?id=75142">Download</a></td><td><dfn>Seeders</dfn><span>29</span></td><td><dfn>Leechers</dfn><span>6</span></td><td><span title="8 818 881 909 Bytes">8.21 GiB</span></td></tr></table></html>'''

class Tests(unittest.TestCase):
    def test_feed_ignores_replies(self):
        items = parse_feed(FEED)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].title.startswith("NFL 2026"))
        self.assertEqual(items[0].attachment_name, "game.mkv.torrent")

    def test_topic_metadata(self):
        item = parse_feed(FEED)[0]
        result = parse_topic(TOPIC, item)
        self.assertEqual(result.download_url, "https://720pier.ru/download/torrent?id=75142")
        self.assertEqual(result.size, 8818881909)
        self.assertEqual(result.seeders, 29)
        self.assertEqual(result.peers, 6)

    def test_matchup_separator(self):
        self.assertEqual(_terms("Seattle Seahawks vs Kansas City Chiefs"), ["seattle", "seahawks", "kansas", "city", "chiefs"])

if __name__ == "__main__": unittest.main()

