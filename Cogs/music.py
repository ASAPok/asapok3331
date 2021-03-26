import discord
from discord.ext import commands

import asyncio
import itertools
import sys
import traceback
from async_timeout import timeout
from functools import partial
from youtube_dl import YoutubeDL


ytdlopts = {
    'format': 'bestaudio/best',
    'outtmpl': 'downloads/%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0'  # ipv6 addresses cause issues sometimes
}

ffmpegopts = {
    'before_options': '-nostdin',
    'options': '-vn'
}

ytdl = YoutubeDL(ytdlopts)


class VoiceConnectionError(commands.CommandError):
    """Пользовательский класс исключений для ошибок подключения."""


class InvalidVoiceChannel(VoiceConnectionError):
    """Исключение для случаев недействительных Голосовых каналов."""


class YTDLSource(discord.PCMVolumeTransformer):

    def __init__(self, source, *, data, requester):
        super().__init__(source)
        self.requester = requester

        self.title = data.get('title')
        self.web_url = data.get('webpage_url')

        # YTDL info dicts (data) have other useful information you might want
        # https://github.com/rg3/youtube-dl/blob/master/README.md

    def __getitem__(self, item: str):
        """Позволяет нам получить доступ к атрибутам, подобным dict.
        Это полезно только тогда, когда вы НЕ загружаете.
        """
        return self.__getattribute__(item)

    @classmethod
    async def create_source(cls, ctx, search: str, *, loop, download=False):
        loop = loop or asyncio.get_event_loop()

        to_run = partial(ytdl.extract_info, url=search, download=download)
        data = await loop.run_in_executor(None, to_run)

        if 'entries' in data:
            # take first item from a playlist
            data = data['entries'][0]

        await ctx.send(f'~~Добавлено {data["title"]} в очередь.~~')

        if download:
            source = ytdl.prepare_filename(data)
        else:
            return {'webpage_url': data['webpage_url'], 'requester': ctx.author, 'title': data['title']}

        return cls(discord.FFmpegPCMAudio(source), data=data, requester=ctx.author)

    @classmethod
    async def regather_stream(cls, data, *, loop):
        """Используется для подготовки потока, а не для загрузки.
        Так как срок действия потоковых ссылок Youtube истекает."""
        loop = loop or asyncio.get_event_loop()
        requester = data['requester']

        to_run = partial(ytdl.extract_info, url=data['webpage_url'], download=False)
        data = await loop.run_in_executor(None, to_run)

        return cls(discord.FFmpegPCMAudio(data['url']), data=data, requester=requester)


class MusicPlayer(commands.Cog):
    """Класс, который присваивается каждой гильдии с помощью бота для музыки.
    Этот класс реализует очередь и цикл, что позволяет разным гильдиям прослушивать разные плейлисты
    одновременно.
    Когда бот отключится от Голоса его экземпляр будет уничтожен.
    """

    __slots__ = ('bot', '_guild', '_channel', '_cog', 'queue', 'next', 'current', 'np', 'volume')

    def __init__(self, ctx):
        self.bot = ctx.bot
        self._guild = ctx.guild
        self._channel = ctx.channel
        self._cog = ctx.cog

        self.queue = asyncio.Queue()
        self.next = asyncio.Event()

        self.np = None  # Now playing message
        self.volume = .5
        self.current = None

        ctx.bot.loop.create_task(self.player_loop())

    async def player_loop(self):
        """Наш главный игрок петля."""
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            self.next.clear()

            try:
                # Wait for the next song. If we timeout cancel the player and disconnect...
                async with timeout(300):  # 5 minutes...
                    source = await self.queue.get()
            except asyncio.TimeoutError:
                return self.destroy(self._guild)

            if not isinstance(source, YTDLSource):
                # Source was probably a stream (not downloaded)
                # So we should regather to prevent stream expiration
                try:
                    source = await YTDLSource.regather_stream(source, loop=self.bot.loop)
                except Exception as e:
                    await self._channel.send(f'<:error:822149922400632893>Произошла ошибка при обработке вашей песни.\n'
                                             f'```css\n[{e}]\n```')
                    continue

            source.volume = self.volume
            self.current = source

            self._guild.voice_client.play(source, after=lambda _: self.bot.loop.call_soon_threadsafe(self.next.set))
            self.np = await self._channel.send(f'<a:laser:821099778049703976>**Теперь Играем:** `{source.title}` по запросу '
                                               f'`{source.requester}`')
            await self.next.wait()

            # Make sure the FFmpeg process is cleaned up.
            source.cleanup()
            self.current = None

            try:
                # We are no longer playing this song...
                await self.np.delete()
            except discord.HTTPException:
                pass

    def destroy(self, guild):
        """Вышел или очистил пользователь с голосового канала."""
        return self.bot.loop.create_task(self._cog.cleanup(guild))


class Music(commands.Cog):
    """Команды, связанные с музыкой."""

    __slots__ = ('bot', 'players')

    def __init__(self, bot):
        self.bot = bot
        self.players = {}

    async def cleanup(self, guild):
        try:
            await guild.voice_client.disconnect()
        except AttributeError:
            pass

        try:
            del self.players[guild.id]
        except KeyError:
            pass

    async def __local_check(self, ctx):
        """Локальная проверка, которая применяется ко всем командам в этом винтике."""
        if not ctx.guild:
            raise commands.NoPrivateMessage
        return True

    async def __error(self, ctx, error):
        """Локальный обработчик ошибок для всех ошибок, возникающих из команд в этом винтике."""
        if isinstance(error, commands.NoPrivateMessage):
            try:
                return await ctx.send('Эта команда не может быть использована в Личных сообщениях.')
            except discord.HTTPException:
                pass
        elif isinstance(error, InvalidVoiceChannel):
            await ctx.send('Ошибка подключения к Голосовому каналу. '
                           'Пожалуйста, убедитесь, что вы находитесь в действительном канале или предоставьте мне его')

        print('Игнорирование исключения в команде {}:'.format(ctx.command), file=sys.stderr)
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)

    def get_player(self, ctx):
        """Получить игрока гильдии или создать его."""
        try:
            player = self.players[ctx.guild.id]
        except KeyError:
            player = MusicPlayer(ctx)
            self.players[ctx.guild.id] = player

        return player

    @commands.command(name='connect', aliases=['присоединиться'])
    async def connect_(self, ctx):
        """Зайдите и напишите и бот зайдёт к вам если не занят."""
        vc = ctx.voice_client

        try:
            channel = ctx.author.voice.channel
        except AttributeError:
            raise InvalidVoiceChannel('Нет канала, чтобы присоединиться :No:.')

        vc = ctx.voice_client

        if vc:
            if vc.channel.id == channel.id:
                return
            try:
                await vc.move_to(channel)
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f'Переход к каналу: <{channel}> тайм-аут⌚📤.')
        else:
            try:
                await channel.connect()
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f'Подключение к каналу: <{channel}> тайм-аут⌚📤.')

        await ctx.send(f'<a:Yes:822095182375157850> Подключено к: **{channel}**', )

    @commands.command(name='p', aliases=['sing', 'play'])
    async def play_(self, ctx, *, search: str):
        """Напишите название песни(ищет в ютубе) или сылку (можно радио)."""
        vc = ctx.voice_client

        await ctx.trigger_typing()

        vc = ctx.voice_client

        if not vc:
            await ctx.invoke(self.connect_)

        player = self.get_player(ctx)

        # If download is False, source will be a dict which will be used later to regather the stream.
        # If download is True, source will be a discord.FFmpegPCMAudio with a VolumeTransformer.
        source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop, download=False)

        await player.queue.put(source)
   
    @commands.command(name='pause')
    async def pause_(self, ctx):
        """Приостановите воспроизведение текущей песни."""
        vc = ctx.voice_client

        if not vc or not vc.is_playing():
            return await ctx.send('<a:No:822096710170968084> В настоящее время я ничего не играю!')
        elif vc.is_paused():
            return

        vc.pause()
        await ctx.send(f'<a:Yes:822095182375157850> **`{ctx.author}`**: Песня остановилась!')

    @commands.command(name='resume')
    async def resume_(self, ctx):
        """Возобновите приостановленную в данный момент песню."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('<a:No:822096710170968084> В настоящее время я ничего не играю!', )
        elif not vc.is_paused():
            return

        vc.resume()
        await ctx.send(f'<a:Yes:822095182375157850> **`{ctx.author}`**: Возобновилась песня!')

    @commands.command(name='skip', aliases=['s'])
    async def skip_(self, ctx):
        """Пропустить песню."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('<a:No:822096710170968084> В данный момент я ничего не играю!')

        if vc.is_paused():
            pass
        elif not vc.is_playing():
            return

        vc.stop()
        await ctx.send(f'<a:Yes:822095182375157850>**`{ctx.author}`**: Пропустил песню!')

    @commands.command(name='queue', aliases=['q', 'playlist'])
    async def queue_info(self, ctx):
        """Извлеките основную очередь предстоящих песен."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('<a:No:822096710170968084>В настоящее время я не подключен к голосовому каналу!')

        player = self.get_player(ctx)
        if player.queue.empty():
            return await ctx.send('<:error:822149922400632893>В настоящее время в очереди больше нет песен.')

        # Grab up to 5 entries from the queue...
        upcoming = list(itertools.islice(player.queue._queue, 0, 5))

        fmt = '\n'.join(f'**`{_["title"]}`**' for _ in upcoming)
        embed = discord.Embed(title=f'Предстоящий - Следующий {len(upcoming)}', description=fmt)

        await ctx.send(embed=embed)

    @commands.command(name='now-playing', aliases=['np', 'current', 'currentsong', 'playing'])
    async def now_playing_(self, ctx):
        """Отображение информации о текущей воспроизводимой песне."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('<a:No:822096710170968084> В настоящее время я не играю музыку!', )

        player = self.get_player(ctx)
        if not player.current:
            return await ctx.send('<a:No:822096710170968084>В данный момент я ничего не играю!')

        try:
            # Remove our previous now_playing message.
            await player.np.delete()
        except discord.HTTPException:
            pass

        player.np = await ctx.send(f'<a:Yes:822095182375157850>**Играет:** `{vc.source.title}` '
                                   f'по запросу `{vc.source.requester}`')

    @commands.command(name='volume', aliases=['vol'])
    async def change_volume(self, ctx, *, vol: float):
        """Измените громкость плеера.
        Параметры
        ------------
        объем: float или int [Требуется]
            Громкость, устанавливаемая игроком в процентах. Это должно быть от 1 до 100.
        """
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('<a:No:822096710170968084>В настоящее время я не подключен к голосовому каналу!', )

        if not 0 < vol < 101:
            return await ctx.send('<a:drink:821099748505288704>Пожалуйста, введите значение от 1 до 100.')

        player = self.get_player(ctx)

        if vc.source:
            vc.source.volume = vol / 100

        player.volume = vol / 100
        await ctx.send(f'**`{ctx.author}`**: установлена громкость **{vol}%**')

    @commands.command(name='stop', aliases=['leave'])
    async def stop_(self, ctx):
        """Остановите проигрываемую в данный момент песню и уничтожьте плеер.
        <a:No:822096710170968084>!Warning!
            Это уничтожит игрока, назначенного вашей гильдией, а также удалит все песни и настройки в очереди.
        """
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('<a:No:822096710170968084>В данный момент я не в голосовом канале!')

        await self.cleanup(ctx.guild)


def setup(client):
    client.add_cog(Music(client))

