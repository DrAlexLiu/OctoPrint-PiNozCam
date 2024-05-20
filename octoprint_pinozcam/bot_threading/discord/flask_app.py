from flask import Flask, request

def create_app(message_queue):
    app = Flask(__name__)

    @app.route('/api', methods=['POST'])
    def handle_api_call():
        message = request.json['message']
        print(f'Received message from API: {message}')
        message_queue.put(message)
        return 'Message received and sent to Discord'

    return app

def run_flask(message_queue):
    app = create_app(message_queue)
    app.run(port=6000)