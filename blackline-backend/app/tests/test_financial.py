import unittest

from app.engine.financial import analyse_financials, calculate_sdlt


class SdltTests(unittest.TestCase):
    def test_standard_residential_bands(self):
        # £200,000 main residence: 0% to 125k, 2% on remaining 75k = 1,500
        self.assertEqual(calculate_sdlt(200_000, is_additional_property=False), 1_500)

    def test_additional_property_surcharge_is_flat_five_percent_extra(self):
        # Surcharge adds a flat 5% of price on top of the standard bands —
        # verified algebraically in the docstring; pinned here as a regression test.
        standard = calculate_sdlt(300_000, is_additional_property=False)
        surcharged = calculate_sdlt(300_000, is_additional_property=True)
        self.assertEqual(surcharged - standard, round(300_000 * 0.05))

    def test_zero_sdlt_below_threshold_main_residence(self):
        self.assertEqual(calculate_sdlt(120_000, is_additional_property=False), 0)

    def test_top_band_applies_above_1_5m(self):
        # £1,600,000, main residence:
        # 0 on 125k, 2% on 125k, 5% on 675k, 10% on 575k, 12% on 100k
        expected = round(125_000 * 0.02 + 675_000 * 0.05 + 575_000 * 0.10 + 100_000 * 0.12)
        self.assertEqual(calculate_sdlt(1_600_000, is_additional_property=False), expected)

    def test_first_time_buyer_relief_applies_under_ceiling(self):
        # £280,000, FTB, not additional property: 0% to 300k -> 0 SDLT
        self.assertEqual(
            calculate_sdlt(280_000, is_additional_property=False, is_first_time_buyer=True), 0
        )

    def test_first_time_buyer_relief_withdrawn_above_ceiling(self):
        # Above £500k, relief doesn't apply — standard bands used instead.
        ftb = calculate_sdlt(600_000, is_additional_property=False, is_first_time_buyer=True)
        standard = calculate_sdlt(600_000, is_additional_property=False, is_first_time_buyer=False)
        self.assertEqual(ftb, standard)


class MortgageAndCashflowTests(unittest.TestCase):
    def test_interest_only_mortgage_payment(self):
        fin = analyse_financials(
            200_000, monthly_rent=1_000,
            ltv_percent=75, mortgage_rate_percent=6.0,
            management_fee_percent=0, maintenance_percent=0,
            void_allowance_percent=0, insurance_monthly=0,
            acquisition_fees=0,
        )
        # loan = 150,000; interest-only monthly = 150,000 * 6% / 12 = 750
        self.assertEqual(fin.loan_amount, 150_000)
        self.assertEqual(fin.deposit, 50_000)
        self.assertEqual(fin.mortgage_monthly, 750)

    def test_cashflow_deducts_mortgage_and_opex(self):
        fin = analyse_financials(
            200_000, monthly_rent=1_000,
            ltv_percent=75, mortgage_rate_percent=6.0,
            management_fee_percent=10, maintenance_percent=5,
            void_allowance_percent=0, insurance_monthly=20,
            acquisition_fees=0,
        )
        # opex = 1000*0.15 + 20 = 170; cashflow = 1000 - 750 - 170 = 80
        self.assertEqual(fin.monthly_opex, 170)
        self.assertEqual(fin.cashflow_monthly, 80)

    def test_yield_and_roi_are_consistent_with_cashflow(self):
        fin = analyse_financials(150_000, monthly_rent=900)
        self.assertGreater(fin.gross_yield_pct, fin.net_yield_pct)
        self.assertAlmostEqual(fin.annual_cashflow, fin.cashflow_monthly * 12)
        self.assertAlmostEqual(
            fin.roi_pct, fin.annual_cashflow / fin.total_cash_invested * 100, places=4
        )

    def test_ui_dict_matches_frontend_contract_keys(self):
        fin = analyse_financials(150_000, monthly_rent=900)
        d = fin.as_ui_dict()
        self.assertEqual(
            set(d.keys()),
            {"purchase", "stampDuty", "deposit", "mortgage", "rent", "cashflow", "yieldPct", "roiPct"},
        )
        self.assertTrue(d["yieldPct"].endswith("%"))
        self.assertTrue(d["roiPct"].endswith("%"))


if __name__ == "__main__":
    unittest.main()
