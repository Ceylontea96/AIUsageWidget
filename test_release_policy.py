"""Run the real PowerShell version guard with offline remote responses."""
import shutil
import subprocess
import unittest
from pathlib import Path


class ReleasePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shell = shutil.which("powershell") or shutil.which("pwsh")
        if not cls.shell:
            raise unittest.SkipTest("PowerShell is required for publish script tests")
        cls.policy = str(Path(__file__).with_name("release_policy.ps1")).replace("'", "''")

    def run_policy(self, command, accepted):
        script = "$ErrorActionPreference='Stop'; . '" + self.policy + "'; try { " + command + "; 'ACCEPT' } catch { 'REJECT: ' + $_.Exception.Message; exit 7 }"
        result = subprocess.run([self.shell, "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(result.returncode, 0 if accepted else 7, result.stdout + result.stderr)
        self.assertIn("ACCEPT" if accepted else "REJECT:", result.stdout)

    def test_equal_version_rejected(self):
        self.run_policy("Assert-NewReleaseVersion '3.4.1' @('v3.4.1')", False)

    def test_lower_version_rejected(self):
        self.run_policy("Assert-NewReleaseVersion '3.4.0' @('3.4.1')", False)

    def test_higher_patch_accepted(self):
        self.run_policy("Assert-NewReleaseVersion '3.4.2' @('3.4.1')", True)

    def test_higher_minor_accepted(self):
        self.run_policy("Assert-NewReleaseVersion '3.5.0' @('3.4.1')", True)

    def test_higher_major_accepted(self):
        self.run_policy("Assert-NewReleaseVersion '4.0.0' @('3.4.1')", True)

    def test_remote_latest_equal_rejected(self):
        self.run_policy("function Invoke-RestMethod { @{version='3.4.1'} }; "
                        "Assert-PublishAllowed -Version '3.4.1' -Feed 'https://test.invalid/latest.json'", False)

    def test_existing_release_rejected_even_if_latest_lags(self):
        self.run_policy("function Invoke-RestMethod { @{version='3.4.0'} }; "
                        "function Fake-Gh { $global:LASTEXITCODE=0; 'v3.4.0'; 'v3.4.1' }; "
                        "Assert-PublishAllowed -Version '3.4.1' -Feed 'https://test.invalid/latest.json' "
                        "-GitHubExecutable Fake-Gh -Repository 'owner/repo'", False)

    def test_verified_higher_target_accepted(self):
        self.run_policy("function Invoke-RestMethod { @{version='3.4.0'} }; "
                        "function Fake-Gh { $global:LASTEXITCODE=0; 'v3.3.0'; 'v3.4.0' }; "
                        "Assert-PublishAllowed -Version '3.4.1' -Feed 'https://test.invalid/latest.json' "
                        "-GitHubExecutable Fake-Gh -Repository 'owner/repo'", True)

    def test_unverifiable_remote_fails_closed(self):
        self.run_policy("function Invoke-RestMethod { throw 'offline' }; "
                        "Assert-PublishAllowed -Version '3.4.1' -Feed 'https://test.invalid/latest.json'", False)

    def test_invalid_feed_version_fails_closed(self):
        self.run_policy("function Invoke-RestMethod { @{notes='no version'} }; "
                        "Assert-PublishAllowed -Version '3.4.1' -Feed 'https://test.invalid/latest.json'", False)

    def test_release_query_failure_fails_closed(self):
        self.run_policy("function Fake-Gh { $global:LASTEXITCODE=1 }; "
                        "Assert-PublishAllowed -Version '3.4.1' -GitHubExecutable Fake-Gh -Repository 'owner/repo'", False)

    def test_publish_guard_precedes_build_and_has_no_overwrite_path(self):
        script = Path(__file__).with_name("publish_update.ps1").read_text(encoding="utf-8-sig")
        self.assertLess(script.index("Assert-PublishAllowed -Version"), script.index("'build_launcher.ps1'"))
        self.assertNotIn("--clobber", script)
        self.assertNotIn("release upload", script)
        self.assertNotIn("release edit", script)
        self.assertIn("release create", script)


if __name__ == "__main__":
    unittest.main()
