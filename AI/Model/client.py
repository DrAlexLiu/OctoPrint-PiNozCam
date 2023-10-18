import requests
from PIL import Image, ImageDraw
from io import BytesIO
import time
import base64
import uuid
import logging

# Logging Configuration
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Variables for the IP and port
ip_address = "192.168.1.229"
port = "8081"

drawbox_threshold = 0.5
area_threshold = 0.04
img_sensitivity = 0.04
scores_threshold = 0.65
max_count = 10
cpu_speed_control = 0.5
count_time = 5 * 60
inference_interval = 5

url = f"http://{ip_address}:{port}/?action=snapshot"

timestamps = []
count = 0
use_streaming = False


client_ready = True
unique_client_uuid = str(uuid.uuid4())  # Generate a unique client UUID once
unique_picture_uuid = None
second_request = False

def process_response_data(scores, boxes, labels, severity, elapsed_time, count, timestamps,input_image):
    # Check if severity is a list and extract the first element if so
    if isinstance(severity, list) and severity:
        severity_value = severity[0]
    elif isinstance(severity, (int, float)):  # if severity is a single number, use it directly
        severity_value = severity
    else:
        logging.error("Unexpected severity format")
        return False,count, timestamps  # Exit the loop if severity format is unexpected

    # If severity is larger than 0.66, increase the count and store the current timestamp
    color = "white"  # default color
    if severity_value > 0.66:
        count += 1
        timestamps.append(time.time())
        color = "red"
    elif 0.33 <= severity_value <= 0.66:
        color = "yellow"
    else:
        color = "green"

    # Check and decrease the count for timestamps older than count_time minutes (count_time seconds)
    current_time = time.time()
    timestamps = [t for t in timestamps if current_time - t <= count_time]
    count = len(timestamps)

    # If count exceeds 10 within the last count_time minutes, break the loop
    if count > max_count:
        return True,count, timestamps

    print('#########################################################')
    print(color)
    print(f"Inference time: {elapsed_time:.4f} seconds")
    print("current count=", count)
    print("scores=", scores)
    print("boxes=", boxes)
    print("labels=", labels)
    print("severity=", severity)

    # Draw boxes
    try:
        draw = ImageDraw.Draw(input_image)
    except Exception as e:
        logging.error(f"Failed to draw on image: {e}")

    # Iterate through all boxes, scores, and labels in the first image
    for box, score, label in zip(boxes[0], scores[0], labels[0]):
        # Check if label is 1 and score is greater than or equal to printlayout_threshold
        if score >= drawbox_threshold:
            # Extract corners
            x1, y1, x2, y2 = box
            draw.rectangle([(x1, y1), (x2, y2)], outline=color, width=2)

            # Format score as a string and draw it
            score_text = f"{score:.2f}"
            text_size = draw.textsize(score_text)
            text_position = (x2 - text_size[0], y1)
            draw.text(text_position, score_text, fill=color)

    # Save the image with boxes
    #input_image.save("result.jpg")
    #print("Result saved as result.jpg")

    # Optional: Sleep for a short duration before the next iteration to avoid overloading the server
    #print("sleep for ", max(0, inference_interval - elapsed_time))
    time.sleep(max(0, inference_interval - elapsed_time))
    # At the end of the function, return False to indicate that the loop should continue
    return False,count, timestamps

def handle_network_request(url, data, client_ready=False):
    try:
        #print("handle_network_request")
        response = requests.post(url, data=data, timeout=10)
        if response.status_code in (503, 404):
            logging.error(f"Error: {response.status_code} - {response.text}")
            if client_ready:
                #print("wait for 6s")
                time.sleep(6)
            #print(f"return 503 response{response.text}")
            return response
        response.raise_for_status()
        return response  # Return the response regardless of the status code
    except requests.RequestException as e:
        # Check the status code and log an error message for certain status codes
        logging.error(f"Network error: {e}")
        #print("wait for 30s")
        time.sleep(30)
        return None  # Or handle this accordingly

def check_server_availability(unique_client_uuid):
    try:
        response = requests.get(
            'http://127.0.0.1:50000/check_available', 
            params={'client_uuid': unique_client_uuid}
        )
        if response.status_code == 200:
            return response.json().get('message', False)
        else:
            logging.error(f"Error: {response.status_code} - {response.text}")
            return False
    except requests.RequestException as e:
        logging.error(f"Network error: {e}")
        return False
    
while True:
    time.sleep(1)  # Loop every second
    #print("start a new loop")

    if client_ready:
        #print("in ready mode")

        # Check server availability before proceeding
        if not check_server_availability(unique_client_uuid):
            #print("Server is busy, waiting for the next loop")
            logging.info('Server is busy, waiting for the next loop')
            time.sleep(10)
            if second_request:
                #break
                pass
            second_request=True
            continue  # If server is busy, skip to the next loop iteration
        else:
            if second_request:
                second_request=False
                #print("Last time failed, try again")
                continue
            
        if use_streaming:
            response = requests.get(url, stream=True, timeout=10)
            if not response:
                continue
            input_image = Image.open(BytesIO(response.content))
        else:
            try:
                input_image = Image.open("test1.jpg")
            except Exception as e:
                logging.error(f"Failed to open image: {e}")
                continue  # Skip to next iteration


        buffered = BytesIO()
        input_image.save(buffered, format="JPEG")
        encoded_string = base64.b64encode(buffered.getvalue()).decode('utf-8')
        
        unique_picture_uuid = str(uuid.uuid4())
        data = {
            'image': encoded_string,
            'client_uuid': unique_client_uuid,  # Now using client_uuid
            'picture_uuid': unique_picture_uuid,  # Now using picture_uuid
            'scores_threshold': scores_threshold,
            'img_sensitivity': img_sensitivity,
            'cpu_speed_control': cpu_speed_control
        }
        
        response = handle_network_request('http://127.0.0.1:50000/submit_request',data,client_ready)
        if response:
            response_message = response.json().get('message', '')
            if response_message == 'Request received, processing started':
                client_ready = False  # Switch status to waiting
                logging.info('Request received, processing started')
        else:
            logging.error('Network error')
            #print("Network error")

    else:  # If status is false (waiting)
        #print("in waiting mode")

        data = {
            'client_uuid': unique_client_uuid,  # Now using client_uuid
            'picture_uuid': unique_picture_uuid  # Now using picture_uuid
        }
        #print(f"sending{unique_uuid}")
        response = handle_network_request('http://127.0.0.1:50000/request_result',data,client_ready)
        if not response:
            client_ready = True  # Ready to send new request
            continue
        response_data = response.json()
        message = response_data.get('message', '')
        if message == 'UUID not found':
            logging.error('UUID not found, fetching a new image.')
            client_ready = True  # Ready to send new request
        elif message == 'Result not ready':
            logging.info('Result not ready, waiting for processing.')
        elif 'result' in response_data:
            response_client_uuid = response_data.get('client_uuid', '')
            response_picture_uuid = response_data.get('picture_uuid', '')
            if response_client_uuid != unique_client_uuid or response_picture_uuid != unique_picture_uuid:
                logging.error('Mismatched client_uuid or picture_uuid in response')
                continue
            result_data = response_data['result']
            if all(key in result_data for key in ['scores', 'boxes', 'labels', 'severity', 'elapsed_time']):
                scores = result_data.get('scores', [])
                boxes = result_data.get('boxes', [])
                labels = result_data.get('labels', [])
                severity = result_data.get('severity', 0)
                elapsed_time = result_data.get('elapsed_time', 0)
                termination_process,count, timestamps = process_response_data(scores, boxes, labels, severity, elapsed_time, count, timestamps, input_image)
                if termination_process:
                    break # Exit the loop if process_response_data returned True
                client_ready = True  # Ready to send new request
        else:
            logging.error('Unexpected response format')




