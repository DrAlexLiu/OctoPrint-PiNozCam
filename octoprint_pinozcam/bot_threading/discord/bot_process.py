from multiprocessing import Process, Queue
from .discord_bot import run_bot
from .flask_app import run_flask

def run_bot_process(token, message_queue):
    bot_process = Process(target=run_bot, args=(token, message_queue))
    bot_process.start()
    return bot_process

def run_flask_process(message_queue):
    flask_process = Process(target=run_flask, args=(message_queue,))
    flask_process.start()
    return flask_process