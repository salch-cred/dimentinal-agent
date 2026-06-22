import os
import sys
import unittest
from fastapi.testclient import TestClient

# Make the package importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app

class TestUpload(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        
    def test_upload_allowed_extension(self):
        # We can upload a dummy content to a .csv file
        res = self.client.post("/upload?filename=test_dummy.csv", content=b"header1,header2\nval1,val2\n")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["filename"], "sample_data/test_dummy.csv")
        
        # Verify the file is created inside sample_data
        target_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample_data", "test_dummy.csv")
        self.assertTrue(os.path.exists(target_path))
        
        # Clean up
        if os.path.exists(target_path):
            os.remove(target_path)
            
    def test_upload_blocked_extension(self):
        # Blocked extension
        res = self.client.post("/upload?filename=test_dummy.txt", content=b"some text")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Unsupported extension", res.json()["detail"])
        
    def test_upload_path_traversal(self):
        # Path traversal should be mitigated by os.path.basename
        res = self.client.post("/upload?filename=../../rogue_test.csv", content=b"dummy")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        # Safe filename resolves to rogue_test.csv under sample_data
        self.assertEqual(data["filename"], "sample_data/rogue_test.csv")
        
        target_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample_data", "rogue_test.csv")
        self.assertTrue(os.path.exists(target_path))
        if os.path.exists(target_path):
            os.remove(target_path)

if __name__ == "__main__":
    unittest.main()
