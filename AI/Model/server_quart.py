from quart import Quart, request, jsonify
from PIL import Image
from io import BytesIO
from inference import image_inference  # Assuming this is your custom module
import base64
import logging
import time
import numpy as np
import multiprocessing
import math
import aioprocessing
import asyncio


app = Quart(__name__)
logging.basicConfig(level=logging.INFO)

class ProcessingManager:
    def __init__(self):
        self.is_busy = False
        self.result_dict = {}
        self.lock = asyncio.Lock()  # use asyncio.Lock instead of threading.Lock
        self.stop_event = asyncio.Event()  # use asyncio.Event

    async def submit_result(self, uuid, result=None):
        async with self.lock:
            if result is None:
                self.result_dict[uuid] = {'result': "", 'timestamp': time.time()}
            else:
                self.result_dict[uuid] = {'result': result, 'timestamp': time.time()}

    async def get_result(self, uuid):
        async with self.lock:
            entry = self.result_dict.get(uuid, None)
            if entry is None:
                return None
            elif entry['result'] == "":
                return ""
            else:
                return self.result_dict.pop(uuid)['result']

    async def set_busy(self, status):
        async with self.lock:
            self.is_busy = status

    async def get_busy(self):
        async with self.lock:
            return self.is_busy

    async def cleanup_results(self):
        while not self.stop_event.is_set():
            current_time = time.time()
            async with self.lock:
                keys_to_delete = [key for key, value in self.result_dict.items() if current_time - value['timestamp'] > 5 * 60]
                for key in keys_to_delete:
                    self.result_dict.pop(key)
            await asyncio.sleep(60)  # use await asyncio.sleep instead of time.sleep

    def stop_cleanup(self):
        self.stop_event.set()

processing_manager = ProcessingManager()

def largest_power_of_two(n):
    exponent = math.floor(math.log2(n))
    return 2 ** exponent

async def process_image(uuid, image_data, scores_threshold, img_sensitivity, cpu_speed_control):
    await processing_manager.submit_result(uuid, None)
    try:
        input_image = Image.open(BytesIO(image_data))
    except Exception as e:
        logging.error(f"Error decoding image data: {e}")
        return

    total_cpu_cores = multiprocessing.cpu_count()
    num_threads_candidate = max(1, math.ceil(total_cpu_cores * cpu_speed_control))
    num_threads = largest_power_of_two(num_threads_candidate)

    try:
        # Assume image_inference is not asynchronous
        scores, boxes, labels, severity, elapsed_time = image_inference(
            input_image, scores_threshold, img_sensitivity, num_threads)
    except Exception as e:
        logging.error(f"Error in image inference: {e}")
        return

    response_dict = {
        'scores': scores.tolist() if isinstance(scores, np.ndarray) else scores,
        'boxes': boxes.tolist() if isinstance(boxes, np.ndarray) else boxes,
        'labels': labels.tolist() if isinstance(labels, np.ndarray) else labels,
        'severity': severity.tolist() if isinstance(severity, np.ndarray) else severity,
        'elapsed_time': elapsed_time,
        'timestamp': time.time()
    }

    await processing_manager.submit_result(uuid, response_dict)
    await processing_manager.set_busy(False)

@app.route('/submit_request', methods=['POST'])
async def submit_request():
    if await processing_manager.get_busy():
        return jsonify({'message': 'Server is busy'}), 503

    form_data = await request.form
    image_data = form_data.get('image', None)
    if not image_data:
        return jsonify({'message': 'Image data is required'}), 400

    image_data = base64.b64decode(image_data)
    scores_threshold = float(form_data.get('scores_threshold', 0.5))
    img_sensitivity = float(form_data.get('img_sensitivity', 0.04))
    cpu_speed_control = float(form_data.get('cpu_speed_control', 0.5))
    uuid = form_data.get('uuid', None)
    if not uuid:
        return jsonify({'message': 'UUID is required'}), 400

    await processing_manager.set_busy(True)
    asyncio.create_task(process_image(uuid, image_data, scores_threshold, img_sensitivity, cpu_speed_control))

    return jsonify({'message': 'Request received, processing started'})

@app.route('/request_result', methods=['POST'])
async def request_result():
    form_data = await request.form
    uuid = form_data.get('uuid', None)
    if not uuid:
        return jsonify({'message': 'UUID is required'}), 400
    result = await processing_manager.get_result(uuid)
    if result is None:
        return jsonify({'message': 'UUID not found'}), 404
    elif result == "":
        return jsonify({'message': 'Result not ready'}), 202

    return jsonify(result)

async def startup():
    cleanup_task = asyncio.create_task(processing_manager.cleanup_results())
    try:
        await app.run_task(host='127.0.0.1', port=50000, debug=False)
    finally:
        cleanup_task.cancel()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=50000)
