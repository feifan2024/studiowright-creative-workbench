"""Fetch the pinned official WebView2 SDK and verify NuGet's published hash."""
import base64, gzip, hashlib, json, urllib.request, zipfile
from pathlib import Path

base=Path(__file__).resolve().parent
version='1.0.3405.78'
url=f'https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/{version}/microsoft.web.webview2.{version}.nupkg'
package=base/'webview2.nupkg'
if package.exists(): raise SystemExit('SDK package already exists')
raw=urllib.request.urlopen(url,timeout=45).read(50*1024*1024)
registration=f'https://api.nuget.org/v3/registration5-gz-semver2/microsoft.web.webview2/{version}.json'
registration_data=json.loads(gzip.decompress(urllib.request.urlopen(registration,timeout=20).read()))
if registration_data['packageContent']!=url:raise SystemExit('Unexpected SDK package URL')
catalog=json.load(urllib.request.urlopen(registration_data['catalogEntry'],timeout=20))
if catalog.get('packageHashAlgorithm')!='SHA512':raise SystemExit('Unexpected SDK hash algorithm')
expected=base64.b64decode(catalog['packageHash'],validate=True)
if hashlib.sha512(raw).digest()!=expected:raise SystemExit('SDK hash mismatch')
package.write_bytes(raw)
with zipfile.ZipFile(package) as z:
    names=[n for n in z.namelist() if n.endswith(('Microsoft.Web.WebView2.Core.dll','Microsoft.Web.WebView2.WinForms.dll','WebView2Loader.dll'))]
print(json.dumps({'verified':True,'bytes':len(raw),'assemblies':names}))
