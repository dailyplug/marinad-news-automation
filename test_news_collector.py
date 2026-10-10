import unittest
from datetime import datetime, timezone, timedelta
import news_collector as nc

class NormalizationTests(unittest.TestCase):
    def test_clean_text_collapses_whitespace(self):
        self.assertEqual(nc.clean_text("  Новость\n\t важная   сегодня "), "Новость важная сегодня")

    def test_normalize_title_collapses_whitespace_and_yo(self):
        self.assertEqual(nc.normalize_title("Ёлка   и   новости"), "елка и новости")

    def test_normalize_title_removes_url(self):
        self.assertEqual(nc.normalize_title("Новость https://example.com/a?q=1 сегодня"),
                         "новость сегодня")

    def test_normalize_keyword_collapses_whitespace(self):
        self.assertEqual(nc.normalize_keyword("  атака \n БПЛА "), "атака бпла")

class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.rules = {
            "high_priority_keywords": ["погибли", "угроза атаки бпла", "обвал рынка"],
            "medium_priority_keywords": ["президент", "впервые", "курс рубля"],
            "low_priority_keywords": ["гороскоп", "выбрал костюм"],
        }

    def item(self, title, description=""):
        return {"title": title, "description": description,
                "_published_dt": datetime.now(timezone.utc) - timedelta(minutes=30)}

    def test_drone_threat_category(self):
        item = self.item("В двух регионах России объявили опасность атаки БПЛА")
        self.assertEqual(nc.classify_news(item["title"], "")[0], "происшествия")
        score, reasons = nc.score_news(item, self.rules)
        self.assertGreaterEqual(score, 45)
        self.assertTrue(any("происшествия" in r for r in reasons))

    def test_first_time_is_not_high_priority(self):
        item = self.item("Впервые прошел фестиваль современного кино")
        score, reasons = nc.score_news(item, self.rules)
        self.assertLess(score, 40)
        self.assertTrue(any("низкий редакционный приоритет" in r for r in reasons))

    def test_economy_category(self):
        self.assertEqual(nc.classify_news(
            "Индекс Мосбиржи снизился после решения Центробанка", "")[0], "экономика")

    def test_lifestyle_penalty(self):
        score, reasons = nc.score_news(self.item("Телеведущий выбрал костюм для шоу"), self.rules)
        self.assertTrue(any("низкий редакционный приоритет" in r for r in reasons))
        self.assertLess(score, 25)

    def test_duplicate_similarity(self):
        a = "В двух регионах России объявили опасность атаки БПЛА"
        b = "В России в двух регионах объявлена опасность атаки БПЛА"
        self.assertGreater(nc.similarity(a, b), 0.5)

class FeedAndStateTests(unittest.TestCase):
    def test_parse_date_invalid_returns_none(self):
        self.assertIsNone(nc.parse_date("not a date"))

    def test_parse_rss_item(self):
        xml = b"""<?xml version="1.0"?><rss><channel><item>
        <title>Test news</title><link>https://example.com/news</link>
        <description>Short description</description>
        <pubDate>Thu, 08 Oct 2026 10:00:00 GMT</pubDate><guid>abc</guid>
        </item></channel></rss>"""
        items = nc.parse_feed(xml, "Test")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Test news")
        self.assertEqual(items[0]["id"], "Test:abc")
        self.assertTrue(items[0]["link"].startswith("https://"))

    def test_deduplicate_keeps_higher_ranked_first(self):
        items = [
            {"title": "В двух регионах России объявили опасность атаки БПЛА", "score": 70},
            {"title": "В России в двух регионах объявлена опасность атаки БПЛА", "score": 50},
        ]
        result = nc.deduplicate(items, 0.70)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["score"], 70)

    def test_format_draft_has_manual_review_warning(self):
        item = {"title": "Тест", "source": "Источник", "link": "https://example.com",
                "score": 50, "score_reasons": ["тест"], "category": "прочее",
                "description": "", "published": ""}
        message = nc.format_draft(item)
        self.assertIn("ЧЕРНОВИК МАРИНАД", message)
        self.assertIn("Проверь факты", message)
        self.assertIn("https://example.com", message)

if __name__ == "__main__":
    unittest.main()
