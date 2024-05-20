import discord
from discord.ext import commands
import asyncio
from .button_view import ButtonView

intents = discord.Intents.default()
intents.guilds = True  # Enable the Guilds intent

def create_bot(message_queue):
    bot = commands.Bot(command_prefix='!', intents=intents)

    @bot.event
    async def on_ready():
        print(f'Logged in as {bot.user}')
        bot.loop.create_task(process_messages(message_queue))
        channel = bot.get_channel(1214726573635276830)
        if channel is not None:
            await channel.send('Failure detection bot is online!')
        else:
            print("Invalid channel ID or bot doesn't have access to the channel.")

    async def process_messages(message_queue):
        while True:
            if not message_queue.empty():
                message = message_queue.get()
                await send_discord_message(message, bot)
            await asyncio.sleep(1)  # Wait for 1 second before checking again

    async def send_discord_message(message, bot):
        channel = bot.get_channel(1214726573635276830)  # Make sure this is a valid channel ID
        if channel is not None:
            view = ButtonView()
            await channel.send(message, view=view)
        else:
            print("Invalid channel ID or bot doesn't have access to the channel.")

    return bot

def run_bot(token, message_queue):
    bot = create_bot(message_queue)
    bot.run(token)