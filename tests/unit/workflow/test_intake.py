"""workflow/intake.py 单元测试：数据画像转换层规范化。

覆盖数据画像 6 大维度在 DataProfile 层面的输出：
  1. 基本面 — FileRecord(真实大小) / TableProfile(样例、候选键、时空覆盖)
  2. 质量 — quality_issues（缺失/重复/离群/不平衡/类型混合）
  3. 分布 — FieldProfile(skew/kurt) / TableProfile.correlated_pairs
  4. 语义 — unit_hint / max_category_share
  5. 时空 — time_coverage / spatial_columns
  6. 建模假设 — modeling_constraints 合并
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from scr.workflow.intake import run_intake


def _run(data_paths: list[str], tmp_path: Path) -> dict:
    return run_intake(
        {"data_paths": data_paths, "output_dir": str(tmp_path / "art")}
    )


class TestRunIntakeBasic:
    def test_status_and_files(self, sample_csv: Path, tmp_path: Path):
        result = _run([str(sample_csv)], tmp_path)
        dp = result["data_profile"]
        assert result["workflow_status"] == "intake_ready"
        assert len(dp.files) == 1
        assert dp.files[0].file_size > 0  # 真实文件大小而非占位 0
        assert dp.files[0].read_status == "success"

    def test_no_data_paths(self):
        dp = run_intake({"data_paths": [], "output_dir": "artifacts/x"})["data_profile"]
        assert dp.preliminary_findings == ["无附件数据"]
        assert len(dp.files) == 0

    def test_failed_file_registered(self, tmp_path: Path):
        missing = tmp_path / "missing.csv"
        dp = _run([str(missing)], tmp_path)["data_profile"]
        assert len(dp.files) == 1
        assert dp.files[0].read_status == "failed"
        assert "读取或画像生成失败" in dp.files[0].error_message


class TestTableProfile:
    def test_full_table_info(self, sample_csv: Path, tmp_path: Path):
        t = _run([str(sample_csv)], tmp_path)["data_profile"].tables[0]
        assert t.n_rows == 5
        assert len(t.sample_rows) == 5  # 样例已填充
        assert "GDP(亿元)" in t.candidate_keys
        assert t.time_coverage.startswith("2023-01-01")
        assert "5 个时间点" in t.time_coverage

    def test_spatial_table(self, spatial_csv: Path, tmp_path: Path):
        t = _run([str(spatial_csv)], tmp_path)["data_profile"].tables[0]
        assert t.spatial_columns == ["经度", "纬度"]

    def test_duplicate_table(self, duplicate_csv: Path, tmp_path: Path):
        t = _run([str(duplicate_csv)], tmp_path)["data_profile"].tables[0]
        assert t.duplicate_rows == 2
        assert t.duplicate_rate == 0.4


class TestFieldProfile:
    def test_extended_fields(self, sample_csv: Path, tmp_path: Path):
        fields = _run([str(sample_csv)], tmp_path)["data_profile"].fields
        gdp = next(f for f in fields if f.field_name == "GDP(亿元)")
        assert gdp.is_candidate_key is True
        assert gdp.skewness is not None
        assert gdp.kurtosis is not None
        assert gdp.value_range == "27670~43215"

        city = next(f for f in fields if f.field_name == "城市")
        assert city.missing_count == 1
        assert city.is_candidate_key is False

    def test_sample_values_pass_through(self, sample_csv: Path, tmp_path: Path):
        fields = _run([str(sample_csv)], tmp_path)["data_profile"].fields
        city = next(f for f in fields if f.field_name == "城市")
        # 城市列 5 行含 1 个缺失，非缺失样例应补齐到 4 个
        assert city.sample_values == ["北京", "上海", "广州", "深圳"]

        growth = next(f for f in fields if f.field_name == "增长率(%)")
        assert len(growth.sample_values) == 4  # 缺失值已剔除
        assert all(isinstance(v, str) for v in growth.sample_values)

    def test_numeric_stats_pass_through(self, sample_csv: Path, tmp_path: Path):
        """数值列的集中/离散/四分位统计应进入 FieldProfile，分类列为 None。"""
        fields = _run([str(sample_csv)], tmp_path)["data_profile"].fields
        gdp = next(f for f in fields if f.field_name == "GDP(亿元)")
        assert gdp.mean == 34010.0
        assert gdp.median == 30664.0
        assert gdp.q1 == 28232.0
        assert gdp.q3 == 40269.0
        assert gdp.iqr == 12037.0
        assert gdp.std is not None and gdp.std > 0

        city = next(f for f in fields if f.field_name == "城市")
        assert city.mean is None and city.median is None


class TestQualityIssues:
    def test_duplicate_issue(self, duplicate_csv: Path, tmp_path: Path):
        issues = _run([str(duplicate_csv)], tmp_path)["data_profile"].quality_issues
        dup = [q for q in issues if q.issue_type == "duplicate"]
        assert len(dup) == 1
        assert dup[0].severity == "high"  # 40% > 5%

    def test_outlier_skips_spatial_and_time(self, spatial_csv: Path, tmp_path: Path):
        issues = _run([str(spatial_csv)], tmp_path)["data_profile"].quality_issues
        outlier_targets = [q.target for q in issues if q.issue_type == "outlier"]
        assert "经度" not in outlier_targets
        assert "纬度" not in outlier_targets

    def test_imbalance_issue(self, imbalanced_csv: Path, tmp_path: Path):
        issues = _run([str(imbalanced_csv)], tmp_path)["data_profile"].quality_issues
        imb = [q for q in issues if q.issue_type == "imbalance"]
        assert len(imb) == 1
        assert imb[0].severity == "high"  # 90% ≥ 90%

    def test_encoding_issue(self, mixed_type_csv: Path, tmp_path: Path):
        issues = _run([str(mixed_type_csv)], tmp_path)["data_profile"].quality_issues
        enc = [q for q in issues if q.issue_type == "encoding"]
        assert len(enc) == 1
        assert enc[0].severity == "medium"

    def test_correlation_issue(self, high_corr_csv: Path, tmp_path: Path):
        issues = _run([str(high_corr_csv)], tmp_path)["data_profile"].quality_issues
        corr = [q for q in issues if q.issue_type == "high_correlation"]
        assert len(corr) >= 1

    def test_missing_and_constant(
        self, sample_csv: Path, all_null_csv: Path, constant_csv: Path, tmp_path: Path
    ):
        issues = _run(
            [str(sample_csv), str(all_null_csv), str(constant_csv)], tmp_path
        )["data_profile"].quality_issues
        types = {q.issue_type for q in issues}
        assert "missing_rate" in types  # 全空列 + 增长率缺失
        assert "constant_column" in types  # constant_csv 的常量列


class TestRelationshipsAndArtifacts:
    def test_same_name_key_relationship(self, tmp_path: Path):
        df1 = pd.DataFrame({"站点": ["A", "B", "C"], "值": [1, 2, 3]})
        df2 = pd.DataFrame({"站点": ["A", "B", "D"], "其他": [4, 5, 6]})
        p1 = tmp_path / "t1.csv"
        p2 = tmp_path / "t2.csv"
        df1.to_csv(p1, index=False)
        df2.to_csv(p2, index=False)

        dp = _run([str(p1), str(p2)], tmp_path)["data_profile"]
        rels = [r for r in dp.relationships if r.left_field == "站点"]
        assert len(rels) >= 1
        assert rels[0].confidence == 0.8  # 站点为候选键且值域重叠 2/3 ≥ 0.5

    def test_key_relationship_downgraded_on_disjoint_values(self, tmp_path: Path):
        """候选键同名但值域完全不相交 → 降级为 0.5（疑似仅同名）。"""
        df1 = pd.DataFrame({"编号": ["A", "B", "C"], "值": [1, 2, 3]})
        df2 = pd.DataFrame({"编号": ["X", "Y", "Z"], "其他": [4, 5, 6]})
        p1 = tmp_path / "k1.csv"
        p2 = tmp_path / "k2.csv"
        df1.to_csv(p1, index=False)
        df2.to_csv(p2, index=False)

        dp = _run([str(p1), str(p2)], tmp_path)["data_profile"]
        rels = [r for r in dp.relationships if r.left_field == "编号"]
        assert len(rels) == 1
        assert rels[0].confidence == 0.5

    def test_same_name_disjoint_values_dropped(self, tmp_path: Path):
        """普通同名列且值域完全不相交 → 剔除（偶然同名）。"""
        df1 = pd.DataFrame({"名称": ["甲", "乙", "甲"], "v1": [1, 2, 3]})
        df2 = pd.DataFrame({"名称": ["子", "丑", "子"], "v2": [4, 5, 6]})
        p1 = tmp_path / "n1.csv"
        p2 = tmp_path / "n2.csv"
        df1.to_csv(p1, index=False)
        df2.to_csv(p2, index=False)

        dp = _run([str(p1), str(p2)], tmp_path)["data_profile"]
        rels = [r for r in dp.relationships if r.left_field == "名称"]
        assert rels == []

    def test_time_coverage_union_of_multiple_time_columns(self, tmp_path: Path):
        """多时间列时覆盖范围取并集，而非只取第一列。"""
        df = pd.DataFrame({
            "起始日期": ["2023-01-01", "2023-01-02", "2023-01-03"],
            "结束日期": ["2023-02-01", "2023-02-02", "2023-02-03"],
            "值": [1, 2, 3],
        })
        p = tmp_path / "multi_time.csv"
        df.to_csv(p, index=False)

        t = _run([str(p)], tmp_path)["data_profile"].tables[0]
        assert t.time_coverage.startswith("2023-01-01 ~ 2023-02-03")
        assert "6 个时间点" in t.time_coverage

    def test_artifacts_registered(self, sample_csv: Path, tmp_path: Path):
        dp = _run([str(sample_csv)], tmp_path)["data_profile"]
        assert len(dp.artifacts) >= 2  # 画像报告 + inventory JSON
        assert all(Path(a).exists() for a in dp.artifacts)


class TestModelingConstraints:
    def test_merged_constraints(self, sample_csv: Path, tmp_path: Path):
        dp = _run([str(sample_csv)], tmp_path)["data_profile"]
        assert len(dp.modeling_constraints) >= 2
        assert any("样本量 5 < 30" in c for c in dp.modeling_constraints)

    def test_spatial_constraint(self, spatial_csv: Path, tmp_path: Path):
        dp = _run([str(spatial_csv)], tmp_path)["data_profile"]
        assert any("空间坐标列" in c for c in dp.modeling_constraints)

    def test_findings_summary(self, sample_csv: Path, tmp_path: Path):
        findings = _run([str(sample_csv)], tmp_path)["data_profile"].preliminary_findings
        assert any("共读取" in f for f in findings)
        assert any("字段构成" in f for f in findings)
        assert any("建模约束" in f for f in findings)
