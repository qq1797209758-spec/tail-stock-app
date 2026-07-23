from datetime import date, datetime, timezone
import unittest
from unittest.mock import patch

import pandas as pd

from components.stock_table import _format_display_value
from services.late_session import LateSessionDataError, analyze_late_session
from services.market_data import MarketDataError, fetch_a_share_spot
from services.trading_session import is_trading_day
from strategy.filters import apply_filters
from strategy.scoring import calculate_candidate_score
from strategy.selection import build_valid_main_board_universe, select_layered_top5, stable_candidate_sort


def candidate(code: int, *, strict: bool, score: float) -> dict[str, object]:
    return {
        "代码": f"00{code:04d}", "名称": f"股票{code}", "最新价": 10.0,
        "成交量": 10000, "成交额": 100000, "涨跌幅": 5.0 if strict else 1.0,
        "量比": 1.5, "换手率": 7.0, "总市值": 10_000_000_000,
        "20日内是否涨停": "是" if strict else "否", "20日涨停次数": 1 if strict else 0,
        "尾盘结构状态": "合格" if strict else "无法验证", "VWAP状态": "合格" if strict else "无法验证",
        "尾盘最大回撤": 0.002 if strict else pd.NA, "综合得分": score,
        "数据完整度": 1.0 if strict else 0.4, "主力资金净流入": score * 10000,
    }


class LayeredSelectionTests(unittest.TestCase):
    def assert_five_unique(self, rows):
        result = select_layered_top5(pd.DataFrame(rows))
        self.assertEqual(len(result.selected), 5)
        self.assertEqual(result.selected["代码"].nunique(), 5)
        return result

    def test_zero_strict_still_returns_five_real_ranked_candidates(self):
        result = self.assert_five_unique([candidate(i, strict=False, score=80-i) for i in range(8)])
        self.assertEqual(set(result.selected["入选类型"]), {"综合评分递补"})

    def test_two_strict_are_prioritized_then_supplemented(self):
        rows = [candidate(i, strict=i < 2, score=90-i) for i in range(8)]
        result = self.assert_five_unique(rows)
        self.assertEqual((result.selected["入选类型"] == "严格入选").sum(), 2)

    def test_more_than_five_strict_keeps_stable_top_five(self):
        rows = [candidate(i, strict=True, score=70+i) for i in range(7)]
        first = self.assert_five_unique(rows).selected
        second = self.assert_five_unique(list(reversed(rows))).selected
        self.assertListEqual(first["代码"].tolist(), second["代码"].tolist())
        self.assertListEqual(first["综合得分"].tolist(), sorted(first["综合得分"], reverse=True))

    def test_all_supplement_layers_are_labeled(self):
        rows = []
        strict = candidate(1, strict=True, score=95)
        level1 = {**candidate(2, strict=True, score=94), "涨跌幅": 2.5, "换手率": 4.0}
        level2 = {**candidate(3, strict=True, score=93), "涨跌幅": 2.5, "换手率": 4.0, "量比": 0.9, "总市值": 40_000_000_000}
        level3 = {**level2, "代码": "000004", "综合得分": 92, "20日内是否涨停": "否"}
        fallback = candidate(5, strict=False, score=91)
        rows.extend([strict, level1, level2, level3, fallback])
        result = self.assert_five_unique(rows)
        self.assertSetEqual(set(result.selected["入选类型"]), {
            "严格入选", "一级递补", "二级递补", "三级递补", "综合评分递补"
        })
        supplemented=result.selected.loc[result.selected["入选类型"].ne("严格入选")]
        self.assertTrue(supplemented["入选原因"].str.contains("未满足严格条件：").all())

    def test_strict_change_range_includes_only_three_to_five_percent(self):
        changes=[2.99,3.00,5.00,5.01]
        rows=[]
        for index,change in enumerate(changes,1):
            row=candidate(index,strict=True,score=90)
            row["涨跌幅"]=change
            rows.append(row)
        result=apply_filters(pd.DataFrame(rows)).final
        self.assertListEqual(sorted(result["涨跌幅"].tolist()),[3.0,5.0])

    def test_less_than_five_valid_rows_reports_exact_shortage(self):
        result = select_layered_top5(pd.DataFrame([
            candidate(1, strict=False, score=90), candidate(2, strict=False, score=80)
        ]))
        self.assertEqual(len(result.selected), 2)
        self.assertEqual(result.missing_count, 3)

    def test_top5_can_be_rescored_after_late_session_without_duplicate_rank(self):
        first = select_layered_top5(pd.DataFrame([
            candidate(i, strict=False, score=90-i) for i in range(5)
        ])).selected
        second = select_layered_top5(first).selected
        self.assertEqual(second.columns.tolist().count("排名"), 1)
        self.assertListEqual(second["排名"].tolist(), [1, 2, 3, 4, 5])

    def test_valid_universe_excludes_st_suspended_and_other_boards(self):
        rows = [
            candidate(1, strict=True, score=90),
            {**candidate(2, strict=True, score=89), "名称": "ST样本"},
            {**candidate(3, strict=True, score=88), "成交量": 0},
            {**candidate(4, strict=True, score=87), "代码": "300001"},
            {**candidate(5, strict=True, score=86), "代码": "600005"},
        ]
        result = build_valid_main_board_universe(pd.DataFrame(rows))
        self.assertListEqual(result["代码"].tolist(), ["000001", "600005"])

    def test_missing_scores_are_renormalized_not_zeroed(self):
        row = pd.Series({"量比": 3.0})
        result = calculate_candidate_score(row, None, None, None, None, [])
        self.assertEqual(result["综合得分"], 100.0)
        self.assertEqual(result["数据完整度"], 0.1364)
        self.assertIn("主力资金净流入", result["缺失字段"])


class ResilienceAndSessionTests(unittest.TestCase):
    @patch("services.market_data.call_with_proxy_fallback", side_effect=RuntimeError("offline"))
    def test_both_market_interfaces_fail_with_clear_error(self, _mock):
        with self.assertRaises(MarketDataError):
            fetch_a_share_spot()

    @patch("services.market_data.fetch_tencent_quote_supplement")
    @patch("services.market_data.call_with_proxy_fallback")
    def test_sina_fallback_codes_are_normalized(self, request, supplement):
        fallback = pd.DataFrame([{
            "代码": "sh600000", "名称": "浦发银行", "最新价": 10,
            "涨跌幅": 1, "成交量": 100, "成交额": 1000,
        }])
        request.side_effect = [RuntimeError("primary offline"), fallback]
        supplement.return_value = pd.DataFrame([{
            "代码": "600000", "换手率": 1.2, "量比": 1.1, "总市值": 100_000_000_000,
        }])
        result = fetch_a_share_spot()
        self.assertEqual(result.iloc[0]["代码"], "600000")
        self.assertEqual(result.attrs["data_source"], "AKShare · 新浪 + 腾讯行情补齐")
        self.assertEqual(result.iloc[0]["换手率"], 1.2)

    @patch("services.market_data.fetch_tencent_quote_supplement")
    @patch("services.market_data.call_with_proxy_fallback")
    def test_missing_snapshot_fields_remain_missing_with_source_reason(self, request, supplement):
        request.side_effect = [RuntimeError("primary offline"), pd.DataFrame([{
            "代码": "sz000001", "名称": "平安银行", "最新价": 10,
            "涨跌幅": 1, "成交量": 100, "成交额": 1000,
        }])]
        supplement.return_value = pd.DataFrame(columns=["代码", "换手率", "量比", "总市值"])
        result = fetch_a_share_spot()
        self.assertTrue(pd.isna(result.iloc[0]["换手率"]))
        self.assertTrue(pd.isna(result.iloc[0]["量比"]))
        self.assertTrue(pd.isna(result.iloc[0]["总市值"]))
        self.assertIn("新浪", result.iloc[0]["当前行情数据源"])

    @patch("services.late_session.call_with_proxy_fallback")
    def test_before_1430_does_not_request_minutes(self, request):
        result = analyze_late_session("000001", datetime(2026, 7, 22, 6, 29, tzinfo=timezone.utc))
        request.assert_not_called()
        self.assertEqual(result["尾盘结构状态"], "无法验证")
        self.assertEqual(result["淘汰原因"], "尚未进入尾盘时段")

    @staticmethod
    def minute_frame(*, zero_volume: bool = False) -> pd.DataFrame:
        times = pd.date_range("2026-07-22 13:50:00", "2026-07-22 14:40:00", freq="min")
        closes = pd.Series(range(len(times)), dtype=float) / 100 + 10
        volume = pd.Series(0 if zero_volume else 1000, index=range(len(times)), dtype=float)
        volume.iloc[-11:] = 0 if zero_volume else 2000
        return pd.DataFrame({
            "时间": times, "收盘": closes, "最高": closes + 0.01,
            "成交量": volume, "成交额": closes * volume,
        })

    @patch("services.late_session._fetch_minute_data")
    def test_after_1430_normal_minutes_have_finite_vwap_and_drawdown(self, fetch):
        fetch.return_value = (self.minute_frame(), "测试真实分钟源", [])
        result = analyze_late_session("000001", datetime(2026, 7, 22, 14, 40))
        self.assertEqual(result["分钟K线条数"], 11)
        self.assertEqual(result["分钟数据源"], "测试真实分钟源")
        self.assertTrue(pd.notna(result["高于VWAP占比"]))
        self.assertTrue(pd.notna(result["尾盘最大回撤"]))
        self.assertNotEqual(result["尾盘最大回撤"], float("inf"))

    @patch("services.late_session._fetch_minute_data")
    def test_empty_minutes_has_specific_reason(self, fetch):
        fetch.return_value = (pd.DataFrame(), "测试真实分钟源", ["主源为空"])
        result = analyze_late_session("000001", datetime(2026, 7, 22, 14, 40))
        self.assertEqual(result["淘汰原因"], "返回数据为空")
        self.assertTrue(result["接口错误原因"])

    @patch("services.late_session._fetch_minute_data")
    def test_zero_volume_has_specific_reason(self, fetch):
        fetch.return_value = (self.minute_frame(zero_volume=True), "测试真实分钟源", [])
        result = analyze_late_session("000001", datetime(2026, 7, 22, 14, 40))
        self.assertIn("成交量为0", result["淘汰原因"])

    @patch("services.late_session._fetch_minute_data", side_effect=LateSessionDataError("分钟接口请求失败（TimeoutError）"))
    def test_minute_interface_error_is_preserved(self, _fetch):
        result = analyze_late_session("000001", datetime(2026, 7, 22, 14, 40))
        self.assertIn("分钟接口请求失败", result["接口错误原因"])

    @patch("services.late_session._fetch_minute_data")
    def test_missing_volume_amount_fields_has_specific_reason(self, fetch):
        frame = self.minute_frame().drop(columns=["成交量", "成交额"])
        fetch.return_value = (frame, "测试真实分钟源", [])
        result = analyze_late_session("000001", datetime(2026, 7, 22, 14, 40))
        self.assertEqual(result["淘汰原因"], "成交量/成交额字段缺失")

    def test_sorting_does_not_write_inf_into_drawdown(self):
        frame = pd.DataFrame([candidate(1, strict=False, score=80)])
        frame["尾盘最大回撤"] = pd.NA
        result = stable_candidate_sort(frame)
        self.assertTrue(pd.isna(result.iloc[0]["尾盘最大回撤"]))
        self.assertEqual(_format_display_value("尾盘最大回撤", float("inf")), "数据不足")

    def test_invalid_market_code_has_specific_reason(self):
        result = analyze_late_session("300001", datetime(2026, 7, 22, 14, 40))
        self.assertIn("股票代码或市场参数错误", result["接口错误原因"])

    def test_trading_and_non_trading_calendar(self):
        calendar = pd.to_datetime(["2026-07-20", "2026-07-21", "2026-07-22"])
        self.assertTrue(is_trading_day(date(2026, 7, 22), calendar))
        self.assertFalse(is_trading_day(date(2026, 7, 25), calendar))


if __name__ == "__main__":
    unittest.main()
