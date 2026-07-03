import os
import unittest

from backend.core.shell_profile import ShellProfileResolver, render_command_tool_guidance


class ShellProfileTests(unittest.TestCase):
    def test_windows_profile_guidance_uses_powershell_examples(self):
        profile = ShellProfileResolver(platform="windows", shell="powershell").resolve()

        self.assertEqual(profile.platform, "windows")
        self.assertEqual(profile.id, "powershell")
        self.assertEqual(profile.highlighter_language, "powershell")

        guidance = render_command_tool_guidance(profile)
        self.assertIn("active shell is PowerShell", guidance)
        self.assertIn("Get-ChildItem -Force", guidance)
        self.assertIn("$env:FOO", guidance)
        self.assertIn("Bash control flow", guidance)
        self.assertNotIn("FOO=bar npm test", guidance)

    def test_posix_profile_guidance_uses_bash_examples(self):
        profile = ShellProfileResolver(platform="linux", shell="bash").resolve()

        self.assertEqual(profile.platform, "linux")
        self.assertEqual(profile.id, "bash")
        self.assertEqual(profile.highlighter_language, "bash")

        guidance = render_command_tool_guidance(profile)
        self.assertIn("active shell is Bash", guidance)
        self.assertIn("ls -la", guidance)
        self.assertIn("FOO=bar npm test", guidance)
        self.assertNotIn("Get-ChildItem -Force", guidance)

    def test_auto_profile_resolves_current_platform(self):
        profile = ShellProfileResolver().resolve()

        if os.name == "nt":
            self.assertEqual(profile.platform, "windows")
            self.assertIn(profile.id, {"pwsh", "powershell", "cmd"})
            self.assertEqual(profile.path_separator, "\\")
        else:
            self.assertIn(profile.platform, {"linux", "darwin"})
            self.assertIn(profile.id, {"bash", "zsh", "sh"})
            self.assertEqual(profile.path_separator, "/")


if __name__ == "__main__":
    unittest.main()
