import os

# Disable tokenizers parallelism globally before any other imports or background threads start
os.environ["TOKENIZERS_PARALLELISM"] = "false"

# Force PyTorch multiprocessing start method to spawn and sharing strategy to file_system
# at the absolute start of the process before any C-extensions, UI threads, or Qdrant/SQLite clients load.
try:
    import torch  # type: ignore

    torch.set_num_threads(1)
    import torch.multiprocessing as mp  # type: ignore

    try:
        mp.set_start_method("spawn", force=True)
    except Exception:
        pass
    try:
        mp.set_sharing_strategy("file_system")
    except Exception:
        pass
except Exception:
    pass

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
