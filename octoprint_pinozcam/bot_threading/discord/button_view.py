import discord

class ButtonView(discord.ui.View):
    def __init__(self):
        super().__init__()

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.success)
    async def button0_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Resuming print...")

    @discord.ui.button(label="⏸️", style=discord.ButtonStyle.secondary)
    async def button1_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Pausing print...")

    @discord.ui.button(label="⏹️", style=discord.ButtonStyle.danger)
    async def button2_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Stopping print...")