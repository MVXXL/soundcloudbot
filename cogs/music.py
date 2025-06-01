import disnake
from disnake.ext import commands
import mafic
import logging
import asyncio
import time
from typing import Optional, List
import aiohttp
from io import BytesIO
from PIL import Image, ImageDraw, ImageFilter
from datetime import datetime
import sqlite3
import os
import json
import random
import traceback

logger = logging.getLogger('music_cog')

class MusicDatabase:
    def __init__(self):
        self.db_path = 'music_history.db'
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS track_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    track_title TEXT NOT NULL,
                    track_author TEXT,
                    user_id INTEGER NOT NULL,
                    guild_id INTEGER NOT NULL,
                    played_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS daily_mixes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    guild_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    tracks TEXT NOT NULL,
                    UNIQUE(user_id, guild_id)
                )
            ''')
            conn.commit()

    async def add_track(self, track_title: str, track_author: str, user_id: int, guild_id: int):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO track_history (track_title, track_author, user_id, guild_id)
                VALUES (?, ?, ?, ?)
            ''', (track_title, track_author, user_id, guild_id))
            conn.commit()

    async def get_user_tracks(self, user_id: int, limit: int = 10):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT track_title, track_author, played_at
                FROM track_history
                WHERE user_id = ?
                ORDER BY played_at DESC
                LIMIT ?
            ''', (user_id, limit))
            return cursor.fetchall()

    async def get_guild_tracks(self, guild_id: int, limit: int = 10):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT track_title, track_author, user_id, played_at
                FROM track_history
                WHERE guild_id = ?
                ORDER BY played_at DESC
                LIMIT ?
            ''', (guild_id, limit))
            return cursor.fetchall()

    async def get_most_played_tracks(self, guild_id: int, limit: int = 10):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT track_title, track_author, COUNT(*) as play_count
                FROM track_history
                WHERE guild_id = ?
                GROUP BY track_title, track_author
                ORDER BY play_count DESC
                LIMIT ?
            ''', (guild_id, limit))
            return cursor.fetchall()

    async def get_user_unique_tracks(self, user_id: int, guild_id: int, limit: int = 100) -> list:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT DISTINCT track_title, track_author
                FROM track_history
                WHERE user_id = ? AND guild_id = ?
                ORDER BY played_at DESC
                LIMIT ?
            ''', (user_id, guild_id, limit))
            return cursor.fetchall()

    async def save_daily_mix(self, user_id: int, guild_id: int, tracks: list):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            tracks_json = json.dumps(tracks)
            cursor.execute('''
                INSERT OR REPLACE INTO daily_mixes (user_id, guild_id, tracks, created_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ''', (user_id, guild_id, tracks_json))
            conn.commit()

    async def get_daily_mix(self, user_id: int, guild_id: int) -> Optional[tuple]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT tracks, created_at
                FROM daily_mixes
                WHERE user_id = ? AND guild_id = ? AND date(created_at) = date('now')
                ORDER BY created_at DESC
                LIMIT 1
            ''', (user_id, guild_id))
            return cursor.fetchone()

class MusicPlayer(mafic.Player):
    def __init__(self, *args, **kwargs):
        self.db = kwargs.pop('db', None)
        super().__init__(*args, **kwargs)
        self.queue: List[mafic.Track] = []
        self.loop_mode: str = None
        self.volume = 100
        self.last_update = datetime.now()
        self.controller_message = None
        self.current_track = None
        self.update_bool = False
        self._track_end_task = None
        self._is_skipping = False
        self._monitor_lock = asyncio.Lock()
        self.last_user_id = None
        self.last_username = None
        self.history: List[mafic.Track] = []
        self.is_247 = False
        self.track_users = {}

    async def play(self, track: mafic.Track, user_id: int = None, username: str = None):
        logger.info(f"[PLAYER] Начало воспроизведения трека: {track.title}")
        self.current_track = track
        if user_id:
            self.last_user_id = user_id
            self.last_username = username
            self.track_users[track.identifier] = {'user_id': user_id, 'username': username}
            logger.info(f"[PLAYER] Установлен last_user_id: {user_id}, last_username: {username}")
        
        async with self._monitor_lock:
            if self._track_end_task and not self._track_end_task.done():
                logger.info("[PLAYER] Отмена предыдущей задачи мониторинга")
                self._track_end_task.cancel()
                try:
                    await self._track_end_task
                except asyncio.CancelledError:
                    pass
                self._track_end_task = None
                
            logger.info(f"[PLAYER] Создание новой задачи мониторинга для трека: {track.title}")
            self._track_end_task = asyncio.create_task(self._track_end_monitor())
        
        await super().play(track)

    async def _track_end_monitor(self):
        try:
            if not self.current_track:
                logger.info("[TRACK_MONITOR] Нет текущего трека для мониторинга")
                return
                
            track_duration = self.current_track.length / 1000
            logger.info(f"[TRACK_MONITOR] Начало мониторинга трека: {self.current_track.title} | Длительность: {track_duration:.2f}с")
            
            await asyncio.sleep(track_duration + 2)
            
            if self.is_connected and self.current_track and not self._is_skipping:
                logger.info(f"[TRACK_MONITOR] Трек достиг конца по таймеру: {self.current_track.title}")
                await self.destroy(self)
            else:
                logger.info(f"[TRACK_MONITOR] Трек больше не играет или был пропущен: {self.current_track.title if self.current_track else 'None'}")
                
        except asyncio.CancelledError:
            logger.info(f"[TRACK_MONITOR] Мониторинг трека отменен: {self.current_track.title if self.current_track else 'None'}")
        except Exception as e:
            logger.error(f"[TRACK_MONITOR] Ошибка при мониторинге трека: {e}")

    async def skip(self):
        if self.current_track:
            self.history.append(self.current_track)
        self._is_skipping = True
        await self.stop()
        self._is_skipping = False
        
        if self.queue:
            next_track = self.queue.pop(0)
            self.current_track = next_track
            logger.info(f"[SKIP] Воспроизведение следующего трека: {next_track.title}")
            
            track_info = self.track_users.get(next_track.identifier)
            if track_info:
                self.last_user_id = track_info['user_id']
                self.last_username = track_info['username']
                logger.info(f"[SKIP] Установлен last_user_id: {self.last_user_id}, last_username: {self.last_username}")
            
            await self.play(next_track, self.last_user_id, self.last_username)
            
            if self.controller_message:
                try:
                    bot = self.guild.me.guild._state._get_client()
                    music_cog = bot.get_cog('Music')
                    if music_cog:
                        controls = MusicControls(bot, self.last_user_id)
                        controls.update_buttons_state(self)
                        banner_file = await music_cog.create_music_banner(self, next_track)
                        
                        channel = self.controller_message.channel
                        self.controller_message = await channel.send(
                            file=banner_file if banner_file else None,
                            content=f"▶️ Сейчас играет: **{next_track.title}**" if not banner_file else None,
                            view=controls
                        )
                except Exception as e:
                    logger.error(f"[SKIP] Ошибка при обновлении баннера: {e}")
        else:
            logger.info("[SKIP] В очереди больше нет треков")
            if self.controller_message:
                await self.controller_message.edit(components=None)
            await self.disconnect()

    async def play_previous(self):
        if self.history:
            previous_track = self.history.pop()
            if self.current_track:
                self.queue.insert(0, self.current_track)
            await self.play(previous_track, self.last_user_id, self.last_username)
            return True
        return False

    @commands.Cog.listener()
    async def on_mafic_track_end(self, player: 'MusicPlayer', track: mafic.Track, reason):
        logger.info(f"[TRACK_END] Трек закончился: {track.title} | Причина: {reason} | Режим повтора: {player.loop_mode} | Треков в очереди: {len(player.queue)}")
        try:
            await self.destroy(player)
        except Exception as e:
            logger.error(f"[TRACK_END] Ошибка при обработке окончания трека: {e}")

    @commands.Cog.listener()
    async def on_mafic_track_exception(self, player: 'MusicPlayer', track: mafic.Track, error: Exception):
        logger.error(f"[TRACK_ERROR] Ошибка трека: {track.title} | Ошибка: {error}")
        await self.destroy(player)

    @commands.Cog.listener()
    async def on_mafic_track_stuck(self, player: 'MusicPlayer', track: mafic.Track, threshold: int):
        logger.warning(f"[TRACK_STUCK] Трек застрял: {track.title} | Порог: {threshold}")
        await self.destroy(player)

    async def destroy(self, player: 'MusicPlayer'):
        logger.info(f"[DESTROY] Обработка окончания трека. Режим повтора: {player.loop_mode} | Треков в очереди: {len(player.queue)}")
        
        if player.current_track and player.last_user_id and player.db:
            try:
                await player.db.add_track(
                    track_title=player.current_track.title,
                    track_author=player.current_track.author,
                    user_id=player.last_user_id,
                    guild_id=player.guild.id
                )
            except Exception as e:
                logger.error(f"[DESTROY] Ошибка при сохранении трека в базу данных: {e}")
        
        if not player.queue and not player.is_247:
            logger.info("[DESTROY] Очередь пуста и режим 24/7 выключен")
            if player.controller_message:
                await player.controller_message.edit(components=None)
            return await player.disconnect()

        if player.loop_mode == 'track' and player.current_track:
            logger.info(f"[DESTROY] Повтор текущего трека: {player.current_track.title}")
            await player.play(player.current_track, player.last_user_id, self.last_username)
            return
        elif player.loop_mode == 'queue' and player.queue:
            track = player.queue.pop(0)
            player.queue.append(track)
            player.current_track = track
            logger.info(f"[DESTROY] Повтор очереди: {track.title}")
            await player.play(track, player.last_user_id, self.last_username)
            return
        elif player.loop_mode is None and player.queue:
            player.current_track = player.queue.pop(0)
            logger.info(f"[DESTROY] Воспроизведение следующего трека: {player.current_track.title}")
            await player.play(player.current_track, player.last_user_id, player.last_username)
            
            if player.controller_message:
                try:
                    bot = player.guild.me.guild._state._get_client()
                    music_cog = bot.get_cog('Music')
                    if music_cog:
                        controls = MusicControls(bot, player.last_user_id)
                        controls.update_buttons_state(player)
                        banner_file = await music_cog.create_music_banner(player, player.current_track)
                        
                        channel = player.controller_message.channel
                        player.controller_message = await channel.send(
                            file=banner_file if banner_file else None,
                            content=f"▶️ Сейчас играет: **{player.current_track.title}**" if not banner_file else None,
                            view=controls
                        )
                except Exception as e:
                    logger.error(f"[DESTROY] Ошибка при обновлении баннера: {e}")
            return

        logger.info("[DESTROY] Нет треков для воспроизведения")
        if not player.is_247:
            logger.info("[DESTROY] Режим 24/7 выключен, отключаемся")
            if player.controller_message:
                await player.controller_message.edit(components=None)
            await player.disconnect()

class MusicControls(disnake.ui.View):
    def __init__(self, bot: commands.InteractionBot, user_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.user_id = user_id
        self.add_control_buttons()

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        if not interaction.guild.voice_client:
            await self.send_temp_message(interaction, "❌ Бот не в голосовом канале!")
            return False
            
        player = interaction.guild.voice_client
        
        if interaction.author.id != player.last_user_id:
            await self.send_temp_message(
                interaction,
                f"❌ Только {interaction.guild.get_member(player.last_user_id).mention} может управлять воспроизведением!"
            )
            return False
        return True

    async def send_temp_message(self, interaction: disnake.MessageInteraction, content: str):
        try:
            await interaction.response.send_message(content, ephemeral=True)
            msg = await interaction.original_message()
            
            await asyncio.sleep(30)
            try:
                await msg.delete()
            except:
                pass
        except Exception as e:
            logger.error(f"Error in send_temp_message: {e}")

    def add_control_buttons(self):
        first_row = [
            ("<:volumeup:1378657097151025152>", "volume_up"),
            ("<:previous:1378658319840841758>", "previous"),
            ("<:pause:1378658318230491268>", "play_pause"),
            ("<:skip:1378658315504058418>", "skip"),
            ("", "volume_display"),
        ]
        second_row = [
            ("<:volumedown:1378657094781370378>", "volume_down"),
            ("<:stop:1378658312073252934>", "stop"),
            ("<:nexttracks:1378658316938510336>", "next_tracks"),
            ("<:looop:1378658308533260499>", "loop"),
            ("", "queue_size")
        ]

        for emoji, custom_id in first_row:
            button = disnake.ui.Button(
                emoji=emoji if emoji else None,
                custom_id=custom_id,
                style=disnake.ButtonStyle.grey,
                row=0
            )
            if custom_id == "volume_display":
                button.disabled = True
            button.callback = getattr(self, f"_{custom_id}")
            self.add_item(button)

        for emoji, custom_id in second_row:
            button = disnake.ui.Button(
                emoji=emoji if emoji else None,
                custom_id=custom_id,
                style=disnake.ButtonStyle.grey,
                row=1
            )
            if custom_id == "queue_size":
                button.disabled = True
            button.callback = getattr(self, f"_{custom_id}")
            self.add_item(button)

    def update_buttons_state(self, player):
        skip_button = next((item for item in self.children if item.custom_id == "skip"), None)
        if skip_button:
            skip_button.disabled = not player.queue

        play_pause_button = next((item for item in self.children if item.custom_id == "play_pause"), None)
        if play_pause_button:
            play_pause_button.disabled = not player.current_track
            play_pause_button.emoji = "<:pause:1378658318230491268>" if not player.paused else "<:play:1378658313755164682>"

        stop_button = next((item for item in self.children if item.custom_id == "stop"), None)
        if stop_button:
            stop_button.disabled = not player.current_track

        volume_up_button = next((item for item in self.children if item.custom_id == "volume_up"), None)
        if volume_up_button:
            volume_up_button.disabled = not player.current_track

        volume_down_button = next((item for item in self.children if item.custom_id == "volume_down"), None)
        if volume_down_button:
            volume_down_button.disabled = not player.current_track

        previous_button = next((item for item in self.children if item.custom_id == "previous"), None)
        if previous_button:
            previous_button.disabled = not player.history

        next_tracks_button = next((item for item in self.children if item.custom_id == "next_tracks"), None)
        if next_tracks_button:
            next_tracks_button.disabled = not player.queue

        volume_display_button = next((item for item in self.children if item.custom_id == "volume_display"), None)
        if volume_display_button:
            volume_display_button.disabled = True
            volume_display_button.label = f"{player.volume}%"
            volume_display_button.emoji = None

        queue_size_button = next((item for item in self.children if item.custom_id == "queue_size"), None)
        if queue_size_button:
            queue_size_button.disabled = True
            queue_size_button.label = f"{len(player.queue)}"
            queue_size_button.emoji = None

    def update_loop_button(self, player):
        loop_button = next((item for item in self.children if item.custom_id == "loop"), None)
        if loop_button:
            if player.loop_mode == 'track':
                loop_button.style = disnake.ButtonStyle.blurple
                loop_button.emoji = "<:looop:1378658308533260499>"
            elif player.loop_mode == 'queue':
                loop_button.style = disnake.ButtonStyle.blurple
                loop_button.emoji = "<:looop:1378658308533260499>"
            else:
                loop_button.style = disnake.ButtonStyle.grey
                loop_button.emoji = "<:looop:1378658308533260499>"
            loop_button.disabled = not player.current_track

    async def _next_tracks(self, interaction: disnake.MessageInteraction):
        if not interaction.guild.voice_client:
            return await interaction.response.send_message("❌ Бот не в голосовом канале!", ephemeral=True)
            
        player = interaction.guild.voice_client
        if not player.queue:
            return await interaction.response.send_message("❌ В очереди больше нет треков!", ephemeral=True)

        next_tracks = player.queue[:3]
        
        embed = disnake.Embed(
            title="📆 Следующие треки",
            color=0x2b2d31
        )
        
        for i, track in enumerate(next_tracks, 1):
            duration = time.strftime("%M:%S", time.gmtime(track.length / 1000))
            embed.add_field(
                name=f"{i}. {track.title}",
                value=f"👤 {track.author}\n⏱️ {duration}",
                inline=False
            )
            
        if len(player.queue) > 3:
            embed.set_footer(text="И ещё треков в очереди...")
            
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _previous(self, interaction: disnake.MessageInteraction):
        if not interaction.guild.voice_client:
            return await interaction.response.send_message("❌ Бот не в голосовом канале!", ephemeral=True)
            
        player = interaction.guild.voice_client
        if await player.play_previous():
            await interaction.response.send_message("⏮️ Предыдущий трек", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Нет предыдущих треков!", ephemeral=True)

    async def _play_pause(self, interaction: disnake.MessageInteraction):
        if not interaction.guild.voice_client:
            return await interaction.response.send_message("❌ Бот не в голосовом канале!", ephemeral=True)
            
        player = interaction.guild.voice_client
        if player.paused:
            await player.resume()
        else:
            await player.pause()
            
        self.update_buttons_state(player)
        await interaction.response.edit_message(view=self)

    async def _skip(self, interaction: disnake.MessageInteraction):
        if not interaction.guild.voice_client:
            return await interaction.response.send_message("❌ Бот не в голосовом канале!", ephemeral=True)
            
        player = interaction.guild.voice_client
        if not player.queue:
            return await interaction.response.send_message("❌ В очереди больше нет треков!", ephemeral=True)

        await interaction.response.defer()
        
        await player.skip()
        
        await interaction.followup.send("⏭️ Пропущено", ephemeral=True)

    async def _loop(self, interaction: disnake.MessageInteraction):
        if not interaction.guild.voice_client:
            return await interaction.response.send_message("❌ Бот не в голосовом канале!", ephemeral=True)
            
        player = interaction.guild.voice_client
        old_mode = player.loop_mode
        if player.loop_mode is None:
            player.loop_mode = 'track'
            logger.info(f"[LOOP] Режим повтора изменен: {old_mode} -> track | Трек: {player.current_track.title if player.current_track else 'None'}")
        elif player.loop_mode == 'track':
            player.loop_mode = 'queue'
            logger.info(f"[LOOP] Режим повтора изменен: {old_mode} -> queue | Треков в очереди: {len(player.queue)}")
        else:
            player.loop_mode = None
            logger.info(f"[LOOP] Режим повтора выключен: {old_mode} -> None")

        self.update_loop_button(player)
        await interaction.response.edit_message(view=self)

    async def _volume_up(self, interaction: disnake.MessageInteraction):
        if not interaction.guild.voice_client:
            return await interaction.response.send_message("❌ Бот не в голосовом канале!", ephemeral=True)
            
        player = interaction.guild.voice_client
        player.volume = min(200, player.volume + 10)
        await player.set_volume(player.volume)
        
        volume_display_button = next((item for item in self.children if item.custom_id == "volume_display"), None)
        if volume_display_button:
            volume_display_button.label = f"{player.volume}%"
        await interaction.response.edit_message(view=self)

    async def _volume_down(self, interaction: disnake.MessageInteraction):
        if not interaction.guild.voice_client:
            return await interaction.response.send_message("❌ Бот не в голосовом канале!", ephemeral=True)
            
        player = interaction.guild.voice_client
        player.volume = max(0, player.volume - 10)
        await player.set_volume(player.volume)
        
        volume_display_button = next((item for item in self.children if item.custom_id == "volume_display"), None)
        if volume_display_button:
            volume_display_button.label = f"{player.volume}%"
        await interaction.response.edit_message(view=self)

    async def _stop(self, interaction: disnake.MessageInteraction):
        if not interaction.guild.voice_client:
            return await interaction.response.send_message("❌ Бот не в голосовом канале!", ephemeral=True)
            
        player = interaction.guild.voice_client
        await player.stop()
        await player.disconnect()
        await interaction.response.send_message("⏹️ Воспроизведение остановлено", ephemeral=True)

    async def _volume_display(self, interaction: disnake.MessageInteraction):
        if not interaction.guild.voice_client:
            return await interaction.response.send_message("❌ Бот не в голосовом канале!", ephemeral=True)
            
        player = interaction.guild.voice_client
        await interaction.response.send_message(f"🔊 Текущая громкость: {player.volume}%", ephemeral=True)

    async def _queue_size(self, interaction: disnake.MessageInteraction):
        if not interaction.guild.voice_client:
            return await interaction.response.send_message("❌ Бот не в голосовом канале!", ephemeral=True)
            
        player = interaction.guild.voice_client
        await interaction.response.send_message(f"📋 В очереди: {len(player.queue)} треков", ephemeral=True)

class Music(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        self.db = MusicDatabase()
        self.command_usage = {}
        self.cooldown_users = set()
        logger.info("Music cog initialized")

    async def send_temp_message(self, inter: disnake.ApplicationCommandInteraction, content: str, ephemeral: bool = True):
        try:
            if inter.response.is_done():
                msg = await inter.followup.send(content, ephemeral=ephemeral)
            else:
                await inter.response.send_message(content, ephemeral=ephemeral)
                msg = await inter.original_message()
            
            await asyncio.sleep(30)
            try:
                await msg.delete()
            except:
                pass
        except Exception as e:
            logger.error(f"Error in send_temp_message: {e}")

    async def check_command_cooldown(self, inter: disnake.ApplicationCommandInteraction) -> bool:
        user_id = inter.author.id
        
        if user_id in self.cooldown_users:
            return False
            
        current_time = time.time()
        
        if user_id not in self.command_usage:
            self.command_usage[user_id] = {
                'count': 1,
                'first_use': current_time
            }
        else:
            usage_data = self.command_usage[user_id]
            
            if current_time - usage_data['first_use'] > 10:
                usage_data['count'] = 1
                usage_data['first_use'] = current_time
            else:
                usage_data['count'] += 1
                
                if usage_data['count'] > 5:
                    self.cooldown_users.add(user_id)
                    await asyncio.sleep(20)
                    self.cooldown_users.remove(user_id)
                    del self.command_usage[user_id]
                    return False
                    
        return True

    async def check_permissions(self, inter: disnake.ApplicationCommandInteraction) -> bool:
        if not await self.check_command_cooldown(inter):
            await inter.response.send_message(
                "⏳ Слишком много команд! Подождите 20 секунд.",
                ephemeral=True
            )
            return False
            
        required_permissions = [
            ("send_messages", "отправлять сообщения"),
            ("view_channel", "видеть канал"),
            ("connect", "подключаться к голосовым каналам"),
            ("speak", "говорить в голосовых каналах")
        ]
        
        missing_permissions = []
        
        if not inter.channel.permissions_for(inter.author).send_messages:
            missing_permissions.append("отправлять сообщения")
        if not inter.channel.permissions_for(inter.author).view_channel:
            missing_permissions.append("видеть канал")
            
        if inter.author.voice:
            if not inter.author.voice.channel.permissions_for(inter.author).connect:
                missing_permissions.append("подключаться к голосовым каналам")
            if not inter.author.voice.channel.permissions_for(inter.author).speak:
                missing_permissions.append("говорить в голосовых каналах")
        
        if missing_permissions:
            embed = disnake.Embed(
                title="❌ Недостаточно прав",
                description="Для использования этой команды вам необходимы следующие права:",
                color=0xFFA500
            )
            
            permissions_list = "\n".join([f"• {perm}" for perm in missing_permissions])
            embed.add_field(
                name="Отсутствующие права:",
                value=permissions_list,
                inline=False
            )
            
            embed.add_field(
                name="💡 Подсказка",
                value="Обратитесь к администратору сервера для получения необходимых прав.",
                inline=False
            )
            
            await inter.response.send_message(embed=embed, ephemeral=True)
            return False
            
        return True

    async def get_dominant_color(self, image_url: str) -> int:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(image_url) as response:
                    if response.status == 200:
                        image_data = await response.read()
                        image = Image.open(BytesIO(image_data))
                        image = image.resize((150, 150))
                        if image.mode != 'RGB':
                            image = image.convert('RGB')
                        colors = image.getcolors(150*150)
                        if not colors:
                            return 0x2b2d31
                        most_common = max(colors, key=lambda x: x[0])[1]
                        r, g, b = most_common
                        return (r << 16) + (g << 8) + b
        except Exception as e:
            logger.error(f"Error getting dominant color: {e}")
            return 0x2b2d31

    async def create_player_embed(self, player: MusicPlayer, track: mafic.Track) -> disnake.Embed:
        color = await self.get_dominant_color(track.artwork_url) if hasattr(track, 'artwork_url') and track.artwork_url else 0x2b2d31
        
        embed = disnake.Embed(
            title="🎵 Сейчас играет",
            description=f"```\n{track.title}\n```",
            color=color
        )
        
        if hasattr(track, 'artwork_url') and track.artwork_url:
            embed.set_thumbnail(url=track.artwork_url)
            
        if track.author:
            embed.add_field(
                name="👤 Исполнитель",
                value=f"```\n{track.author}\n```",
                inline=True
            )
            
        duration = time.strftime("%M:%S", time.gmtime(track.length / 1000))
        embed.add_field(
            name="⏱️ Длительность",
            value=f"```\n{duration}\n```",
            inline=True
        )
            
        embed.set_footer(
            text=f"Громкость: {player.volume}% | Нагрузка: {player.node.stats.cpu.system_load:.2f}% | Подключений: {player.node.stats.playing_player_count}"
        )
        
        return embed

    async def update_embed(self, player: MusicPlayer):
        while not player.paused and player.current_track:
            try:
                controls = MusicControls(self.bot, player.last_user_id)
                controls.update_buttons_state(player)
                await player.controller_message.edit(view=controls)
            except (disnake.NotFound, disnake.Forbidden):
                break
            await asyncio.sleep(10)

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info("Bot is ready, attempting to connect to Lavalink...")
        if not self.bot.mafic_ready:
            try:
                logger.info("Creating mafic node...")
                logger.info(f"Connection settings: host=localhost, port=7183, password=youshallnotpass")
                logger.info(f"Attempting to connect to Lavalink at: http://localhost:7183")
                
                self.bot.node = await self.bot.pool.create_node(
                    host='localhost',
                    port=7183,
                    label='main',
                    password='youshallnotpass',
                    secure=False
                )
                
                logger.info("Node created, waiting for connection...")
                await asyncio.sleep(15)
                
                if self.bot.node:
                    logger.info(f"Node status: connected={self.bot.node.is_connected()}")
                    logger.info(f"Node info: {self.bot.node.identifier}")
                    logger.info(f"Node stats: {self.bot.node.stats if hasattr(self.bot.node, 'stats') else 'No stats available'}")
                    
                    if self.bot.node.is_connected():
                        self.bot.mafic_ready = True
                        logger.info("Mafic node connected and ready!")
                        logger.info(f"WebSocket URL: ws://localhost:7183/v3/websocket")
                        logger.info(f"HTTP URL: http://localhost:7183/v3")
                    else:
                        logger.error("Node exists but not connected!")
                        logger.error(f"Connection status: {self.bot.node.is_connected()}")
                        self.bot.mafic_ready = False
                else:
                    logger.error("Node creation failed!")
                    logger.error("No node object created")
                    self.bot.mafic_ready = False
                    
            except Exception as e:
                logger.error(f"Failed to connect to Lavalink: {e}")
                logger.error(f"Error type: {type(e)}")
                logger.error(f"Connection attempt details:")
                logger.error(f"- Host: localhost")
                logger.error(f"- Port: 7183")
                logger.error(f"- Password: youshallnotpass")
                logger.error(f"- Secure: false")
                import traceback
                logger.error(f"Traceback: {traceback.format_exc()}")
                self.bot.mafic_ready = False

    @commands.Cog.listener()
    async def on_mafic_node_ready(self, node: mafic.Node):
        logger.info(f"Mafic node ready event received for node {node.identifier}")
        self.bot.mafic_ready = True
        self.bot.node = node
        logger.info("Mafic node is now ready!")

    @commands.Cog.listener()
    async def on_mafic_node_connect(self, node: mafic.Node):
        logger.info(f"Mafic node connected: {node.identifier}")

    @commands.Cog.listener()
    async def on_mafic_node_disconnect(self, node: mafic.Node):
        logger.info(f"Mafic node disconnected: {node.identifier}")
        self.bot.mafic_ready = False

    @commands.Cog.listener()
    async def on_mafic_track_end(self, player: MusicPlayer, track: mafic.Track, reason):
        logger.info(f"[TRACK_END] Трек закончился: {track.title} | Причина: {reason} | Режим повтора: {player.loop_mode} | Треков в очереди: {len(player.queue)}")
        try:
            await self.destroy(player)
        except Exception as e:
            logger.error(f"[TRACK_END] Ошибка при обработке окончания трека: {e}")

    @commands.Cog.listener()
    async def on_mafic_track_exception(self, player: MusicPlayer, track: mafic.Track, error: Exception):
        logger.error(f"[TRACK_ERROR] Ошибка трека: {track.title} | Ошибка: {error}")
        await self.destroy(player)

    @commands.Cog.listener()
    async def on_mafic_track_stuck(self, player: MusicPlayer, track: mafic.Track, threshold: int):
        logger.warning(f"[TRACK_STUCK] Трек застрял: {track.title} | Порог: {threshold}")
        await self.destroy(player)

    @commands.Cog.listener()
    async def on_mafic_player_update(self, player: MusicPlayer, data: dict):
        try:
            if player.current_track and player.position >= player.current_track.length:
                logger.info(f"[PLAYER_UPDATE] Трек достиг конца по обновлению: {player.current_track.title} | Позиция: {player.position} | Длина: {player.current_track.length}")
                await self.node.dispatch_event('track_end', player, player.current_track, 'finished')
        except Exception as e:
            logger.error(f"[PLAYER_UPDATE] Ошибка при обновлении плеера: {e}")

    @commands.slash_command(
        name="top",
        description="Показать самые популярные треки"
    )
    async def top(
        self,
        inter: disnake.ApplicationCommandInteraction
    ):
        if not await self.check_permissions(inter):
            return
            
        await inter.response.defer()
        
        tracks = await self.db.get_most_played_tracks(inter.guild.id, 10)
        
        if not tracks:
            return await inter.edit_original_response("История пуста")

        banner_file = await self.create_top_banner(tracks, inter.guild.name)
        
        if banner_file:
            await inter.edit_original_response(file=banner_file)
        else:
            await inter.edit_original_response("❌ Не удалось создать баннер с топ-треками")

    @commands.slash_command(
        name="play",
        description="Запустить трек или плейлист",
        dm_permission=False
    )
    async def play(
        self,
        inter: disnake.ApplicationCommandInteraction,
        query: str = commands.Param(
            name="запрос",
            description="название трека или ссылка"
        )
    ):
        if not await self.check_permissions(inter):
            return
            
        await inter.response.defer()
        
        if not inter.author.voice:
            return await self.send_temp_message(inter, "❌ Вы должны быть в голосовом канале!")
            
        try:
            if not inter.guild.voice_client:
                voice_client = await inter.author.voice.channel.connect(cls=MusicPlayer)
                voice_client.self_deaf = True
                player = voice_client
                player.db = self.db
                logger.info(f"[PLAY] Создан новый плеер для канала {inter.author.voice.channel.name} (self_deaf=True)")
            else:
                player = inter.guild.voice_client
                if not hasattr(player, 'db'):
                    player.db = self.db
                
            if inter.guild.voice_client and inter.author.voice.channel.id != inter.guild.voice_client.channel.id:
                return await self.send_temp_message(
                    inter,
                    f"❌ Я уже играю в канале {inter.guild.voice_client.channel.mention}!"
                )

            try:
                tracks = await player.fetch_tracks(query, mafic.SearchType.SOUNDCLOUD)
                if not tracks:
                    logger.warning(f"[PLAY] Трек не найден: {query}")
                    return await inter.edit_original_response("❌ Трек не найден")

                if isinstance(tracks, mafic.Playlist):
                    for track in tracks.tracks:
                        player.track_users[track.identifier] = {
                            'user_id': inter.author.id,
                            'username': inter.author.display_name
                        }
                    player.queue.extend(tracks.tracks)
                    logger.info(f"[PLAY] Добавлен плейлист: {tracks.name} | Треков: {len(tracks.tracks)}")
                    
                    if player.current_track:
                        msg = await inter.edit_original_response(content="✅ Плейлист добавлен в очередь")
                        await asyncio.sleep(30)
                        try:
                            await msg.delete()
                        except:
                            pass
                        if player.controller_message:
                            controls = MusicControls(self.bot, inter.author.id)
                            controls.update_buttons_state(player)
                            await player.controller_message.edit(view=controls)
                    else:
                        player.current_track = player.queue[0]
                        player.queue.pop(0)
                        logger.info(f"[PLAY] Начало воспроизведения плейлиста. Первый трек: {player.current_track.title}")
                        await player.play(player.current_track, inter.author.id, inter.author.display_name)
                        controls = MusicControls(self.bot, inter.author.id)
                        controls.update_buttons_state(player)
                        banner_file = await self.create_music_banner(player, player.current_track)
                        if banner_file:
                            player.controller_message = await inter.edit_original_response(
                                file=banner_file,
                                view=controls
                            )
                        else:
                            player.controller_message = await inter.edit_original_response(
                                content=f"▶️ Сейчас играет: **{player.current_track.title}**",
                                view=controls
                            )
                        asyncio.create_task(self.update_embed(player))
                else:
                    track = tracks[0]
                    player.track_users[track.identifier] = {
                        'user_id': inter.author.id,
                        'username': inter.author.display_name
                    }
                    player.queue.append(track)
                    logger.info(f"[PLAY] Добавлен трек в очередь: {track.title}")
                    
                    if player.current_track:
                        msg = await inter.edit_original_response(content="✅ Трек добавлен в очередь")
                        await asyncio.sleep(30)
                        try:
                            await msg.delete()
                        except:
                            pass
                        if player.controller_message:
                            controls = MusicControls(self.bot, inter.author.id)
                            controls.update_buttons_state(player)
                            await player.controller_message.edit(view=controls)
                    else:
                        player.current_track = track
                        player.queue.pop(0)
                        logger.info(f"[PLAY] Начало воспроизведения трека: {track.title}")
                        await player.play(track, inter.author.id, inter.author.display_name)
                        controls = MusicControls(self.bot, inter.author.id)
                        controls.update_buttons_state(player)
                        banner_file = await self.create_music_banner(player, track)
                        if banner_file:
                            player.controller_message = await inter.edit_original_response(
                                file=banner_file,
                                view=controls
                            )
                        else:
                            player.controller_message = await inter.edit_original_response(
                                content=f"▶️ Сейчас играет: **{track.title}**",
                                view=controls
                            )
                        asyncio.create_task(self.update_embed(player))
                        
            except mafic.errors.TrackLoadException as e:
                logger.error(f"[PLAY] Ошибка загрузки трека: {e}")
                return await inter.edit_original_response("❌ Не удалось загрузить трек")
                
        except mafic.NoNodesAvailable:
            logger.error("[PLAY] Нет доступных музыкальных серверов")
            return await inter.edit_original_response("❌ Нет доступных музыкальных серверов")
        except Exception as e:
            logger.error(f"[PLAY] Ошибка в команде play: {e}")
            logger.error(f"Error type: {type(e)}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            await inter.edit_original_response("❌ Произошла ошибка при воспроизведении")

    @commands.slash_command(
        name="stop",
        description="Остановить воспроизведение и отключиться"
    )
    async def stop(self, inter: disnake.ApplicationCommandInteraction):
        if not await self.check_permissions(inter):
            return
            
        if not inter.guild.voice_client:
            return await self.send_temp_message(inter, "❌ Бот не в голосовом канале!")
            
        player = inter.guild.voice_client
        await player.stop()
        await player.disconnect()
        await self.send_temp_message(inter, "⏹️ Остановлено и отключено")

    @commands.slash_command(
        name="mix",
        description="Создать персональный микс дня"
    )
    async def mix(
        self,
        inter: disnake.ApplicationCommandInteraction
    ):
        if not await self.check_permissions(inter):
            return
            
        await inter.response.defer()
        
        existing_mix = await self.db.get_daily_mix(inter.author.id, inter.guild.id)
        
        if existing_mix:
            tracks, created_at = existing_mix
            created_time = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
            time_diff = datetime.now() - created_time
            
            if time_diff.total_seconds() < 86400:
                hours_left = int((86400 - time_diff.total_seconds()) // 3600)
                minutes_left = int(((86400 - time_diff.total_seconds()) % 3600) // 60)
                
                return await inter.edit_original_response(
                    f"⏳ Ваш микс дня уже создан! Подождите еще {hours_left}ч {minutes_left}м для создания нового микса."
                )
        
        tracks = await self.db.get_user_unique_tracks(inter.author.id, inter.guild.id)
        
        if not tracks:
            return await inter.edit_original_response("❌ У вас пока нет прослушанных треков для создания микса")
        
        random.shuffle(tracks)
        
        await self.db.save_daily_mix(inter.author.id, inter.guild.id, tracks)
        
        playlist_tracks = []
        for track_title, track_author in tracks:
            try:
                search_query = f"{track_title} {track_author}"
                track = await self.bot.node.fetch_tracks(search_query, search_type="scsearch")
                if track:
                    playlist_tracks.append(track[0])
            except Exception as e:
                logger.error(f"Error fetching track for mix: {e}")
                continue
        
        if not playlist_tracks:
            return await inter.edit_original_response("❌ Не удалось создать микс")
        
        if not inter.author.voice:
            return await inter.edit_original_response("❌ Вы должны быть в голосовом канале!")
            
        try:
            if not inter.guild.voice_client:
                voice_client = await inter.author.voice.channel.connect(cls=MusicPlayer)
                voice_client.self_deaf = True
                player = voice_client
                player.db = self.db
            else:
                player = inter.guild.voice_client
                if not hasattr(player, 'db'):
                    player.db = self.db
                
            if inter.guild.voice_client and inter.author.voice.channel.id != inter.guild.voice_client.channel.id:
                return await inter.edit_original_response(
                    f"❌ Я уже играю в канале {inter.guild.voice_client.channel.mention}!"
                )
            
            player.queue.clear()
            
            player.queue.extend(playlist_tracks)
            
            if player.current_track:
                await inter.edit_original_response("✅ Микс дня добавлен в очередь")
            else:
                player.current_track = player.queue[0]
                player.queue.pop(0)
                await player.play(player.current_track, inter.author.id, inter.author.display_name)
                
                controls = MusicControls(self.bot, inter.author.id)
                controls.update_buttons_state(player)
                banner_file = await self.create_music_banner(player, player.current_track)
                
                if banner_file:
                    player.controller_message = await inter.edit_original_response(
                        file=banner_file,
                        view=controls
                    )
                else:
                    player.controller_message = await inter.edit_original_response(
                        content=f"▶️ Сейчас играет: **{player.current_track.title}**",
                        view=controls
                    )
                asyncio.create_task(self.update_embed(player))
                
        except Exception as e:
            logger.error(f"Error in mix command: {e}")
            return await inter.edit_original_response("❌ Произошла ошибка при создании микса")

    @commands.slash_command(
        name="247",
        description="Включить/выключить режим 24/7"
    )
    async def mode_247(
        self,
        inter: disnake.ApplicationCommandInteraction
    ):
        if not await self.check_permissions(inter):
            return
            
        if not inter.guild.voice_client:
            return await self.send_temp_message(inter, "❌ Бот не в голосовом канале!")
            
        player = inter.guild.voice_client
        player.is_247 = not player.is_247
        
        status = "включен" if player.is_247 else "выключен"
        await self.send_temp_message(inter, f"✅ Режим 24/7 {status}")
        
        if player.is_247:
            logger.info(f"[24/7] Режим 24/7 включен в канале {player.channel.name}")
        else:
            logger.info(f"[24/7] Режим 24/7 выключен в канале {player.channel.name}")
            if not player.queue and not player.current_track:
                await player.disconnect()
                await self.send_temp_message(inter, "👋 Бот отключен от голосового канала")

    @commands.slash_command(
        name="shuffle",
        description="Перемешать очередь (минимум 3 трека)"
    )
    async def shuffle(
        self,
        inter: disnake.ApplicationCommandInteraction
    ):
        if not await self.check_permissions(inter):
            return
            
        if not inter.guild.voice_client:
            return await self.send_temp_message(inter, "❌ Бот не в голосовом канале!")
            
        player = inter.guild.voice_client
        
        if len(player.queue) < 3:
            return await self.send_temp_message(
                inter,
                "❌ Для перемешивания нужно минимум 3 трека в очереди!"
            )
            
        current_track = player.current_track
        
        random.shuffle(player.queue)
        
        if player.loop_mode == 'track':
            player.loop_mode = None
            
        await inter.response.send_message(
            f"🔀 Очередь перемешана! Треков в очереди: {len(player.queue)}",
            ephemeral=True
        )
        
        if player.controller_message:
            try:
                controls = MusicControls(self.bot, player.last_user_id)
                controls.update_buttons_state(player)
                await player.controller_message.edit(view=controls)
            except Exception as e:
                logger.error(f"[SHUFFLE] Ошибка при обновлении кнопок: {e}")

    @commands.slash_command(
        name="help",
        description="Показать информацию о боте и командах"
    )
    async def help(
        self,
        inter: disnake.ApplicationCommandInteraction
    ):
        if not await self.check_permissions(inter):
            return

        main_embed = disnake.Embed(
            title="🎵 Music Bot",
            description="Бот для воспроизведения музыки с SoundCloud\n\n"
                       "**Основные возможности:**\n"
                       "• Воспроизведение треков\n"
                       "• Создание персональных миксов\n"
                       "• Топ популярных треков\n"
                       "• Управление воспроизведением\n"
                       "• Режим 24/7\n\n"
                       "**Выберите команду в меню ниже для подробной информации**",
            color=0xFFA500
        )
        
        main_embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        
        select = disnake.ui.Select(
            placeholder="Выберите команду",
            options=[
                disnake.SelectOption(
                    label="Play",
                    description="Воспроизвести трек или плейлист",
                    value="play",
                    emoji="<:helpplay:1378687588394860654>"
                ),
                disnake.SelectOption(
                    label="Mix",
                    description="Создать персональный микс дня",
                    value="mix",
                    emoji="<:helpmix:1378687586171617361>"
                ),
                disnake.SelectOption(
                    label="Top",
                    description="Показать популярные треки",
                    value="top",
                    emoji="<:helptop:1378687593708916887>"
                ),
                disnake.SelectOption(
                    label="Shuffle",
                    description="Перемешать очередь",
                    value="shuffle",
                    emoji="<:helpshuffle:1378687589653024799>"
                ),
                disnake.SelectOption(
                    label="24/7",
                    description="Включить/выключить режим 24/7",
                    value="247",
                    emoji="<:help247:1378687591020363856>"
                ),
                disnake.SelectOption(
                    label="Stop",
                    description="Остановить воспроизведение",
                    value="stop",
                    emoji="<:helpstop:1378687592266072165>"
                )
            ]
        )

        command_descriptions = {
            "play": {
                "title": "<:helpplay:1378687588394860654> Команда Play",
                "description": "**Воспроизводит трек или плейлист с SoundCloud**\n\n"
                             "**Использование:**\n"
                             "`/play <запрос>`\n\n"
                             "**Примеры:**\n"
                             "• `/play Never Gonna Give You Up`\n"
                             "• `/play https://soundcloud.com/...`\n\n"
                             "**Особенности:**\n"
                             "• Поддерживает поиск по названию\n"
                             "• Поддерживает прямые ссылки\n"
                             "• Автодополнение при поиске"
            },
            "mix": {
                "title": "<:helpmix:1378687586171617361> Команда Mix",
                "description": "**Создает персональный микс дня из ваших треков**\n\n"
                             "**Использование:**\n"
                             "`/mix`\n\n"
                             "**Особенности:**\n"
                             "• Создается раз в 24 часа\n"
                             "• Использует вашу историю прослушиваний\n"
                             "• Автоматически перемешивает треки"
            },
            "top": {
                "title": "<:helptop:1378687593708916887> Команда Top",
                "description": "**Показывает самые популярные треки на сервере**\n\n"
                             "**Использование:**\n"
                             "`/top`\n\n"
                             "**Особенности:**\n"
                             "• Показывает топ-10 треков\n"
                             "• Учитывает количество прослушиваний\n"
                             "• Красивый баннер с обложками"
            },
            "shuffle": {
                "title": "<:helpshuffle:1378687589653024799> Команда Shuffle",
                "description": "**Перемешивает очередь воспроизведения**\n\n"
                             "**Использование:**\n"
                             "`/shuffle`\n\n"
                             "**Требования:**\n"
                             "• Минимум 3 трека в очереди\n\n"
                             "**Особенности:**\n"
                             "• Случайное перемешивание\n"
                             "• Сохраняет текущий трек"
            },
            "247": {
                "title": "<:help247:1378687591020363856> Команда 24/7",
                "description": "**Включает/выключает режим 24/7**\n\n"
                             "**Использование:**\n"
                             "`/247`\n\n"
                             "**Особенности:**\n"
                             "• Бот не отключается при пустой очереди\n"
                             "• Работает даже когда все вышли из канала\n"
                             "• Можно включить/выключить в любой момент"
            },
            "stop": {
                "title": "<:helpstop:1378687592266072165> Команда Stop",
                "description": "**Останавливает воспроизведение и отключает бота**\n\n"
                             "**Использование:**\n"
                             "`/stop`\n\n"
                             "**Действия:**\n"
                             "• Останавливает текущий трек\n"
                             "• Очищает очередь\n"
                             "• Отключает бота от канала"
            }
        }

        async def select_callback(interaction: disnake.MessageInteraction):
            command = interaction.data["values"][0]
            desc = command_descriptions[command]
            
            embed = disnake.Embed(
                title=desc["title"],
                description=desc["description"],
                color=0xFFA500
            )
            
            await interaction.response.edit_message(embed=embed, view=view)

        select.callback = select_callback
        
        view = disnake.ui.View()
        view.add_item(select)
        
        await inter.response.send_message(embed=main_embed, view=view)

    async def create_top_banner(self, tracks: list, guild_name: str) -> disnake.File:
        try:
            width, height = 800, 400
            
            banner = Image.new('RGB', (width, height), (30, 30, 30))
            draw = ImageDraw.Draw(banner)
            
            accent_color = (255, 165, 0)  
            
            if tracks and len(tracks) > 0:
                try:
                    async with aiohttp.ClientSession() as session:
                        track_title = tracks[0][0]  
                        search_url = f"https://api-v2.soundcloud.com/search/tracks?q={track_title}&client_id=YOUR_CLIENT_ID"
                        async with session.get(search_url) as response:
                            if response.status == 200:
                                data = await response.json()
                                if data and len(data) > 0:
                                    artwork_url = data[0].get('artwork_url')
                                    if artwork_url:
                                        artwork_url = artwork_url.replace('large', 't500x500')
                                        async with session.get(artwork_url) as img_response:
                                            if img_response.status == 200:
                                                image_data = await img_response.read()
                                                album_art = Image.open(BytesIO(image_data))
                                                
                                                bg_image = album_art.resize((width, height))
                                                bg_image = bg_image.filter(ImageFilter.GaussianBlur(radius=20))
                                                overlay = Image.new('RGBA', (width, height), (0, 0, 0, 200))
                                                bg_image = Image.alpha_composite(bg_image.convert('RGBA'), overlay).convert('RGB')
                                                banner.paste(bg_image)
                                                
                                                primary_color, _ = await self.get_dominant_colors(artwork_url)
                except Exception as e:
                    logger.error(f"Error loading background image: {e}")
            
            overlay = Image.new('RGBA', (width, height), (0, 0, 0, 50))
            banner = Image.alpha_composite(banner.convert('RGBA'), overlay).convert('RGB')
            draw = ImageDraw.Draw(banner)
            
            title_font = self.get_font_path("arial.ttf", 28)
            track_font = self.get_font_path("arial.ttf", 18)
            info_font = self.get_font_path("arial.ttf", 14)
            
            title = f"Самые популярные треки на {guild_name}"
            draw.text((width//2, 20), title, font=title_font, fill=(255, 255, 255), anchor="mm")
            
            draw.line([(50, 60), (width - 50, 60)], fill=accent_color, width=2)
            
            current_y = 90
            tracks_per_column = 5
            column_width = width // 2
            
            for i, (track_title, track_author, play_count) in enumerate(tracks[:10], 1):
                column = 0 if i <= tracks_per_column else 1
                x_offset = 50 + (column * column_width)
                
                draw.text((x_offset, current_y), f"#{i}", font=track_font, fill=accent_color)
                
                track_title = track_title if len(track_title) <= 25 else track_title[:22] + "..."
                draw.text((x_offset + 30, current_y), track_title, font=track_font, fill=(255, 255, 255))
                
                info_text = f"👤 {track_author} • ▶️ {play_count}"
                draw.text((x_offset + 30, current_y + 25), info_text, font=info_font, fill=(200, 200, 200))
                
                if i == tracks_per_column:
                    current_y = 90
                else:
                    current_y += 50
            
            temp_file = BytesIO()
            banner.save(temp_file, format='PNG')
            temp_file.seek(0)
            
            return disnake.File(temp_file, filename='top_tracks.png')
            
        except Exception as e:
            logger.error(f"Ошибка при создании баннера топ-треков: {e}")
            return None

    def get_font_path(self, font_name: str, size: int):
        try:
            from PIL import ImageFont
            try:
                return ImageFont.truetype(font_name, size)
            except:
                try:
                    return ImageFont.truetype("DejaVuSans.ttf", size)
                except:
                    return ImageFont.load_default()
        except Exception as e:
            logger.error(f"Error loading font: {e}")
            return ImageFont.load_default()

    async def get_dominant_colors(self, image_url: str) -> tuple:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(image_url) as response:
                    if response.status == 200:
                        image_data = await response.read()
                        image = Image.open(BytesIO(image_data))
                        image = image.resize((150, 150))
                        if image.mode != 'RGB':
                            image = image.convert('RGB')
                        colors = image.getcolors(150*150)
                        if not colors:
                            return (43, 45, 49), (100, 100, 100)
                        most_common = max(colors, key=lambda x: x[0])[1]
                        r, g, b = most_common
                        accent_r = min(255, r + 50)
                        accent_g = min(255, g + 50)
                        accent_b = min(255, b + 50)
                        return (r, g, b), (accent_r, accent_g, accent_b)
        except Exception as e:
            logger.error(f"Error getting dominant colors: {e}")
            return (43, 45, 49), (100, 100, 100)

    async def create_music_banner(self, player: 'MusicPlayer', track: mafic.Track) -> disnake.File:
        try:
            width, height = 800, 300
            
            banner = Image.new('RGB', (width, height), (30, 30, 30))
            draw = ImageDraw.Draw(banner)
            
            album_art = None
            primary_color = (43, 45, 49)
            accent_color = (100, 100, 100)
            
            if hasattr(track, 'artwork_url') and track.artwork_url:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(track.artwork_url) as response:
                            if response.status == 200:
                                image_data = await response.read()
                                album_art = Image.open(BytesIO(image_data))
                                album_art = album_art.resize((250, 250))
                                
                                primary_color, accent_color = await self.get_dominant_colors(track.artwork_url)
                except Exception as e:
                    logger.error(f"Error loading album art: {e}")
            
            if album_art:
                bg_image = album_art.resize((width, height))
                bg_image = bg_image.filter(ImageFilter.GaussianBlur(radius=20))
                overlay = Image.new('RGBA', (width, height), (0, 0, 0, 200))
                bg_image = Image.alpha_composite(bg_image.convert('RGBA'), overlay).convert('RGB')
                banner.paste(bg_image)
            else:
                for x in range(width):
                    ratio = x / width
                    r = int(primary_color[0] * (1 - ratio) + accent_color[0] * ratio * 0.3)
                    g = int(primary_color[1] * (1 - ratio) + accent_color[1] * ratio * 0.3)
                    b = int(primary_color[2] * (1 - ratio) + accent_color[2] * ratio * 0.3)
                    
                    for y in range(height):
                        y_ratio = y / height
                        final_r = int(r * (1 - y_ratio * 0.2))
                        final_g = int(g * (1 - y_ratio * 0.2))
                        final_b = int(b * (1 - y_ratio * 0.2))
                        draw.point((x, y), (final_r, final_g, final_b))
            
            overlay = Image.new('RGBA', (width, height), (0, 0, 0, 50))
            banner = Image.alpha_composite(banner.convert('RGBA'), overlay).convert('RGB')
            draw = ImageDraw.Draw(banner)
            
            if album_art:
                shadow_offset = 8
                shadow = Image.new('RGBA', (250 + shadow_offset, 250 + shadow_offset), (0, 0, 0, 80))
                banner.paste(shadow, (25 + shadow_offset, 25 + shadow_offset), shadow)
                
                border = Image.new('RGB', (254, 254), accent_color)
                banner.paste(border, (23, 23))
                banner.paste(album_art, (25, 25))
            else:
                placeholder_color = accent_color
                draw.rectangle([25, 25, 275, 275], fill=placeholder_color, outline=(255, 255, 255), width=2)
                
                note_size = 100
                note_x = 150 - note_size // 2
                note_y = 150 - note_size // 2
                draw.ellipse([note_x, note_y + 60, note_x + 40, note_y + 100], fill=(255, 255, 255))
                draw.rectangle([note_x + 35, note_y, note_x + 40, note_y + 70], fill=(255, 255, 255))
                draw.ellipse([note_x + 30, note_y - 10, note_x + 50, note_y + 10], fill=(255, 255, 255))
            
            title_font = self.get_font_path("DejaVuSans.ttf", 32)
            artist_font = self.get_font_path("DejaVuSans.ttf", 24)
            info_font = self.get_font_path("DejaVuSans.ttf", 18)
            label_font = self.get_font_path("DejaVuSans.ttf", 14)
            
            text_x = 300
            current_y = 40
            
            draw.text((text_x, current_y), "♪ СЕЙЧАС ИГРАЕТ", font=label_font, fill=accent_color)
            current_y += 35
            
            track_title = track.title if len(track.title) <= 35 else track.title[:32] + "..."
            draw.text((text_x, current_y), track_title, font=title_font, fill=(255, 255, 255))
            current_y += 45
            
            if track.author:
                artist_name = track.author if len(track.author) <= 40 else track.author[:37] + "..."
                draw.text((text_x, current_y), f"Исполнитель: {artist_name}", font=artist_font, fill=(200, 200, 200))
                current_y += 40
            
            duration = time.strftime("%M:%S", time.gmtime(track.length / 1000))
            draw.text((text_x, current_y), f"Длительность: {duration}", font=info_font, fill=(180, 180, 180))
            current_y += 30
            
            if player.last_username:
                draw.text((text_x, current_y), f"Добавил: {player.last_username}", font=info_font, fill=(180, 180, 180))
            
            temp_file = BytesIO()
            banner.save(temp_file, format='PNG')
            temp_file.seek(0)
            
            return disnake.File(temp_file, filename='music_banner.png')
            
        except Exception as e:
            logger.error(f"Ошибка при создании баннера: {e}")
            return None

def setup(bot: commands.InteractionBot):
    bot.add_cog(Music(bot))