using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace StudioWrightDesktop
{
    static class Program
    {
        public static readonly string AppDir = Path.GetDirectoryName(Application.ExecutablePath);
        public static readonly string DataDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "StudioWrightDesktop");
        public static readonly string ProfileDir = Path.Combine(DataDir, "WebView2");
        public const int DiscoveryPort = 18925;
        public const string Workbench = "http://127.0.0.1:18920/";

        public static Icon BrandIcon(int size)
        {
            using (var stream = typeof(Program).Assembly.GetManifestResourceStream("StudioWright.AppIcon.ico"))
            {
                if (stream == null) throw new InvalidOperationException("工作台图标资源缺失。");
                using (var icon = new Icon(stream, size, size)) return (Icon)icon.Clone();
            }
        }

        [STAThread]
        static void Main(string[] args)
        {
            if (args.Length == 1 && args[0] == "--self-check")
            {
                Environment.Exit(SelfCheck() ? 0 : 1); return;
            }
            if (args.Length == 1 && args[0] == "--discovery-check")
            {
                try { Environment.Exit(Discovery.Scan().Count > 0 ? 0 : 1); }
                catch { Environment.Exit(1); }
                return;
            }
            bool created;
            using (var mutex = new Mutex(true, @"Local\StudioWrightDesktop.Instance", out created))
            {
                if (!created)
                {
                    try { using (var existing = EventWaitHandle.OpenExisting(@"Local\StudioWrightDesktop.Show")) existing.Set(); }
                    catch (WaitHandleCannotBeOpenedException) { }
                    return;
                }
                using (var show = new EventWaitHandle(false, EventResetMode.AutoReset, @"Local\StudioWrightDesktop.Show"))
                {
                    Application.EnableVisualStyles();
                    Application.SetCompatibleTextRenderingDefault(false);
                    var window = new DesktopWindow();
                    var listener = new Thread(delegate() {
                        while (show.WaitOne())
                        {
                            if (window.IsDisposed) return;
                            try { window.BeginInvoke((Action)window.RestoreWindow); } catch (InvalidOperationException) { }
                        }
                    });
                    listener.IsBackground = true; listener.Start();
                    Application.Run(window);
                    show.Set();
                }
            }
        }

        static bool SelfCheck()
        {
            using (var icon = BrandIcon(16))
            {
              return icon.Width == 16 && icon.Height == 16 &&
                   File.Exists(Path.Combine(AppDir, "Microsoft.Web.WebView2.Core.dll")) &&
                   File.Exists(Path.Combine(AppDir, "Microsoft.Web.WebView2.WinForms.dll")) &&
                   File.Exists(Path.Combine(AppDir, "WebView2Loader.dll")) &&
                   Discovery.IsPrivate(IPAddress.Parse("192.168.1.5")) &&
                   !Discovery.IsPrivate(IPAddress.Parse("8.8.8.8"));
            }
        }
    }

    sealed class Offer
    {
        public string Address;
        public string Host;
        public string User;
        public int SshPort;
        public override string ToString() { return Host + "（" + Address + "）"; }
    }

    static class Discovery
    {
        static readonly byte[] Probe = Encoding.ASCII.GetBytes("STUDIOWRIGHT_DISCOVER_V1\n");

        public static bool IsPrivate(IPAddress ip)
        {
            byte[] b = ip.GetAddressBytes();
            return b.Length == 4 && (b[0] == 10 || (b[0] == 172 && b[1] >= 16 && b[1] <= 31) ||
                (b[0] == 192 && b[1] == 168) || (b[0] == 169 && b[1] == 254));
        }

        static Offer Parse(byte[] packet, IPEndPoint peer)
        {
            if (packet.Length > 1024 || !IsPrivate(peer.Address) || peer.Port != Program.DiscoveryPort) return null;
            try
            {
                var data = new JavaScriptSerializer().DeserializeObject(Encoding.UTF8.GetString(packet)) as Dictionary<string, object>;
                if (data == null || !data.ContainsKey("product") || Convert.ToString(data["product"]) != "StudioWright" ||
                    !data.ContainsKey("protocol") || Convert.ToInt32(data["protocol"]) != 1 ||
                    !data.ContainsKey("workbench_port") || Convert.ToInt32(data["workbench_port"]) != 18920) return null;
                string host = Convert.ToString(data["host"]), user = Convert.ToString(data["user"]);
                int port = Convert.ToInt32(data["ssh_port"]);
                if (!Regex.IsMatch(host, @"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$") ||
                    !Regex.IsMatch(user, @"^[A-Za-z_][A-Za-z0-9_-]{0,63}$") || port < 1 || port > 65535) return null;
                return new Offer { Address = peer.Address.ToString(), Host = host, User = user, SshPort = port };
            }
            catch { return null; }
        }

        public static List<Offer> Scan()
        {
            var sockets = new List<UdpClient>();
            var found = new Dictionary<string, Offer>();
            try
            {
                foreach (var nic in NetworkInterface.GetAllNetworkInterfaces())
                {
                    if (nic.OperationalStatus != OperationalStatus.Up || nic.NetworkInterfaceType == NetworkInterfaceType.Loopback ||
                        nic.NetworkInterfaceType == NetworkInterfaceType.Tunnel) continue;
                    IPInterfaceProperties properties;
                    try { properties=nic.GetIPProperties(); } catch (NetworkInformationException) { continue; }
                    foreach (var address in properties.UnicastAddresses)
                    {
                        if (address.Address.AddressFamily != AddressFamily.InterNetwork || !IsPrivate(address.Address)) continue;
                        try
                        {
                            if (address.IPv4Mask==null) continue;
                            byte[] ip = address.Address.GetAddressBytes(), mask = address.IPv4Mask.GetAddressBytes(), broadcast = new byte[4];
                            if (mask[0] == 255 && mask[1] == 255 && mask[2] == 255 && mask[3] >= 254) continue;
                            for (int i = 0; i < 4; i++) broadcast[i] = (byte)(ip[i] | (byte)~mask[i]);
                            var udp = new UdpClient(new IPEndPoint(address.Address, 0));
                            udp.EnableBroadcast = true;
                            udp.Send(Probe, Probe.Length, new IPEndPoint(new IPAddress(broadcast), Program.DiscoveryPort));
                            sockets.Add(udp);
                        }
                        catch (SocketException) { }
                        catch (NotSupportedException) { }
                    }
                }
                var end = DateTime.UtcNow.AddSeconds(3);
                while (DateTime.UtcNow < end)
                {
                    bool pending = false;
                    foreach (var udp in sockets)
                    {
                        while (udp.Available > 0)
                        {
                            pending = true;
                            var peer = new IPEndPoint(IPAddress.Any, 0);
                            byte[] bytes = udp.Receive(ref peer);
                            Offer offer = Parse(bytes, peer);
                            if (offer != null) found[offer.Address + ":" + offer.SshPort] = offer;
                        }
                    }
                    if (!pending) Thread.Sleep(35);
                }
            }
            finally { foreach (var socket in sockets) socket.Close(); }
            return found.Values.OrderBy(x => x.Host).ThenBy(x => x.Address).ToList();
        }
    }

    static class Connection
    {
        public static bool PortOpen(int port)
        {
            try { using (var client = new TcpClient()) { var a = client.BeginConnect("127.0.0.1", port, null, null);
                bool ok = a.AsyncWaitHandle.WaitOne(300); if (ok) client.EndConnect(a); return ok; } }
            catch { return false; }
        }

        public static bool Health(int port, string path, string marker)
        {
            try {
                var request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port + path);
                request.Proxy = null; request.Timeout = 1800;
                using (var response = request.GetResponse())
                using (var reader = new StreamReader(response.GetResponseStream()))
                {
                    var content = new JavaScriptSerializer().DeserializeObject(reader.ReadToEnd()) as Dictionary<string, object>;
                    return content != null && content.ContainsKey("application") && Convert.ToString(content["application"]) == marker;
                }
            } catch { return false; }
        }

        public static bool FlowHealth()
        {
            try {
                var request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:18888/health");
                request.Proxy = null; request.Timeout = 1800;
                request.Headers[HttpRequestHeader.Authorization] = "Bearer sw-readonly-probe";
                request.Headers["X-Studiowright-Probe"] = "1";
                using (var response = request.GetResponse())
                using (var reader = new StreamReader(response.GetResponseStream()))
                {
                    var content = new JavaScriptSerializer().DeserializeObject(reader.ReadToEnd()) as Dictionary<string, object>;
                    return content != null && content.ContainsKey("application") && Convert.ToString(content["application"]) == "studiowright-console";
                }
            }
            catch (WebException error) {
                try { using (var response = error.Response as HttpWebResponse) {
                    if (response == null || response.StatusCode != HttpStatusCode.Unauthorized) return false;
                    using (var reader = new StreamReader(response.GetResponseStream())) {
                        var content = new JavaScriptSerializer().DeserializeObject(reader.ReadToEnd()) as Dictionary<string, object>;
                        return content != null && content.ContainsKey("error") && Convert.ToString(content["error"]) == "API Key 无效或已撤销。";
                    }
                }} catch { return false; }
            } catch { return false; }
        }

        public static bool WorkbenchReady() { return Health(18920, "/api/health", "unified-workbench"); }
        public static bool StackReady() { return WorkbenchReady() && FlowHealth() && Health(18922, "/health", "studiowright-unified-api"); }

        public static void StartManager()
        {
            if (Health(18890, "/health", "studiowright-chrome-manager") || PortOpen(18890)) return;
            string python = Path.Combine(Program.AppDir, @"runtime\pythonw.exe");
            string script = Path.Combine(Program.AppDir, "manager_bridge.py");
            if (!File.Exists(python) || !File.Exists(script)) return;
            var start = new ProcessStartInfo(python, "-B \"" + script + "\"");
            start.UseShellExecute = false; start.CreateNoWindow = true; start.WorkingDirectory = Program.AppDir;
            try { Process.Start(start); } catch { }
        }

        public static bool TryReuseConfiguredConnection()
        {
            string legacy=@"E:\google\unified-workbench\open_ubuntu.py";
            string python=Path.Combine(Program.AppDir,@"runtime\python.exe");
            string script=Path.Combine(Program.AppDir,"reuse_existing.py");
            if (!File.Exists(legacy) || !File.Exists(python) || !File.Exists(script)) return false;
            try {
                var start=new ProcessStartInfo(python,"-B \"" + script + "\" --no-browser");
                start.UseShellExecute=false;start.CreateNoWindow=true;start.WorkingDirectory=Program.AppDir;
                using (var process=Process.Start(start)) {
                    if (!process.WaitForExit(25000)) { try { process.Kill(); } catch { } return false; }
                    return process.ExitCode==0 && WorkbenchReady();
                }
            } catch { return false; }
        }
    }

    sealed class ConnectDialog : Form
    {
        readonly ComboBox servers = new ComboBox { Left=150, Top=66, Width=275, DropDownStyle=ComboBoxStyle.DropDownList };
        readonly Button scan = new Button { Left=435, Top=65, Width=85, Height=29, Text="重新查找" };
        readonly TextBox host = new TextBox { Left=150, Top=108, Width=275 };
        readonly TextBox user = new TextBox { Left=150, Top=146, Width=155 };
        readonly TextBox port = new TextBox { Left=375, Top=146, Width=50, Text="22" };
        readonly Label state = new Label { Left=25, Top=244, Width=500, Height=65, Text="正在查找同一局域网内的 Ubuntu 工作台……" };
        readonly Button connect = new Button { Left=25, Top=192, Width=142, Height=35, Text="连接工作台" };
        readonly Button cancel = new Button { Left=180, Top=192, Width=100, Height=35, Text="取消" };
        public Process NewTunnel;

        public ConnectDialog()
        {
            Text="连接 Ubuntu 工作台"; Width=570; Height=360; StartPosition=FormStartPosition.CenterParent;
            Controls.Add(new Label { Left=25, Top=20, Width=500, Height=33, Text="自动发现只提供地址；首次连接需确认服务器身份。" });
            Controls.Add(new Label { Left=25, Top=69, Width=115, Text="发现的服务" });
            Controls.Add(new Label { Left=25, Top=111, Width=115, Text="Ubuntu 地址" });
            Controls.Add(new Label { Left=25, Top=149, Width=115, Text="Ubuntu 用户名" });
            Controls.Add(new Label { Left=320, Top=149, Width=50, Text="端口" });
            Controls.Add(servers);Controls.Add(scan);Controls.Add(host);Controls.Add(user);Controls.Add(port);
            Controls.Add(connect);Controls.Add(cancel);Controls.Add(state);
            servers.SelectedIndexChanged += delegate { var offer=servers.SelectedItem as Offer; if (offer!=null) {
                host.Text=offer.Address; user.Text=offer.User; port.Text=offer.SshPort.ToString(); } };
            scan.Click += delegate { Find(); }; connect.Click += delegate { Connect(); };
            cancel.Click += delegate { DialogResult=DialogResult.Cancel; Close(); };
            FormClosed += delegate { if (DialogResult!=DialogResult.OK) StopTunnel(); };
            Shown += delegate { Find(); };
        }

        void StopTunnel()
        {
            if(NewTunnel==null) return;
            try { if(!NewTunnel.HasExited) NewTunnel.Kill(); } catch { }
            NewTunnel=null;
        }

        async void Find()
        {
            scan.Enabled=false; state.Text="正在查找局域网中的工作台……";
            try {
                var found=await Task.Run(() => Discovery.Scan());
                if (IsDisposed) return;
                servers.Items.Clear(); foreach (var offer in found) servers.Items.Add(offer);
                if (servers.Items.Count==1) servers.SelectedIndex=0;
                state.Text=servers.Items.Count==0 ? "未发现服务器，可输入地址，或检查两台设备是否在同一局域网。" :
                    "已发现 " + servers.Items.Count + " 台服务器。请选择后连接。";
            } catch { if (!IsDisposed) state.Text="查找未完成，可以手动填写地址。"; }
            finally { if (!IsDisposed) scan.Enabled=true; }
        }

        async void Connect()
        {
            IPAddress address; int sshPort;
            string h=host.Text.Trim(), u=user.Text.Trim();
            if (!IPAddress.TryParse(h,out address) || !Discovery.IsPrivate(address) ||
                !Regex.IsMatch(u,@"^[A-Za-z_][A-Za-z0-9_-]{0,63}$") || !Int32.TryParse(port.Text.Trim(),out sshPort) || sshPort<1 || sshPort>65535)
            { state.Text="请填写有效的内网地址、Ubuntu 用户名和端口。"; return; }
            if (Connection.PortOpen(18920) || Connection.PortOpen(18888) || Connection.PortOpen(18922))
            { state.Text="本机连接端口正在使用，请先确认现有工作台连接。"; return; }
            string ssh=Path.Combine(Environment.SystemDirectory,@"OpenSSH\ssh.exe");
            if (!File.Exists(ssh)) { state.Text="Windows 未安装 OpenSSH 客户端。"; return; }
            connect.Enabled=scan.Enabled=false;
            state.Text="正在连接；首次使用请在 SSH 窗口核对服务器身份并登录。";
            string forwards=" -L 127.0.0.1:18920:127.0.0.1:18920 -L 127.0.0.1:18888:127.0.0.1:18888 -L 127.0.0.1:18922:127.0.0.1:18922";
            try {
                var start=new ProcessStartInfo(ssh,"-N -o StrictHostKeyChecking=ask -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -p " + sshPort + forwards + " " + u + "@" + h);
                start.UseShellExecute=true; start.WindowStyle=ProcessWindowStyle.Normal;
                NewTunnel=Process.Start(start);
                bool ok=await Task.Run(() => {
                    Process process=NewTunnel;
                    for(int i=0;i<60;i++) { if (Connection.StackReady()) return true; if(process==null || process.HasExited) return false; Thread.Sleep(1000); }
                    return false;
                });
                if (IsDisposed) return;
                if (ok) { Connection.StartManager(); DialogResult=DialogResult.OK; Close(); return; }
                StopTunnel(); state.Text="连接未完成，请检查服务器身份、SSH 登录和服务状态。";
            } catch { StopTunnel(); state.Text="无法启动连接，请检查 Windows OpenSSH。"; }
            connect.Enabled=scan.Enabled=true;
        }
    }

    sealed class DesktopWindow : Form
    {
        readonly WebView2 web = new WebView2 { Dock=DockStyle.Fill };
        readonly Label message = new Label { Dock=DockStyle.Fill, TextAlign=ContentAlignment.MiddleCenter, Text="正在连接创作工作台……" };
        readonly NotifyIcon tray;
        readonly ToolStripStatusLabel status = new ToolStripStatusLabel("正在连接…");
        Process tunnel;
        bool exiting;

        public DesktopWindow()
        {
            Text="创作工作台"; Width=1380; Height=920; MinimumSize=new Size(800,560); StartPosition=FormStartPosition.CenterScreen;
            Icon=Program.BrandIcon(32);
            var bar=new StatusStrip(); var reconnect=new ToolStripDropDownButton("连接");
            reconnect.DropDownItems.Add("重新连接 Ubuntu",null,delegate { OpenConnection(); });
            reconnect.DropDownItems.Add("刷新页面",null,delegate { if (web.CoreWebView2!=null) web.Reload(); });
            bar.Items.Add(reconnect);bar.Items.Add(status);
            Controls.Add(message);Controls.Add(bar);
            var menu=new ContextMenuStrip(); menu.Items.Add("打开工作台",null,delegate { RestoreWindow(); });
            menu.Items.Add("退出",null,delegate { exiting=true; Close(); });
            tray=new NotifyIcon { Icon=Program.BrandIcon(16), Text="创作工作台", Visible=true, ContextMenuStrip=menu };
            tray.DoubleClick+=delegate { RestoreWindow(); };
            Resize+=delegate { if (WindowState==FormWindowState.Minimized) Hide(); };
            FormClosing+=delegate(object sender, FormClosingEventArgs e) {
                if (!exiting && e.CloseReason==CloseReason.UserClosing) { e.Cancel=true; Hide(); }
            };
            FormClosed+=delegate { tray.Visible=false;tray.Dispose();if(tunnel!=null && !tunnel.HasExited) try { tunnel.Kill(); } catch { } };
            Shown+=async delegate {
                bool ready=await Task.Run(() => Connection.WorkbenchReady());
                if (!ready) ready=await Task.Run(() => Connection.TryReuseConfiguredConnection());
                if (IsDisposed) return;
                if (ready) { Connection.StartManager(); ShowWorkbench(); } else OpenConnection();
            };
        }

        public void RestoreWindow() { if (IsDisposed) return; Show(); WindowState=FormWindowState.Normal; Activate(); }

        void OpenConnection()
        {
            using(var dialog=new ConnectDialog()) {
                if(dialog.ShowDialog(this)==DialogResult.OK) { tunnel=dialog.NewTunnel; ShowWorkbench(); }
                else { status.Text="尚未连接";message.Text="尚未连接。点击左下角“连接”重新查找。"; }
            }
        }

        async void ShowWorkbench()
        {
            try {
                if (web.CoreWebView2==null) {
                    var environment=await CoreWebView2Environment.CreateAsync(null,Program.ProfileDir);
                    await web.EnsureCoreWebView2Async(environment);
                    web.CoreWebView2.NewWindowRequested+=delegate(object sender, CoreWebView2NewWindowRequestedEventArgs e) {
                        e.Handled=true;
                        Uri uri;
                        if (Uri.TryCreate(e.Uri,UriKind.Absolute,out uri) && (uri.Scheme=="http" || uri.Scheme=="https")) {
                            var child=new EmbeddedWindow(uri,environment); child.Show(this);
                        }
                    };
                }
                if (!Controls.Contains(web)) { Controls.Add(web);web.BringToFront(); }
                web.Source=new Uri(Program.Workbench); status.Text="已连接 Ubuntu 工作台 · 最小化后收纳到系统托盘";
            } catch { status.Text="桌面窗口启动失败";message.Text="无法启动桌面显示组件，请检查 WebView2 运行环境。"; }
        }
    }

    sealed class EmbeddedWindow : Form
    {
        public EmbeddedWindow(Uri uri, CoreWebView2Environment environment)
        {
            Text="创作工作台 · 附加窗口";Width=1200;Height=800;StartPosition=FormStartPosition.CenterParent;
            Icon=Program.BrandIcon(32);
            var view=new WebView2 { Dock=DockStyle.Fill };Controls.Add(view);
            Shown+=async delegate {
                try { await view.EnsureCoreWebView2Async(environment);
                    view.CoreWebView2.NewWindowRequested+=delegate(object sender,CoreWebView2NewWindowRequestedEventArgs e) {
                        e.Handled=true;Uri next;
                        if(Uri.TryCreate(e.Uri,UriKind.Absolute,out next) && (next.Scheme=="http" || next.Scheme=="https"))
                            new EmbeddedWindow(next,environment).Show(this);
                    };
                    view.Source=uri;
                } catch { Text="附加窗口无法打开"; }
            };
        }
    }
}
