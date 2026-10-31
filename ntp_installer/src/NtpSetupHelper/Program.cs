using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.ServiceProcess;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace NtpSetupHelper
{
    internal static class Program
    {
        [STAThread]
        private static void Main()
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new MainForm());
        }
    }

    internal class MainForm : Form
    {
        private static readonly string[] NtpInstallDirs =
        {
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86), "NTP"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "NTP")
        };

        // 内网专用配置：以本机时钟为唯一时间源（无外网依赖），对外授时
        private const string NtpConfContent =
            "restrict 127.0.0.1\r\n" +
            "restrict -6 ::1\r\n" +
            "restrict 0.0.0.0 mask 0.0.0.0 nomodify notrap\r\n" +
            "driftfile \"C:\\Program Files (x86)\\NTP\\etc\\ntp.drift\"\r\n" +
            "server 127.127.1.0\r\n" +
            "fudge 127.127.1.0 stratum 10\r\n";

        private static readonly string LogFile = Path.Combine(Path.GetTempPath(), "NtpSetupHelper.log");

        private readonly TextBox _log;
        private readonly Button _btnSetup;
        private readonly Button _btnTest;
        private readonly Button _btnClose;
        private bool _isRunning;

        public MainForm()
        {
            Text = "NTP 服务器一键设置工具 (静默安装/配置/测试)";
            Icon = LoadEmbeddedIcon();
            StartPosition = FormStartPosition.CenterScreen;
            Size = new Size(680, 480);
            MinimumSize = new Size(680, 480);

            _log = new TextBox
            {
                Multiline = true,
                ReadOnly = true,
                ScrollBars = ScrollBars.Vertical,
                BackColor = Color.White,
                Dock = DockStyle.Fill,
                Font = new Font("Consolas", 9F)
            };

            _btnSetup = new Button { Text = "一键设置 / 重新配置", Width = 190, Dock = DockStyle.Left };
            _btnTest = new Button { Text = "测试 NTP 时间", Width = 140, Dock = DockStyle.Left };
            _btnClose = new Button { Text = "关闭", Width = 90, Dock = DockStyle.Right };
            _btnSetup.Click += async (s, e) => await RunSetupFlowAsync(false);
            _btnTest.Click += async (s, e) => await TestNtpTimeAsync();
            _btnClose.Click += (s, e) => Close();

            var bottom = new Panel { Height = 42, Dock = DockStyle.Bottom, Padding = new Padding(6) };
            bottom.Controls.Add(_btnClose);
            bottom.Controls.Add(_btnTest);
            bottom.Controls.Add(_btnSetup);

            Controls.Add(_log);
            Controls.Add(bottom);

            Shown += async (s, e) => await AutoFlowAsync();
        }

        private static void FileLog(string msg)
        {
            try { File.AppendAllText(LogFile, "[" + DateTime.Now.ToString("HH:mm:ss") + "] " + msg + Environment.NewLine); } catch { }
        }

        private static Icon LoadEmbeddedIcon()
        {
            try
            {
                using (Stream s = Assembly.GetExecutingAssembly().GetManifestResourceStream("NtpSetupHelper.ntp.ico"))
                    return s == null ? null : new Icon(s);
            }
            catch { return null; }
        }

        private void Log(string msg)
        {
            FileLog(msg);
            if (IsDisposed) return;
            if (InvokeRequired)
            {
                try { Invoke(new Action<string>(AppendUi), msg); } catch { }
                return;
            }
            AppendUi(msg);
        }

        private void AppendUi(string msg)
        {
            if (IsDisposed) return;
            _log.AppendText("[" + DateTime.Now.ToString("HH:mm:ss") + "] " + msg + Environment.NewLine);
        }

        private void SetRunning(bool running)
        {
            if (InvokeRequired) { try { Invoke(new Action(() => SetRunning(running))); } catch { } return; }
            _isRunning = running;
            _btnSetup.Enabled = !running;
            _btnTest.Enabled = !running;
        }

        private static string FindNtpInstallDir()
        {
            return NtpInstallDirs.FirstOrDefault(d => Directory.Exists(d));
        }

        private static bool IsServiceInstalled()
        {
            return ServiceController.GetServices()
                .Any(s => s.ServiceName.Equals("NTP", StringComparison.OrdinalIgnoreCase) ||
                          s.DisplayName.Equals("Network Time Protocol", StringComparison.OrdinalIgnoreCase));
        }

        // 启动后自动流程：已配置则跳过安装并直接测试；未配置则全自动安装配置
        // 启动后自动流程：配置一致则跳过并测试；否则自动重新配置（无弹框）
        private async Task AutoFlowAsync()
        {
            Log("程序已启动（管理员权限）。");
            string installDir = FindNtpInstallDir();
            string confPath = installDir == null ? null : Path.Combine(installDir, "etc", "ntp.conf");
            bool configured = confPath != null && IsServiceInstalled() && File.Exists(confPath);
            if (configured)
            {
                string existing = File.ReadAllText(confPath).Trim();
                if (existing == NtpConfContent.Trim())
                {
                    Log("检测到 NTP 服务器配置与目标配置一致，跳过安装配置。");
                    await TestNtpTimeAsync();
                    return;
                }
                Log("检测到已有配置但与目标配置不一致，自动重新配置。");
            }
            await RunSetupFlowAsync(false);
            await TestNtpTimeAsync();
        }

        // 完整流程：安装（如需要）-> 写配置 -> 重启服务 -> 测试
        private async Task RunSetupFlowAsync(bool unused)
        {
            if (_isRunning) return;
            SetRunning(true);
            try
            {
                string installDir = FindNtpInstallDir();

                if (installDir == null || !IsServiceInstalled())
                {
                    Log("未检测到 NTP 服务，开始静默安装 Meinberg NTP Server（自动接受许可，无弹框）...");
                    await Task.Run(() => InstallNtpServerSilently());
                    installDir = FindNtpInstallDir();
                }
                else
                {
                    Log("检测到已安装 NTP 服务，跳过安装步骤。");
                }

                if (installDir == null)
                {
                    Log("失败：未找到 NTP 安装目录。");
                    return;
                }

                if (!IsServiceInstalled())
                {
                    Log("安装程序未注册服务，尝试手动创建 NTP 服务...");
                    await Task.Run(() => CreateNtpService(installDir));
                    if (!IsServiceInstalled())
                    {
                        Log("失败：无法创建 NTP 服务。");
                        return;
                    }
                }

                Log("写入 ntp.conf 配置...");
                await Task.Run(() =>
                {
                    string etcDir = Path.Combine(installDir, "etc");
                    Directory.CreateDirectory(etcDir);
                    File.WriteAllText(Path.Combine(etcDir, "ntp.conf"), NtpConfContent, new UTF8Encoding(false));
                });
                Log("配置写入完成: " + Path.Combine(installDir, "etc", "ntp.conf"));

                Log("重启 NTP 服务...");
                await Task.Run(() => RestartNtpService());
                Log("NTP 服务已重启。");

                Log("NTP 服务器设置完成。");
            }
            catch (Exception ex)
            {
                Log("错误: " + ex.Message);
            }
            finally
            {
                SetRunning(false);
            }
        }

        private static void InstallNtpServerSilently()
        {
            string tempFile = Path.Combine(Path.GetTempPath(), "ntp-setup-" + Guid.NewGuid().ToString("N") + ".exe");
            try
            {
                using (Stream res = Assembly.GetExecutingAssembly().GetManifestResourceStream("NtpSetupHelper.NtpInstaller.exe"))
                {
                    if (res == null) throw new Exception("内嵌安装包资源未找到。");
                    using (var fs = new FileStream(tempFile, FileMode.Create, FileAccess.Write))
                        res.CopyTo(fs);
                }

                // Meinberg NTP 安装程序基于 NSIS，/S 为静默安装（自动接受许可，不弹框）
                string[][] argSets =
                {
                    new[] { "/S" },
                    new[] { "/S", "/SD" },
                    new[] { "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES", "/SP-" }
                };

                foreach (var args in argSets)
                {
                    if (IsServiceInstalled()) return;
                    var psi = new ProcessStartInfo
                    {
                        FileName = tempFile,
                        Arguments = string.Join(" ", args),
                        UseShellExecute = false,
                        CreateNoWindow = true,
                        RedirectStandardOutput = true,
                        RedirectStandardError = true
                    };
                    using (var p = Process.Start(psi))
                    {
                        if (p == null) continue;
                        string stdout = p.StandardOutput.ReadToEnd();
                        string stderr = p.StandardError.ReadToEnd();
                        if (!p.WaitForExit(10 * 60 * 1000)) { try { p.Kill(); } catch { } return; }
                        FileLog("安装程序参数 [" + string.Join(" ", args) + "] 退出码: " + p.ExitCode +
                                (stdout.Length > 0 ? " 输出: " + stdout : "") +
                                (stderr.Length > 0 ? " 错误: " + stderr : ""));
                        if (p.ExitCode == 0) return;
                    }
                }
            }
            catch (Exception ex)
            {
                FileLog("静默安装异常: " + ex.Message);
            }
            finally
            {
                try { if (File.Exists(tempFile)) File.Delete(tempFile); } catch { }
            }
        }

        private static void CreateNtpService(string installDir)
        {
            string ntpd = Path.Combine(installDir, "bin", "ntpd.exe");
            string conf = Path.Combine(installDir, "etc", "ntp.conf");
            Directory.CreateDirectory(Path.Combine(installDir, "etc"));
            if (!File.Exists(conf))
                File.WriteAllText(conf, NtpConfContent, new UTF8Encoding(false));

            RunCmd("sc", "stop ntp");
            RunCmd("sc", "delete ntp");
            Thread.Sleep(500);
            string binPath = "\\\"" + ntpd + "\\\" -c \\\"" + conf + "\\\" -g";
            RunCmd("sc", "create ntp binPath= \"" + binPath + "\" start= auto");
            RunCmd("sc", "description ntp \"Network Time Protocol\"");
        }

        private static void RestartNtpService()
        {
            string found = null;
            foreach (var name in new[] { "NTP", "ntp" })
            {
                try
                {
                    var ctl = new ServiceController(name);
                    var st = ctl.Status;
                    found = name;
                    break;
                }
                catch { }
            }
            if (found == null) throw new Exception("找不到 NTP 服务。");

            RunCmd("net", "stop \"" + found + "\"");
            Thread.Sleep(1000);
            RunCmd("net", "start \"" + found + "\"");
        }

        private static void RunCmd(string file, string args)
        {
            var psi = new ProcessStartInfo
            {
                FileName = file,
                Arguments = args,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            using (var p = Process.Start(psi))
            {
                if (p == null) return;
                p.WaitForExit(120 * 1000);
                string outLine = p.StandardOutput.ReadLine();
                string errLine = p.StandardError.ReadLine();
                if (!string.IsNullOrEmpty(outLine)) FileLog(file + " " + args + " => " + outLine);
                if (!string.IsNullOrEmpty(errLine)) FileLog(file + " " + args + " 错误 => " + errLine);
            }
        }

        private async Task TestNtpTimeAsync()
        {
            if (_isRunning) return;
            SetRunning(true);
            try
            {
                Log("NTP 工作原理：客户端向服务器 UDP 123 端口发送时间查询包，服务器从应答包的");
                Log("  发送/接收时间戳计算本机与服务器的时间偏差；ntpd 刚重启时需要先完成初始");
                Log("  同步（通常 1~5 分钟），此期间应答 stratum=0，属正常现象，本工具会自动等待。");
                Log("等待 NTP 服务就绪，然后向 127.0.0.1:123 发送查询（最多等待 5 分钟）...");
                DateTime ntpTime = await Task.Run(() =>
                {
                    // 轮询等待服务完成同步，最多 5 分钟（ntpd 初次同步通常需要数分钟）
                    for (int i = 0; i < 60; i++)
                    {
                        try { return QueryNtp("127.0.0.1"); }
                        catch (Exception ex)
                        {
                            // 区分两种未就绪原因：服务已应答但同步中（stratum=0），或端口无应答
                            bool answeredButNotSynced = ex.Message.IndexOf("未返回有效时间", StringComparison.Ordinal) >= 0;
                            string state = answeredButNotSynced
                                ? "服务已应答，尚未完成同步（stratum=0，同步中），继续等待"
                                : "服务无应答（" + ex.Message + "）";
                            if (i < 5 || (i + 1) % 12 == 0)
                                Log("第 " + (i + 1) + " 次查询: " + state);
                            FileLog("第 " + (i + 1) + " 次查询: " + state);
                            Thread.Sleep(5000);
                        }
                    }
                    throw new Exception("NTP 服务器 5 分钟内未就绪（127.0.0.1:123）。可能原因：ntpd 同步异常、UDP 123 端口被防火墙拦截，请查看 %TEMP%\\NtpSetupHelper.log 并用 ntpq -p 检查。");
                });
                TimeSpan offset = ntpTime.ToUniversalTime() - DateTime.UtcNow;
                Log("NTP 服务器时间(UTC): " + ntpTime.ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss"));
                Log("本机系统时间(UTC):  " + DateTime.UtcNow.ToString("yyyy-MM-dd HH:mm:ss"));
                Log("时间偏差: " + Math.Abs(offset.TotalMilliseconds).ToString("F0") + " ms");
                Log("测试成功：NTP 服务器工作正常。");
            }
            catch (Exception ex)
            {
                Log("NTP 测试失败: " + ex.Message);
            }
            finally
            {
                SetRunning(false);
            }
        }

        private static DateTime QueryNtp(string server)
        {
            var ntpData = new byte[48];
            ntpData[0] = 0x1B; // LI=0, VN=3, Mode=3 (client)

            var addresses = Dns.GetHostAddresses(server);
            var ip = addresses.First(a => a.AddressFamily == AddressFamily.InterNetwork);

            using (var socket = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp))
            {
                socket.ReceiveTimeout = 5000;
                socket.SendTimeout = 5000;
                socket.Connect(ip, 123);
                socket.Send(ntpData);
                socket.Receive(ntpData);
            }

            if (ntpData[1] == 0) throw new Exception("NTP 服务器未返回有效时间。");

            const ulong ntpEpoch = 2208988800UL;
            ulong seconds = ((ulong)ntpData[40] << 24) | ((ulong)ntpData[41] << 16) |
                            ((ulong)ntpData[42] << 8) | ntpData[43];
            ulong fraction = ((ulong)ntpData[44] << 24) | ((ulong)ntpData[45] << 16) |
                             ((ulong)ntpData[46] << 8) | ntpData[47];

            if (seconds < ntpEpoch) throw new Exception("NTP 返回的时间戳无效。");

            var utc = new DateTime(1900, 1, 1, 0, 0, 0, DateTimeKind.Utc)
                .AddSeconds(seconds - ntpEpoch)
                .AddMilliseconds(fraction * 1000 / (1UL << 32));
            return utc.ToLocalTime();
        }
    }
}
