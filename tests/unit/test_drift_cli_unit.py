"""The command line's small pieces: windows, the injected shift, times and the model's split."""

import json
from datetime import datetime, timedelta

import polars as pl
import pytest

pytest.importorskip("sklearn")  # the command loads a model through the API package
pytest.importorskip("fastapi")

from mmrisk.drift import cli  # noqa: E402

T = datetime(2025, 3, 1)


def frame(offsets):
    return pl.DataFrame(
        {
            "timestamp": [T + timedelta(hours=h) for h in offsets],
            "amount_kes": [100 * (i + 1) for i in range(len(offsets))],
        }
    )


def test_a_window_excludes_its_start_and_includes_its_end_like_the_model_split():
    df = frame([0, 1, 2, 3])
    got = cli._window(df, T + timedelta(hours=1), T + timedelta(hours=3))
    assert got["timestamp"].to_list() == [T + timedelta(hours=2), T + timedelta(hours=3)]


def test_the_injected_shift_scales_only_what_comes_after_the_start():
    df = frame([0, 1, 2, 3])
    out = cli._inject(df, T + timedelta(hours=1), 2.5)
    assert out["amount_kes"].to_list() == [100, 200, 750, 1000]
    assert out["amount_kes"].dtype == pl.Int64
    assert cli._inject(df, T, 1.0)["amount_kes"].to_list() == df["amount_kes"].to_list()


def test_the_injected_shift_rounds_to_whole_shillings():
    out = cli._inject(frame([5]), T, 1.5)  # 100 * 1.5 = 150, exact; 0.333 rounds
    assert out["amount_kes"].to_list() == [150]
    assert cli._inject(frame([5]), T, 0.337)["amount_kes"].to_list() == [34]  # 33.7, not 33


def test_times_are_parsed_or_refused_with_a_message():
    assert cli._when(None, "--from") is None and cli._when("", "--from") is None
    assert cli._when("2025-12-24T10:30:00", "--from") == datetime(2025, 12, 24, 10, 30)
    with pytest.raises(cli.UsageError, match="--to must be an ISO time"):
        cli._when("tomorrow", "--to")


def test_the_validation_window_is_read_from_the_saved_model(tmp_path):
    (tmp_path / "risk_model.json").write_text(
        json.dumps(
            {"split": {"val_start": "2025-08-18 23:55:49", "test_start": "2025-10-02 23:55:49"}}
        )
    )
    lo, hi = cli._split_bounds(tmp_path)
    assert lo == datetime(2025, 8, 18, 23, 55, 49) and hi == datetime(2025, 10, 2, 23, 55, 49)
    assert lo < hi  # validation is the part between the two, not train_end to val_start


@pytest.mark.parametrize(
    "content", [None, "{not json", "{}", '{"split": {"val_start": "x"}}', '{"split": 3}']
)
def test_a_model_without_a_readable_split_asks_for_explicit_times(tmp_path, content):
    if content is not None:
        (tmp_path / "risk_model.json").write_text(content)
    with pytest.raises(cli.UsageError, match="--from and --to"):
        cli._split_bounds(tmp_path)


def test_the_defaults_point_at_the_data_folder_and_a_week():
    args = cli.build_parser().parse_args(["report"])
    assert str(args.reference).replace("\\", "/") == "data/drift/reference.json"
    assert str(args.out).replace("\\", "/") == "data/drift" and args.last_days == 7.0
    assert args.series_window == "1d" and args.scale_amounts is None
    ref = cli.build_parser().parse_args(["reference"])
    assert str(ref.out).replace("\\", "/") == "data/drift/reference.json"
