"""Configuration must fail fast and loudly, never silently fall back.

Each test below pairs a plausible operator mistake with the failure it should produce.
The point is that "bad config stops the run" becomes a verified claim rather than a
sentence in a docstring -- the prototype is the cautionary tale here, since its
``.get(key, default)`` calls had already drifted away from the values it shipped with
and nothing failed to tell anyone.

Where a message is asserted, it is asserted because an operator has to act on it: the
offending key must be named, and the reason must be stated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from trackbox_pitch.config import Settings, load_settings
from trackbox_pitch.errors import ConfigError, ExitCode


def set_value(data: dict[str, Any], dotted_key: str, value: Any) -> dict[str, Any]:
    """Set a nested key using dotted notation, e.g. ``video.target_fps``."""
    *parents, leaf = dotted_key.split(".")
    cursor = data
    for key in parents:
        cursor = cursor[key]
    cursor[leaf] = value
    return data


# --------------------------------------------------------------------------------------
# A good configuration loads
# --------------------------------------------------------------------------------------


class TestValidConfiguration:
    def test_shipped_config_loads(self, config_data, write_config):
        settings = load_settings(write_config(config_data), environ={})

        assert isinstance(settings, Settings)
        assert settings.video.path == Path("synthetic_pitch_feed.mp4")
        assert settings.video.target_fps == 5.0
        assert settings.field_detector.type == "green_threshold"
        assert settings.field_detector.sport == "football"
        assert settings.field_detector.min_area == 1000
        assert settings.field_detector.hsv_lower == (35, 40, 40)
        assert settings.logging.format == "json"
        assert settings.reporting.api_url == "http://mock_api:5000"

    def test_a_section_may_be_absent_only_if_required(self, config_data, write_config):
        # Every block is required: there is no implicit section either.
        del config_data["aggregation"]
        with pytest.raises(ConfigError, match="aggregation"):
            load_settings(write_config(config_data), environ={})

    def test_settings_are_immutable(self, config_data, write_config):
        settings = load_settings(write_config(config_data), environ={})
        with pytest.raises(ValidationError):
            settings.video.target_fps = 99.0

    def test_nested_sections_are_immutable(self, config_data, write_config):
        settings = load_settings(write_config(config_data), environ={})
        with pytest.raises(ValidationError):
            settings.field_detector.min_area = 1


# --------------------------------------------------------------------------------------
# The file itself is unusable
# --------------------------------------------------------------------------------------


class TestFileProblems:
    def test_missing_file(self, tmp_path: Path):
        with pytest.raises(ConfigError) as exc:
            load_settings(tmp_path / "absent.yaml", environ={})
        assert "not found" in str(exc.value)
        assert "absent.yaml" in str(exc.value)

    def test_path_is_a_directory(self, tmp_path: Path):
        # Windows surfaces this as a PermissionError and POSIX as IsADirectoryError,
        # so assert the contract (a ConfigError naming the path), not the wording.
        with pytest.raises(ConfigError) as exc:
            load_settings(tmp_path, environ={})
        assert str(tmp_path) in str(exc.value)

    def test_empty_file(self, write_config):
        with pytest.raises(ConfigError, match="empty"):
            load_settings(write_config(""), environ={})

    def test_whitespace_only_file(self, write_config):
        with pytest.raises(ConfigError, match="empty"):
            load_settings(write_config("   \n\n"), environ={})

    def test_malformed_yaml(self, write_config):
        with pytest.raises(ConfigError, match="not valid YAML"):
            load_settings(write_config("video:\n  path: [unclosed\n"), environ={})

    def test_top_level_must_be_a_mapping(self, write_config):
        with pytest.raises(ConfigError, match="must be a mapping"):
            load_settings(write_config([{"video": {}}]), environ={})

    def test_top_level_scalar_is_rejected(self, write_config):
        with pytest.raises(ConfigError, match="must be a mapping"):
            load_settings(write_config("just a string"), environ={})

    def test_error_message_names_the_file(self, write_config, config_data):
        set_value(config_data, "video.target_fps", -1)
        path = write_config(config_data)
        with pytest.raises(ConfigError) as exc:
            load_settings(path, environ={})
        assert str(path) in str(exc.value)


# --------------------------------------------------------------------------------------
# Unknown keys -- the typo case, which is the whole reason extra="forbid" is on
# --------------------------------------------------------------------------------------


class TestUnknownKeys:
    def test_misspelled_nested_key_names_both_the_typo_and_the_missing_field(
        self, config_data, write_config
    ):
        del config_data["field_detector"]["min_area"]
        config_data["field_detector"]["min_are"] = 1000

        with pytest.raises(ConfigError) as exc:
            load_settings(write_config(config_data), environ={})

        message = str(exc.value)
        assert "min_are" in message, "the offending key must be named"
        assert "Extra inputs are not permitted" in message
        assert "min_area" in message, "and the key that is now missing"

    def test_unknown_top_level_key(self, config_data, write_config):
        config_data["debug_mode"] = True
        with pytest.raises(ConfigError, match="debug_mode"):
            load_settings(write_config(config_data), environ={})

    def test_unknown_key_inside_a_nested_section(self, config_data, write_config):
        config_data["reporting"]["retries"] = 5
        with pytest.raises(ConfigError, match="retries"):
            load_settings(write_config(config_data), environ={})

    def test_unknown_detector_parameter(self, config_data, write_config):
        config_data["field_detector"]["model_path"] = "sam.onnx"
        with pytest.raises(ConfigError, match="model_path"):
            load_settings(write_config(config_data), environ={})


# --------------------------------------------------------------------------------------
# Values that are present but wrong
# --------------------------------------------------------------------------------------


class TestValueValidation:
    @pytest.mark.parametrize(
        ("key", "bad_value", "expected"),
        [
            ("video.target_fps", -1, "greater than 0"),
            ("video.target_fps", 0, "greater than 0"),
            ("video.target_fps", 500, "less than or equal to 240"),
            ("video.target_fps", "lots", "unable to parse"),
            ("field_detector.type", "sam_mask_v1", "should be 'green_threshold'"),
            ("field_detector.sport", "", "at least 1 character"),
            ("field_detector.min_area", 0, "greater than 0"),
            ("field_detector.min_area", -5, "greater than 0"),
            ("aggregation.min_confidence", 1.5, "less than or equal to 1"),
            ("aggregation.min_confidence", -0.1, "greater than or equal to 0"),
            ("aggregation.consensus_polygon_window", 0, "greater than 0"),
            ("aggregation.min_valid_samples", 0, "greater than 0"),
            ("reporting.timeout_seconds", 0, "greater than 0"),
            ("reporting.timeout_seconds", 120, "less than or equal to 60"),
            ("reporting.max_attempts", 0, "greater than or equal to 1"),
            ("reporting.max_attempts", 99, "less than or equal to 10"),
            ("reporting.progress_every_frames", 0, "greater than 0"),
            ("reporting.progress_every_seconds", 0, "greater than 0"),
            ("logging.level", "TRACE", "Input should be 'DEBUG'"),
            ("logging.format", "yaml", "Input should be 'json'"),
        ],
    )
    def test_out_of_range_or_wrong_type_is_rejected(
        self, config_data, write_config, key, bad_value, expected
    ):
        set_value(config_data, key, bad_value)
        with pytest.raises(ConfigError) as exc:
            load_settings(write_config(config_data), environ={})

        message = str(exc.value)
        assert expected in message, f"expected {expected!r} in: {message}"
        assert key in message, "the offending key must be named"

    @pytest.mark.parametrize(
        "key", ["video", "field_detector", "aggregation", "reporting", "logging"]
    )
    def test_every_block_is_required(self, config_data, write_config, key):
        del config_data[key]
        with pytest.raises(ConfigError, match=key):
            load_settings(write_config(config_data), environ={})

    @pytest.mark.parametrize(
        "key",
        [
            "video.target_fps",
            "field_detector.sport",
            "field_detector.min_area",
            "field_detector.hsv_lower",
            "field_detector.hsv_upper",
            "aggregation.min_confidence",
            "aggregation.early_exit",
            "reporting.api_url",
            "logging.level",
        ],
    )
    def test_no_field_has_an_implicit_default(self, config_data, write_config, key):
        """Removing any field fails, rather than falling back to a built-in value.

        This is the specific defect being guarded against: the prototype silently
        substituted 500 for min_area and "soccer" for sport.
        """
        *parents, leaf = key.split(".")
        cursor: Any = config_data
        for parent in parents:
            cursor = cursor[parent]
        del cursor[leaf]

        with pytest.raises(ConfigError) as exc:
            load_settings(write_config(config_data), environ={})
        assert leaf in str(exc.value)


class TestCrossFieldValidation:
    def test_hsv_lower_above_upper(self, config_data, write_config):
        set_value(config_data, "field_detector.hsv_lower", [95, 40, 40])
        with pytest.raises(ConfigError) as exc:
            load_settings(write_config(config_data), environ={})
        message = str(exc.value)
        assert "must not exceed" in message
        assert "hue" in message

    def test_hue_outside_opencv_range(self, config_data, write_config):
        # OpenCV hue is 0-179 for 8-bit images, not the 0-360 people expect.
        set_value(config_data, "field_detector.hsv_upper", [300, 255, 255])
        with pytest.raises(ConfigError, match="between 0 and 179"):
            load_settings(write_config(config_data), environ={})

    def test_saturation_outside_byte_range(self, config_data, write_config):
        set_value(config_data, "field_detector.hsv_upper", [85, 300, 255])
        with pytest.raises(ConfigError, match="between 0 and 255"):
            load_settings(write_config(config_data), environ={})

    def test_early_exit_needs_at_least_two_samples(self, config_data, write_config):
        set_value(config_data, "aggregation.early_exit", True)
        set_value(config_data, "aggregation.min_valid_samples", 1)
        with pytest.raises(ConfigError, match="at least 2"):
            load_settings(write_config(config_data), environ={})

    @pytest.mark.parametrize("url", ["mock_api:5000", "ftp://host/report", "//host", "host"])
    def test_api_url_must_be_http(self, config_data, write_config, url):
        set_value(config_data, "reporting.api_url", url)
        with pytest.raises(ConfigError, match="http"):
            load_settings(write_config(config_data), environ={})

    def test_api_url_trailing_slash_is_normalised(self, config_data, write_config):
        set_value(config_data, "reporting.api_url", "http://mock_api:5000/")
        settings = load_settings(write_config(config_data), environ={})
        assert settings.reporting.api_url == "http://mock_api:5000"


# --------------------------------------------------------------------------------------
# Environment overrides: allowed, but never a way around validation
# --------------------------------------------------------------------------------------


class TestEnvironmentOverrides:
    def test_overrides_apply(self, config_data, write_config):
        settings = load_settings(
            write_config(config_data),
            environ={
                "TRACKBOX_VIDEO_PATH": "other.mp4",
                "TRACKBOX_TARGET_FPS": "12.5",
                "TRACKBOX_API_URL": "http://localhost:5000",
                "TRACKBOX_LOG_LEVEL": "DEBUG",
                "TRACKBOX_LOG_FORMAT": "console",
            },
        )
        assert settings.video.path == Path("other.mp4")
        assert settings.video.target_fps == 12.5
        assert settings.reporting.api_url == "http://localhost:5000"
        assert settings.logging.level == "DEBUG"
        assert settings.logging.format == "console"

    def test_overrides_do_not_mutate_the_file(self, config_data, write_config):
        path = write_config(config_data)
        original = path.read_text(encoding="utf-8")
        load_settings(path, environ={"TRACKBOX_TARGET_FPS": "12.5"})
        assert path.read_text(encoding="utf-8") == original

    def test_invalid_override_is_rejected_like_the_file_would_be(self, config_data, write_config):
        with pytest.raises(ConfigError) as exc:
            load_settings(write_config(config_data), environ={"TRACKBOX_TARGET_FPS": "abc"})
        assert "video.target_fps" in str(exc.value)

    def test_override_of_a_literal_is_still_constrained(self, config_data, write_config):
        with pytest.raises(ConfigError, match="logging.level"):
            load_settings(write_config(config_data), environ={"TRACKBOX_LOG_LEVEL": "TRACE"})

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_blank_override_is_ignored_not_treated_as_empty_value(
        self, config_data, write_config, blank
    ):
        settings = load_settings(write_config(config_data), environ={"TRACKBOX_TARGET_FPS": blank})
        assert settings.video.target_fps == 5.0

    def test_unset_variables_leave_values_alone(self, config_data, write_config):
        settings = load_settings(write_config(config_data), environ={})
        assert settings.video.target_fps == 5.0

    def test_environment_does_not_leak_from_the_process(
        self, config_data, write_config, monkeypatch
    ):
        """Passing ``environ`` explicitly must fully isolate the loader."""
        monkeypatch.setenv("TRACKBOX_TARGET_FPS", "99")
        settings = load_settings(write_config(config_data), environ={})
        assert settings.video.target_fps == 5.0


# --------------------------------------------------------------------------------------
# The failure maps onto the documented exit code
# --------------------------------------------------------------------------------------


class TestExitCode:
    def test_config_error_maps_to_exit_code_2(self, tmp_path: Path):
        with pytest.raises(ConfigError) as exc:
            load_settings(tmp_path / "absent.yaml", environ={})
        assert exc.value.exit_code is ExitCode.CONFIG
        assert int(exc.value.exit_code) == 2

    def test_exit_codes_are_distinct(self):
        codes = [int(code) for code in ExitCode]
        assert len(codes) == len(set(codes)), "exit codes must not collide"
        assert int(ExitCode.OK) == 0
