from flask import Flask, request, jsonify
from PIL import Image
from io import BytesIO
from inference import image_inference
import base64
import numpy as np
import multiprocessing
import math

app = Flask(__name__)


def largest_power_of_two(n):
    # Find the exponent of the largest power of 2 less than or equal to n
    exponent = math.floor(math.log2(n))
    # Return the largest power of 2
    return 2 ** exponent

@app.route('/infer', methods=['POST'])
def infer():
    # Get the image from the request
    image_data = request.form['image']
    image_data = base64.b64decode(image_data)
    input_image = Image.open(BytesIO(image_data))

    # Get the thresholds and parameters from the request
    scores_threshold = float(request.form['scores_threshold'])
    img_sensitivity = float(request.form['img_sensitivity'])
    cpu_speed_control = float(request.form['cpu_speed_control'])

    # Get the total number of CPU cores
    total_cpu_cores = multiprocessing.cpu_count()

    # Use half of the total CPU cores, or 1 if there's only 1 core
    num_threads_candidate = max(1, math.ceil(total_cpu_cores * cpu_speed_control))
    num_threads = largest_power_of_two(num_threads_candidate)

    # Call the image_inference function
    scores, boxes, labels, severity, elapsed_time = image_inference(
        input_image, scores_threshold, img_sensitivity,num_threads)

    # Convert ndarray to list
    if isinstance(scores, np.ndarray):
        scores = scores.tolist()
    if isinstance(boxes, np.ndarray):
        boxes = boxes.tolist()
    if isinstance(labels, np.ndarray):
        labels = labels.tolist()
    if isinstance(severity, np.ndarray):
        severity = severity.tolist()


    # Create a response dictionary
    response_dict = {
        'scores': scores,
        'boxes': boxes,
        'labels': labels,
        'severity': severity,
        'elapsed_time': elapsed_time
    }

    # Return the response as JSON
    return jsonify(response_dict)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=50000, debug=False)
