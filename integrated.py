"""One integrated remote job. Price first; measure admission; run full checks once."""
from pathlib import Path, PurePosixPath
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import time
import zipfile

P = Path(__file__).resolve().parent
START = time.monotonic()
GIB = 1024**3
RESULT = {'status': 'RUNNING', 'product_execution_started': False}
OWN_CONTAINER = None
COMMAND_COUNT = 0
LAST_MEMORY_SAMPLE = 0


def command(args, seconds, cap=16*1024**2, job_metadata=False):
    import selectors
    global COMMAND_COUNT, LAST_MEMORY_SAMPLE
    COMMAND_COUNT += 1
    log_path = P / ('remote-command-%03d.log' % COMMAND_COUNT)
    assert not log_path.exists()
    child_env = dict(os.environ)
    if not job_metadata:
        child_env.pop('GH_TOKEN', None)
        child_env.pop('GITHUB_TOKEN', None)
    proc = subprocess.Popen(args, env=child_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    chunks = []
    size = 0
    clock = time.monotonic()
    try:
        while selector.get_map():
            assert time.monotonic() - clock < seconds and time.monotonic() - START < 150*60, 'REMOTE_TIME_CAP'
            assert shutil.disk_usage(P).free >= 12*GIB, 'RUNTIME_DISK_PRESSURE'
            available = int(re.search(r'^MemAvailable:\s+(\d+)', Path('/proc/meminfo').read_text(), re.M)[1])*1024
            assert available >= 2*GIB, 'HOST_MEMORY_PRESSURE'
            if OWN_CONTAINER and time.monotonic()-LAST_MEMORY_SAMPLE >= 10:
                LAST_MEMORY_SAMPLE = time.monotonic()
                try:
                    observed = subprocess.run(['docker','stats','--no-stream','--format','{{json .}}', OWN_CONTAINER], capture_output=True, timeout=5, check=True, env=child_env)
                    assert len(observed.stdout) < 8192 and len(observed.stderr) < 8192, 'MONITOR_OUTPUT_CAP'
                    state_read = subprocess.run(['docker','inspect','--format','{{json .State}}', OWN_CONTAINER], capture_output=True, timeout=5, check=True, env=child_env)
                    assert len(state_read.stdout) < 8192, 'MONITOR_STATE_CAP'
                    state = json.loads(state_read.stdout)
                    assert not state['OOMKilled'], 'CONTAINER_OOM'
                    if state['Running']:
                        sample = json.loads(observed.stdout)
                        value = sample['MemPerc']
                        assert re.fullmatch(r'[0-9]+(?:\.[0-9]+)?%', value), 'MONITOR_FAILED_MEMORY_FORMAT'
                        assert float(value[:-1]) < 93.75, 'CONTAINER_WORKING_SET_PRESSURE'
                except (subprocess.SubprocessError, OSError, ValueError) as monitor_error:
                    raise RuntimeError('MONITOR_FAILED_' + type(monitor_error).__name__) from None
                with (P/'memory-samples.jsonl').open('a') as record:
                    record.write(json.dumps({'at_seconds':time.monotonic()-START,'stats':observed.stdout.decode().strip(),'oom_killed':state['OOMKilled'],'running':state['Running']})+'\n')

            for key, _ in selector.select(1):
                data = os.read(key.fd, 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                size += len(data)
                assert size <= cap, 'REMOTE_OUTPUT_CAP'
                chunks.append(data)
        assert proc.wait(timeout=5) == 0, 'REMOTE_COMMAND_FAILED'
    except BaseException:
        if proc.poll() is None:
            
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
        raise
    finally:
        log_path.write_bytes(b''.join(chunks))
        selector.close()
    return b''.join(chunks)


def inspect_capacity(floor):
    info = json.loads(command(['docker', 'info', '--format', '{"root":{{json .DockerRootDir}},"memory":{{.MemTotal}},"cpus":{{.NCPU}}}'], 10))
    memory = int(re.search(r'^MemAvailable:\s+(\d+)', Path('/proc/meminfo').read_text(), re.M)[1])*1024
    docker_root = Path(info['root'])
    filesystem = command(['stat', '-f', '-c', '%T', str(P)], 10).decode().strip()
    observation = {'free_workspace': shutil.disk_usage(P).free, 'free_docker': shutil.disk_usage(docker_root).free,
                   'docker_memory': info['memory'], 'available_memory': memory, 'cpus': info['cpus'],
                   'filesystem': filesystem, 'disk_floor': floor}
    assert platform.machine() in ('arm64', 'aarch64') and filesystem not in ('tmpfs', 'ramfs')
    assert min(observation['free_workspace'], observation['free_docker']) >= floor, 'R3_STORAGE_CAPACITY'
    assert info['memory'] >= 10*GIB and memory >= 10*GIB and info['cpus'] >= 4, 'R4_MEMORY_CAPACITY'
    return observation


def verified_zip(archive, expected, target, *, browsers=False):
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected
    assert not target.exists()
    with zipfile.ZipFile(archive) as z:
        assert len(z.infolist()) <= 250000
        total = 0
        for entry in z.infolist():
            name = PurePosixPath(entry.filename)
            assert not name.is_absolute() and '..' not in name.parts and '\\' not in entry.filename and '\x00' not in entry.filename
            total += entry.file_size
            assert total <= 16*GIB and not entry.flag_bits&1
            if stat.S_ISLNK(entry.external_attr >> 16):
                assert browsers
                link = z.read(entry).decode()
                assert not link.startswith('/') and '\\' not in link and '\x00' not in link
                stack = list(name.parent.parts)
                for part in PurePosixPath(link).parts:
                    if part == '..':
                        assert stack, 'ZIP_SYMLINK_ESCAPE'
                        stack.pop()
                    elif part != '.':
                        stack.append(part)
    target.mkdir(parents=True)
    # unzip preserves the vendor's executable modes, unlike ZipFile.extractall.
    command(['unzip', '-q', str(archive), '-d', str(target)], 120)


try:
    RESULT['initial_capacity'] = inspect_capacity(24*GIB)
    price_log = command(['python3', str(P/'price.py')], 1505, 3*1024**2, job_metadata=True)
    (P/'price.log').write_bytes(price_log)
    match = re.search(rb'FULL_PRICE_JSON_BEGIN\n(.*?)\nFULL_PRICE_JSON_END', price_log, re.S)
    assert match, 'MISSING_PRICING_RESULT'
    price = json.loads(match[1])
    assert price['status'] == 'PRICED_NOT_EXECUTION_ADMISSION' and price['disk_admitted_before_setup']
    (P/'price-result.json').write_text(json.dumps(price))
    RESULT['priced_capacity'] = inspect_capacity(price['disk_floor_before_setup'])
    image = 'mcr.microsoft.com/playwright@' + price['container_image']['arm64_digest']
    command(['docker', 'pull', image], 600)
    actual_image = json.loads(command(['docker', 'image', 'inspect', image], 10))[0]
    assert actual_image['Architecture'] == 'arm64'
    RESULT['measured_image_expansion'] = actual_image['Size']
    expansion = price['closure_expansion_bytes'] - price['container_image']['estimated_expanded_bytes'] + actual_image['Size']
    RESULT['final_capacity'] = inspect_capacity(24*GIB + expansion)
    source = P/'source'
    assert command(['git', '-C', str(source), 'rev-parse', 'HEAD'], 10).decode().strip() == 'f5ff8ca8f1ff21141d4621b182b5750ea1e3a63a'
    assert command(['git', '-C', str(source), 'status', '--porcelain'], 10) == b''
    expected = json.loads((P/'expected-source-hashes.json').read_text())
    assert hashlib.sha256((P/'I2.patch').read_bytes()).hexdigest() == expected['patch_sha256']
    command(['git', '-C', str(source), 'apply', '--check', str(P/'I2.patch')], 10)
    baseline_loader = command(['git', '-C', str(source), 'show', 'HEAD:js/web/lib/wasm/wasm-utils-import.ts'], 10)
    command(['git', '-C', str(source), 'apply', str(P/'I2.patch')], 10)
    candidate_loader = (source/'js/web/lib/wasm/wasm-utils-import.ts').read_bytes()
    for name, data in [('baseline', baseline_loader), ('candidate', candidate_loader)]:
        with (P/(name+'-loader.ts')).open('xb') as f:
            f.write(data)
    (P/'frozen-loaders.json').write_text(json.dumps({'baseline':hashlib.sha256(baseline_loader).hexdigest(), 'candidate':hashlib.sha256(candidate_loader).hexdigest()}))

    # The dist directory must be new: no artifact overwrites or generated-bundle patching.
    verified_zip(P/'Release_wasm.zip', 'f2469ee4f02eef820f57e7779f2143d167412785fbcf125a5c47717bbe005ffe', source/'js/web/dist')
    for name, folder in [('firefox142.0.1','firefox-1495'),('chromium141.0.7390.37','chromium-1194'),('chromium-headless-shell141.0.7390.37','chromium_headless_shell-1194')]:
        verified_zip(P/(name+'.zip'), price['browsers'][name]['sha256'], P/'browsers'/folder, browsers=True)
    node_bin = Path('/opt/hostedtoolcache/node/24.22.1/arm64/bin')
    assert node_bin.is_dir(), 'EXACT_NODE24_MISSING'
    assert price['node'] == 'v24.22.1' and price['npm_version'] == '11.19.1', 'NODE_IDENTITY_CHANGED'
    for name in ('home','npm-cache'):
        (P/name).mkdir()
    chrome = list((P/'browsers/chromium-1194').glob('chrome-linux*/chrome'))
    assert len(chrome) == 1
    firefox = P/'browsers/firefox-1495/firefox/firefox'
    assert firefox.is_file()
    assert hashlib.sha256((P/'seccomp_profile.json').read_bytes()).hexdigest() == 'cc3e61cabda6bbc1e53e54d27ba4d55a9d3be829b6dd1a596f4a7b31b1cc7849'
    args = ['docker', 'create', '--init', '--security-opt', 'seccomp='+str(P/'seccomp_profile.json'), '--name', 'ort33735-integrated', '--user', str(os.getuid())+':'+str(os.getgid()),
            '--memory', '8g', '--memory-swap', '8g', '--cpus', '4', '--shm-size', '1g',
            '--workdir', '/work/source', '--volume', str(P)+':/work', '--volume', str(node_bin.parent)+':/node:ro',
            '--env', 'HOME=/work/home', '--env', 'CHROME_BIN=/work/'+str(chrome[0].relative_to(P)),
            '--env', 'FIREFOX_BIN=/work/'+str(firefox.relative_to(P)), image, '/node/bin/node', '/work/run-in-container.mjs']
    container_id = command(args, 20).decode().strip()
    assert re.fullmatch('[0-9a-f]{64}', container_id)
    OWN_CONTAINER = container_id
    RESULT['container_id'] = container_id
    RESULT['product_execution_started'] = True
    command(['docker', 'start', '--attach', container_id], 110*60+15)
    state = json.loads(command(['docker','inspect','--format','{{json .State}}',container_id], 10))
    assert state['ExitCode'] == 0 and not state['OOMKilled'] and not state['Running']
    executed = json.loads((P/'execution-result.json').read_text())
    assert executed['status'] == 'PASS'
    for filename, sha in expected['files'].items():
        assert hashlib.sha256((source/filename).read_bytes()).hexdigest() == sha, 'FINAL_SOURCE_IDENTITY_CHANGED'
    RESULT['status'] = 'PASS'
except BaseException as error:
    RESULT['status'] = 'STOP_NO_RETRY'
    RESULT['error'] = str(error)
    raise
finally:
    if OWN_CONTAINER:
        # Stop only this job's owned workload; preserve its container and data.
        try:
            subprocess.run(['docker','stop','--time','10',OWN_CONTAINER], timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as cleanup_error:
            RESULT['cleanup_error'] = type(cleanup_error).__name__
    RESULT['elapsed_seconds'] = time.monotonic() - START
    (P/'integrated-result.json').write_text(json.dumps(RESULT, indent=2))
    print('INTEGRATED_RESULT_BEGIN', flush=True)
    print(json.dumps(RESULT), flush=True)
    for name in ('execution-result.json','reporter-browser-result.json','base-browser-result.json','candidate-browser-result.json','package-control-result.json','test-data-cache-receipt.json'):
        if (P/name).is_file():
            data = (P/name).read_text()
            assert len(data.encode()) < 256*1024
            print(name + '\n' + data, flush=True)
    print('INTEGRATED_RESULT_END', flush=True)
