import importlib.util
import unittest
from datetime import datetime
from pathlib import Path
spec=importlib.util.spec_from_file_location('credit_reader',Path(__file__).parents[1]/'packaging/ubuntu/snapshot/flow/console/balance_reader.py')
reader=importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)
class CreditRenewalTest(unittest.TestCase):
    def test_official_tooltip(self):
        self.assertEqual(reader.parse_credit_renewal('1,050 个 Google Flow 点数\n重置时间：11月19日20:30',datetime(2026,11,10)),'2026-11-19 20:30')
    def test_year_boundary(self):
        self.assertEqual(reader.parse_credit_renewal('Google Flow 点数\n重置时间：1月2日20:30',datetime(2026,12,29)),'2027-01-02 20:30')
    def test_observation_is_not_renewal(self):
        self.assertIsNone(reader.parse_credit_renewal('积分余额 125\n更新时间：11月19日20:30'))
    def test_invalid_date(self):
        self.assertIsNone(reader.parse_credit_renewal('Google Flow 点数\n重置时间：2月30日20:30'))
