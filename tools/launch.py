"""ダブルクリック起動用のブートストラップ。

起動.bat / 起動.command から呼ばれ、初回は .venv を作ってライブラリを入れ、
その後アプリを起動する。表示は Python 側で行うので、コンソールの文字コードに依存しない。
"""

import os
import subprocess
import sys
import venv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV = os.path.join(ROOT, ".venv")
IS_WIN = sys.platform == "win32"
PY = os.path.join(VENV, "Scripts" if IS_WIN else "bin", "python.exe" if IS_WIN else "python")
PYW = os.path.join(VENV, "Scripts", "pythonw.exe") if IS_WIN else PY
LOG = os.path.join(ROOT, "camtrapdesk-setup.log")


def say(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode(), flush=True)


def run(cmd: list[str], logf) -> int:
    logf.write("$ " + " ".join(cmd) + "\n")
    logf.flush()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace", cwd=ROOT)
    assert p.stdout is not None
    for line in p.stdout:
        logf.write(line)
        line = line.rstrip()
        if line.startswith(("Collecting", "Downloading", "Installing", "Successfully", "ERROR", "error")):
            say("  " + line[:100])
    return p.wait()


def fail(msg: str) -> None:
    say("")
    say("!! " + msg)
    say(f"   ログ: {LOG}")
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            tail = f.read().splitlines()[-25:]
        say("   ---- ログの末尾 ----")
        for line in tail:
            say("   " + line)
    except OSError:
        pass
    try:
        input("Enter キーで閉じます ")
    except EOFError:
        pass
    sys.exit(1)


def venv_home() -> str:
    """.venv の元になった Python のフォルダ（pyvenv.cfg の home）。無ければ今の Python。"""
    cfg = os.path.join(VENV, "pyvenv.cfg")
    try:
        with open(cfg, encoding="utf-8") as f:
            for line in f:
                if line.lower().startswith("home"):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return os.path.dirname(sys.executable)


def fix_conda_path() -> None:
    """Anaconda/Miniconda の Python から作った venv は、conda を activate していないと
    OpenSSL や SQLite の DLL（<conda>\\Library\\bin）が見つからず、pip がダウンロードできない・
    アプリが sqlite3 を開けない。その DLL フォルダを PATH の先頭に足す（Windows のみ）。"""
    if not IS_WIN:
        return
    home = venv_home()
    cands = [home, os.path.dirname(home)]   # home が <conda> か <conda>\\bin のどちらでも
    for base in cands:
        libbin = os.path.join(base, "Library", "bin")
        if os.path.isdir(libbin):
            extra = [libbin, os.path.join(base, "Library", "usr", "bin"), os.path.join(base, "Scripts"),
                     os.path.join(base, "DLLs"), base]
            extra = [p for p in extra if os.path.isdir(p)]
            os.environ["PATH"] = os.pathsep.join(extra + [os.environ.get("PATH", "")])
            return


def main() -> None:
    os.chdir(ROOT)
    with open(LOG, "a", encoding="utf-8") as logf:
        if not os.path.exists(PY):
            say("=" * 60)
            say(" camtrapdesk 初回セットアップ")
            say(f" Python: {sys.executable} ({sys.version.split()[0]})")
            say(" 必要なライブラリ（PySide6 など）を取得します。数分かかります。")
            say("=" * 60)
            try:
                venv.EnvBuilder(with_pip=True, clear=True).create(VENV)
            except Exception as e:
                logf.write(f"venv create failed: {e}\n")
                fail("仮想環境（.venv）の作成に失敗しました。")
        fix_conda_path()
        check = [PY, "-c", "import camtrapdesk.app, openpyxl, xxhash, imageio_ffmpeg, ssl, sqlite3"]
        if run(check, logf) != 0:
            say("ライブラリを取得中…（PySide6 は 250MB ほどあります）")
            run([PY, "-m", "pip", "install", "--upgrade", "pip"], logf)
            # パッケージ自体はこのフォルダから直接読み込む（インストールしない）ので、
            # フォルダを別のドライブへ移しても動く。依存ライブラリだけ入れる。
            if run([PY, "-m", "pip", "install", "-r", os.path.join(ROOT, "requirements.txt")], logf) != 0:
                fail("ライブラリの取得に失敗しました。ネットワーク接続を確認して、もう一度実行してください。")
            if run(check, logf) != 0:
                fail("ライブラリの読み込みに失敗しました。.venv フォルダを削除してから、もう一度実行してください。")
            say("セットアップが終わりました。")
    say("camtrapdesk を起動します。")
    kwargs = {}
    if IS_WIN:
        kwargs["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([PYW, "-m", "camtrapdesk"], cwd=ROOT, env=os.environ.copy(), **kwargs)


if __name__ == "__main__":
    main()
