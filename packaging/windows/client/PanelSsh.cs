using System;
using System.Diagnostics;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using Renci.SshNet;

namespace StudioWrightDesktop
{
    interface IConnectionTunnel : IDisposable
    {
        bool IsAlive { get; }
        bool Reconnect();
    }

    sealed class OpenSshTunnel : IConnectionTunnel
    {
        Process process;
        readonly ProcessStartInfo start;
        public OpenSshTunnel(Process process) { this.process = process; start = process.StartInfo; }
        public bool IsAlive { get { return !process.HasExited; } }
        public bool Reconnect()
        {
            try {
                if (!process.HasExited) process.Kill();
                process.WaitForExit(3000);
                process.Dispose();
                process = Process.Start(start);
                return process != null;
            } catch { return false; }
        }
        public void Dispose()
        {
            try { if (!process.HasExited) process.Kill(); }
            catch (InvalidOperationException) { }
            catch (System.ComponentModel.Win32Exception) { }
            finally { process.Dispose(); }
        }
    }

    sealed class PanelSsh : IConnectionTunnel
    {
        readonly SshClient client;
        readonly byte[] credential;
        public bool IsAlive { get { return client.IsConnected; } }

        PanelSsh(SshClient client, byte[] credential) { this.client = client; this.credential = credential; }
        public bool Reconnect()
        {
            try {
                foreach (var port in client.ForwardedPorts) if (port.IsStarted) port.Stop();
                client.Disconnect();
                client.Connect();
                foreach (var port in client.ForwardedPorts) port.Start();
                return client.IsConnected;
            } catch { return false; }
        }
        public void Dispose() { try { client.Dispose(); } finally { Array.Clear(credential,0,credential.Length); } }

        internal static PanelSsh Open(string address, int port, string user, string password,
                                      string trustDirectory, Func<string, bool> approve,
                                      int[] localPorts, int[] remotePorts)
        {
            string pin = Path.Combine(trustDirectory, address + "-" + port + ".sha256");
            string existing = File.Exists(pin) ? File.ReadAllText(pin).Trim() : null;
            string observed = null;
            string trustError = null;
            bool retained = false;
            byte[] credential = Encoding.UTF8.GetBytes(password);
            var auth = new PasswordAuthenticationMethod(user, credential);
            var info = new ConnectionInfo(address, port, user, auth) { Timeout = TimeSpan.FromSeconds(15) };
            var ssh = new SshClient(info) { KeepAliveInterval = TimeSpan.FromSeconds(30) };
            EventHandler<Renci.SshNet.Common.HostKeyEventArgs> verify = (sender, key) => {
                key.CanTrust = false;
                using (var sha = SHA256.Create())
                    observed = "SHA256:" + Convert.ToBase64String(sha.ComputeHash(key.HostKey)).TrimEnd('=');
                if (existing == null)
                {
                    key.CanTrust = approve(observed);
                    if (!key.CanTrust) trustError = "未确认服务器指纹，连接已取消。";
                }
                else if (String.Equals(existing, observed, StringComparison.Ordinal)) key.CanTrust = true;
                else trustError = "服务器指纹与上次不同，已停止登录。请先核实服务器身份。";
            };
            ssh.HostKeyReceived += verify;
            try
            {
                try { ssh.Connect(); }
                catch (Exception) { if (trustError != null) throw new InvalidDataException(trustError); throw; }
                if (existing == null)
                {
                    if (observed == null) throw new InvalidDataException("未能确认服务器指纹。");
                    Directory.CreateDirectory(trustDirectory);
                    using (var stream = new FileStream(pin, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                    using (var writer = new StreamWriter(stream, Encoding.ASCII)) writer.WriteLine(observed);
                }
                for (int i = 0; i < localPorts.Length; ++i)
                {
                    var forward = new ForwardedPortLocal("127.0.0.1", (uint)localPorts[i], "127.0.0.1", (uint)remotePorts[i]);
                    ssh.AddForwardedPort(forward);
                    forward.Start();
                }
                retained = true;
                return new PanelSsh(ssh, credential);
            }
            catch { ssh.Dispose(); throw; }
            // Keep the verifier attached for SSH rekeying, without reopening the confirmation UI.
            finally {
                if (!retained) Array.Clear(credential, 0, credential.Length);
                if (trustError == null && observed != null) { existing = observed; }
            }
        }
    }
}
