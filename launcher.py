"""MDVA desktop entry. Standard-library-only bootstrap, independent of engine imports."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.client import HTTPConnection

ROOT = Path(__file__).resolve().parent
STATE = ROOT / '.mdva' / 'launcher.json'
PROFILE = 1
URL = 'http://127.0.0.1:8765/'
PACKAGES = {'numpy': 'numpy', 'scipy': 'scipy', 'pandas': 'pandas',
            'Pillow': 'PIL', 'astropy': 'astropy', 'scikit-image': 'skimage',
            'openpyxl': 'openpyxl'}
HIDDEN = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


def read_state():
    try:
        value = json.loads(STATE.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) and value.get('profile') == PROFILE else {}
    except (OSError, ValueError):
        return {}


def remember(executable):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE.with_suffix('.tmp')
    temp.write_text(json.dumps({'profile': PROFILE, 'python': str(executable),
                                'configured_at': time.time()}, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(STATE)


def candidates():
    paths = [os.environ.get('MDVA_PY'), read_state().get('python')]
    for folder in (ROOT / '.venv', ROOT / 'venv'):
        paths.append(folder / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python'))
    try:
        envs = (Path.home() / '.conda' / 'environments.txt').read_text(encoding='utf-8').splitlines()
    except OSError:
        envs = []
    # The project's named environment is preferred over unrelated conda envs.
    envs.sort(key=lambda p: (Path(p).name.lower() != 'mdva', p))
    paths.extend(Path(p) / ('python.exe' if os.name == 'nt' else 'bin/python') for p in envs)
    paths.extend([sys.executable, shutil.which('python'), shutil.which('python3')])
    result, seen = [], set()
    for value in paths:
        if not value:
            continue
        path = Path(value).resolve()
        if path.name.lower() == 'pythonw.exe' and path.with_name('python.exe').is_file():
            path = path.with_name('python.exe')
        key = os.path.normcase(str(path))
        if path.is_file() and key not in seen:
            result.append(str(path)); seen.add(key)
    return result


def probe(executable):
    """Actually import packages and the application in the candidate interpreter."""
    code = '''import sys, json, importlib
sys.path.insert(0, sys.argv[1])
failures=[]
if sys.version_info < (3,10): failures.append('需要 Python 3.10 或更新版本')
for name, module in json.loads(sys.argv[2]).items():
    try: importlib.import_module(module)
    except Exception as exc: failures.append(name + ': ' + str(exc))
if not failures:
    try:
        import mdva.cli, mdva.web.server, mdva.web.solar
    except Exception as exc: failures.append('应用加载: ' + str(exc))
print(json.dumps({'ok': not failures, 'version': sys.version.split()[0], 'failures': failures}, ensure_ascii=True))
'''
    try:
        done = subprocess.run([str(executable), '-c', code, str(ROOT / 'src'), json.dumps(PACKAGES)],
                              cwd=ROOT, capture_output=True, timeout=45, creationflags=HIDDEN)
        if done.returncode:
            raise ValueError(done.stderr.decode('utf-8', errors='replace').strip()[-1500:] or '解释器无法运行')
        return json.loads(done.stdout.decode('utf-8').strip().splitlines()[-1])
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {'ok': False, 'version': '', 'failures': [str(exc)]}


def health():
    conn = HTTPConnection('127.0.0.1', 8765, timeout=1)
    try:
        conn.request('GET', '/api/health')
        response = conn.getresponse()
        value = json.loads(response.read(65536))
        return value if response.status == 200 and isinstance(value, dict) else None
    except (OSError, ValueError):
        return None
    finally:
        conn.close()


def same_service(info):
    return bool(info and info.get('app') == 'mdva' and info.get('revision') == 'web-solar-ai-4' and info.get('monitor_revision') == 'sw-2'
                and Path(info.get('root', '')).resolve() == (ROOT / 'data').resolve()
                and Path(info.get('out_dir', '')).resolve() == (ROOT / 'out').resolve()
                and set(default_sources()) <= set(info.get('sources', [])))


def default_sources():
    names = {'ssafd': 'Situation Awareness Dataset',
             'solar_obs': 'Standardized Solar Observation Image Dataset',
             'solar_forecast': 'Solar Flare Forecasting Dataset'}
    return {name: ROOT / 'datas of importance' / folder for name, folder in names.items()
            if (ROOT / 'datas of importance' / folder).is_dir()}


def start_service(executable):
    info = health()
    if same_service(info):
        return
    if info:
        raise RuntimeError('端口 8765 正在运行其他工作区或旧版本服务，请先关闭该服务后重试。')
    (ROOT / 'data').mkdir(exist_ok=True)
    log = ROOT / '.mdva' / 'service.log'
    log.parent.mkdir(exist_ok=True)
    args = [str(executable), '-u', str(ROOT / 'main.py'), 'serve', '--data', str(ROOT / 'data'),
            '--out', str(ROOT / 'out'), '--reuse']
    for name, source in default_sources().items():
        args.extend(['--source', name + '=' + str(source)])
    with log.open('ab') as stream:
        child = subprocess.Popen(args, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stream,
                                 stderr=stream, env={**os.environ, 'MDVA_MONITOR_ONLINE': '1'}, creationflags=HIDDEN, start_new_session=os.name != 'nt')
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if same_service(health()):
            return
        if child.poll() is not None:
            raise RuntimeError(f'应用未能启动。端口可能被其他程序占用，详情已记录到：\n{log}')
        time.sleep(.2)
    # Do not terminate a service which may still be initializing a workspace.
    raise RuntimeError(f'服务仍未就绪，请稍后重试。启动日志：\n{log}')


def setup_window(initial_error=''):
    import tkinter as tk
    from tkinter import ttk, filedialog
    window = tk.Tk()
    window.title('MDVA · 首次使用设置')
    window.geometry('700x475')
    window.minsize(650, 440)
    window.configure(bg='#f4f6fa')
    style = ttk.Style(window)
    if 'vista' in style.theme_names():
        style.theme_use('vista')
    style.configure('TFrame', background='#f4f6fa')
    style.configure('TLabel', background='#f4f6fa', font=('Microsoft YaHei UI', 10))
    body = ttk.Frame(window, padding=24); body.pack(fill='both', expand=True)
    ttk.Label(body, text='欢迎使用 MDVA', font=('Microsoft YaHei UI', 20, 'bold')).pack(anchor='w')
    ttk.Label(body, text='首次检查运行环境。完成设置后，下次将直接打开网页版。').pack(anchor='w', pady=(8, 18))
    selected = tk.StringVar()
    row = ttk.Frame(body); row.pack(fill='x')
    chooser = ttk.Combobox(row, textvariable=selected, state='readonly')
    chooser.pack(side='left', fill='x', expand=True)
    status = tk.StringVar(value=initial_error or '正在查找可用的 Python 环境…')
    ttk.Label(body, textvariable=status, wraplength=640).pack(anchor='w', pady=(14, 8))
    details = tk.Text(body, height=9, wrap='word', relief='flat', padx=12, pady=10,
                      font=('Microsoft YaHei UI', 9), state='disabled')
    details.pack(fill='both', expand=True)
    events = queue.Queue()
    state = {'busy': False, 'ready': '', 'options': []}

    def detail(text):
        details.configure(state='normal'); details.delete('1.0', 'end')
        details.insert('end', text); details.configure(state='disabled')

    def work(kind, fn):
        state['busy'] = True; state['ready'] = ''
        launch.configure(state='disabled'); check.configure(state='disabled')
        install.configure(state='disabled'); browse.configure(state='disabled')
        chooser.configure(state='disabled')
        def run():
            try: events.put((kind, fn(), None))
            except Exception as exc: events.put((kind, None, str(exc)))
        threading.Thread(target=run, daemon=True).start()

    def scan(paths=None):
        status.set('正在检查环境，请稍候…')
        def run():
            options = paths if paths is not None else candidates()
            rows = []
            for path in options:
                result = probe(path); rows.append((path, result))
                if result['ok']: break
            return rows
        work('scan', run)

    def choose():
        value = filedialog.askopenfilename(title='选择 Python 解释器', filetypes=[('Python', 'python.exe'), ('所有文件', '*')])
        if value: selected.set(value); scan([value])

    browse = ttk.Button(row, text='选择其他环境', command=choose); browse.pack(side='right', padx=(8, 0))
    actions = ttk.Frame(body); actions.pack(fill='x', pady=(16, 0))
    check = ttk.Button(actions, text='重新检查', command=lambda: scan([selected.get()] if selected.get() else None))
    check.pack(side='left')

    def install_deps():
        from tkinter import messagebox
        path = selected.get()
        if not path: return
        if not messagebox.askyesno('安装运行依赖', '将使用所选 Python 安装项目运行依赖，需要网络。\n不会安装开发测试工具。\n\n' + path, parent=window): return
        status.set('正在安装运行依赖，完成后将自动重新检查…')
        detail('安装可能需要几分钟，请保持此窗口打开。')
        def run():
            log = ROOT / '.mdva' / 'install.log'; log.parent.mkdir(exist_ok=True)
            with log.open('wb') as stream:
                result = subprocess.run([path, '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.txt'),
                                         '-r', str(ROOT / 'requirements/science.txt')], cwd=ROOT,
                                        stdout=stream, stderr=stream, creationflags=HIDDEN)
            if result.returncode: raise RuntimeError(f'安装未完成，请检查网络或更换环境。日志：{log}')
            return path
        work('install', run)
    install = ttk.Button(actions, text='安装所需依赖', command=install_deps); install.pack(side='left', padx=8)

    def start():
        path = state['ready']
        if not path: return
        status.set('正在启动应用…')
        def run():
            start_service(path)
            remember(path)
            webbrowser.open(URL)
        work('launch', run)
    launch = ttk.Button(actions, text='启动应用', command=start, state='disabled'); launch.pack(side='right')
    chooser.bind('<<ComboboxSelected>>', lambda _: scan([selected.get()]))

    def receive():
        try:
            kind, value, error = events.get_nowait()
        except queue.Empty:
            window.after(100, receive); return
        state['busy'] = False
        check.configure(state='normal'); browse.configure(state='normal'); chooser.configure(state='readonly')
        if error:
            status.set('操作未完成，请检查详情后重试。'); detail(error)
        elif kind == 'launch':
            window.destroy(); return
        elif kind == 'install':
            scan([value])
        else:
            options = list(dict.fromkeys(state['options'] + [path for path, _ in value]))
            state['options'] = options; chooser.configure(values=options)
            if not value:
                status.set('未找到 Python，请选择已安装的 Python 3.10 或更新版本。')
            else:
                path, result = value[-1]; selected.set(path)
                if result['ok']:
                    state['ready'] = path
                    status.set('环境检查通过，请处理启动问题后重试。' if initial_error else '环境检查通过，可以启动应用。')
                    detail((initial_error + '\n\n' if initial_error else '') + '✓ Python ' + result['version'] + '\n✓ 网页服务及应用模块\n✓ 图像、科学数据和实例掩膜依赖\n\n运行环境：\n' + path + '\n\n此设置仅首次显示；下次直接打开应用。')
                    launch.configure(state='normal')
                else:
                    status.set('当前环境尚未就绪，可以更换环境或安装所需依赖。')
                    detail('\n\n'.join(path + '\n' + '\n'.join(result['failures']) for path, result in value))
                    install.configure(state='normal')
        window.after(100, receive)
    window.after(100, receive)
    window.after(150, scan)
    window.mainloop()
    return 0


def main():
    saved = read_state()
    selected = os.environ.get('MDVA_PY') or saved.get('python')
    error = ''
    if saved and selected:
        result = probe(selected)
        if result['ok']:
            try:
                start_service(selected)
                webbrowser.open(URL)
                return 0
            except Exception as exc:
                error = str(exc)
        else:
            error = '已保存的环境发生变化，需要重新检查。'
    try:
        return setup_window(error)
    except Exception as exc:
        # No engine dependency is needed to display/report bootstrap failures.
        log = ROOT / '.mdva' / 'launcher-error.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(str(exc), encoding='utf-8')
        if os.name == 'nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, '启动器无法打开，请使用带 Tk 支持的 Python。\n详情：' + str(log), 'MDVA 启动器', 0x10)
        else:
            print(f'MDVA 启动器无法打开：{exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
