"""Bounded public-input sizing. No project imports, installation or extraction."""
import concurrent.futures
import base64
import glob
import hashlib
import io
import json
import os
import pathlib
import platform
import re
import resource
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent
START = time.monotonic()
GIB = 1024**3
LOCK = threading.Lock()
FAILED = threading.Event()
COUNTS = {"requests": 0, "bytes": 0, "binary_bytes": 0}
RESULT = {"kind": "ort33735_full_input_price", "execution_admitted": False}
MAIN = "f5ff8ca8f1ff21141d4621b182b5750ea1e3a63a"
AS_HARD = resource.getrlimit(resource.RLIMIT_AS)[1]
resource.setrlimit(resource.RLIMIT_AS, (2 * GIB, AS_HARD))


def child_address_space():
    resource.setrlimit(resource.RLIMIT_AS, (AS_HARD, AS_HARD))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def check_clock():
    assert not FAILED.is_set(), "PEER_REQUEST_FAILED"
    assert time.monotonic() - START < 25 * 60, "PRICING_AGGREGATE_TIMEOUT"


def response(url, *, token=False, redirect=False):
    check_clock()
    parsed = urllib.parse.urlsplit(url)
    assert parsed.scheme == "https" and parsed.username is None
    allowed = parsed.hostname in {
        "api.github.com", "github.com", "codeload.github.com",
        "registry.npmjs.org", "cdn.playwright.dev",
        "playwright.download.prss.microsoft.com",
        "mcr.microsoft.com",
    } or parsed.hostname.endswith(".blob.core.windows.net")
    assert allowed, "UNEXPECTED_DOWNLOAD_HOST"
    assert not token or parsed.hostname == "api.github.com"
    with LOCK:
        assert COUNTS["requests"] < 1600, "REQUEST_CAP"
        COUNTS["requests"] += 1
    headers = {"User-Agent": "ort-web-input-pricing", "Accept-Encoding": "identity"}
    if parsed.hostname == "mcr.microsoft.com":
        headers["Accept"] = ", ".join([
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ])
    if token:
        headers["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]
    try:
        return OPENER.open(urllib.request.Request(url, headers=headers), timeout=20)
    except urllib.error.HTTPError as error:
        if redirect and error.code in (301, 302, 303, 307, 308):
            return error
        raise RuntimeError("HTTP_STATUS_" + str(error.code)) from None


def read_bytes(stream, cap, *, binary=False, destination=None):
    chunks = []
    size = 0
    while True:
        check_clock()
        chunk = stream.read(min(256 * 1024, cap - size + 1))
        if not chunk:
            break
        size += len(chunk)
        assert size <= cap, "RESPONSE_BYTE_CAP"
        with LOCK:
            COUNTS["bytes"] += len(chunk)
            if binary:
                COUNTS["binary_bytes"] += len(chunk)
            assert COUNTS["bytes"] - COUNTS["binary_bytes"] <= 160 * 1024**2
            assert COUNTS["binary_bytes"] <= 3 * GIB
        if destination:
            destination.write(chunk)
        else:
            chunks.append(chunk)
    return size if destination else b"".join(chunks)


def metadata(url):
    with response(url) as r:
        return json.loads(read_bytes(r, 2 * 1024**2))


def download(url, name, cap, *, first_token=False):
    # Redirect targets are checked; authorization is never forwarded.
    for hop in range(5):
        with response(url, token=first_token and hop == 0, redirect=True) as r:
            if r.code in (301, 302, 303, 307, 308):
                url = urllib.parse.urljoin(url, r.headers["Location"])
                continue
            assert r.code == 200
            dest = ROOT / name
            assert not dest.exists()
            with dest.open("xb") as output:
                size = read_bytes(r, cap, binary=True, destination=output)
            return dest, size
    raise RuntimeError("REDIRECT_CAP")


def zip_price(path, *, artifact=False, node_prefix=None):
    with zipfile.ZipFile(path) as z:
        assert len(z.infolist()) <= 250000
        total = selected = 0
        symlinks = 0
        for item in z.infolist():
            part = pathlib.PurePosixPath(item.filename)
            assert not part.is_absolute() and ".." not in part.parts
            assert "\\" not in item.filename and "\x00" not in item.filename
            total += item.file_size
            if node_prefix and node_prefix in item.filename:
                selected += item.file_size
            symlinks += int((item.external_attr >> 16) & 0o170000 == 0o120000)
        assert total <= 16 * GIB
        assert not artifact or symlinks == 0
        return {"expanded_bytes": total, "node_data_bytes": selected,
                "entries": len(z.infolist()), "symlinks_not_extracted": symlinks}


def registry_pair(path, item):
    parsed = urllib.parse.urlsplit(item["resolved"])
    assert parsed.scheme == "https" and parsed.hostname == "registry.npmjs.org"
    name = urllib.parse.unquote(parsed.path.split("/-/", 1)[0].lstrip("/"))
    assert re.fullmatch(r"(?:@[a-z0-9._-]+/)?[a-z0-9._-]+", name)
    assert not item.get("name") or item["name"] == name
    assert urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1]).endswith("-" + item["version"] + ".tgz")
    return name, item["version"]


def package_price(pair):
    check_clock()
    name, version = pair
    value = metadata("https://registry.npmjs.org/" + urllib.parse.quote(name, safe="") + "/" + urllib.parse.quote(version, safe=""))
    assert value["name"] == name and value["version"] == version
    dist = value["dist"]
    size = dist.get("unpackedSize")
    digest = dist.get("integrity")
    alternatives = (digest or "").split()
    if re.fullmatch(r"[a-f0-9]{40}", dist.get("shasum", "")):
        alternatives.append("sha1-" + base64.b64encode(bytes.fromhex(dist["shasum"])).decode())
    if not isinstance(size, int) or not digest:
        url = dist["tarball"]
        assert urllib.parse.urlsplit(url).hostname == "registry.npmjs.org"
        key = hashlib.sha256((name + "@" + version).encode()).hexdigest()
        archive, _ = download(url, "npm-" + key + ".tgz", 64 * 1024**2)
        h = hashlib.sha1(archive.read_bytes()).hexdigest()
        assert h == dist["shasum"]
        alternatives.append("sha512-" + base64.b64encode(hashlib.sha512(archive.read_bytes()).digest()).decode())
        size = 0
        with tarfile.open(archive) as t:
            for count, entry in enumerate(t):
                check_clock()
                size += entry.size
                assert count < 100000 and size <= GIB
    assert size >= 0
    return pair, {"unpacked_bytes": size, "integrity": " ".join(alternatives),
                  "sha1": dist.get("shasum"), "scripts": value.get("scripts", {})}


def guarded_package_price(pair):
    try:
        return package_price(pair)
    except BaseException:
        FAILED.set()
        raise


def main():
    assert platform.machine() in ("aarch64", "arm64")
    assert shutil.disk_usage(ROOT).free >= 24 * GIB
    # Use only already installed Node 24; no setup/install fallback.
    bins = sorted(glob.glob("/opt/hostedtoolcache/node/24.*/arm64/bin"))
    assert bins, "NODE24_NOT_PREINSTALLED"
    env = dict(os.environ, PATH=bins[-1] + ":" + os.environ["PATH"], NODE_OPTIONS="--max-old-space-size=512")
    env.pop("GH_TOKEN", None)
    version = subprocess.check_output([bins[-1] + "/node", "--version"], env=env, timeout=10,
                                      preexec_fn=child_address_space).decode().strip()
    assert version.startswith("v24.")
    RESULT["node"] = version
    RESULT["npm_version"] = subprocess.check_output([bins[-1] + "/npm", "--version"], env=env, timeout=10,
                                             preexec_fn=child_address_space).decode().strip()
    node_root = pathlib.Path(bins[-1]).parent
    RESULT["node_runtime_bind_bytes"] = sum(x.stat().st_size for x in node_root.rglob("*") if x.is_file())

    current = metadata("https://api.github.com/repos/microsoft/onnxruntime/git/ref/heads/main")
    assert current["object"]["sha"] == MAIN, "UPSTREAM_MAIN_CHANGED"
    issue = metadata("https://api.github.com/repos/microsoft/onnxruntime/issues/33735")
    assert issue["state"] == "open" and not issue["assignees"] and issue["comments"] == 0, "ISSUE_STATE_CHANGED"
    artifact_info = metadata("https://api.github.com/repos/microsoft/onnxruntime/actions/artifacts/13020595908")
    assert not artifact_info["expired"] and artifact_info["workflow_run"]["head_sha"] == MAIN
    RESULT["upstream"] = {"main": MAIN, "issue_state": "open", "issue_comments": 0,
                          "artifact_id": artifact_info["id"]}

    # Fail cheaply on inaccessible cross-repository artifact authorization.
    try:
        artifact, artifact_bytes = download(
            "https://api.github.com/repos/microsoft/onnxruntime/actions/artifacts/13020595908/zip",
            "Release_wasm.zip", 25_000_000, first_token=True)
    except RuntimeError as error:
        if str(error) in ("HTTP_STATUS_401", "HTTP_STATUS_403"):
            dev = metadata("https://registry.npmjs.org/onnxruntime-web/dev")
            commit = dev.get("gitHead")
            RESULT["public_dev_observation"] = {
                "version": dev.get("version"), "gitHead": commit,
                "selected_version_exact_match": commit == MAIN if commit else None,
                "coverage": "One dev-tag version only; nearest overall and other exact matches unverified.",
                "compare_link_not_diff_proof": "https://github.com/microsoft/onnxruntime/compare/" + commit + "..." + MAIN if commit and re.fullmatch(r"[a-f0-9]{40}", commit) else None,
                "downloaded": False,
            }
        raise
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert digest == "f2469ee4f02eef820f57e7779f2143d167412785fbcf125a5c47717bbe005ffe"
    RESULT["artifact"] = dict(zip_price(artifact, artifact=True), compressed_bytes=artifact_bytes, sha256=digest)

    fixture = ROOT / "fixture"
    fixture.mkdir()
    (fixture / "package.json").write_text(json.dumps({"private": True, "dependencies": {
        "react": "19.2.0", "react-dom": "19.2.0", "vite": "7.3.7",
        "vite-plugin-singlefile": "2.3.3", "rollup": "4.59.0",
        "playwright": "1.56.1", "onnxruntime-web": "1.30.0"}}))
    subprocess.run([bins[-1] + "/npm", "install", "--package-lock-only", "--ignore-scripts",
                    "--no-audit", "--no-fund", "--fetch-retries=0", "--fetch-timeout=20000",
                    "--maxsockets=4"], cwd=fixture, env=env, check=True, timeout=90,
                   preexec_fn=child_address_space)
    locks = json.loads((ROOT / "locks.json").read_text())
    locks["fixture"] = json.loads((fixture / "package-lock.json").read_text())
    packages = {}
    for label, data in locks.items():
        for path, item in data["packages"].items():
            if item.get("resolved", "").startswith("https://registry.npmjs.org/"):
                pair = registry_pair(path, item)
                packages[pair] = item
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        prices = dict(pool.map(guarded_package_price, sorted(packages)))
    totals = {}
    for label, data in locks.items():
        total = 0
        for path, item in data["packages"].items():
            if item.get("resolved", "").startswith("https://registry.npmjs.org/"):
                pair = registry_pair(path, item)
                price = prices[pair]
                if price["integrity"] and item.get("integrity"):
                    assert set(price["integrity"].split()) & set(item["integrity"].split()), "LOCK_INTEGRITY_MISMATCH"
                total += price["unpacked_bytes"]
        totals[label] = total
    RESULT["npm"] = {"unique_versions": len(prices), "unpacked_per_lock": totals,
                     "fixture_lock": locks["fixture"],
                     "package_prices": {a + "@" + b: v for (a, b), v in prices.items()}}

    RESULT["browsers"] = {}
    for name, suffix in [
        ("firefox142.0.1", "firefox/1495/firefox-ubuntu-24.04-arm64.zip"),
        ("chromium141.0.7390.37", "chromium/1194/chromium-linux-arm64.zip"),
        ("chromium-headless-shell141.0.7390.37", "chromium/1194/chromium-headless-shell-linux-arm64.zip"),
    ]:
        path, size = download("https://cdn.playwright.dev/dbazure/download/playwright/builds/" + suffix,
                              name + ".zip", 512 * 1024**2)
        RESULT["browsers"][name] = dict(zip_price(path), compressed_bytes=size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())

    image_url = "https://mcr.microsoft.com/v2/playwright/manifests/"
    image = metadata(image_url + "v1.56.1-noble")
    assert "manifests" in image, "IMAGE_PLATFORM_INDEX_REQUIRED"
    arm = [x for x in image["manifests"] if x.get("platform", {}).get("os") == "linux"
           and x.get("platform", {}).get("architecture") == "arm64"]
    assert len(arm) == 1 and re.fullmatch(r"sha256:[a-f0-9]{64}", arm[0]["digest"])
    manifest = metadata(image_url + arm[0]["digest"])
    compressed = sum(x["size"] for x in manifest["layers"]) + manifest["config"]["size"]
    RESULT["container_image"] = {"reference": "mcr.microsoft.com/playwright:v1.56.1-noble",
                                 "arm64_digest": arm[0]["digest"], "layers": len(manifest["layers"]),
                                 "compressed_bytes": compressed, "estimated_expanded_bytes": compressed * 4,
                                 "expanded_measured": False, "pulled": False,
                                 "runtime_node": "Read-only bind of same-OS/architecture Node24 directory, not bundled Node."}

    RESULT["onnx_test_archives"] = {}
    for version in ["1.16.2", "1.15.0", "1.14.0", "1.13.1", "1.12.1", "1.11.0", "1.10.2", "1.9.1", "1.8.1", "1.7.0", "1.6.1", "1.5.0", "1.4.1", "1.3.0", "1.2.3"]:
        path, size = download("https://github.com/onnx/onnx/archive/refs/heads/rel-" + version + ".zip",
                              "onnx-" + version + ".zip", 128 * 1024**2)
        RESULT["onnx_test_archives"][version] = dict(zip_price(path, node_prefix="/onnx/backend/test/data/node/"),
                                                     compressed_bytes=size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    RESULT["status"] = "PRICED_NOT_EXECUTION_ADMISSION"


try:
    main()
except Exception as error:
    RESULT["status"] = "STOP_NO_RETRY"
    # No response URLs or environment/token values in errors.
    RESULT["error_type"] = type(error).__name__
    RESULT["error"] = str(error) if isinstance(error, (AssertionError, RuntimeError)) else "Pricing step failed; no retry."
    raise
finally:
    RESULT["network"] = COUNTS
    RESULT["elapsed_seconds"] = time.monotonic() - START
    print("FULL_PRICE_JSON_BEGIN", flush=True)
    manifest = json.dumps(RESULT, separators=(",", ":"))
    assert len(manifest.encode()) <= 2 * 1024**2, "RESULT_LOG_CAP"
    print(manifest, flush=True)
    print("FULL_PRICE_JSON_END", flush=True)
