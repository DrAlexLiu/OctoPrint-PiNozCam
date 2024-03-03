import base64
import json
import math
import multiprocessing
import os
import threading
import time
from collections import deque
from io import BytesIO
import requests
from PIL import Image, ImageDraw
from flask import Response
import octoprint.plugin
from octoprint.events import Events

from .inference import image_inference


class PinozcamPlugin(octoprint.plugin.StartupPlugin,
                     octoprint.plugin.TemplatePlugin,
                     octoprint.plugin.SettingsPlugin,
                     octoprint.plugin.AssetPlugin,
                     octoprint.plugin.BlueprintPlugin,
                     octoprint.plugin.EventHandlerPlugin):
    """
    An OctoPrint plugin that enhances 3D printing with AI-based monitoring for potential print failures.
    
    Attributes:
        lock (threading.Lock): A lock to ensure thread-safe operations.
        stop_event (threading.Event): An event to signal stopping of threads.
        current_encoded_image (str): Base64 encoded string of the current image being processed.
        count (int): A counter to track detected failures.
        action (int): Determines the action to take upon detection (0: notify, 1: pause, 2: stop).
        ai_input_image (PIL.Image.Image): The current image being analyzed by AI.
        ai_results (collections.deque): Stores recent AI analysis results.
        telegram_bot_token (str): Token for Telegram bot integration.
        telegram_chat_id (str): Chat ID for Telegram notifications.
        ai_running (bool): Indicates if AI processing is active.
        num_threads (int): Num of threads to use for AI inference.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.current_encoded_image = None
        self.count = 0
        self.action = 0
        self.ai_input_image = None
        self.ai_results = deque(maxlen=100)
        self.telegram_bot_token = ""
        self.telegram_chat_id = ""
        self.ai_running = False
        self.num_threads = 1
        self.snapshot = ""

    def _encode_no_camera_image(self):
        """
        Encodes a 'no camera' placeholder image to a base64 string.
        
        Returns:
            A base64 encoded string of the 'no camera' image.
        """
        no_camera_path = os.path.join(os.path.dirname(__file__), 'static', 'no_camera.jpg')
        try:
            with open(no_camera_path, "rb") as image_file:
                return f"data:image/jpeg;base64,{base64.b64encode(image_file.read()).decode('utf-8')}"
        except FileNotFoundError:
            self._logger.error(f"No camera image not found at {no_camera_path}")
            return ""

    def get_printer_status(self):
        """
        Retrieves the current printing status from the printer.

        Returns:
            bool: True if the printer is currently printing, False otherwise.
        """
        printer_status = self._printer.get_current_data()
        return printer_status["state"]["flags"]["printing"]
    
    def cpu_is_raspberry_pi(self):
        """
        Checks if the script is running on a Raspberry Pi.

        Returns:
            bool: True if the CPU is a Raspberry Pi, False otherwise.
        """
        try:
            with open("/proc/cpuinfo", "r") as f:
                return "Raspberry Pi" in f.read()
        except IOError:
            return False


    def get_cpu_temperature(self):
        if self.cpu_is_raspberry_pi():
            try:
                with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                    temp_str = f.read()
                    temp_c = int(temp_str) / 1000.0
                    return temp_c
            except FileNotFoundError:
                return 0
        else:
            return 0
    
    def get_settings_defaults(self):
        return dict(
            action=0,
            printLayoutThreshold=0.5,
            imgSensitivity=0.08,
            scoresThreshold=0.7,
            maxCount=2,
            countTime=300,
            cpuSpeedControl=1,
            snapshot="http://127.0.0.1:8080/?action=snapshot",
            telegramBotToken="",
            telegramChatID="",
        )
    
    def perform_action(self):
        """
        Executes an action based on the current setting of 'self.action'.
        - If action is 1, it pauses the print.
        - If action is 2, it stops the print.
        - Otherwise, it logs that there is no interference with the printing process.
        """
        if self.action == 1:
            self._logger.info("Pausing print...")
            self._printer.pause_print()
            self._logger.info("Print paused.")
        elif self.action == 2:
            self._logger.info("Stopping print...")
            self._printer.cancel_print()
            self._logger.info("Print stopped.")
        else:
            self._logger.info("No interference with the printing process.")
    
    def on_after_startup(self):
        """
        Initializes plugin settings after startup by loading values from the configuration.
        It also logs the initialized settings for verification.
        """
        self.action = self._settings.get_int(["action"])
        self.print_layout_threshold = self._settings.get_float(["printLayoutThreshold"])
        self.img_sensitivity = self._settings.get_float(["imgSensitivity"])
        self.scores_threshold = self._settings.get_float(["scoresThreshold"])
        self.max_count = self._settings.get_int(["maxCount"])
        self.count_time = self._settings.get_int(["countTime"])
        self.cpu_speed_control = self._settings.get_float(["cpuSpeedControl"])
        self.snapshot = self._settings.get(["snapshot"])
        self.telegram_bot_token = self._settings.get(["telegramBotToken"])
        self.telegram_chat_id = self._settings.get(["telegramChatID"])
        # Calculate the number of threads to use for AI inference       
        self._thread_calculation()
    
    def telegram_send(self, image, severity, percentage_area):
        """
        Sends an alert message with an image to a Telegram chat.
        The message includes details such as printer name, severity, failure area, failure count, and max failure count.

        Parameters:
        - image: The PIL Image object to send.
        - severity: The severity of the failure detected.
        - percentage_area: The area of the print affected by the failure.
        """
        telegram_api_url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendPhoto"
        title = self._settings.global_get(["appearance", "name"])
        severity_percentage = severity * 100
        with self.lock:
            failure_count = self.count
        caption = (f"Printer {title}\n"
                f"Severity: {severity_percentage:.2f}%\n"
                f"Failure Area: {percentage_area:.2f}\n"
                f"Failure Count: {failure_count}\n"
                f"Max Failure Count: {self.max_count}")

        image_stream = BytesIO()
        image.save(image_stream, format='JPEG')
        image_stream.seek(0)

        files = {'photo': ('image.jpeg', image_stream, 'image/jpeg')}
        data = {'chat_id': self.telegram_chat_id, 'caption': caption}

        response = requests.post(telegram_api_url, files=files, data=data)
        if response.status_code == 200:
            self._logger.info("Telegram message sent successfully.")
        else:
            self._logger.error(f"Failed to send message to Telegram: {response.text}")

    def on_event(self, event, payload):
        """
        Handles OctoPrint events to start or stop AI image processing based on the printer's status.
        - Initiates AI processing when a print job starts or resumes.
        - Stops AI processing when a print job is done, failed, cancelled, or paused.
        """
        if event in [Events.PRINT_STARTED, Events.PRINT_RESUMED]:
            self._logger.info(f"{event}: {payload}")
            self._logger.info("Print started, beginning AI image processing.")
            if event == Events.PRINT_STARTED:
                self._logger.info("Count and results are cleared.")
                self.count = 0
                self.ai_results.clear()
            if not hasattr(self, 'ai_thread') or not self.ai_thread.is_alive():
                self.ai_thread = threading.Thread(target=self.process_ai_image)
                self.ai_thread.daemon = True
                self.ai_thread.start()
        elif event in [Events.PRINT_DONE, Events.PRINT_FAILED, Events.PRINT_CANCELLED, Events.PRINT_PAUSED]:
            self._logger.info(f"{event}: {payload}")
            self._logger.info("Print ended, stopping AI image processing.")
            self.ai_running = False

    def encode_image_to_base64(self, image):
        """
        Encodes a PIL Image object to a base64 string for easy embedding or storage.
        """
        buffered = BytesIO()
        image.save(buffered, format="JPEG")
        return "data:image/jpeg;base64," + base64.b64encode(buffered.getvalue()).decode('utf-8')

    def process_ai_image(self):
        """
        Continuously processes images from a camera to detect failures using AI inference.
        It fetches the latest image, performs inference, and takes action based on the results.
        This function runs in a dedicated thread to not block the main plugin operations.
        """
        self.ai_running = True
        while self.ai_running:
            if not self.get_printer_status():
                self._logger.info("Printer is not currently printing. Pausing AI processing.")
                time.sleep(10)
                continue

            with self.lock:
                while self.ai_results and time.time() - self.ai_results[0]['time'] > self.count_time:
                    result = self.ai_results.popleft()
                    if result['severity'] > 0.66:
                        self.count -= 1
                        self._logger.info(f"Reduced failure count: {self.count}")
            self._logger.info("Begin to process one image.")
            try:
                response = requests.get(self.snapshot, timeout=1)
                response.raise_for_status()
            except requests.RequestException as e:
                self._logger.error(f"Failed to fetch image for AI processing: {e}")
                continue

            ai_input_image = Image.open(BytesIO(response.content))
            
            try:
                scores, boxes, labels, severity, percentage_area, elapsed_time = image_inference(
                    ai_input_image, self.scores_threshold, self.img_sensitivity, self.num_threads)
            except Exception as e:
                self._logger.error(f"AI inference error: {e}")
                continue
            self._logger.info(f"scores=f{scores} boxes={boxes} labels={labels} severity={severity} percentage_area={percentage_area} elapsed_time={elapsed_time}")
            #draw the result image
            ai_result_image = self.draw_response_data(scores, boxes, labels, severity, ai_input_image)
            
            # Store the result
            if severity > 0.33:
                result = {
                    'time': time.time(),
                    'scores': scores,
                    'boxes': boxes,
                    'labels': labels,
                    'severity': severity,
                    'percentage_area': percentage_area,
                    'elapsed_time': elapsed_time,
                    'ai_input_image': self.encode_image_to_base64(ai_input_image),
                    'ai_result_image': self.encode_image_to_base64(ai_result_image)
                }
                with self.lock:
                    self.ai_results.append(result)
                self._logger.info("Stored new AI inference result.")
                if severity > 0.66:
                    with self.lock:
                        self.count += 1
                    self._logger.info(f"self.count increased by 1 self.count={self.count}")
                    # If count exceeds  within the last count_time minutes, perform action
                    if self.telegram_bot_token and self.telegram_chat_id and self.get_printer_status():
                        self.telegram_send(ai_result_image,severity,percentage_area)
                    
                    with self.lock:
                        failure_count = self.count
                    if failure_count >= self.max_count:
                        self.perform_action()
    
    @staticmethod
    def _largest_power_of_two(n):
        exponent = math.floor(math.log2(n))
        return 2 ** exponent
    
    def _thread_calculation(self):
        total_cpu_cores = multiprocessing.cpu_count()
        num_threads_candidate = max(1, math.ceil(total_cpu_cores * self.cpu_speed_control))
        self.num_threads = self._largest_power_of_two(num_threads_candidate)
        self._logger.info(f"num_threads:{self.num_threads}")
    
    def draw_response_data(self, scores, boxes, labels, severity, image):
        """
        Draws bounding boxes and labels on the image based on inference results.

        Parameters:
        - scores: Confidence scores of the detected objects.
        - boxes: Coordinates of the bounding boxes for detected objects.
        - labels: Class labels for the detected objects.
        - severity: The severity level of the detection.
        - image: The original image on which detections are to be drawn.

        Returns:
        - The image with bounding boxes and labels drawn on it.
        """
        draw = ImageDraw.Draw(image)
        color = "green"  # Default color for bounding boxes

        # Change the color based on the severity of the detection
        if severity > 0.66:
            color = "red"
        elif severity > 0.33:
            color = "yellow"

        # Assuming you've already created an ImageDraw.Draw object named 'draw'
        for box, score in zip(boxes[0], scores[0]):
            if score < self.scores_threshold:
                break
            x1, y1, x2, y2 = box
            draw.rectangle([(x1, y1), (x2, y2)], outline=color, width=2)
            
            # Text to be drawn
            spaghetti_text = "Spaghetti"
            score_text = f"{score:.2f}"  # Format the score to two decimal places
            
            # Calculate text positions without specifying a font
            spaghetti_text_position = (x1, y1 - 10)  
            score_text_position = (x2-15, y1 - 10) 
            
            # Draw text using the default font
            draw.text(spaghetti_text_position, spaghetti_text, fill=color)
            draw.text(score_text_position, score_text, fill=color)

        return image
    
    def on_settings_save(self, data):
        """
        Handles saving of plugin settings. Updates the plugin's configuration
        according to the data provided by the user in the UI.

        Parameters:
        - data: A dictionary containing the settings data to be saved.
        """
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)

        # Update the plugin settings based on the data provided
        self.action = int(data.get("action", self.action))
        self.print_layout_threshold = float(data.get("printLayoutThreshold", self.print_layout_threshold))
        self.img_sensitivity = float(data.get("imgSensitivity", self.img_sensitivity))
        self.scores_threshold = float(data.get("scoresThreshold", self.scores_threshold))
        self.max_count = int(data.get("maxCount", self.max_count))
        self.count_time = int(data.get("countTime", self.count_time))
        self.cpu_speed_control = float(data.get("cpuSpeedControl", self.cpu_speed_control))
        self.snapshot = data.get("snapshot", self.snapshot)
        self.telegram_bot_token = data.get("telegramBotToken", self.telegram_bot_token)
        self.telegram_chat_id = data.get("telegramChatID", self.telegram_chat_id)
                
        self._logger.info("Plugin settings saved.")
        self._thread_calculation()
    
    def check_response(self, base64EncodedImage):
        """
        Helper method to construct a JSON response for checking the AI processing status.

        Parameters:
        - base64EncodedImage: The base64 encoded image to be included in the response.

        Returns:
        - Flask.Response: JSON response containing the image and additional status information.
        """
        failure_count = 0
        with self.lock:
            failure_count = self.count
        response_data  = {
                "image": base64EncodedImage,  
                "failureCount": failure_count,  
                "printingStatus": "ON" if self.get_printer_status() else "OFF",
                "cpuTemperature": int(self.get_cpu_temperature())
            }
        return Response(json.dumps(response_data), mimetype="application/json")
    
    @octoprint.plugin.BlueprintPlugin.route("/check", methods=["GET"])
    def check(self):
        """
        Endpoint to check the current status of the AI processing and
        return the latest processed image or camera snapshot.

        Returns:
        - Flask.Response: JSON response containing the image data and additional information.
        """
        
        with self.lock:
            if self.ai_results and (time.time() - self.ai_results[-1]['time']) <= 5:
                ai_result_image = self.ai_results[-1]['ai_result_image']  
            else:
                ai_result_image = None 

        if ai_result_image:
            return self.check_response(ai_result_image)
        
        # Otherwise, fetch the latest snapshot from the camera
        try:
            if not self.snapshot:
                return self.check_response(self._encode_no_camera_image())
            response = requests.get(self.snapshot, timeout=1)
            response.raise_for_status()
            input_image = Image.open(BytesIO(response.content))
            encoded_image = self.encode_image_to_base64(input_image)
            return self.check_response(encoded_image)
        except requests.RequestException as e:
            self._logger.info(f"Error fetching camera snapshot: {e}")
            # Return no camera image response if fetching snapshot fails
            return self.check_response(self._encode_no_camera_image())


    def get_template_configs(self):
        return [
            dict(type="tab", custom_bindings=False)
        ]

    def get_assets(self):
        return dict(js=["js/pinozcam.js"],
                    css=["css/pinozcam.css"],
                )


__plugin_name__ = "PiNozCam"
__plugin_pythoncompat__ = ">=3.7,<4"
__plugin_implementation__ = PinozcamPlugin()
