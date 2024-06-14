# button_view.py
import discord

class ButtonView(discord.ui.View):
    def __init__(self, pinozcam_instance):
        super().__init__()
        self.pinozcam_instance = pinozcam_instance

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.success)
    async def button0_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer()
            self.pinozcam_instance._printer.resume_print()
            await interaction.followup.send("Print resumed.")
        except Exception as e:
            error_message = f"An error occurred while resuming the print: {str(e)}"
            await interaction.followup.send(error_message)

    @discord.ui.button(label="⏸️", style=discord.ButtonStyle.secondary)
    async def button1_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer()
            self.pinozcam_instance._printer.pause_print()
            await interaction.followup.send("Print paused.")
        except Exception as e:
            error_message = f"An error occurred while pausing the print: {str(e)}"
            await interaction.followup.send(error_message)

    @discord.ui.button(label="⏹️", style=discord.ButtonStyle.danger)
    async def button2_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer()
            self.pinozcam_instance._printer.cancel_print()
            await interaction.followup.send("Print stopped.")
        except Exception as e:
            error_message = f"An error occurred while stopping the print: {str(e)}"
            await interaction.followup.send(error_message)