import requests
from PIL import Image, ImageDraw
from io import BytesIO
import time
import base64

# Variables for the IP and port
ip_address = "192.168.1.229"
port = "8081"

printlayout_threshold = 0.5
area_threshold = 0.04
img_sensitivity = 0.04
scores_threshold = 0.65
max_count=10
count_time=5*60
inference_interval=10

# URL of the mjpg-streamer snapshot
url = f"http://{ip_address}:{port}/?action=snapshot"

# List to store timestamps when severity exceeds 0.66
timestamps = []

# Counter for severity occurrences
count = 0
use_streaming=False

while True:
    if use_streaming:
        # Make a GET request to fetch the raw image data
        response = requests.get(url, stream=True)
        response.raise_for_status()

        # Get the image from the response content
        input_image = Image.open(BytesIO(response.content))
    else:
        # Load image and resize
        input_image = Image.open("test1.jpg")

    # Convert image to base64 for sending via POST request
    buffered = BytesIO()
    input_image.save(buffered, format="JPEG")
    encoded_string = base64.b64encode(buffered.getvalue()).decode('utf-8')

    # Send POST request
    response = requests.post('http://127.0.0.1:50000/infer', 
                             data={'image': encoded_string, 
                                   'scores_threshold': scores_threshold, 
                                   'img_sensitivity': img_sensitivity})

    # Check the response
    if response.ok:
        response_data = response.json()
        scores = response_data.get('scores', [])
        boxes = response_data.get('boxes', [])
        labels = response_data.get('labels', [])
        severity = response_data.get('severity', 0)
        elapsed_time = response_data.get('elapsed_time', 0)

        # Check if severity is a list and extract the first element if so
        if isinstance(severity, list) and severity:
            severity_value = severity[0]
        elif isinstance(severity, (int, float)):  # if severity is a single number, use it directly
            severity_value = severity
        else:
            print("Unexpected severity format")
            break  # Exit the loop if severity format is unexpected

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
            break

        print('#########################################################')
        print(color)
        print(f"Inference time: {elapsed_time:.4f} seconds")
        print("current count=", count)
        print("scores=", scores)
        print("boxes=", boxes)
        print("labels=", labels)
        print("severity=", severity)

        # Optional: Sleep for a short duration before the next iteration to avoid overloading the server
        print("sleep for ", max(0, inference_interval - elapsed_time))
        time.sleep(max(0, inference_interval - elapsed_time))


    else:
        print(f"Failed: {response.text}")
        break   # This will exit the while loop if the response is not ok

# Draw boxes
draw = ImageDraw.Draw(input_image)
for box in boxes[0]:  # Assuming you have a single image and boxes is a list of bounding boxes
    # Extract corners
    x1, y1, x2, y2 = box
    draw.rectangle([(x1, y1), (x2, y2)], outline="red", width=2)

# Save the image with boxes
input_image.save("result.jpg")
print("Result saved as result.jpg")