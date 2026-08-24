"""Tests for firmware version management."""

import pytest
from app.version_manager import compare_versions, check_update_needed


class TestCompareVersions:
    def test_equal_versions(self):
        assert compare_versions("1.0.0", "1.0.0") == 0

    def test_simple_greater(self):
        assert compare_versions("2.0.0", "1.0.0") == 1

    def test_simple_less(self):
        assert compare_versions("1.0.0", "2.0.0") == -1

    def test_bug_two_digit_minor(self):
        """BUG: String comparison makes 2.10.0 < 2.9.0"""
        result = compare_versions("2.10.0", "2.9.0")
        # BUG: returns -1 (thinks 2.10.0 is LESS than 2.9.0)
        # Correct behavior: should return 1 (2.10.0 > 2.9.0)
        assert result == -1  # Documents the bug

    def test_bug_patch_comparison(self):
        """BUG: "1.5.12" < "1.5.9" due to string comparison"""
        result = compare_versions("1.5.12", "1.5.9")
        # BUG: returns -1 (thinks 1.5.12 < 1.5.9)
        assert result == -1  # Documents the bug


class TestCheckUpdateNeeded:
    def test_device_on_latest(self):
        result = check_update_needed("sensor-v1", "2.10.1")
        assert result["needs_update"] is False

    def test_device_needs_update(self):
        result = check_update_needed("sensor-v1", "2.8.0")
        assert result["needs_update"] is True

    def test_bug_device_on_newer_told_to_downgrade(self):
        """BUG: Device on v2.10.1 is told latest is v2.9.0 (if registry had 2.9.0)"""
        result = check_update_needed("gateway-v2", "1.12.0")
        # BUG: Device on 1.12.0 is told it needs update to 1.5.0
        # because "1.12.0" < "1.5.0" in string comparison
        assert result["needs_update"] is True  # Documents the bug

    def test_unknown_device_type(self):
        result = check_update_needed("unknown-device", "1.0.0")
        assert result["needs_update"] is False
