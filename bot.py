import disnake
from disnake.ext import commands
import os
import logging
import asyncio
import mafic
from dotenv import load_dotenv
from cogs.music import Music

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger('bot')

load_dotenv()

class MusicBot(commands.InteractionBot):
    def __init__(self):
        intents = disnake.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.mafic_ready = False
        self.node = None
        self.pool = mafic.NodePool(self)
        logger.info("Bot initialized")

    async def load_initial_cogs(self):
        logger.info("Loading initial cogs...")
        try:
            if "cogs.music" not in self.extensions:
                logger.info("Loading Music cog...")
                self.load_extension("cogs.music")
                logger.info("Music cog loaded successfully")
            else:
                logger.info("Music cog already loaded")
            try:
                logger.info("Connecting to Lavalink...")
                await self.pool.create_node(
                    host="127.0.0.1",
                    port=7183,
                    password="youshallnotpass",
                    label="MAIN"
                )
                logger.info("Successfully connected to Lavalink")
            except Exception as e:
                logger.error(f"Failed to connect to Lavalink: {e}")
                raise
                
        except Exception as e:
            logger.error(f"Error loading cogs: {e}")
            raise

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        logger.info("------")
        await self.load_initial_cogs()

    async def on_error(self, event_method: str, *args, **kwargs):
        logger.error(f"Error in {event_method}:", exc_info=True)

    @commands.slash_command()
    async def ping(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.send_message(f"Pong! Latency: {round(self.latency * 1000)}ms")

    @commands.slash_command()
    async def reload(self, inter: disnake.ApplicationCommandInteraction):
        if inter.author.id != 1193877999465025586:   
            return await inter.response.send_message("❌ You don't have permission to use this command!", ephemeral=True)
            
        try:
            self.unload_extension("cogs.music")
            self.load_extension("cogs.music")
            await inter.response.send_message("✅ Music cog reloaded!", ephemeral=True)
        except Exception as e:
            await inter.response.send_message(f"❌ Error reloading music cog: {e}", ephemeral=True)

def main():
    try:
        token = os.getenv('TOKEN')
        if not token:
            logger.error("No token found in .env file!")
            return
            
        bot = MusicBot()
        bot.run(token)
        
    except Exception as e:
        logger.error(f"Error starting bot: {e}")

if __name__ == "__main__":
    main()