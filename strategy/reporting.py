"""策略报告的排除记录和数据缺失记录整理。"""

import pandas as pd


def _selected_columns(data: pd.DataFrame) -> pd.DataFrame:
    columns = [column for column in ("代码", "名称", "最新价", "综合得分") if column in data]
    return data.loc[:, columns].copy()


def _text(value: object, fallback: str = "") -> str:
    return fallback if value is None or pd.isna(value) else str(value).strip()


def build_excluded_results(
    history_results: pd.DataFrame,
    late_session_results: pd.DataFrame,
    scoring_results: pd.DataFrame,
    final_results: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """每只股票仅在它首次未通过的阶段记录排除原因。"""
    frames: list[pd.DataFrame] = []
    selected_codes = pd.Series(dtype="string")
    if final_results is not None and not final_results.empty and "代码" in final_results:
        selected_codes = final_results["代码"].astype("string").str.zfill(6)

    def not_selected(data: pd.DataFrame) -> pd.Series:
        return ~data["代码"].astype("string").str.zfill(6).isin(selected_codes)

    if not history_results.empty:
        rejected = history_results.loc[
            history_results["涨停判断"].ne("符合") & not_selected(history_results)
        ].copy()
        if not rejected.empty:
            output = _selected_columns(rejected)
            output["排除阶段"] = "20日涨停验证"
            output["排除原因"] = rejected.apply(
                lambda row: (
                    "最近20个有效交易日未触及理论涨停价"
                    if row.get("数据状态") == "正常"
                    else _text(row.get("数据状态"), "无法验证")
                ),
                axis=1,
            ).values
            frames.append(output)
    if not late_session_results.empty:
        rejected = late_session_results.loc[
            late_session_results["尾盘结构状态"].ne("合格") & not_selected(late_session_results)
        ].copy()
        if not rejected.empty:
            output = _selected_columns(rejected)
            output["排除阶段"] = "尾盘结构"
            reasons = rejected.get(
                "淘汰原因",
                rejected.get(
                    "尾盘排除原因",
                    pd.Series("无法验证", index=rejected.index),
                ),
            )
            output["排除原因"] = reasons.fillna("无法验证").values
            frames.append(output)
    if not scoring_results.empty:
        rejected = scoring_results.loc[not_selected(scoring_results)].copy()
        if not rejected.empty:
            output = _selected_columns(rejected)
            output["排除阶段"] = "综合评分"
            output["排除原因"] = rejected["综合得分"].map(
                lambda score: f"综合得分 {float(score):.2f}，稳定排序后未进入Top5"
            ).values
            frames.append(output)
    columns = ["代码", "名称", "最新价", "综合得分", "排除阶段", "排除原因"]
    if not frames:
        return pd.DataFrame(columns=columns)
    return (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates("代码", keep="first")
        .reindex(columns=columns)
    )


def build_missing_records(
    history_results: pd.DataFrame,
    late_session_results: pd.DataFrame,
    scoring_results: pd.DataFrame,
) -> pd.DataFrame:
    """整理各阶段真实数据缺失和无法验证记录。"""
    records: list[dict[str, object]] = []
    for _, row in history_results.iterrows():
        if row.get("数据状态") != "正常":
            records.append({"阶段": "20日涨停验证", "代码": row.get("代码"), "名称": row.get("名称"), "缺失或异常": _text(row.get("历史错误原因"), _text(row.get("数据状态")))})
    for _, row in late_session_results.iterrows():
        if row.get("尾盘结构状态") == "无法验证":
            records.append({"阶段": "尾盘结构", "代码": row.get("代码"), "名称": row.get("名称"), "缺失或异常": row.get("淘汰原因", row.get("尾盘排除原因"))})
    for _, row in scoring_results.iterrows():
        missing = _text(row.get("缺失字段", row.get("缺失项")))
        if missing:
            records.append({"阶段": "综合评分", "代码": row.get("代码"), "名称": row.get("名称"), "缺失或异常": missing})
    return pd.DataFrame(records, columns=["阶段", "代码", "名称", "缺失或异常"])
