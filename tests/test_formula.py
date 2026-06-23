import os
import sys
import unittest

# Make the package importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingest import _evaluate_formula

class MockCell:
    def __init__(self, coord, value):
        self.coordinate = coord
        self.value = value

class MockWorksheet:
    def __init__(self):
        self.cells = {}
        
    def __getitem__(self, key):
        if ":" in key:
            # Expand range manually for test mock
            start, end = key.split(":")
            start_col, start_row = start[0], int(start[1:])
            end_col, end_row = end[0], int(end[1:])
            
            # Simple single letter column support for testing mock
            col_range = [chr(c) for c in range(ord(start_col), ord(end_col) + 1)]
            row_range = range(start_row, end_row + 1)
            
            result = []
            for r in row_range:
                row_cells = []
                for c in col_range:
                    coord = f"{c}{r}"
                    cell = self.cells.get(coord, MockCell(coord, None))
                    row_cells.append(cell)
                result.append(tuple(row_cells))
            return tuple(result)
        else:
            return self.cells.get(key, MockCell(key, None))

class TestFormulaEvaluation(unittest.TestCase):
    def setUp(self):
        self.ws = MockWorksheet()
        
    def test_basic_values(self):
        self.ws.cells["A1"] = MockCell("A1", 10.0)
        self.ws.cells["B1"] = MockCell("B1", 5.0)
        
        # Simple references and addition
        self.assertEqual(_evaluate_formula(self.ws, "=A1"), 10.0)
        self.assertEqual(_evaluate_formula(self.ws, "=A1+B1"), 15.0)
        self.assertEqual(_evaluate_formula(self.ws, "=A1-B1"), 5.0)
        self.assertEqual(_evaluate_formula(self.ws, "=A1*B1"), 50.0)
        self.assertEqual(_evaluate_formula(self.ws, "=A1/B1"), 2.0)
        
    def test_sum_function(self):
        self.ws.cells["A1"] = MockCell("A1", 10.0)
        self.ws.cells["A2"] = MockCell("A2", 20.0)
        self.ws.cells["A3"] = MockCell("A3", 30.0)
        
        # SUM with range
        self.assertEqual(_evaluate_formula(self.ws, "=SUM(A1:A3)"), 60.0)
        # SUM with individual references
        self.assertEqual(_evaluate_formula(self.ws, "=SUM(A1,A2,A3)"), 60.0)
        # SUM mixed with operators
        self.assertEqual(_evaluate_formula(self.ws, "=SUM(A1:A2)*2 + A3"), 90.0)
        
    def test_recursive_formulas(self):
        self.ws.cells["A1"] = MockCell("A1", 10.0)
        self.ws.cells["A2"] = MockCell("A2", "=A1*2") # 20.0
        self.ws.cells["A3"] = MockCell("A3", "=SUM(A1:A2)") # 30.0
        
        self.assertEqual(_evaluate_formula(self.ws, "=A3+5"), 35.0)
        
    def test_cycle_detection(self):
        self.ws.cells["A1"] = MockCell("A1", "=B1")
        self.ws.cells["B1"] = MockCell("B1", "=A1")
        
        # Cycle should resolve to 0.0 without infinite recursion / stack overflow
        self.assertEqual(_evaluate_formula(self.ws, "=A1"), 0.0)

if __name__ == "__main__":
    unittest.main()
