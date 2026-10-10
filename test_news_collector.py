import unittest
from datetime import datetime, timezone, timedelta
import news_collector as nc

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

if __name__ == "__main__":
    unittest.main()
