import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import run_proof_pipeline
from ingest import ingest

class TestHardBenchmark(unittest.TestCase):
    def setUp(self):
        self.doc_path = "sample_data/acme_global_hard.xlsx"
        
    def test_nested_hierarchical_context_retribution(self):
        # The metric name should carry the parent section header ("Revenue" or "Costs")
        spans = ingest(self.doc_path)
        
        # Verify that sub-metrics are prepended with their parent sections
        revenue_us_span = [s for s in spans if "US Division" in s.text and "Revenue" in s.text]
        cost_us_span = [s for s in spans if "US Cost" in s.text and "Costs" in s.text]
        
        self.assertTrue(len(revenue_us_span) > 0, "Failed to prepend parent section 'Revenue' to 'US Division'")
        self.assertTrue(len(cost_us_span) > 0, "Failed to prepend parent section 'Costs' to 'US Cost'")
        
    def test_dynamic_formula_resolution(self):
        # Querying Net Profit for FY2024 (evaluated from formulas without cached data)
        # Net Profit = Total Revenue (B8/C8) - Total Cost (B13/C13)
        # Total Revenue (C8) = C6 (1200000) + C7 (900000) = 2100000
        # Total Cost (C13) = C11 (700000) + C12 (550000) = 1250000
        # Net Profit (C15) = 2100000 - 1250000 = 850000
        # Given "Amounts in thousands", the scaled answer is 850,000 * 1,000 = 850,000,000 USD.
        
        question = "What was the Net Profit for FY2024?"
        proof = run_proof_pipeline(question, self.doc_path)
        
        print("\n--- Hard Benchmark Output ---")
        print(f"Status: {'Rejected' if proof.rejected else 'Verified'}")
        print(f"Calculated Answer: {proof.answer}")
        print(f"Citations: {proof.citations}")
        print(f"Dimension Checks: {proof.dimension_checks}")
        print(f"Verifier Verdict: {proof.verifier_verdict}")
        
        self.assertFalse(proof.rejected, f"Resolution rejected: {proof.reason}")
        self.assertEqual(proof.normalized_value, 850000000.0)

if __name__ == "__main__":
    unittest.main()
