using System;
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
    static int Main()
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
            if (IsWidgetRoot(exeDir))
            {
                root = Path.GetFullPath(exeDir);
                bool asked = ReadShortcutAsked(installFile);
                SaveInstall(installFile, root, asked);
                if (!asked)
                {
                    DialogResult choice = MessageBox.Show(
                        "바탕화면에 바로가기를 만들까요?\n나중에 위젯에서 우클릭으로도 만들 수 있습니다.",
                        AppName,
                        MessageBoxButtons.YesNo,
                        MessageBoxIcon.Question);
                    SaveInstall(installFile, root, true);
                    if (choice == DialogResult.Yes)
                        CreateShortcut(root);
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
            string setup = Path.Combine(root, SetupPs1);
            Process.Start(new ProcessStartInfo
            {
                FileName = Path.Combine(Environment.SystemDirectory, "WindowsPowerShell\\v1.0\\powershell.exe"),
                Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"" + setup + "\"",
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true
            });
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
            process.WaitForExit(15000);
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
