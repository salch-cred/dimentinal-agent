import os
import sys
import unittest
from fastapi.testclient import TestClient

# Make the package importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app

class TestStress(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        
    def test_stress_test_endpoint(self):
        res = self.client.post("/stress_test", json={"doc_path": "sample_data/acme_financials.xlsx"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("results", data)
        self.assertEqual(len(data["results"]), 5)
        
        # Verify specific traps exist in response
        ids = [item["id"] for item in data["results"]]
        self.assertIn("scale", ids)
        self.assertIn("period", ids)
        self.assertIn("entity", ids)
        self.assertIn("flowstock", ids)
        self.assertIn("clean", ids)
        
        # Verify content schema of results
        for item in data["results"]:
            self.assertIn("name", item)
            self.assertIn("description", item)
            self.assertIn("question", item)
            self.assertIn("baseline", item)
            self.assertIn("proof", item)
            self.assertIn("answer", item["baseline"])
            self.assertIn("rejected", item["proof"])

if __name__ == "__main__":
    unittest.main()
