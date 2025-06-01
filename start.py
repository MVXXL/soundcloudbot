import os
import sys
import time
import signal
import subprocess
import psutil
import logging
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log')
    ]
)

logger = logging.getLogger(__name__)

class ProcessManager:
    def __init__(self):
        self.lavalink_process = None
        self.bot_process = None
        self.is_shutting_down = False

    def start_lavalink(self):
        try:
            if not os.path.exists('lavalink/Lavalink.jar'):
                logger.error("Lavalink.jar not found in lavalink folder!")
                return False

            if not os.path.exists('lavalink/application.yml'):
                logger.error("application.yml not found in lavalink folder!")
                return False

            logger.info("Starting Lavalink server...")
            self.lavalink_process = subprocess.Popen(
                ['java', '-jar', 'Lavalink.jar'],
                cwd='lavalink',
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            return True
        except Exception as e:
            logger.error(f"Failed to start Lavalink: {e}")
            return False

    def start_bot(self):
        try:
            logger.info("Starting Discord bot...")
            self.bot_process = subprocess.Popen(
                [sys.executable, 'bot.py'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            return True
        except Exception as e:
            logger.error(f"Failed to start bot: {e}")
            return False

    def stop_processes(self):
        if self.is_shutting_down:
            return

        self.is_shutting_down = True
        logger.info("Shutting down processes...")

        if self.bot_process:
            try:
                self.bot_process.terminate()
                self.bot_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.bot_process.kill()
            except Exception as e:
                logger.error(f"Error stopping bot process: {e}")

        if self.lavalink_process:
            try:
                self.lavalink_process.terminate()
                self.lavalink_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.lavalink_process.kill()
            except Exception as e:
                logger.error(f"Error stopping Lavalink process: {e}")

        logger.info("All processes stopped")

    def monitor_processes(self):
        while not self.is_shutting_down:
            if self.bot_process and self.bot_process.poll() is not None:
                logger.info("Bot process stopped, restarting...")
                self.stop_processes()
                break

            if self.lavalink_process and self.lavalink_process.poll() is not None:
                logger.info("Lavalink process stopped, restarting...")
                self.stop_processes()
                break

            time.sleep(1)

def signal_handler(signum, frame):
    logger.info(f"Received signal {signum}")
    manager.stop_processes()
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    manager = ProcessManager()

    if not manager.start_lavalink():
        logger.error("Failed to start Lavalink, exiting...")
        sys.exit(1)

    time.sleep(10)

    if not manager.start_bot():
        logger.error("Failed to start bot, exiting...")
        manager.stop_processes()
        sys.exit(1)

    try:
        manager.monitor_processes()
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
        manager.stop_processes()
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        manager.stop_processes()
        sys.exit(1) 