import os
import shutil
import subprocess
import sys
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
        self.assertIn("Native pipeline stdin uses UTF-8 without BOM", guidance)
        self.assertIn("Bash control flow", guidance)
        self.assertNotIn("FOO=bar npm test", guidance)

    @unittest.skipUnless(os.name == "nt", "requires Windows PowerShell")
    def test_windows_command_contract(self):
        tested = []
        for shell in ("powershell", "pwsh"):
            if not shutil.which(shell):
                continue
            profile = ShellProfileResolver(platform="windows", shell=shell).resolve()
            for command, stdout, returncode in (
                ("param([string]$Name = 'ok')\nWrite-Output $Name", "ok", 0),
                ("using namespace System.Text\nWrite-Output ([Encoding]::UTF8.WebName)", "utf-8", 0),
                ("#requires -Version 5.1\nWrite-Output 'requires-ok'", "requires-ok", 0),
                ("Write-Output 'before-exit'\nexit 7", "before-exit", 7),
                ("cmd.exe /d /c exit 3", "", 1),
            ):
                result = subprocess.run(
                    profile.command_argv(command),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
                self.assertEqual(result.returncode, returncode)
                self.assertEqual(result.stdout.strip(), stdout)
                self.assertEqual(result.stderr, "")

            text = "ASCII — 中文 😀"
            command = f"@'\n{text}\n'@ | & \"{sys.executable}\" -c \"import sys;print(sys.stdin.buffer.read().hex())\""

            result = subprocess.run(
                profile.command_argv(command),
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            piped = bytes.fromhex(result.stdout.strip())
            if piped.startswith(b"\xef\xbb\xbf"):
                piped = piped[3:]
            self.assertEqual(piped.rstrip(b"\r\n"), text.encode("utf-8"))
            if shell == "pwsh":
                self.assertFalse(piped.startswith(b"\xef\xbb\xbf"))

            command = (
                "$OutputEncoding = [System.Text.Encoding]::GetEncoding(936); "
                f"@'\n中文\n'@ | & \"{sys.executable}\" -c \"import sys;print(sys.stdin.buffer.read().hex())\""
            )
            result = subprocess.run(
                profile.command_argv(command),
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            gbk_piped = bytes.fromhex(result.stdout.strip())
            if gbk_piped.startswith(b"\xef\xbb\xbf"):
                gbk_piped = gbk_piped[3:]
            self.assertEqual(gbk_piped.rstrip(b"\r\n"), "中文".encode("gbk"))
            tested.append(shell)

        self.assertTrue(tested)

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
