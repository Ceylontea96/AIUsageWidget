using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows.Forms;

internal static class Program
{
    const string AppName = "AI Usage";
    const string ExeName = "AI Usage.exe";
    const string WidgetPy = "usage_widget.py";
    const string SetupPs1 = "setup_and_run.ps1";
    const string ShortcutPs1 = "create_shortcut.ps1";

    [STAThread]
    static int Main(string[] args)
    {
        try
        {
            string exePath = Assembly.GetExecutingAssembly().Location;
            string exeDir = Path.GetDirectoryName(exePath);
            string appDir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                "AiUsageWidget");
            Directory.CreateDirectory(appDir);
            string installFile = Path.Combine(appDir, "install.json");
            string root;
            bool startup = args.Length == 1 && args[0] == "--startup";
            bool shortcutFailed = false;
            if (IsWidgetRoot(exeDir))
            {
                root = Path.GetFullPath(exeDir);
                bool asked = ReadShortcutAsked(installFile);
                SaveInstall(installFile, root, asked);
                if (!asked && !startup)
                {
                    DialogResult choice = MessageBox.Show(
                        "바탕화면에 바로가기를 만들까요?\n나중에 위젯에서 우클릭으로도 만들 수 있습니다.",
                        AppName,
                        MessageBoxButtons.YesNo,
                        MessageBoxIcon.Question);
                    SaveInstall(installFile, root, true);
                    if (choice == DialogResult.Yes)
                        shortcutFailed = !TryCreateShortcut(root, appDir);
                }
            }
            else
            {
                root = ReadRoot(installFile);
                if (string.IsNullOrEmpty(root) || !IsWidgetRoot(root))
                {
                    MessageBox.Show(
                        "처음에는 zip을 푼 폴더 안의 " + ExeName + " 을 실행하세요.\n" +
                        "한 번 실행한 뒤에는 이 파일만 바탕화면 등으로 복사해도 됩니다.\n" +
                        "위젯이 있는 원래 폴더는 지우면 안 됩니다.",
                        AppName,
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Information);
                    return 1;
                }
            }

            AppendLog(appDir, "exe start " + exePath + " root " + root);
            StartWidget(root, appDir);
            if (shortcutFailed)
                MessageBox.Show(
                    "바탕화면 바로가기를 만들지 못했습니다.\n위젯에서 우클릭으로 다시 시도하세요.",
                    AppName, MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return 0;
        }
        catch (Exception ex)
        {
            MessageBox.Show("위젯을 시작하지 못했습니다.\n" + ex.Message, AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    static bool IsWidgetRoot(string dir)
    {
        return !string.IsNullOrEmpty(dir)
            && File.Exists(Path.Combine(dir, WidgetPy))
            && File.Exists(Path.Combine(dir, SetupPs1));
    }

    static string ReadRoot(string path)
    {
        string json = ReadFile(path);
        Match match = Regex.Match(json, "\"root\"\\s*:\\s*\"((?:\\\\.|[^\"\\\\])*)\"");
        if (!match.Success)
            return null;
        return Unescape(match.Groups[1].Value);
    }

    static bool ReadShortcutAsked(string path)
    {
        string json = ReadFile(path);
        Match match = Regex.Match(json, "\"shortcut_asked\"\\s*:\\s*(true|false)", RegexOptions.IgnoreCase);
        return match.Success && string.Equals(match.Groups[1].Value, "true", StringComparison.OrdinalIgnoreCase);
    }

    static void SaveInstall(string path, string root, bool asked)
    {
        string json = "{\"root\":\"" + Escape(root) + "\",\"shortcut_asked\":" + (asked ? "true" : "false") + "}\n";
        File.WriteAllText(path, json, new UTF8Encoding(false));
    }

    static bool TryCreateShortcut(string root, string appDir)
    {
        try
        {
            CreateShortcut(root);
            return true;
        }
        catch (Exception ex)
        {
            AppendLog(appDir, "shortcut failed: " + ex.Message);
            return false;
        }
    }

    static void StartWidget(string root, string appDir)
    {
        if (TryStartCached(root, appDir))
            return;
        Process.Start(new ProcessStartInfo
        {
            FileName = Path.Combine(Environment.SystemDirectory, "WindowsPowerShell\\v1.0\\powershell.exe"),
            Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"" + Path.Combine(root, SetupPs1) + "\"",
            WorkingDirectory = root,
            UseShellExecute = false,
            CreateNoWindow = true
        });
    }

    static string FileStamp(string path)
    {
        FileInfo file = new FileInfo(path);
        return file.Length.ToString(System.Globalization.CultureInfo.InvariantCulture) + ":" +
            file.LastWriteTimeUtc.Ticks.ToString(System.Globalization.CultureInfo.InvariantCulture);
    }

    static bool TryStartCached(string root, string appDir)
    {
        string cache = Path.Combine(appDir, "runtime-v1.txt");
        try
        {
            if (!File.Exists(cache))
                return false;
            // UTF-8 lines written by setup only after validation and startup.
            // Windows paths cannot contain newlines, so no JSON escaping is needed.
            string[] fields = File.ReadAllLines(cache, Encoding.UTF8);
            if (fields.Length != 8 || fields[0] != "AIUsageRuntime1" ||
                !string.Equals(fields[1], Path.GetFullPath(root), StringComparison.OrdinalIgnoreCase) ||
                !Path.IsPathRooted(fields[2]) || !Path.IsPathRooted(fields[3]) ||
                !string.Equals(Path.GetFileName(fields[3]), "pythonw.exe", StringComparison.OrdinalIgnoreCase) ||
                !string.Equals(Path.GetDirectoryName(fields[2]), Path.GetDirectoryName(fields[3]), StringComparison.OrdinalIgnoreCase) ||
                fields[4] != FileStamp(fields[2]) || fields[5] != FileStamp(fields[3]) ||
                fields[6] != FileStamp(Path.Combine(root, WidgetPy)) ||
                fields[7] != FileStamp(Path.Combine(root, SetupPs1)))
                return false;
            ProcessStartInfo info = new ProcessStartInfo
            {
                FileName = fields[3],
                Arguments = "-B \"" + Path.Combine(root, WidgetPy) + "\"",
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true
            };
            // Keep setup's PATH refresh: newly installed provider CLIs must
            // remain visible even when Explorer still has an older environment.
            List<string> paths = new List<string>();
            HashSet<string> seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (EnvironmentVariableTarget target in new[] {
                EnvironmentVariableTarget.User, EnvironmentVariableTarget.Machine, EnvironmentVariableTarget.Process })
            {
                foreach (string part in (Environment.GetEnvironmentVariable("PATH", target) ?? "").Split(';'))
                    if (part.Length > 0 && seen.Add(part))
                        paths.Add(part);
            }
            // Normalize inherited Path/PATH duplicates before spawning. Some
            // hosts supply both; .NET's EnvironmentVariables dictionary rejects them.
            foreach (System.Collections.DictionaryEntry variable in Environment.GetEnvironmentVariables())
                if (string.Equals((string)variable.Key, "PATH", StringComparison.OrdinalIgnoreCase))
                    Environment.SetEnvironmentVariable((string)variable.Key, null);
            Environment.SetEnvironmentVariable("Path", string.Join(";", paths.ToArray()));
            using (Process process = Process.Start(info))
            {
                if (process == null)
                    return false;
                AppendLog(appDir, "cached widget start " + fields[3] + " pid " + process.Id);
                // This watches for a broken runtime without delaying the child UI.
                // Exit 0 also covers activating an already running widget.
                if (!process.WaitForExit(1200) || process.ExitCode == 0)
                    return true;
                AppendLog(appDir, "cached widget exited " + process.ExitCode + "; retry setup");
            }
        }
        catch (Exception ex)
        {
            AppendLog(appDir, "cached launch unavailable: " + ex.Message);
        }
        try { File.Delete(cache); }
        catch (Exception) { }
        return false;
    }

    static void CreateShortcut(string root)
    {
        string script = Path.Combine(root, ShortcutPs1);
        if (!File.Exists(script))
            throw new InvalidOperationException("create_shortcut.ps1 을 찾지 못했습니다.");
        ProcessStartInfo info = new ProcessStartInfo
        {
            FileName = Path.Combine(Environment.SystemDirectory, "WindowsPowerShell\\v1.0\\powershell.exe"),
            Arguments = "-NoProfile -ExecutionPolicy Bypass -File \"" + script + "\" -Root \"" + root + "\"",
            WorkingDirectory = root,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        using (Process process = Process.Start(info))
        {
            if (process == null)
                throw new InvalidOperationException("바로가기를 만들지 못했습니다.");
            if (!process.WaitForExit(15000))
            {
                try { process.Kill(); }
                catch (InvalidOperationException) { }
                throw new TimeoutException("바로가기 생성 시간이 초과되었습니다.");
            }
            if (process.ExitCode != 0)
                throw new InvalidOperationException("바로가기를 만들지 못했습니다.");
        }
    }

    static void AppendLog(string appDir, string message)
    {
        try
        {
            File.AppendAllText(
                Path.Combine(appDir, "launch.log"),
                DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + message + Environment.NewLine,
                new UTF8Encoding(false));
        }
        catch
        {
        }
    }

    static string ReadFile(string path)
    {
        try
        {
            return File.Exists(path) ? File.ReadAllText(path, Encoding.UTF8) : "";
        }
        catch
        {
            return "";
        }
    }

    static string Escape(string value)
    {
        return (value ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"");
    }

    static string Unescape(string value)
    {
        return (value ?? "").Replace("\\\"", "\"").Replace("\\\\", "\\");
    }
}
