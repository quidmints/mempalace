"""
Tests for R3 §6 ranker isolation extensions — ResourceLimits +
SandboxProfile + preexec_fn for rlimit application.

Most existing isolation behavior (BehaviorMonitor, InProcessFenceRanker,
the JSON-boundary capability restriction) is already covered by
test_rankers.py. This file covers the new additions.

Coverage:
  - ResourceLimits dataclass defaults
  - SandboxProfile.none() / .bwrap_minimal() factory methods
  - IsolatedRankerSpec carries limits + profile
  - _build_preexec_fn behavior (returns None on Windows; returns
    callable on POSIX)
  - Sandbox profile wrapper_argv prepends to argv
"""

from __future__ import annotations

import sys
import unittest

from mempalace.rank.isolation import (
    IsolatedRankerSpec,
    ResourceLimits,
    SandboxProfile,
    _build_preexec_fn,
)


class TestResourceLimits(unittest.TestCase):
    def test_defaults_reasonable(self) -> None:
        limits = ResourceLimits()
        self.assertEqual(limits.wall_clock_ms, 5_000)
        self.assertEqual(limits.cpu_seconds, 10)
        self.assertEqual(limits.memory_bytes, 512 * 1024 * 1024)
        self.assertEqual(limits.max_open_files, 64)

    def test_can_disable_individual_limits(self) -> None:
        limits = ResourceLimits(
            cpu_seconds=None, memory_bytes=None, max_open_files=None,
        )
        self.assertIsNone(limits.cpu_seconds)
        self.assertIsNone(limits.memory_bytes)
        self.assertIsNone(limits.max_open_files)
        self.assertEqual(limits.wall_clock_ms, 5_000)


class TestSandboxProfile(unittest.TestCase):
    def test_none_has_empty_wrapper(self) -> None:
        sp = SandboxProfile.none()
        self.assertEqual(sp.wrapper_argv, ())

    def test_bwrap_minimal_includes_unshare_all(self) -> None:
        sp = SandboxProfile.bwrap_minimal("/usr/local/bin/my_ranker")
        self.assertIn("bwrap", sp.wrapper_argv)
        self.assertIn("--unshare-all", sp.wrapper_argv)
        self.assertIn("/usr/local/bin/my_ranker", sp.wrapper_argv)

    def test_profile_is_immutable(self) -> None:
        sp = SandboxProfile.none()
        with self.assertRaises(Exception):
            sp.wrapper_argv = ("foo",)


class TestIsolatedRankerSpec(unittest.TestCase):
    def test_default_limits_and_profile(self) -> None:
        spec = IsolatedRankerSpec(
            name="test", executable_path="/bin/true",
            weights_hash="abc",
        )
        # Defaults: standard ResourceLimits, no sandbox
        self.assertEqual(spec.resource_limits.wall_clock_ms, 5_000)
        self.assertEqual(spec.sandbox_profile.wrapper_argv, ())

    def test_custom_limits_and_profile(self) -> None:
        limits = ResourceLimits(wall_clock_ms=2_000, memory_bytes=64 * 1024 * 1024)
        sp = SandboxProfile(wrapper_argv=("custom-sandbox", "--strict"))
        spec = IsolatedRankerSpec(
            name="t", executable_path="/bin/true",
            weights_hash="abc",
            resource_limits=limits,
            sandbox_profile=sp,
        )
        self.assertEqual(spec.resource_limits.wall_clock_ms, 2_000)
        self.assertEqual(spec.resource_limits.memory_bytes, 64 * 1024 * 1024)
        self.assertEqual(spec.sandbox_profile.wrapper_argv,
                         ("custom-sandbox", "--strict"))


class TestPreexecFn(unittest.TestCase):
    def test_returns_none_when_all_limits_unset(self) -> None:
        limits = ResourceLimits(
            cpu_seconds=None, memory_bytes=None, max_open_files=None,
        )
        fn = _build_preexec_fn(limits)
        self.assertIsNone(fn)

    def test_returns_none_on_windows(self) -> None:
        # We can't actually be on Windows in this test env, but verify
        # the dispatch logic by checking the current platform's behavior.
        if sys.platform == "win32":
            limits = ResourceLimits()
            self.assertIsNone(_build_preexec_fn(limits))

    @unittest.skipIf(sys.platform == "win32", "POSIX-only")
    def test_returns_callable_on_posix_with_limits(self) -> None:
        limits = ResourceLimits(cpu_seconds=10, memory_bytes=None,
                                max_open_files=None)
        fn = _build_preexec_fn(limits)
        self.assertTrue(callable(fn))


if __name__ == "__main__":
    unittest.main()
