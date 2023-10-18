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
        self.server_busy = False
        self.client_results_dict = {}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

    def submit_result(self, client_uuid, picture_uuid, result=None):
        with self.lock:
            if client_uuid not in self.client_results_dict:
                self.client_results_dict[client_uuid] = {}
            if result is None:
                self.client_results_dict[client_uuid][picture_uuid] = {'result': "", 'timestamp': time.time()}
            else:
                self.client_results_dict[client_uuid][picture_uuid] = {'result': result, 'timestamp': time.time()}

    def get_result(self, client_uuid, picture_uuid):
        with self.lock:
            client_dict = self.client_results_dict.get(client_uuid, None)
            if client_dict is None:
                return None
            entry = client_dict.get(picture_uuid, None)
            if entry is None:
                return None
            elif entry['result'] == "":
                return ""
            else:
                return self.client_results_dict[client_uuid].pop(picture_uuid)['result']

    def get_result_len(self):
        with self.lock:
            #print("No. Picture:"+str(len(self.client_results_dict)))

            return len(self.client_results_dict)

    def set_busy(self, status):
        with self.lock:
            #print("set busy to "+str(status))
            self.server_busy = status

    def get_busy(self):
        with self.lock:
            #("get busy status "+str(self.server_busy))
            return self.server_busy

    def cleanup_results(self):
        # This method could be called periodically to clean up old results
        while not self.stop_event.is_set():
            current_time = time.time()
            print("clean process start")
            logging.info("clean process start")
            # First, clean up old results
            keys_to_delete = []
            for client_uuid, picture_dict in self.client_results_dict.items():
                picture_keys_to_delete = [key for key, value in picture_dict.items() if current_time - value['timestamp'] > 1 * 30]
                for key in picture_keys_to_delete:
                    picture_dict.pop(key)
                # If the picture dictionary is now empty, mark the client_uuid for deletion
                if not picture_dict:
                    keys_to_delete.append(client_uuid)
            
            # Now, delete empty client_uuid entries from client_results_dict
            for key in keys_to_delete:
                del self.client_results_dict[key]
            
            #print("Cleaned, No. Picture is:"+str(len(self.client_results_dict)))
            logging.info("Cleaned, No. Picture is:"+str(len(self.client_results_dict)))

            time.sleep(30)

    def stop_cleanup(self):
        self.stop_event.set()     

processing_manager = ProcessingManager()

def largest_power_of_two(n):
    exponent = math.floor(math.log2(n))
    return 2 ** exponent

def process_image(client_uuid, picture_uuid, image_data, scores_threshold, img_sensitivity, cpu_speed_control):
    processing_manager.submit_result(client_uuid, picture_uuid, None)
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

    processing_manager.submit_result(client_uuid, picture_uuid, response_dict)
    processing_manager.set_busy(False)

@app.route('/submit_request', methods=['POST'])
def submit_request():
    if processing_manager.get_busy():
        return jsonify({'message': 'Server is busy'}), 503

    image_data = request.form.get('image', None)
    if not image_data:
        return jsonify({'message': 'Image data is required'}), 400
    
    client_uuid = request.form.get('client_uuid', None)
    if not client_uuid:
        return jsonify({'message': 'Client UUID is required'}), 400

    image_data = base64.b64decode(image_data)
    scores_threshold = float(request.form.get('scores_threshold', 0.5))
    img_sensitivity = float(request.form.get('img_sensitivity', 0.04))
    cpu_speed_control = float(request.form.get('cpu_speed_control', 0.5))
    picture_uuid = request.form.get('picture_uuid', None)
    if not picture_uuid:
        return jsonify({'message': 'Picture UUID is required'}), 400

    processing_manager.set_busy(True)
    #print(f"start to process {uuid}")
    threading.Thread(target=process_image, args=(client_uuid, picture_uuid, image_data, scores_threshold, img_sensitivity, cpu_speed_control)).start()

    return jsonify({'message': 'Request received, processing started'})

@app.route('/request_result', methods=['POST'])
def request_result():
    client_uuid = request.form.get('client_uuid', None)
    if not client_uuid:
        return jsonify({'message': 'Client UUID is required'}), 400
    
    picture_uuid = request.form.get('picture_uuid', None)
    if not picture_uuid:
        return jsonify({'message': 'Picture UUID is required'}), 400
    #print(f"requesting {uuid}")
    result = processing_manager.get_result(client_uuid, picture_uuid)
    if result is None:
        #print('UUID not found')
        return jsonify({'message': 'UUID not found'}), 404
    elif result == "":
        #print('Result not ready')
        return jsonify({'message': 'Result not ready'}), 202

    return jsonify({'result': result, 'client_uuid': client_uuid, 'picture_uuid': picture_uuid})

@app.route('/check_available', methods=['GET'])
def check_available():
    client_uuid = request.args.get('client_uuid', None)
    if not client_uuid:
        return jsonify({'message': 'Client UUID is required'}), 400
    
    results_dict_len = processing_manager.get_result_len()
    server_busy = processing_manager.get_busy()
    
    # Check if results_dict is not empty
    if results_dict_len > 0:
        # Check if client_uuid exists in the results_dict
        with processing_manager.lock:
            if client_uuid in processing_manager.client_results_dict:
                # If client_uuid exists and server is not busy, return true
                is_available = not server_busy
            else:
                # If client_uuid does not exist, return false regardless of server_busy status
                is_available = False
    else:
        # If results_dict is empty, check server_busy status
        is_available = not server_busy

    return jsonify({'message': is_available}), 200


if __name__ == '__main__':
    cleanup_thread = threading.Thread(target=processing_manager.cleanup_results)
    cleanup_thread.start()  

    try:
        app.run(host='127.0.0.1', port=50000, debug=False)
    finally:
        processing_manager.stop_cleanup()  
        cleanup_thread.join()  
