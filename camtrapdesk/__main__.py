import sys


def main() -> None:
    # 絶対インポートにしておく（PyInstaller でスクリプトとして実行されても動くように）
    from camtrapdesk.app import run
    sys.exit(run())


if __name__ == "__main__":
    main()
