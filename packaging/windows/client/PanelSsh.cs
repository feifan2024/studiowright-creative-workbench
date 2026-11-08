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
    }

    sealed class OpenSshTunnel : IConnectionTunnel
    {
        readonly Process process;
        public OpenSshTunnel(Process process) { this.process = process; }
        public bool IsAlive { get { return !process.HasExited; } }
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
        public bool IsAlive { get { return client.IsConnected; } }

        PanelSsh(SshClient client) { this.client = client; }
        public void Dispose() { client.Dispose(); }

        internal static PanelSsh Open(string address, int port, string user, string password,
                                      string trustDirectory, Func<string, bool> approve,
                                      int[] localPorts, int[] remotePorts)
        {
            string pin = Path.Combine(trustDirectory, address + "-" + port + ".sha256");
            string existing = File.Exists(pin) ? File.ReadAllText(pin).Trim() : null;
            string observed = null;
            string trustError = null;
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
                return new PanelSsh(ssh);
            }
            catch { ssh.Dispose(); throw; }
            // Keep the verifier attached for SSH rekeying, without reopening the confirmation UI.
            finally {
                Array.Clear(credential, 0, credential.Length);
                if (trustError == null && observed != null) { existing = observed; }
            }
        }
    }
}
