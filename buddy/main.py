from buddy.logger.logger import get_logger

logger = get_logger("main")


def main() -> int:
    try:
        from buddy.ui.textual_app import run_textual

        run_textual()  # bootstrap runs inside BootScreen
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as ex:
        logger.error("Main crashed: %r", ex)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
