from flask import Flask, request, jsonify
from PIL import Image
from io import BytesIO
from inference import image_inference
import base64
import threading
import logging
import time
import numpy as np
import multiprocessing
import math

app = Flask(__name__)

# Setup logging
logging.basicConfig(level=logging.INFO)

class ProcessingManager:
    def __init__(self):
        self.is_busy = False
        self.result_dict = {}
        self.lock = threading.Lock()
        self.stop_event = threading.Event() 

    def submit_result(self, uuid, result=None):
        with self.lock:
            if result is None:
                self.result_dict[uuid] = {'result': "", 'timestamp': time.time()}
            else:
                self.result_dict[uuid] = {'result': result, 'timestamp': time.time()}

    def get_result(self, uuid):
        with self.lock:
            #print(len(self.result_dict))
            entry = self.result_dict.get(uuid, None)
            if entry is None:
                # If the entry is None, simply return None
                return None
            elif entry['result'] == "":
                # If the result is an empty string, return an empty string
                return ""
            else:
                # If there's an actual result, pop the entry and return the result
                #print("pop a result")
                return self.result_dict.pop(uuid)['result']

    # def picture_in_queue(self):
    #     with self.lock:
    #         print("No. Picture:"+str(len(self.result_dict)))

    def set_busy(self, status):
        with self.lock:
            self.is_busy = status

    def get_busy(self):
        with self.lock:
            return self.is_busy

    def cleanup_results(self):
        # This method could be called periodically to clean up old results
        while not self.stop_event.is_set():
            current_time = time.time()
            keys_to_delete = [key for key, value in self.result_dict.items() if current_time - value['timestamp'] > 5 * 60]
            for key in keys_to_delete:
                self.result_dict.pop(key)
            time.sleep(60)

    def stop_cleanup(self):
        self.stop_event.set()     

processing_manager = ProcessingManager()

def largest_power_of_two(n):
    exponent = math.floor(math.log2(n))
    return 2 ** exponent

def process_image(uuid, image_data, scores_threshold, img_sensitivity, cpu_speed_control):
    processing_manager.submit_result(uuid, None)
    #processing_manager.picture_in_queue()
    #print(f"processing {uuid}")
    
    try:
        input_image = Image.open(BytesIO(image_data))
    except Exception as e:
        logging.error(f"Error decoding image data: {e}")
        return

    total_cpu_cores = multiprocessing.cpu_count()
    num_threads_candidate = max(1, math.ceil(total_cpu_cores * cpu_speed_control))
    num_threads = largest_power_of_two(num_threads_candidate)

    try:
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

    processing_manager.submit_result(uuid, response_dict)
    processing_manager.set_busy(False)

@app.route('/submit_request', methods=['POST'])
def submit_request():
    if processing_manager.get_busy():
        return jsonify({'message': 'Server is busy'}), 503

    image_data = request.form.get('image', None)
    if not image_data:
        return jsonify({'message': 'Image data is required'}), 400

    image_data = base64.b64decode(image_data)
    scores_threshold = float(request.form.get('scores_threshold', 0.5))
    img_sensitivity = float(request.form.get('img_sensitivity', 0.04))
    cpu_speed_control = float(request.form.get('cpu_speed_control', 0.5))
    uuid = request.form.get('uuid', None)
    if not uuid:
        return jsonify({'message': 'UUID is required'}), 400

    processing_manager.set_busy(True)
    #print(f"start to process {uuid}")
    threading.Thread(target=process_image, args=(uuid, image_data, scores_threshold, img_sensitivity, cpu_speed_control)).start()

    return jsonify({'message': 'Request received, processing started'})

@app.route('/request_result', methods=['POST'])
def request_result():
    uuid = request.form.get('uuid', None)
    if not uuid:
        return jsonify({'message': 'UUID is required'}), 400
    #print(f"requesting {uuid}")
    result = processing_manager.get_result(uuid)
    if result is None:
        #print('UUID not found')
        return jsonify({'message': 'UUID not found'}), 404
    elif result == "":
        #print('Result not ready')
        return jsonify({'message': 'Result not ready'}), 202

    return jsonify(result)

if __name__ == '__main__':
    cleanup_thread = threading.Thread(target=processing_manager.cleanup_results)
    cleanup_thread.start()  

    try:
        app.run(host='127.0.0.1', port=50000, debug=False)
    finally:
        processing_manager.stop_cleanup()  
        cleanup_thread.join()  
