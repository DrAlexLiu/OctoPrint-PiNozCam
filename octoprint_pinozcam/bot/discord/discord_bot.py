# discord_bot.py

import discord
import discord.ext
from .button_view import ButtonView

def run_bot(token,channel_id, pinozcam_instance):
    intents = discord.Intents.default()
    intents.guilds = True
    bot = discord.Client(intents=intents)
    tree = discord.app_commands.CommandTree(bot)

    @bot.event
    async def on_ready():
        print(f'Logged in as {bot.user}')
        
        try:
            await tree.sync()
        except Exception as e:
            print(f"Failed to sync commands: {e}")
        
        channel = bot.get_channel(channel_id)
        if channel is not None:
            await channel.send('Failure detection bot is online!')
        else:
            print("Invalid channel ID or bot doesn't have access to the channel.")
            
    @tree.command(name="status", description="Check the status of the printer.")
    async def status(interaction: discord.Interaction):
        try:
            title, state, progress, nozzle_temp, bed_temp, file_metadata = pinozcam_instance.get_printer_status()
            
            embed = discord.Embed(title="Printer Status", color=discord.Color.blue())
            embed.add_field(name="Printer", value=title, inline=False)
            embed.add_field(name="Status", value=state, inline=False)
            embed.add_field(name="Progress", value=progress, inline=False)
            embed.add_field(name="Nozzle Temp", value=f"{nozzle_temp}°C", inline=True)
            embed.add_field(name="Bed Temp", value=f"{bed_temp}°C", inline=True)
            if file_metadata:
                embed.add_field(name="File", value=file_metadata.get('name', 'Unknown'), inline=False)
            
            view = ButtonView(pinozcam_instance)
            await interaction.response.send_message(embed=embed, view=view)
        except Exception as e:
            error_message = f"An error occurred while retrieving printer status: {str(e)}"
            await interaction.response.send_message(error_message)

    bot.run(token)