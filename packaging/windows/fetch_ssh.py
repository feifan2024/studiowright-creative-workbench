"""Prepare pinned .NET Framework assemblies for desktop SSH."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from io import BytesIO
from ipaddress import ip_address
from pathlib import Path
from socket import getaddrinfo, IPPROTO_TCP
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener
from zipfile import ZipFile

PACKAGES = (
    ('ssh.net', '2026.0.0', 'b2515de616821198f5cf5530f5ea198912730bbe3504ef4c6aee00a661fecac2', 'net462', 'Renci.SshNet.dll'),
    ('bouncycastle.cryptography', '2.7.0', 'f091ffccab4d03993e660bace277659a79dee0972f54d7f1f4bd46d680966241', 'net461', 'BouncyCastle.Cryptography.dll'),
    ('microsoft.bcl.cryptography', '10.0.10', '4b8eb4562ddc2066e352c0be51325d5536df2ee75a15c76daf809b0b4b19b22d', 'net462', 'Microsoft.Bcl.Cryptography.dll'),
    ('microsoft.extensions.logging.abstractions', '8.0.3', 'e4c498d5a13051b4577a148f1d8c3470167215c507e2392069b75dc61322bb74', 'net462', 'Microsoft.Extensions.Logging.Abstractions.dll'),
    ('system.formats.asn1', '10.0.10', '21963ab1dfee2b2b87e7b6348a3a23a4772af500247e4401b0bbe3156cedb29c', 'net462', 'System.Formats.Asn1.dll'),
    ('system.memory', '4.6.3', '26078aeb758c9ae985e8bf851f973026061da6a5eb4837204d0c2d2204c72955', 'net462', 'System.Memory.dll'),
    ('system.runtime.compilerservices.unsafe', '6.1.2', '5f6a7f53af3465f92beb6da873ebe0e496206c313313b98badee4355a6b25937', 'net462', 'System.Runtime.CompilerServices.Unsafe.dll'),
    ('microsoft.extensions.dependencyinjection.abstractions', '8.0.2', '51f2df1100245f10da54f0bb7e813f277155117777d4fbbab902214e27372606', 'net462', 'Microsoft.Extensions.DependencyInjection.Abstractions.dll'),
    ('system.buffers', '4.6.1', 'b00451e91d016fbec091ad1e361f3a7015e1d91d4047f7e48a74455b2a673d79', 'net462', 'System.Buffers.dll'),
    ('system.numerics.vectors', '4.6.1', '2bc500a86dcb02f2032d6d877f9e2d6e9e4a79080e57239b4198679d4031f2c7', 'net462', 'System.Numerics.Vectors.dll'),
    ('microsoft.bcl.asyncinterfaces', '8.0.0', 'f5a5a68b03092ab2abf68843d4a4aea25dfbcbe8dd0f13c625cb779b6fc1927c', 'net462', 'Microsoft.Bcl.AsyncInterfaces.dll'),
    ('system.threading.tasks.extensions', '4.5.4', 'a304a963cc0796c5179f9c6b7d8022bbce3b2fa7c029eb6196f631f7b462d678', 'net461', 'System.Threading.Tasks.Extensions.dll'),
    ('system.valuetuple', '4.6.2', '76fd0e366a2b90655fd15d557b0b79504197748358cb578fe3193f6d97bdd9aa', 'net462', 'System.ValueTuple.dll'),
)

PINNED_HOST = 'api.nuget.org'


class PinnedRedirectHandler(HTTPRedirectHandler):
    """Refuse redirects that leave the pinned package host."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urlsplit(newurl)
        if parts.scheme != 'https' or parts.hostname != PINNED_HOST:
            raise RuntimeError('refusing redirect outside the pinned package host')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_pinned(url, timeout):
    """Open only pinned https://api.nuget.org URLs.

    Enforces scheme and host before any request, resolves the pinned host and
    refuses private/loopback/link-local results (DNS rebinding guard), and
    rejects redirects that leave the pinned host."""
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname != PINNED_HOST or parts.username or parts.password:
        raise RuntimeError('refusing non-pinned package URL: ' + url)
    for _, _, _, _, sockaddr in getaddrinfo(parts.hostname, 443, proto=IPPROTO_TCP):
        address = ip_address(sockaddr[0])
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast:
            raise RuntimeError('package host resolved to a non-public address')
    return build_opener(PinnedRedirectHandler).open(url, timeout=timeout)


def prepare(payload):
    def fetch(item):
        name, version, expected, target, dll = item
        url = f'https://api.nuget.org/v3-flatcontainer/{name}/{version}/{name}.{version}.nupkg'
        with open_pinned(url, timeout=25) as response:
            data = response.read()
        if sha256(data).hexdigest() != expected:
            raise RuntimeError(f'{name}: package hash mismatch')
        with ZipFile(BytesIO(data)) as archive:
            (payload / dll).write_bytes(archive.read(f'lib/{target}/{dll}'))
            notices = payload / 'licenses' / name
            for member in archive.namelist():
                if '/' not in member and ('license' in member.lower() or 'notice' in member.lower()):
                    notices.mkdir(parents=True, exist_ok=True)
                    (notices / member).write_bytes(archive.read(member))

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(fetch, PACKAGES))
    license_source = Path(__file__).with_name('licenses')
    for file in license_source.glob('*.txt'):
        (payload / 'licenses').mkdir(exist_ok=True)
        (payload / 'licenses' / file.name).write_bytes(file.read_bytes())


if __name__ == '__main__':
    prepare(Path(__file__).resolve().parent / 'client-payload')
