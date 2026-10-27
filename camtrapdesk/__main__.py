import sys


def main() -> None:
    from .app import run
    sys.exit(run())


if __name__ == "__main__":
    main()
