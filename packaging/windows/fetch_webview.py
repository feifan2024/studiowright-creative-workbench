"""Fetch the pinned official WebView2 SDK and verify NuGet's published hash."""
import base64, gzip, hashlib, ipaddress, json, socket, urllib.request, zipfile
from pathlib import Path
from urllib.parse import urlsplit

base=Path(__file__).resolve().parent
version='1.0.3405.78'
url=f'https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/{version}/microsoft.web.webview2.{version}.nupkg'
package=base/'webview2.nupkg'
if package.exists(): raise SystemExit('SDK package already exists')

PINNED_HOST='api.nuget.org'

class PinnedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects that leave the pinned package host."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts=urlsplit(newurl)
        if parts.scheme!='https' or parts.hostname!=PINNED_HOST:
            raise RuntimeError('refusing redirect outside the pinned package host')
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def open_pinned(url,timeout):
    """Open only pinned https://api.nuget.org URLs.

    Enforces scheme and host before any request, resolves the pinned host and
    refuses private/loopback/link-local results (DNS rebinding guard), and
    rejects redirects that leave the pinned host."""
    parts=urlsplit(url)
    if parts.scheme!='https' or parts.hostname!=PINNED_HOST or parts.username or parts.password:
        raise RuntimeError('refusing non-pinned package URL: '+url)
    for _,_,_,_,sockaddr in socket.getaddrinfo(parts.hostname,443,proto=socket.IPPROTO_TCP):
        address=ipaddress.ip_address(sockaddr[0])
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast:
            raise RuntimeError('package host resolved to a non-public address')
    return urllib.request.build_opener(PinnedRedirectHandler).open(url,timeout=timeout)

raw=open_pinned(url,45).read(50*1024*1024)
registration=f'https://api.nuget.org/v3/registration5-gz-semver2/microsoft.web.webview2/{version}.json'
registration_data=json.loads(gzip.decompress(open_pinned(registration,20).read()))
if registration_data['packageContent']!=url:raise SystemExit('Unexpected SDK package URL')
catalog=json.load(open_pinned(registration_data['catalogEntry'],20))
if catalog.get('packageHashAlgorithm')!='SHA512':raise SystemExit('Unexpected SDK hash algorithm')
expected=base64.b64decode(catalog['packageHash'],validate=True)
if hashlib.sha512(raw).digest()!=expected:raise SystemExit('SDK hash mismatch')
package.write_bytes(raw)
with zipfile.ZipFile(package) as z:
    names=[n for n in z.namelist() if n.endswith(('Microsoft.Web.WebView2.Core.dll','Microsoft.Web.WebView2.WinForms.dll','WebView2Loader.dll'))]
print(json.dumps({'verified':True,'bytes':len(raw),'assemblies':names}))
