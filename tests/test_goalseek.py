import os
import sys
import unittest
from fastapi.testclient import TestClient

# Make the package importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app

class TestGoalSeek(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        
    def test_goal_seek_identity(self):
        res = self.client.post("/goal_seek", json={
            "question": "What was ACME's FY2024 gross margin as a percentage?",
            "doc_path": "sample_data/acme_financials.xlsx",
            "target_value": 45.0
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["op"], "identity")
        self.assertIn("adjustments", data)
        self.assertEqual(len(data["adjustments"]), 1)
        
        # Verify adjustments details
        adj = data["adjustments"][0]
        self.assertIn("Income Statement__", adj["source"])
        self.assertEqual(adj["unit"], "percent")
        self.assertEqual(adj["current_value"], 40.87)
        self.assertEqual(adj["target_value"], 45.0)
        self.assertAlmostEqual(adj["delta"], 4.13, places=4)

if __name__ == "__main__":
    unittest.main()
