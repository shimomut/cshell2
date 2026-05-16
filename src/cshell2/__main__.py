"""Entry point for cshell2."""

from .shell import Shell


def main():
    shell = Shell()
    shell.run()


if __name__ == "__main__":
    main()
